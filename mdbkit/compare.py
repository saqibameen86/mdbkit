"""`mdbkit compare` — did the change actually help?

You ran `mdbkit advise`, created an index, and waited a day. The question
now is whether it worked, and the answer is sitting in two log files.

This diffs query shapes between a "before" and an "after" log and reports
what improved, what regressed, and what is new — with the plan change
(COLLSCAN to IXSCAN) called out, because that is usually the real answer.

Deterministic, offline, read-only like everything else.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .analysis import QueryAggregator, ShapeStats

# A shape counts as materially changed only past these thresholds, so noise
# in a quiet log does not read as a regression.
MIN_COUNT = 3
PCT_THRESHOLD = 20.0


@dataclass
class ShapeDelta:
    key: str
    ns: str
    operation: str
    shape: str
    before: Optional[ShapeStats]
    after: Optional[ShapeStats]

    # -- derived ----------------------------------------------------------
    @property
    def status(self) -> str:
        if self.before is None:
            return "new"
        if self.after is None:
            return "gone"
        if self.plan_improved:
            return "improved"
        if self.plan_regressed:
            return "regressed"
        if self.mean_pct <= -PCT_THRESHOLD:
            return "improved"
        if self.mean_pct >= PCT_THRESHOLD:
            return "regressed"
        return "unchanged"

    @property
    def mean_pct(self) -> float:
        if not self.before or not self.after or not self.before.mean_ms:
            return 0.0
        return 100.0 * (self.after.mean_ms - self.before.mean_ms) / self.before.mean_ms

    @property
    def scan_pct(self) -> float:
        if not self.before or not self.after:
            return 0.0
        b = self.before.scan_ratio or 0.0
        a = self.after.scan_ratio or 0.0
        if not b:
            return 0.0
        return 100.0 * (a - b) / b

    @property
    def plan_improved(self) -> bool:
        return bool(self.before and self.after
                    and self.before.collscan and not self.after.collscan)

    @property
    def plan_regressed(self) -> bool:
        return bool(self.before and self.after
                    and not self.before.collscan and self.after.collscan)

    @property
    def sort_fixed(self) -> bool:
        return bool(self.before and self.after
                    and self.before.in_memory_sort and not self.after.in_memory_sort)

    def to_dict(self) -> dict:
        def side(s):
            if s is None:
                return None
            return {"count": s.count, "meanMs": round(s.mean_ms, 1),
                    "maxMs": s.max_ms, "totalMs": s.total_ms,
                    "docsExamined": s.docs_examined,
                    "scanRatio": round(s.scan_ratio, 1) if s.n_returned else None,
                    "collscan": s.collscan, "inMemorySort": s.in_memory_sort}
        return {"ns": self.ns, "operation": self.operation, "shape": self.shape,
                "status": self.status,
                "meanChangePct": round(self.mean_pct, 1),
                "scanChangePct": round(self.scan_pct, 1),
                "planImproved": self.plan_improved,
                "planRegressed": self.plan_regressed,
                "sortFixed": self.sort_fixed,
                "before": side(self.before), "after": side(self.after)}


@dataclass
class CompareResult:
    deltas: List[ShapeDelta]
    before_total_ms: float
    after_total_ms: float
    before_shapes: int
    after_shapes: int

    def by_status(self, status: str) -> List[ShapeDelta]:
        return [d for d in self.deltas if d.status == status]

    def to_dict(self) -> dict:
        return {
            "beforeTotalMs": self.before_total_ms,
            "afterTotalMs": self.after_total_ms,
            "totalChangePct": round(self._pct(), 1),
            "beforeShapes": self.before_shapes,
            "afterShapes": self.after_shapes,
            "shapes": [d.to_dict() for d in self.deltas],
        }

    def _pct(self) -> float:
        if not self.before_total_ms:
            return 0.0
        return 100.0 * (self.after_total_ms - self.before_total_ms) / self.before_total_ms


def _key(s: ShapeStats) -> str:
    return "%s|%s|%s" % (s.shape.ns, s.shape.operation, s.shape.pretty())


def compare(before: List[ShapeStats], after: List[ShapeStats],
            min_count: int = MIN_COUNT,
            ns: Optional[str] = None) -> CompareResult:
    """Diff two sets of aggregated query shapes."""
    b_map: Dict[str, ShapeStats] = {_key(s): s for s in before
                                    if s.count >= min_count
                                    and (ns is None or s.shape.ns == ns)}
    a_map: Dict[str, ShapeStats] = {_key(s): s for s in after
                                    if s.count >= min_count
                                    and (ns is None or s.shape.ns == ns)}

    deltas: List[ShapeDelta] = []
    for key in set(b_map) | set(a_map):
        b = b_map.get(key)
        a = a_map.get(key)
        sample = b or a
        deltas.append(ShapeDelta(
            key=key, ns=sample.shape.ns, operation=sample.shape.operation,
            shape=sample.shape.pretty(), before=b, after=a))

    # Biggest absolute time swing first — that is what people care about.
    def weight(d: ShapeDelta) -> float:
        b = d.before.total_ms if d.before else 0.0
        a = d.after.total_ms if d.after else 0.0
        return -abs(a - b)

    deltas.sort(key=weight)
    return CompareResult(
        deltas=deltas,
        before_total_ms=sum(s.total_ms for s in b_map.values()),
        after_total_ms=sum(s.total_ms for s in a_map.values()),
        before_shapes=len(b_map), after_shapes=len(a_map))


def aggregate_file(paths, min_ms: int = 0,
                   include_system: bool = False) -> Tuple[List[ShapeStats], object]:
    """Aggregate query shapes across one or more log files."""
    from .parser import ParseStats, iter_entries_multi
    stats = ParseStats()
    agg = QueryAggregator(min_ms=min_ms, include_system=include_system)
    for entry in iter_entries_multi(paths, stats):
        agg.consume(entry)
    return agg.results(), stats
