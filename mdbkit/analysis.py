"""Aggregation of parsed log entries into DBA-useful summaries.

Three analyses, mirroring the most-used mtools workflows:

* summarize      -> `mdbkit loginfo`  (like mloginfo)
* QueryAggregator -> `mdbkit queries` (like mloginfo --queries)
* ConnectionAggregator -> `mdbkit connections` (like mloginfo --connections)
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .parser import (
    ID_BUILD_INFO,
    ID_CLIENT_METADATA,
    ID_CONN_ACCEPTED,
    ID_CONN_ENDED,
    ID_STARTUP,
    LogEntry,
)

# ---------------------------------------------------------------------------
# Query shape extraction
# ---------------------------------------------------------------------------

# Operators that keep equality semantics for index purposes.
EQUALITY_OPS = {"$eq", "$in"}
RANGE_OPS = {"$gt", "$gte", "$lt", "$lte"}
LOW_SELECTIVITY_OPS = {"$ne", "$nin", "$exists", "$not"}
SPECIAL_OPS = {"$text", "$where", "$expr", "$geoWithin", "$geoIntersects", "$near", "$nearSphere"}


@dataclass(frozen=True)
class QueryShape:
    """A literal-free signature of a query: what fields, which operators, what sort."""

    ns: str
    operation: str
    filter_fields: Tuple[Tuple[str, Tuple[str, ...]], ...]  # ((path, (ops...)), ...)
    sort_fields: Tuple[Tuple[str, int], ...]  # ((path, direction), ...)
    flags: Tuple[str, ...] = ()  # e.g. ("$or", "$text")

    def pretty(self) -> str:
        parts = []
        for path, ops in self.filter_fields:
            parts.append(f"{path}:{'|'.join(o.lstrip('$') for o in ops)}")
        text = "{" + ", ".join(parts) + "}"
        if self.sort_fields:
            text += " sort:{" + ", ".join(f"{p}:{d}" for p, d in self.sort_fields) + "}"
        if self.flags:
            text += " [" + ",".join(self.flags) + "]"
        return text


def _walk_filter(node, prefix: str, out: Dict[str, set], flags: set, depth: int = 0):
    """Recursively collect (field path -> operator set) from a filter document."""
    if depth > 12 or not isinstance(node, dict):
        return
    for key, value in node.items():
        if key in ("$and",):
            if isinstance(value, list):
                for sub in value:
                    _walk_filter(sub, prefix, out, flags, depth + 1)
        elif key in ("$or", "$nor"):
            flags.add(key)
            if isinstance(value, list):
                for sub in value:
                    _walk_filter(sub, prefix, out, flags, depth + 1)
        elif key in SPECIAL_OPS:
            flags.add(key)
        elif key.startswith("$"):
            # An operator appearing at document level we don't model; note it.
            flags.add(key)
        else:
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                ops = {op for op in value.keys() if op.startswith("$")}
                if ops:
                    out[path].update(ops)
                    inner = value.get("$elemMatch")
                    if isinstance(inner, dict):
                        _walk_filter(inner, path, out, flags, depth + 1)
                    if "$regex" in ops:
                        flags.add("$regex")
                else:
                    # Sub-document equality match on the whole object.
                    out[path].add("$eq")
            else:
                out[path].add("$eq")


def extract_shape(entry: LogEntry) -> Optional[QueryShape]:
    """Build a QueryShape from a 'Slow query' log entry, or None if not applicable."""
    attr = entry.attr
    ns = attr.get("ns", "")
    if not ns or ns.endswith(".$cmd"):
        ns = attr.get("command", {}).get("$db", "") + "." + str(
            attr.get("command", {}).get("find")
            or attr.get("command", {}).get("aggregate")
            or ""
        ) if isinstance(attr.get("command"), dict) else ns

    command = attr.get("command") if isinstance(attr.get("command"), dict) else {}
    op_type = attr.get("type", "")

    filter_doc: dict = {}
    sort_doc: dict = {}
    operation = "unknown"

    if "find" in command:
        operation = "find"
        filter_doc = command.get("filter") or {}
        sort_doc = command.get("sort") or {}
    elif "aggregate" in command:
        operation = "aggregate"
        pipeline = command.get("pipeline") or []
        for stage in pipeline:
            if not isinstance(stage, dict):
                continue
            if "$match" in stage and not filter_doc:
                filter_doc = stage["$match"] or {}
            elif "$sort" in stage and not sort_doc:
                sort_doc = stage["$sort"] or {}
            elif filter_doc or sort_doc:
                break  # only leading $match/$sort benefit from an index
    elif "count" in command:
        operation = "count"
        filter_doc = command.get("query") or {}
    elif "distinct" in command:
        operation = "distinct"
        filter_doc = command.get("query") or {}
    elif "getMore" in command:
        origin = command.get("originatingCommand")
        if isinstance(origin, dict):
            fake = LogEntry(
                ts=entry.ts, severity=entry.severity, component=entry.component,
                msg_id=entry.msg_id, ctx=entry.ctx, msg=entry.msg,
                attr={"ns": ns, "command": origin},
            )
            shape = extract_shape(fake)
            if shape:
                return QueryShape(shape.ns, "getMore(" + shape.operation + ")",
                                  shape.filter_fields, shape.sort_fields, shape.flags)
        operation = "getMore"
    elif op_type in ("update", "remove"):
        operation = op_type
        filter_doc = command.get("q") or {}
    elif "findAndModify" in command:
        operation = "findAndModify"
        filter_doc = command.get("query") or {}
        sort_doc = command.get("sort") or {}
    elif "insert" in command or op_type == "insert":
        operation = "insert"
    elif command:
        operation = next(iter(command.keys()), "unknown")

    fields: Dict[str, set] = defaultdict(set)
    flags: set = set()
    _walk_filter(filter_doc, "", fields, flags)

    filter_fields = tuple(sorted((p, tuple(sorted(ops))) for p, ops in fields.items()))
    sort_fields = tuple(
        (k, int(v) if isinstance(v, (int, float)) else 1) for k, v in sort_doc.items()
    ) if isinstance(sort_doc, dict) else ()

    return QueryShape(ns=ns, operation=operation, filter_fields=filter_fields,
                      sort_fields=sort_fields, flags=tuple(sorted(flags)))


# ---------------------------------------------------------------------------
# Slow query aggregation
# ---------------------------------------------------------------------------

@dataclass
class ShapeStats:
    shape: QueryShape
    count: int = 0
    durations: List[int] = field(default_factory=list)
    docs_examined: int = 0
    keys_examined: int = 0
    n_returned: int = 0
    collscan: bool = False
    in_memory_sort: bool = False
    plan_summaries: Counter = field(default_factory=Counter)
    example: str = ""

    def add(self, entry: LogEntry):
        attr = entry.attr
        self.count += 1
        self.durations.append(int(attr.get("durationMillis", 0) or 0))
        self.docs_examined += int(attr.get("docsExamined", 0) or 0)
        self.keys_examined += int(attr.get("keysExamined", 0) or 0)
        self.n_returned += int(attr.get("nreturned", attr.get("nMatched", 0)) or 0)
        plan = attr.get("planSummary", "")
        if plan:
            self.plan_summaries[plan] += 1
            if "COLLSCAN" in plan:
                self.collscan = True
        if attr.get("hasSortStage"):
            self.in_memory_sort = True
        if not self.example:
            self.example = entry.raw[:2000]

    # -- derived metrics ---------------------------------------------------
    @property
    def total_ms(self) -> int:
        return sum(self.durations)

    @property
    def mean_ms(self) -> float:
        return self.total_ms / self.count if self.count else 0.0

    @property
    def max_ms(self) -> int:
        return max(self.durations) if self.durations else 0

    @property
    def p95_ms(self) -> int:
        if not self.durations:
            return 0
        ordered = sorted(self.durations)
        idx = max(0, math.ceil(0.95 * len(ordered)) - 1)
        return ordered[idx]

    @property
    def scan_ratio(self) -> float:
        """docsExamined per document returned — the classic inefficiency signal."""
        return self.docs_examined / self.n_returned if self.n_returned else float(
            self.docs_examined or 0
        )

    def to_dict(self) -> dict:
        return {
            "ns": self.shape.ns,
            "operation": self.shape.operation,
            "shape": self.shape.pretty(),
            "count": self.count,
            "totalMs": self.total_ms,
            "meanMs": round(self.mean_ms, 1),
            "maxMs": self.max_ms,
            "p95Ms": self.p95_ms,
            "docsExamined": self.docs_examined,
            "keysExamined": self.keys_examined,
            "nReturned": self.n_returned,
            "scanRatio": round(self.scan_ratio, 1),
            "collscan": self.collscan,
            "inMemorySort": self.in_memory_sort,
            "planSummaries": dict(self.plan_summaries),
        }


class QueryAggregator:
    def __init__(self, min_ms: int = 0):
        self.min_ms = min_ms
        self.shapes: Dict[QueryShape, ShapeStats] = {}

    def consume(self, entry: LogEntry):
        if not entry.is_slow_query:
            return
        if int(entry.attr.get("durationMillis", 0) or 0) < self.min_ms:
            return
        shape = extract_shape(entry)
        if shape is None or shape.operation == "insert":
            return
        stats = self.shapes.get(shape)
        if stats is None:
            stats = self.shapes[shape] = ShapeStats(shape=shape)
        stats.add(entry)

    def results(self, sort_by: str = "totalMs", limit: int = 0) -> List[ShapeStats]:
        keymap = {
            "duration": lambda s: s.total_ms,
            "totalMs": lambda s: s.total_ms,
            "count": lambda s: s.count,
            "mean": lambda s: s.mean_ms,
            "max": lambda s: s.max_ms,
            "docsExamined": lambda s: s.docs_examined,
            "scanRatio": lambda s: s.scan_ratio,
        }
        key = keymap.get(sort_by, keymap["totalMs"])
        ordered = sorted(self.shapes.values(), key=key, reverse=True)
        return ordered[:limit] if limit else ordered


# ---------------------------------------------------------------------------
# Connections
# ---------------------------------------------------------------------------

def _ip_of(remote: str) -> str:
    return remote.rsplit(":", 1)[0] if remote else "unknown"


@dataclass
class ConnectionReport:
    accepted: Counter = field(default_factory=Counter)
    ended: Counter = field(default_factory=Counter)
    app_names: Counter = field(default_factory=Counter)
    drivers: Counter = field(default_factory=Counter)
    peak_count: int = 0

    def to_dict(self) -> dict:
        return {
            "totalAccepted": sum(self.accepted.values()),
            "totalEnded": sum(self.ended.values()),
            "peakConnectionCount": self.peak_count,
            "byIp": [
                {"ip": ip, "accepted": n, "ended": self.ended.get(ip, 0)}
                for ip, n in self.accepted.most_common()
            ],
            "appNames": dict(self.app_names.most_common()),
            "drivers": dict(self.drivers.most_common()),
        }


class ConnectionAggregator:
    def __init__(self):
        self.report = ConnectionReport()

    def consume(self, entry: LogEntry):
        attr = entry.attr
        if entry.msg_id == ID_CONN_ACCEPTED:
            self.report.accepted[_ip_of(attr.get("remote", ""))] += 1
            self.report.peak_count = max(
                self.report.peak_count, int(attr.get("connectionCount", 0) or 0)
            )
        elif entry.msg_id == ID_CONN_ENDED:
            self.report.ended[_ip_of(attr.get("remote", ""))] += 1
            self.report.peak_count = max(
                self.report.peak_count, int(attr.get("connectionCount", 0) or 0)
            )
        elif entry.msg_id == ID_CLIENT_METADATA:
            doc = attr.get("doc", {}) or {}
            app = (doc.get("application") or {}).get("name")
            if app:
                self.report.app_names[app] += 1
            driver = (doc.get("driver") or {}).get("name")
            if driver:
                self.report.drivers[driver] += 1


# ---------------------------------------------------------------------------
# Whole-log summary (loginfo)
# ---------------------------------------------------------------------------

@dataclass
class LogSummary:
    versions: List[str] = field(default_factory=list)
    startups: int = 0
    host_info: List[str] = field(default_factory=list)
    severity_counts: Counter = field(default_factory=Counter)
    component_counts: Counter = field(default_factory=Counter)
    slow_queries: int = 0
    slowest_ms: int = 0
    connections_accepted: int = 0
    warnings: int = 0
    errors: int = 0

    def to_dict(self) -> dict:
        return {
            "startups": self.startups,
            "versions": self.versions,
            "hosts": self.host_info,
            "slowQueries": self.slow_queries,
            "slowestMs": self.slowest_ms,
            "connectionsAccepted": self.connections_accepted,
            "warnings": self.warnings,
            "errors": self.errors,
            "bySeverity": dict(self.severity_counts),
            "byComponent": dict(self.component_counts.most_common()),
        }


class SummaryAggregator:
    def __init__(self):
        self.summary = LogSummary()

    def consume(self, entry: LogEntry):
        s = self.summary
        s.severity_counts[entry.severity] += 1
        s.component_counts[entry.component] += 1
        if entry.severity == "W":
            s.warnings += 1
        elif entry.severity in ("E", "F"):
            s.errors += 1
        if entry.msg_id == ID_STARTUP:
            s.startups += 1
            host = entry.attr.get("host")
            port = entry.attr.get("port")
            if host:
                s.host_info.append(f"{host}:{port}" if port else str(host))
        elif entry.msg_id == ID_BUILD_INFO:
            version = (entry.attr.get("buildInfo") or {}).get("version")
            if version and version not in s.versions:
                s.versions.append(version)
        elif entry.msg_id == ID_CONN_ACCEPTED:
            s.connections_accepted += 1
        if entry.is_slow_query:
            s.slow_queries += 1
            s.slowest_ms = max(s.slowest_ms, int(entry.attr.get("durationMillis", 0) or 0))
