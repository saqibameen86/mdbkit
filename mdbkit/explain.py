"""Explain-plan analyzer for `mdbkit explain`.

Reads a saved explain output (JSON produced by
`db.coll.find(...).explain("executionStats")` in mongosh, or exported from
Compass) and answers the question every DBA asks: *why is this query slow,
and what would fix it?*

Handles classic and SBE (6.0+) plan shapes, aggregate explains ($cursor),
and sharded winning plans. Reuses the same deterministic advisor as
`mdbkit advise`, so the recommendation logic is identical everywhere.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .advisor import Recommendation, advise
from .analysis import QueryAggregator
from .parser import LogEntry


@dataclass
class ExplainReport:
    ns: str
    stage_chain: List[str]
    collscan: bool
    blocking_sort: bool
    indexes_used: List[str]
    n_returned: int
    keys_examined: int
    docs_examined: int
    exec_ms: int
    rejected_plans: int
    verdicts: List[str] = field(default_factory=list)
    recommendation: Optional[Recommendation] = None

    def to_dict(self) -> dict:
        return {
            "ns": self.ns,
            "stages": self.stage_chain,
            "collscan": self.collscan,
            "blockingSort": self.blocking_sort,
            "indexesUsed": self.indexes_used,
            "nReturned": self.n_returned,
            "keysExamined": self.keys_examined,
            "docsExamined": self.docs_examined,
            "executionTimeMillis": self.exec_ms,
            "rejectedPlans": self.rejected_plans,
            "verdicts": self.verdicts,
            "recommendation": self.recommendation.to_dict() if self.recommendation else None,
        }


def _collect_stages(plan, out: List[dict], depth: int = 0):
    """Walk a winning plan tree collecting stage docs (classic, SBE, sharded)."""
    if depth > 20 or not isinstance(plan, dict):
        return
    if plan.get("stage"):
        out.append(plan)
    for key in ("queryPlan", "winningPlan", "inputStage", "innerStage",
                "outerStage", "thenStage", "elseStage"):
        if key in plan:
            _collect_stages(plan[key], out, depth + 1)
    for key in ("inputStages", "shards"):
        subs = plan.get(key)
        if isinstance(subs, list):
            for sub in subs:
                _collect_stages(sub, out, depth + 1)


def _find_query_planner(doc: dict) -> Tuple[dict, dict]:
    """Locate queryPlanner + executionStats in find or aggregate explains."""
    if "queryPlanner" in doc:
        return doc.get("queryPlanner") or {}, doc.get("executionStats") or {}
    for stage in doc.get("stages", []) or []:
        if not isinstance(stage, dict):
            continue
        cursor = stage.get("$cursor") or stage.get("$geoNearCursor")
        if isinstance(cursor, dict) and "queryPlanner" in cursor:
            return cursor.get("queryPlanner") or {}, cursor.get("executionStats") or {}
    return {}, {}


_SHELL_CTORS = re.compile(
    r'\b(?:NumberLong|NumberInt|NumberDecimal|ISODate|BinData|UUID|Timestamp)'
    r'\s*\(\s*([^()]*?)\s*\)')
_OBJECTID = re.compile(r'\bObjectId\s*\(\s*([\'"][^\'"]*[\'"])\s*\)')


def _relax_shell_json(text: str) -> str:
    """Convert legacy mongo-shell constructors into plain JSON values.

    The old `mongo` shell (and copy/paste from Compass) emits things like
    NumberLong(42) and ISODate("...") which are JavaScript, not JSON. The
    values themselves do not affect plan analysis, so we unwrap them rather
    than making the user re-export.
    """
    text = _OBJECTID.sub(lambda m: m.group(1).replace("'", '"'), text)

    def unwrap(m):
        inner = m.group(1).strip()
        if not inner:
            return "0"
        if inner[0] in "\'\"":
            return '"%s"' % inner[1:-1]
        if "," in inner:  # Timestamp(a, b) / BinData(t, "...")
            inner = inner.split(",", 1)[1].strip()
            if inner and inner[0] in "\'\"":
                return '"%s"' % inner[1:-1]
        return inner or "0"

    text = _SHELL_CTORS.sub(unwrap, text)
    return text


def load_explain(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    try:
        doc = json.loads(text)
    except json.JSONDecodeError:
        try:
            doc = json.loads(_relax_shell_json(text))
        except json.JSONDecodeError as exc:
            raise ValueError(
                "Could not parse this file as JSON, even after relaxing "
                "mongo-shell constructors. Re-export with mongosh:\n"
                "  mongosh --quiet --eval "
                "'EJSON.stringify(db.COLL.find({...}).explain(\"executionStats\"))'"
                " > explain.json"
            ) from exc
    if isinstance(doc, list) and doc:
        doc = doc[0]
    if not isinstance(doc, dict):
        raise ValueError("Expected a single explain JSON document.")
    return doc


def analyze_explain(doc: dict, indexes=None, schema=None) -> ExplainReport:
    qp, es_inner = _find_query_planner(doc)
    es = doc.get("executionStats") or es_inner or {}
    winning = qp.get("winningPlan") or {}

    stages: List[dict] = []
    _collect_stages(winning, stages)
    chain = [s.get("stage", "?") for s in stages]

    collscan = any(s.get("stage") == "COLLSCAN" for s in stages)
    blocking_sort = any(s.get("stage") == "SORT" for s in stages)
    fetch = any(s.get("stage") == "FETCH" for s in stages)

    indexes_used: List[str] = []
    for s in stages:
        if s.get("stage") in ("IXSCAN", "DISTINCT_SCAN", "COUNT_SCAN"):
            name = s.get("indexName") or json.dumps(s.get("keyPattern", {}))
            if name and name not in indexes_used:
                indexes_used.append(name)

    n_returned = int(es.get("nReturned", 0) or 0)
    keys_examined = int(es.get("totalKeysExamined", 0) or 0)
    docs_examined = int(es.get("totalDocsExamined", 0) or 0)
    exec_ms = int(es.get("executionTimeMillis", 0) or 0)
    rejected = len(qp.get("rejectedPlans") or [])

    verdicts: List[str] = []
    if collscan:
        verdicts.append(
            f"Full collection scan: examined {docs_examined:,} documents to "
            f"return {n_returned:,}. An index on the filter fields would let "
            "MongoDB skip straight to matching documents."
        )
    if blocking_sort:
        verdicts.append(
            "In-memory (blocking) SORT stage: results are sorted after "
            "retrieval instead of being read from an index in order. Sorts "
            "over 100MB abort the query unless allowDiskUse is set."
        )
    if not collscan and indexes_used:
        if n_returned and docs_examined > 10 * n_returned:
            verdicts.append(
                f"Index used ({', '.join(indexes_used)}) but it is weakly "
                f"selective here: {docs_examined:,} docs fetched for "
                f"{n_returned:,} returned. The index may match the filter "
                "only partially."
            )
        elif keys_examined and n_returned and keys_examined > 10 * n_returned:
            verdicts.append(
                f"Index scanned many more keys ({keys_examined:,}) than "
                f"documents returned ({n_returned:,}); check field order vs "
                "the ESR guideline."
            )
        else:
            verdicts.append(
                f"Index used efficiently ({', '.join(indexes_used)}): "
                f"keysExamined={keys_examined:,}, "
                f"docsExamined={docs_examined:,}, nReturned={n_returned:,}."
            )
    if indexes_used and not fetch and docs_examined == 0 and n_returned:
        verdicts.append(
            "Covered query: answered entirely from the index without touching "
            "documents. This is as good as it gets."
        )
    if rejected:
        verdicts.append(
            f"{rejected} alternative plan(s) were considered and rejected — "
            "if plan choice flaps, look at rejectedPlans in the raw output."
        )
    if not verdicts:
        verdicts.append("No obvious pathology detected in this plan.")

    # Reuse the advisor for a recommendation when the plan needs help.
    recommendation = None
    command = doc.get("command") if isinstance(doc.get("command"), dict) else {}
    ns = qp.get("namespace") or (command.get("$db", "") + "." +
                                 str(command.get("find") or
                                     command.get("aggregate") or "")).strip(".")
    if (collscan or blocking_sort or
            (n_returned and docs_examined > 10 * n_returned)) and command:
        entry = LogEntry(
            ts=None, severity="I", component="COMMAND", msg_id=51803,
            ctx="explain", msg="Slow query",
            attr={
                "ns": ns,
                "command": command,
                "planSummary": "COLLSCAN" if collscan else "IXSCAN",
                "docsExamined": docs_examined,
                "nreturned": n_returned,
                "hasSortStage": blocking_sort,
                "durationMillis": exec_ms,
            },
        )
        agg = QueryAggregator()
        agg.consume(entry)
        recs = advise(agg.results(), indexes=indexes, schema=schema,
                      single_sample=True)
        if recs:
            recommendation = recs[0]

    return ExplainReport(
        ns=ns or "(unknown)",
        stage_chain=chain,
        collscan=collscan,
        blocking_sort=blocking_sort,
        indexes_used=indexes_used,
        n_returned=n_returned,
        keys_examined=keys_examined,
        docs_examined=docs_examined,
        exec_ms=exec_ms,
        rejected_plans=rejected,
        verdicts=verdicts,
        recommendation=recommendation,
    )


def render_explain(report: ExplainReport) -> str:
    parts = ["== mdbkit explain ==", ""]
    parts.append(f"namespace : {report.ns}")
    parts.append(f"plan      : {' -> '.join(report.stage_chain) or '(no stages found)'}")
    if report.indexes_used:
        parts.append(f"index(es) : {', '.join(report.indexes_used)}")
    parts.append(
        f"stats     : nReturned={report.n_returned:,}  "
        f"keysExamined={report.keys_examined:,}  "
        f"docsExamined={report.docs_examined:,}  "
        f"time={report.exec_ms:,} ms"
    )
    parts.append("")
    for v in report.verdicts:
        parts.append(f"* {v}")
    rec = report.recommendation
    if rec:
        parts.append("")
        parts.append(f"candidate index ({rec.confidence.upper()} confidence): "
                     f"{rec.candidate_str()}")
        for c in rec.caveats:
            parts.append(f"  caveat: {c}")
        parts.append(f"  validate: {rec.validation}")
    return "\n".join(parts)
