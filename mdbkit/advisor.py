"""Deterministic index advisor.

Rules-based, no AI, no network. Given aggregated slow-query shapes (and
optionally the deployment's existing indexes and a sampled schema), it
proposes *candidate* indexes using the ESR guideline:

    Equality fields first, then Sort fields, then Range fields.

Design principles (deliberate, not accidental):
* Recommendations are candidates to validate, never commands to run blindly.
* Every recommendation carries evidence, confidence, tradeoffs, and a
  validation step.
* We never advise dropping an index; at most we flag overlap to investigate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .analysis import (
    EQUALITY_OPS,
    LOW_SELECTIVITY_OPS,
    RANGE_OPS,
    ShapeStats,
)

Confidence = str  # "high" | "medium" | "low"


@dataclass
class Recommendation:
    ns: str
    shape: str
    candidate: List[Tuple[str, int]]  # ordered (field, direction)
    confidence: Confidence
    evidence: List[str]
    caveats: List[str]
    covered_by: Optional[str] = None
    extends: Optional[str] = None
    validation: str = (
        "Re-run the query with .explain('executionStats') after creating the "
        "index on a staging copy: keysExamined should be close to nReturned "
        "and the plan should no longer contain COLLSCAN or an in-memory SORT."
    )

    def candidate_str(self) -> str:
        return "{ " + ", ".join(f"{f}: {d}" for f, d in self.candidate) + " }"

    def to_dict(self) -> dict:
        return {
            "ns": self.ns,
            "shape": self.shape,
            "candidateIndex": {f: d for f, d in self.candidate},
            "confidence": self.confidence,
            "evidence": self.evidence,
            "caveats": self.caveats,
            "coveredByExisting": self.covered_by,
            "extendsExisting": self.extends,
            "validation": self.validation,
        }


# ---------------------------------------------------------------------------
# Existing index / schema loading
# ---------------------------------------------------------------------------

def load_indexes(path: str) -> Dict[str, List[dict]]:
    """Load existing indexes.

    Accepts either the `mdbkit export-script indexes` output:
        {"db": "shop", "collections": {"orders": [ ...getIndexes()... ]}}
    or a raw getIndexes() array (then the caller must map it to a namespace).
    Returns {namespace: [ {name, key(ordered pairs)} ]}.
    """
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    result: Dict[str, List[dict]] = {}
    if isinstance(data, dict) and "collections" in data:
        db = data.get("db", "")
        for coll, idx_list in (data.get("collections") or {}).items():
            ns = f"{db}.{coll}" if db else coll
            result[ns] = _normalize_indexes(idx_list)
    elif isinstance(data, list):
        result["*"] = _normalize_indexes(data)
    return result


def _normalize_indexes(idx_list) -> List[dict]:
    out = []
    for idx in idx_list or []:
        if not isinstance(idx, dict):
            continue
        key = idx.get("key") or {}
        out.append({
            "name": idx.get("name", ""),
            "key": [(f, _dir(v)) for f, v in key.items()],
            "unique": bool(idx.get("unique")),
            "partial": "partialFilterExpression" in idx,
            "sparse": bool(idx.get("sparse")),
            "ttl": "expireAfterSeconds" in idx,
        })
    return out


def _dir(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0  # hashed / text / 2dsphere etc.


def load_schema(path: str) -> Dict[str, dict]:
    """Load the `mdbkit export-script schema` output.

    Returns {namespace: {fieldPath: {"types": [...], "presence": float}}}.
    """
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    result: Dict[str, dict] = {}
    db = data.get("db", "")
    for coll, info in (data.get("collections") or {}).items():
        ns = f"{db}.{coll}" if db else coll
        result[ns] = (info or {}).get("fields", {}) or {}
    return result


# ---------------------------------------------------------------------------
# Advice
# ---------------------------------------------------------------------------

def _covers(existing_key: List[Tuple[str, int]], candidate: List[Tuple[str, int]]) -> bool:
    """True if `existing_key` starts with the full candidate (same or fully
    reversed directions — a reversed index serves the mirrored sort)."""
    if len(existing_key) < len(candidate):
        return False
    prefix = existing_key[: len(candidate)]
    same = all(a == b for a, b in zip(prefix, candidate))
    mirrored = all(
        a[0] == b[0] and a[1] == -b[1] and a[1] != 0 for a, b in zip(prefix, candidate)
    )
    return same or mirrored


def advise(
    shapes: List[ShapeStats],
    indexes: Optional[Dict[str, List[dict]]] = None,
    schema: Optional[Dict[str, dict]] = None,
    min_count: int = 1,
) -> List[Recommendation]:
    indexes = indexes or {}
    schema = schema or {}
    recs: List[Recommendation] = []

    for stats in shapes:
        if stats.count < min_count:
            continue
        needs_help = stats.collscan or stats.in_memory_sort or stats.scan_ratio > 10
        if not needs_help:
            continue

        shape = stats.shape
        caveats: List[str] = []
        evidence: List[str] = []

        equality: List[str] = []
        ranges: List[str] = []
        for path, ops in shape.filter_fields:
            ops_set = set(ops)
            if ops_set & LOW_SELECTIVITY_OPS and not (ops_set & (EQUALITY_OPS | RANGE_OPS)):
                caveats.append(
                    f"'{path}' is only used with low-selectivity operators "
                    f"({', '.join(sorted(ops_set & LOW_SELECTIVITY_OPS))}); "
                    "excluded from the candidate — an index rarely helps there."
                )
                continue
            if ops_set & EQUALITY_OPS or (not ops_set):
                equality.append(path)
                if "$in" in ops_set:
                    caveats.append(
                        f"'{path}' uses $in: fine for small lists, but very large "
                        "$in arrays reduce index effectiveness."
                    )
            elif ops_set & RANGE_OPS:
                ranges.append(path)
            elif "$regex" in ops_set:
                ranges.append(path)
                caveats.append(
                    f"'{path}' uses $regex: an index only helps if the pattern is "
                    "left-anchored (e.g. /^abc/) and case-sensitive."
                )

        sort_fields = [(p, d) for p, d in shape.sort_fields if p not in equality]

        candidate: List[Tuple[str, int]] = [(f, 1) for f in equality]
        candidate += sort_fields
        candidate += [(f, 1) for f in ranges if f not in {p for p, _ in candidate}]

        if not candidate:
            continue

        # Evidence trail.
        if stats.collscan:
            evidence.append("COLLSCAN observed in planSummary")
        if stats.in_memory_sort:
            evidence.append("in-memory sort (hasSortStage) observed")
        if stats.n_returned:
            evidence.append(
                f"examined {stats.docs_examined:,} docs to return "
                f"{stats.n_returned:,} ({stats.scan_ratio:.0f}:1)"
            )
        evidence.append(
            f"seen {stats.count}x, total {stats.total_ms:,} ms, max {stats.max_ms:,} ms"
        )

        # Confidence.
        if stats.collscan and stats.scan_ratio > 100:
            confidence = "high"
        elif stats.collscan or stats.in_memory_sort or stats.scan_ratio > 10:
            confidence = "medium"
        else:
            confidence = "low"
        if stats.count < 3:
            confidence = "low" if confidence == "medium" else confidence
            caveats.append(
                f"Shape seen only {stats.count} time(s); confirm it is recurring "
                "before creating an index for it."
            )

        # Flags from the shape.
        if "$or" in shape.flags:
            caveats.append(
                "Query contains $or: MongoDB may need a separate index per $or "
                "branch; this candidate covers the merged field set only."
            )
        for flag in shape.flags:
            if flag in ("$text", "$where", "$expr"):
                caveats.append(f"Query uses {flag}, which this rule engine does not model.")

        # Schema-aware caveats.
        ns_schema = schema.get(shape.ns) or {}
        for f, _d in candidate:
            info = ns_schema.get(f)
            if info is None and ns_schema:
                caveats.append(
                    f"Field '{f}' was not seen in the sampled schema — check for typos."
                )
            elif info:
                if "array" in info.get("types", []):
                    caveats.append(
                        f"'{f}' is an array field: this becomes a multikey index "
                        "(larger, and compound bounds behave differently)."
                    )
                if info.get("types") == ["bool"]:
                    caveats.append(
                        f"'{f}' is boolean — very low cardinality; place it only "
                        "alongside more selective fields."
                    )

        caveats.append(
            "Every index adds write and storage overhead and takes time to build; "
            "create with { background/rolling build } strategy appropriate to your "
            "version and validate on staging first."
        )

        # Overlap with existing indexes.
        covered_by = extends = None
        for idx in indexes.get(shape.ns, []) + indexes.get("*", []):
            if _covers(idx["key"], candidate):
                covered_by = idx["name"] or str(idx["key"])
                break
            if idx["key"] and _covers(candidate, idx["key"]):
                extends = idx["name"] or str(idx["key"])
        if covered_by:
            evidence.append(
                f"an existing index ('{covered_by}') already has this candidate as "
                "a prefix — the query may be failing to use it; check with explain()."
            )
        if extends:
            caveats.append(
                f"Candidate extends existing index '{extends}'. If created, the "
                "narrower index may become redundant — investigate (do not drop "
                "automatically)."
            )

        recs.append(
            Recommendation(
                ns=shape.ns,
                shape=shape.pretty(),
                candidate=candidate,
                confidence=confidence,
                evidence=evidence,
                caveats=caveats,
                covered_by=covered_by,
                extends=extends,
            )
        )

    order = {"high": 0, "medium": 1, "low": 2}
    recs.sort(key=lambda r: order.get(r.confidence, 3))
    return recs
