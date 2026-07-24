"""Plain-text rendering. No third-party dependencies, pipe-friendly output."""

from __future__ import annotations

import json
from typing import List, Sequence

from .advisor import Recommendation
from .analysis import ConnectionReport, LogSummary, ShapeStats
from .parser import ParseStats


def dump_json(obj) -> str:
    return json.dumps(obj, indent=2, default=str)


def _table(headers: Sequence[str], rows: List[Sequence[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines = [fmt.format(*headers), fmt.format(*("-" * w for w in widths))]
    lines += [fmt.format(*(str(c) for c in row)) for row in rows]
    return "\n".join(lines)


def _ms(v: float) -> str:
    if v >= 60_000:
        return f"{v/60_000:.1f}m"
    if v >= 1_000:
        return f"{v/1_000:.1f}s"
    return f"{int(v)}ms"


def render_parse_stats(stats: ParseStats) -> str:
    span = ""
    if stats.first_ts and stats.last_ts:
        span = f"  span: {stats.first_ts.isoformat()} -> {stats.last_ts.isoformat()}"
    return (
        f"lines: {stats.total_lines:,}  parsed: {stats.parsed:,}  "
        f"unparsed: {stats.unparsed:,}{span}"
    )


def render_summary(summary: LogSummary, stats: ParseStats) -> str:
    parts = ["== mdbkit loginfo ==", render_parse_stats(stats), ""]
    parts.append(f"server version(s): {', '.join(summary.versions) or 'not found in log'}")
    if summary.host_info:
        parts.append(f"host: {', '.join(dict.fromkeys(summary.host_info))}")
    parts.append(f"restarts/startups seen: {summary.startups}")
    parts.append(f"connections accepted: {summary.connections_accepted:,}")
    parts.append(
        f"slow queries logged: {summary.slow_queries:,}"
        + (f" (slowest {_ms(summary.slowest_ms)})" if summary.slow_queries else "")
    )
    parts.append(f"warnings: {summary.warnings:,}   errors: {summary.errors:,}")
    if summary.warnings:
        parts.append(f"  next: mdbkit filter <log> --severity W")
    if summary.errors:
        parts.append(f"  next: mdbkit filter <log> --severity E")
    parts.append("")
    top = summary.component_counts.most_common(8)
    if top:
        parts.append(_table(["component", "lines"], [(c, f"{n:,}") for c, n in top]))
    return "\n".join(parts)


def _plan_label(s) -> str:
    """Shortest useful plan label from planSummaries counter."""
    if not s.plan_summaries:
        return "?"
    top = s.plan_summaries.most_common(1)[0][0]
    if "COLLSCAN" in top:
        return "COLLSCAN" + ("+SORT" if s.in_memory_sort else "")
    if "IXSCAN" in top:
        # "IXSCAN { a: 1, b: -1 }" -> "IXSCAN{a,b}"
        inner = top[top.find("{"):top.find("}")+1] if "{" in top else ""
        fields = ",".join(p.strip().split(":")[0].strip() for p in inner.strip("{}").split(",") if p.strip())
        label = f"IXSCAN{{{fields}}}" if fields else "IXSCAN"
        return label + ("+SORT" if s.in_memory_sort else "")
    return top[:18]


def render_queries(results: List[ShapeStats], stats: ParseStats) -> str:
    parts = ["== mdbkit queries (slow query shapes) ==", render_parse_stats(stats), ""]
    if not results:
        parts.append("No slow queries found. (mongod logs operations exceeding "
                      "slowms, default 100 ms; lower slowms or enable profiling "
                      "to capture more.)")
        return "\n".join(parts)
    rows = []
    for s in results:
        # scan ratio: handle zero-return ops (updates, deletes) gracefully
        if s.n_returned:
            scan = f"{s.scan_ratio:.0f}:1"
        elif s.docs_examined:
            scan = f"{s.docs_examined:,}ex/0ret"
        else:
            scan = "-"
        rows.append((
            s.shape.ns,
            s.shape.operation,
            s.count,
            _ms(s.total_ms),
            _ms(s.mean_ms),
            _ms(s.max_ms),
            f"{s.docs_examined:,}" if s.docs_examined else "-",
            scan,
            _plan_label(s),
            s.shape.pretty()[:55],
        ))
    parts.append(_table(
        ["namespace", "op", "count", "cumMs", "mean", "max", "docsEx", "scan", "plan", "shape"],
        rows,
    ))
    parts.append("")
    parts.append(
        "cumMs  = total wall time accumulated across ALL occurrences (not one query)\n"
        "docsEx = total documents examined across all occurrences\n"
        "scan   = docsExamined per returned doc (high = index missing or weak)\n"
        "plan   = most common query plan; COLLSCAN/+SORT = index needed\n"
        "         empty plan (?) = plan not present in log (below slowms threshold)\n"
        "next   : mdbkit advise <log> [--ns <namespace>] for index candidates"
    )
    return "\n".join(parts)


def render_connections(report: ConnectionReport, stats: ParseStats) -> str:
    parts = ["== mdbkit connections ==", render_parse_stats(stats), ""]
    d = report.to_dict()
    parts.append(
        f"accepted: {d['totalAccepted']:,}   ended: {d['totalEnded']:,}   "
        f"peak concurrent (as logged): {d['peakConnectionCount']:,}"
    )
    if d["byIp"]:
        parts.append("")
        parts.append(_table(
            ["source ip", "accepted", "ended"],
            [(r["ip"], r["accepted"], r["ended"]) for r in d["byIp"][:20]],
        ))
    if d["appNames"]:
        parts.append("")
        parts.append(_table(["appName", "handshakes"], list(d["appNames"].items())[:15]))
    return "\n".join(parts)


def render_recommendations(recs: List[Recommendation], stats: ParseStats,
                           limit: int = 0) -> str:
    parts = ["== mdbkit advise (candidate indexes) ==", render_parse_stats(stats), ""]
    if not recs:
        parts.append("No index candidates: no slow query shapes showed COLLSCAN, "
                      "in-memory sorts, or high scan ratios. Good sign — or the "
                      "log window is too quiet to judge.")
        return "\n".join(parts)

    total = len(recs)
    counts = {"high": 0, "medium": 0, "low": 0}
    for r in recs:
        counts[r.confidence] = counts.get(r.confidence, 0) + 1
    by_ns = {}
    for r in recs:
        by_ns.setdefault(r.ns, 0)
        by_ns[r.ns] += 1

    shown = recs[:limit] if limit else recs
    parts.append(
        f"{total} candidate(s): {counts['high']} high, {counts['medium']} medium, "
        f"{counts['low']} low  |  across {len(by_ns)} namespace(s)"
    )
    if len(shown) < total:
        parts.append(f"showing top {len(shown)} by confidence "
                     f"(--limit 0 for all, --ns <namespace> to focus)")
    parts.append("")

    for i, rec in enumerate(shown, 1):
        parts.append(f"[{i}] {rec.ns}  —  confidence: {rec.confidence.upper()}")
        parts.append(f"    query shape : {rec.shape}")
        parts.append(f"    candidate   : {rec.candidate_str()}")
        if rec.covered_by:
            parts.append(f"    NOTE        : may already be covered by existing "
                         f"index '{rec.covered_by}' — investigate before creating")
        for e in rec.evidence:
            parts.append(f"    evidence    : {e}")
        for c in rec.caveats:
            parts.append(f"    caveat      : {c}")
        parts.append(f"    validate    : {rec.validation}")
        parts.append("")
    parts.append("These are CANDIDATES, not commands. Review, test on staging, and "
                  "watch write latency and index build impact before production.")
    return "\n".join(parts)


# ----------------------------------------------------------------- FTDC ----

def _human(n: float, label: str = "") -> str:
    """Format an FTDC value, honouring the unit implied by its label.

    Only byte-valued metrics get binary-prefix formatting; a metric already
    expressed in KB or MB must not be re-scaled as if it were bytes.
    """
    low = label.lower()
    if low.endswith("bytes"):
        if n >= 2 ** 30:
            return "%.1f GiB" % (n / 2 ** 30)
        if n >= 2 ** 20:
            return "%.1f MiB" % (n / 2 ** 20)
        if n >= 1024:
            return "%.1f KiB" % (n / 1024.0)
        return "%.0f B" % n
    if low.endswith("kb"):
        return "%.1f GiB" % (n / 1048576.0) if n >= 1048576 else \
               "%.1f MiB" % (n / 1024.0)
    if low.endswith("mb"):
        return "%.1f GiB" % (n / 1024.0) if n >= 1024 else "%.0f MiB" % n
    if low.endswith("ms"):
        return "%.1f s" % (n / 1000.0) if n >= 1000 else "%.0f ms" % n
    return "{:,.0f}".format(n)


def render_ftdc_summary(reader, file_count: int) -> str:
    parts = ["== mdbkit ftdc summary =="]
    span = ""
    if reader.first_ts and reader.last_ts:
        span = "  %s -> %s" % (reader.first_ts.strftime("%Y-%m-%d %H:%M"),
                               reader.last_ts.strftime("%H:%M"))
    parts.append("files: %d   chunks: %d   samples: %s%s" % (
        file_count, reader.chunks, format(reader.samples, ","), span))
    if reader.errors:
        parts.append("corrupt/skipped chunks: %d" % reader.errors)
    parts.append("")
    if not reader.series:
        parts.append("No curated metrics found. The file decoded but none of "
                     "the expected serverStatus/systemMetrics paths were "
                     "present — try `mdbkit ftdc export` to see raw metrics.")
        return "\n".join(parts)

    rows = []
    for label in sorted(reader.series):
        s = reader.series[label]
        st = s.stats()
        if not st:
            continue
        rate = reader.rate(label) if s.kind == "counter" else None
        rows.append((
            label,
            _human(st["min"], label),
            _human(st["avg"], label),
            _human(st["max"], label),
            _human(st["last"], label),
            "%.1f/s" % rate if rate is not None else "-",
        ))
    parts.append(_table(["metric", "min", "avg", "max", "last", "rate"], rows))

    pct = reader.cache_pct()
    if pct is not None:
        parts.append("")
        parts.append("WiredTiger cache peak: %.0f%% of configured maximum" % pct)
    parts.append("")
    parts.append("FTDC is MongoDB's own always-on recorder: these numbers were "
                 "already on disk, no monitoring agent required.")
    return "\n".join(parts)


def render_ftdc_timeline(reader, step: int = 60) -> str:
    parts = ["== mdbkit ftdc timeline ==", ""]
    if not reader.series:
        parts.append("No metrics decoded.")
        return "\n".join(parts)
    labels = sorted(reader.series)
    base = reader.series[labels[0]]
    if not base.times:
        parts.append("No samples.")
        return "\n".join(parts)

    buckets = {}
    order = []
    for i, t in enumerate(base.times):
        key = int(t.timestamp() // step) * step
        if key not in buckets:
            buckets[key] = {}
            order.append(key)
        for lb in labels:
            vals = reader.series[lb].values
            if i < len(vals):
                buckets[key].setdefault(lb, []).append(vals[i])

    from datetime import datetime, timezone
    rows = []
    for key in order:
        row = [datetime.fromtimestamp(key, tz=timezone.utc).strftime("%H:%M")]
        for lb in labels:
            vals = buckets[key].get(lb) or []
            row.append(_human(max(vals), lb) if vals else "-")
        rows.append(row)
    parts.append(_table(["time"] + labels, rows))
    parts.append("")
    parts.append("Each row is the peak within a %ds bucket." % step)
    return "\n".join(parts)
