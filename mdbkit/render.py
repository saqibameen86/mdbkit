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
    from .sharding import ROLE_LABEL
    parts = ["== mdbkit loginfo ==", render_parse_stats(stats), ""]
    parts.append(f"server version(s): {', '.join(summary.versions) or 'not found in log'}")
    role = getattr(summary, "role", None)
    if role:
        extra = ""
        if role == "mongos" and summary.config_set:
            extra = f" — config servers: {summary.config_set}"
        elif summary.repl_set:
            extra = f" — replica set {summary.repl_set}"
        parts.append(f"role: {ROLE_LABEL.get(role, role)}{extra}")
    if summary.host_info:
        parts.append(f"host: {', '.join(dict.fromkeys(summary.host_info))}")
    parts.append(f"restarts/startups seen: {summary.startups}")
    parts.append(f"connections accepted: {summary.connections_accepted:,}")
    parts.append(
        f"slow queries logged: {summary.slow_queries:,}"
        + ((f" ({summary.user_slow_queries:,} on your collections, slowest "
            f"{_ms(summary.slowest_ms)}; the rest are internal operations)"
            if summary.user_slow_queries != summary.slow_queries else
            f" (slowest {_ms(summary.slowest_ms)})")
           if summary.user_slow_queries else
           (" (all on internal admin/config/local namespaces)"
            if summary.slow_queries else ""))
    )
    if getattr(summary, "slow_in_progress", 0):
        parts.append(f"still-running operations logged (8.3+): "
                     f"{summary.slow_in_progress:,}")
    mig = getattr(summary, "migrations", None)
    if mig:
        parts.append(f"chunk migrations: {mig['started']:,} started here, "
                     f"{mig['moved']:,} moved, {mig['failed']:,} failed"
                     + (f", {mig['receiveFailed']:,} incoming failed"
                        if mig.get("receiveFailed") else ""))
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
    """Shortest useful plan label, plus +SPILL if the shape wrote to disk."""
    label = _plan_core(s)
    return label + "+SPILL" if getattr(s, "spills", 0) else label


def _plan_core(s) -> str:
    if not s.plan_summaries:
        # An insert has no query plan; "?" means "plan not logged".
        return "-" if s.shape.operation == "insert" else "?"
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


def render_queries(results: List[ShapeStats], stats: ParseStats,
                   total_shards: int = 0) -> str:
    parts = ["== mdbkit queries (slow query shapes) ==", render_parse_stats(stats), ""]
    if not results:
        parts.append("No slow queries found. (mongod logs operations exceeding "
                      "slowms, default 100 ms; lower slowms or enable profiling "
                      "to capture more.)")
        return "\n".join(parts)
    if any(r.routed for r in results):
        return _render_router_queries(results, parts, total_shards)
    rows = []
    for s in results:
        # scan ratio: handle zero-return ops (updates, deletes) gracefully
        if s.n_returned and not s.docs_examined and not s.keys_examined \
                and s.shape.operation.startswith("getMore"):
            # A getMore only hands over results; the scanning was done by
            # the find/aggregate that opened the cursor (its own row).
            scan = "-"
        elif s.n_returned:
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
        "plan   = most common query plan; COLLSCAN/+SORT = index needed;\n"
        "         +SPILL = wrote temporary files to disk (sort/group too big for memory)\n"
        "         empty plan (?) = plan not present in log (below slowms threshold)\n"
        "next   : mdbkit advise <log> [--ns <namespace>] for index candidates"
    )
    timed = [r for r in results if r.timed_count]
    total = sum(r.timed_total_ms for r in timed)
    if total:
        waiting = sum(r.waiting_ms for r in timed)
        parts.append("")
        parts.append(
            "MongoDB 8.0+ timing: %.0f%% of this time was spent waiting "
            "(tickets, locks, flow control) rather than executing — "
            "--shape N shows the split per shape." % (100.0 * waiting / total))
    return "\n".join(parts)


def _shards_label(s: ShapeStats) -> str:
    if not s.shard_counts:
        return "-"
    lo, hi = min(s.shard_counts), max(s.shard_counts)
    return str(hi) if lo == hi else "%d-%d" % (lo, hi)


def _render_router_queries(results: List[ShapeStats], parts: List[str],
                           total_shards: int = 0) -> str:
    """A mongos log: no plans or documents examined (the shards have those),
    but how each query was routed. The shard count comes from the shard
    registry lines when the log has them; otherwise the most shards any
    query reached stands in for it."""
    seen = max(r.max_shards for r in results)
    total = max(total_shards, seen)
    known = total_shards >= seen and total_shards > 0
    rows = []
    for s in results:
        all_runs = s.all_shard_runs(total)
        rows.append((
            s.shape.ns, s.shape.operation, s.count, _ms(s.total_ms),
            _ms(s.mean_ms), _ms(s.max_ms), _shards_label(s),
            ("%d/%d" % (all_runs, s.count)) if total >= 2 and s.routed else "-",
            ("%.0f%%" % (100.0 * s.remote_wait_ms / s.total_ms))
            if s.routed and s.total_ms else "-",
            s.shape.pretty()[:55],
        ))
    parts.append(_table(["namespace", "op", "count", "cumMs", "mean", "max",
                         "shards", "to all", "shard wait", "shape"], rows))
    parts.append("")
    parts.append(
        "This is a mongos (router) log: it records how each query was routed,\n"
        "while plans and documents examined are recorded on the shards.\n"
        "shards     = shards each execution was sent to\n"
        "to all     = executions sent to every shard (%d %s): scatter-gather,\n"
        "             usually a filter without the shard key, or a range that\n"
        "             spans every shard\n"
        "shard wait = share of the time spent waiting for the shards\n"
        "next       : mdbkit queries <shard log> for plans; mdbkit triage <this log>"
        % (total, "in this cluster" if known else
           "here: the most any query reached, since the log does not list the shards"))
    return "\n".join(parts)


def _short_ts(iso: str) -> str:
    """Trim an ISO timestamp to something readable in a table."""
    if not iso:
        return "-"
    return iso.replace("T", " ")[:19]


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
            ["source ip", "accepted", "ended", "first seen", "last seen", "appName"],
            [(r["ip"], r["accepted"], r["ended"],
              _short_ts(r["firstSeen"]), _short_ts(r["lastSeen"]),
              ", ".join(list(r["appNames"])[:2]) or "-")
             for r in d["byIp"][:20]],
        ))

    if d["byUser"]:
        parts.append("")
        parts.append("authenticated users")
        parts.append(_table(
            ["user", "auth db", "ok", "failed", "last authenticated", "from"],
            [(u["user"], u["authDb"] or "-", u["successes"], u["failures"],
              _short_ts(u["lastSeen"]),
              ", ".join(list(u["sourceIps"])[:2]) or "-")
             for u in d["byUser"][:20]],
        ))
        failing = [u for u in d["byUser"] if u["failures"]]
        if failing:
            parts.append("")
            for u in failing[:5]:
                parts.append("  %s: %d failed authentication(s)%s"
                             % (u["user"], u["failures"],
                                " — last error: " + u["lastError"]
                                if u["lastError"] else ""))
    else:
        parts.append("")
        parts.append("No authentication events in this log. Either the "
                     "deployment has no auth enabled, or the log window "
                     "contains no new logins (clients reuse connections).")

    if d["appNames"]:
        parts.append("")
        parts.append(_table(["appName", "handshakes"],
                            list(d["appNames"].items())[:15]))
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

def _human(n, label: str = "") -> str:
    """Format an FTDC value, honouring the unit implied by its label.

    Only byte-valued metrics get binary-prefix formatting; a metric already
    expressed in KB or MB must not be re-scaled as if it were bytes.

    Tolerant of odd input on purpose: a display helper must never be the
    thing that takes down a whole command.
    """
    if n is None:
        return "-"
    if not isinstance(n, (int, float)):
        return str(n)
    low = str(label).lower()
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
        span = "  %s -> %s UTC" % (reader.first_ts.strftime("%Y-%m-%d %H:%M"),
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

    # Gauges and counters are summarised differently: a gauge's min/avg/max
    # is meaningful, a cumulative counter's is not.
    gauges, counters = [], []
    for label in sorted(reader.series):
        if label.startswith("_"):
            continue
        s = reader.series[label]
        st = s.stats()
        if not st:
            continue
        if st.get("cumulative"):
            rate = reader.rate(label)
            counters.append((
                label,
                _human(st.get("change"), label) if st.get("change") is not None else "-",
                ("%.1f/s" % rate) if rate is not None else "-",
                _human(st.get("last"), label)))
        else:
            gauges.append((label, _human(st["min"], label),
                           _human(st["avg"], label), _human(st["max"], label),
                           _human(st["last"], label)))

    if gauges:
        parts.append("current values (min / average / max over the window)")
        parts.append(_table(["metric", "min", "avg", "max", "last"], gauges))
        parts.append("")
    if counters:
        parts.append("activity (counters are cumulative since server start, "
                     "so this is the change across the window)")
        parts.append(_table(["metric", "change in window", "per second",
                             "counter now"], counters))
        parts.append("")

    pct = reader.cache_pct()
    if pct is not None:
        parts.append("WiredTiger cache peaked at %.1f%% of configured size."
                     % pct)

    disks = reader.disks()
    if disks:
        parts.append("")
        parts.append("disk activity")
        parts.append(_table(
            ["device", "utilisation", "operations", "ops/sec", "avg wait"],
            [(dev, "%.1f%%" % d["utilPct"], format(d["ops"], ","),
              d["opsPerSec"] if d["opsPerSec"] is not None else "-",
              ("%.1f ms" % d["avgWaitMs"]) if d["avgWaitMs"] is not None else "-")
             for dev, d in sorted(disks.items())]))

    lag = reader.series.get("repl.lagMs")
    if lag and lag.vmax is not None:
        parts.append("")
        parts.append("Replication: widest gap between member optimes was "
                     "%.1fs (average %.1fs)."
                     % (lag.vmax / 1000.0, (lag.total / lag.n) / 1000.0))
    return "\n".join(parts)


def render_ftdc_timeline(reader, step: int = 60, show_internal: bool = False) -> str:
    """One row per time bucket. Gauges show the bucket's peak; cumulative
    counters show their rate over the bucket, since a running total since
    server start says nothing about that minute."""
    parts = ["== mdbkit ftdc timeline ==", ""]
    if not reader.series:
        parts.append("No metrics decoded.")
        return "\n".join(parts)
    all_labels = sorted(reader.series)
    # "_disk.*" and "_repl.*" are helper columns for the summary's disk and
    # replication-lag tables, not readable on their own.
    labels = [lb for lb in all_labels if show_internal or not lb.startswith("_")]
    if not labels:
        labels = all_labels
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
                buckets[key].setdefault(lb, []).append((t, vals[i]))

    from datetime import datetime, timezone
    rows = []
    prev_last = {}
    for key in order:
        row = [datetime.fromtimestamp(key, tz=timezone.utc).strftime(
            "%H:%M" if step % 60 == 0 else "%H:%M:%S")]
        for lb in labels:
            pts = buckets[key].get(lb) or []
            if not pts:
                row.append("-")
                continue
            if getattr(reader.series[lb], "kind", "gauge") == "counter":
                t0, v0 = prev_last.get(lb, pts[0])
                t1, v1 = pts[-1]
                secs = (t1 - t0).total_seconds()
                prev_last[lb] = pts[-1]
                if secs <= 0 or v1 < v0:      # first sample, or a restart
                    row.append("-")
                else:
                    row.append("%s/s" % _rate((v1 - v0) / secs))
            else:
                row.append(_human(max(v for _, v in pts), lb))
        rows.append(row)
    parts.append(_table(["UTC"] + labels, rows))
    parts.append("")
    parts.append("Gauges show the peak within each %ds bucket; counters "
                 "(ops.*, sys.cpu.*, ...) show their rate per second." % step)
    return "\n".join(parts)


def _rate(v: float) -> str:
    if v >= 100:
        return format(int(round(v)), ",")
    return "%.1f" % v


# -------------------------------------------------------------- compare ----

def _pct_str(v: float) -> str:
    if v == 0:
        return "0%"
    return "%+.0f%%" % v


def render_compare(result, stats_before, stats_after, limit: int = 15) -> str:
    parts = ["== mdbkit compare (did it help?) =="]
    parts.append("before: %s lines   after: %s lines" % (
        format(stats_before.parsed, ","), format(stats_after.parsed, ",")))

    total_pct = result.to_dict()["totalChangePct"]
    verdict = ("slow-query time DOWN %.0f%%" % abs(total_pct) if total_pct < 0
               else "slow-query time UP %.0f%%" % total_pct if total_pct > 0
               else "slow-query time unchanged")
    parts.append("%s  (%s -> %s across compared shapes)" % (
        verdict, _ms(result.before_total_ms), _ms(result.after_total_ms)))

    buckets = [("improved", "IMPROVED"), ("regressed", "REGRESSED"),
               ("new", "NEW"), ("gone", "GONE")]
    counts = {name: len(result.by_status(name)) for name, _ in buckets}
    parts.append("shapes: %d improved, %d regressed, %d new, %d gone, "
                 "%d unchanged" % (
                     counts["improved"], counts["regressed"], counts["new"],
                     counts["gone"], len(result.by_status("unchanged"))))
    parts.append("")

    shown = 0
    for status, label in buckets:
        items = result.by_status(status)
        if not items:
            continue
        parts.append("%s" % label)
        for d in items:
            if shown >= limit:
                break
            shown += 1
            if d.before and d.after:
                note = []
                if d.plan_improved:
                    note.append("COLLSCAN -> index")
                if d.plan_regressed:
                    note.append("index -> COLLSCAN")
                if d.sort_fixed:
                    note.append("in-memory sort gone")
                extra = ("  [%s]" % ", ".join(note)) if note else ""
                parts.append("  %s %s" % (d.ns, d.shape[:64]))
                parts.append("    mean %s -> %s (%s)   scan %s -> %s%s" % (
                    _ms(d.before.mean_ms), _ms(d.after.mean_ms),
                    _pct_str(d.mean_pct),
                    ("%.0f:1" % d.before.scan_ratio) if d.before.n_returned else "-",
                    ("%.0f:1" % d.after.scan_ratio) if d.after.n_returned else "-",
                    extra))
            elif d.after:
                parts.append("  %s %s" % (d.ns, d.shape[:64]))
                parts.append("    not in the before log; now %dx, mean %s%s" % (
                    d.after.count, _ms(d.after.mean_ms),
                    "  [COLLSCAN]" if d.after.collscan else ""))
            else:
                parts.append("  %s %s" % (d.ns, d.shape[:64]))
                parts.append("    was %dx at mean %s; absent from the after log" % (
                    d.before.count, _ms(d.before.mean_ms)))
        parts.append("")

    if shown < len([d for d in result.deltas if d.status != "unchanged"]):
        parts.append("(--limit %d shown; use --limit 0 for all)" % limit)
    parts.append("Shapes seen fewer than the --min-count threshold are ignored, "
                 "so a quiet log does not read as a regression.")
    return "\n".join(parts)


def render_shape_detail(s, stats) -> str:
    """Everything known about one query shape."""
    parts = ["== mdbkit queries — shape detail ==", render_parse_stats(stats), ""]
    parts.append("namespace : %s" % s.shape.ns)
    parts.append("operation : %s" % s.shape.operation)
    parts.append("shape     : %s" % s.shape.pretty())
    parts.append("")
    parts.append("occurrences   : %d" % s.count)
    parts.append("total time    : %s" % _ms(s.total_ms))
    parts.append("mean / max    : %s / %s" % (_ms(s.mean_ms), _ms(s.max_ms)))
    if s.routed:
        parts.append("docs returned : %s" % format(s.n_returned, ","))
        parts.append("")
        parts.append("routing (mongos)")
        for n, runs in sorted(s.shard_counts.items()):
            parts.append("  sent to %d shard(s) : %dx" % (n, runs))
        if s.total_ms:
            parts.append("  waiting on shards  : %s (%.0f%%)" % (
                _ms(s.remote_wait_ms), 100.0 * s.remote_wait_ms / s.total_ms))
        if s.routing_ms:
            parts.append("  routing lookups    : %s (refreshing the routing table)"
                         % _ms(s.routing_ms))
        if s.max_shards >= 2:
            parts.append("  a query sent to several shards usually has no shard key "
                         "in its filter, or a range that spans shards")
    else:
        parts.append("docs examined : %s" % format(s.docs_examined, ","))
        parts.append("docs returned : %s" % format(s.n_returned, ","))
        if s.n_returned:
            parts.append("scan ratio    : %.0f examined per document returned"
                         % s.scan_ratio)
        parts.append("keys examined : %s" % format(s.keys_examined, ","))
    parts.append("")
    if s.errors:
        parts.append("failed executions")
        for name, n in s.errors.most_common(5):
            parts.append("  %-38s %dx" % (name, n))
        parts.append("")
    if s.plan_summaries:
        parts.append("plans observed")
        for plan, n in s.plan_summaries.most_common():
            parts.append("  %-40s %dx" % (plan[:40], n))
        parts.append("")
    flags = []
    if s.collscan:
        flags.append("COLLSCAN — no index used for at least one execution")
    if s.in_memory_sort:
        flags.append("in-memory SORT — results sorted after retrieval")
    if flags:
        parts.append("flags")
        for f in flags:
            parts.append("  %s" % f)
        parts.append("")
    if s.app_names:
        parts.append("client applications")
        for app, n in s.app_names.most_common(5):
            parts.append("  %-30s %dx" % (app, n))
        parts.append("")

    if s.timed_count and not s.routed:
        parts.append("where the time went (MongoDB 8.0+)")
        parts.append("  executing     : %s" % _ms(s.working_ms))
        parts.append("  waiting       : %s (%.0f%%) — tickets, locks, flow control"
                     % (_ms(s.waiting_ms), s.waiting_pct or 0))
        if s.queued_us:
            parts.append("  ticket queue  : %s of the wait" % _ms(s.queued_us / 1000.0))
        parts.append("")
    extra = []
    if s.spills:
        extra.append("disk spills   : %d%s" % (
            s.spills, " (%s written)" % _human_bytes(s.spilled_bytes)
            if s.spilled_bytes else ""))
    if s.peak_mem_bytes:
        extra.append("peak memory   : %s per operation (8.3+ tracked memory)"
                     % _human_bytes(s.peak_mem_bytes))
    if s.cpu_nanos:
        extra.append("CPU time      : %s total" % _ms(s.cpu_nanos / 1e6))
    if s.query_frameworks:
        extra.append("engine        : %s" % ", ".join(
            "%s %dx" % kv for kv in s.query_frameworks.most_common()))
    if extra:
        parts.append("resources")
        parts.extend("  " + e for e in extra)
        parts.append("")
    if s.query_shape_hashes or s.plan_cache_hashes:
        parts.append("server identifiers")
        if s.query_shape_hashes:
            h = s.query_shape_hashes.most_common(1)[0][0]
            parts.append("  queryShapeHash     : %s" % h)
            # Query settings apply to find, distinct and aggregate only.
            base_op = s.shape.operation.replace("getMore(", "").rstrip(")")
            if base_op in ("find", "distinct", "aggregate"):
                parts.append("    pin or block this shape without a code change (8.0+):")
                parts.append("    db.adminCommand({setQuerySettings: \"%s\", settings: {...}})" % h)
        if s.plan_cache_hashes:
            parts.append("  planCacheShapeHash : %s  (queryHash before 8.0)"
                         % s.plan_cache_hashes.most_common(1)[0][0])
        parts.append("")
    if s.shape.operation == "insert":
        if s.waiting_pct is not None and s.waiting_pct >= 40:
            parts.append("next: an index cannot speed up an insert. Most of this "
                         "time was waiting: look for lock holders, flow control "
                         "and write-concern waits at those times.")
        else:
            parts.append("next: an index cannot speed up an insert. Look at "
                         "document size, the number of indexes to maintain, and "
                         "write concern.")
    elif s.routed:
        parts.append("next: the plan is on the shards: mdbkit queries <shard log> "
                     "--ns %s" % s.shape.ns)
    else:
        parts.append("next: mdbkit advise <log> --ns %s" % s.shape.ns)
    return "\n".join(parts)


def _human_bytes(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%.1f TiB" % n


# ---------------------------------------------------------------- oslog ----

def render_oslog(groups, scanned, stats_note: str = "") -> str:
    parts = ["== mdbkit oslog: what the operating system saw =="]
    parts.append("scanned: %s" % ", ".join(scanned))
    if stats_note:
        parts.append(stats_note)
    parts.append("")
    if not groups:
        parts.append("Nothing of database interest found — no OOM kills, "
                     "file-descriptor limits, I/O errors or service "
                     "restarts in this file.")
        return "\n".join(parts)
    counts = {}
    for g in groups:
        counts[g["severity"]] = counts.get(g["severity"], 0) + 1
    parts.append("findings: %s" % ", ".join(
        "%d %s" % (n, s.lower()) for s, n in sorted(counts.items())))
    parts.append("")
    for g in groups:
        when = ""
        if g["last"]:
            when = "  last at %s" % g["last"].strftime("%Y-%m-%d %H:%M:%S")
        parts.append("[%s] %s — %d occurrence(s)%s"
                     % (g["severity"], g["kind"], g["count"], when))
        if g["explanation"]:
            parts.append("        %s" % g["explanation"])
        if g["processes"]:
            parts.append("        processes: %s" % ", ".join(g["processes"]))
        for ex in g["examples"][:2]:
            parts.append("        > %s" % ex[:160])
        parts.append("")
    return "\n".join(parts).rstrip()


# --------------------------------------------------------- serverstatus ----

def render_serverstatus(checks) -> str:
    parts = ["== mdbkit serverstatus =="]
    counts = {}
    for c in checks:
        counts[c.severity] = counts.get(c.severity, 0) + 1
    order = ["CRIT", "WARN", "OK", "INFO"]
    parts.append("checks: %s" % ", ".join(
        "%d %s" % (counts[s], s.lower()) for s in order if s in counts))
    parts.append("")
    ranked = sorted(checks, key=lambda c: order.index(c.severity)
                    if c.severity in order else 9)
    for c in ranked:
        parts.append("[%s] %s: %s" % (c.severity, c.title, c.detail))
        for e in c.evidence:
            parts.append("        - %s" % e)
        if c.next_step:
            parts.append("        next: %s" % c.next_step)
        parts.append("")
    return "\n".join(parts).rstrip()


# ---------------------------------------------------------------- audit ----

def render_audit(res, stats) -> str:
    parts = ["== mdbkit audit: startup configuration ==", render_parse_stats(stats)]
    src = " from %s" % res.source if res.source and res.source != "log" else ""
    parts.append("startups seen: %d%s%s" % (
        res.startups, src,
        ("   version(s): %s" % ", ".join(res.versions)) if res.versions else ""))
    parts.append("")
    if not res.items:
        if res.startups:
            parts.append("mongod reported no configuration warnings at startup.")
        else:
            from .audit import GETLOG_HINT
            parts.append("No startup in this log, so there is nothing to audit.")
            parts.append(GETLOG_HINT)
        return "\n".join(parts)
    counts = {}
    for i in res.items:
        counts[i.severity] = counts.get(i.severity, 0) + 1
    parts.append("findings: %s" % ", ".join(
        "%d %s" % (counts[k], k.lower()) for k in ("CRIT", "WARN", "INFO")
        if k in counts))
    parts.append("")
    for i in res.items:
        parts.append("[%s] %s" % (i.severity, i.title))
        parts.append("        mongod said: %s" % i.message[:200])
        parts.append("        fix: %s" % i.advice)
        for change in i.changes[:6]:
            parts.append("          - %s" % change)
        if i.startups > 1:
            parts.append("        seen at %d startups" % i.startups)
        elif i.count > 1:
            parts.append("        %d related messages at this startup" % i.count)
        parts.append("")
    return "\n".join(parts).rstrip()
