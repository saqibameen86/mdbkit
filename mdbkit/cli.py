"""mdbkit command-line interface.

Fully offline: reads files/stdin, writes stdout. No network, no telemetry.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import deque

from . import __version__
from .advisor import advise, load_indexes, load_schema
from .analysis import ConnectionAggregator, QueryAggregator, SummaryAggregator
from .filtering import Filter, parse_when
from .parser import PRE_44_HINT, ParseStats, iter_entries
from .render import (
    dump_json,
    render_connections,
    render_queries,
    render_recommendations,
    render_summary,
)
from .scripts import INDEXES_SCRIPT, SCHEMA_SCRIPT


def _warn_if_pre44(stats: ParseStats) -> None:
    if stats.total_lines and stats.unparsed_ratio > 0.5:
        print(f"warning: {PRE_44_HINT}", file=sys.stderr)


def cmd_loginfo(args) -> int:
    _warn_large_file(args.logfile)
    stats = ParseStats()
    agg = SummaryAggregator()
    for entry in iter_entries(args.logfile, stats):
        agg.consume(entry)
    _warn_if_pre44(stats)
    if args.json:
        out = agg.summary.to_dict()
        out["parse"] = {"lines": stats.total_lines, "parsed": stats.parsed,
                        "unparsed": stats.unparsed}
        print(dump_json(out))
    else:
        print(render_summary(agg.summary, stats))
    return 0


def cmd_queries(args) -> int:
    _warn_large_file(args.logfile)
    stats = ParseStats()
    agg = QueryAggregator(min_ms=args.min_ms,
                          include_system=getattr(args, "include_system", False))
    for entry in iter_entries(args.logfile, stats):
        agg.consume(entry)
    _warn_if_pre44(stats)
    results = agg.results(sort_by=args.sort, limit=args.limit)
    if agg.skipped_system:
        print("note: excluded %d internal operation(s) on admin/config/local "
              "(--include-system to show them)" % agg.skipped_system,
              file=sys.stderr)
    if args.report:
        from .report import Report, stamp
        rep = Report("MongoDB slow query analysis", stamp())
        rows = [[s.shape.ns, s.shape.operation, s.count, s.total_ms,
                 round(s.mean_ms), s.max_ms, s.docs_examined,
                 ("%.0f:1" % s.scan_ratio) if s.n_returned else "-",
                 s.shape.pretty()[:80]] for s in results]
        rep.table("Slow query shapes",
                  ["namespace", "op", "count", "cumMs", "mean", "max",
                   "docsEx", "scan", "shape"], rows)
        rep.text("Note",
                 "cumMs is time summed across all occurrences of a shape, not "
                 "a single query. Literal values are never included: these are "
                 "query shapes only.")
        print("wrote %s" % rep.write(args.report), file=sys.stderr)
        return 0
    if args.json:
        print(dump_json([s.to_dict() for s in results]))
    else:
        print(render_queries(results, stats))
    return 0


def cmd_connections(args) -> int:
    stats = ParseStats()
    agg = ConnectionAggregator()
    for entry in iter_entries(args.logfile, stats):
        agg.consume(entry)
    _warn_if_pre44(stats)
    if args.json:
        print(dump_json(agg.report.to_dict()))
    else:
        print(render_connections(agg.report, stats))
    return 0


def _emit(entry, as_explain: bool, wrap: bool):
    """Render one matching entry. Never raises: a single odd log line must
    not abort a filter run over a million-line file."""
    if not as_explain:
        return entry.raw
    from .rebuild import to_mongosh, wrap_for_export
    try:
        cmd = to_mongosh(entry)
    except Exception:
        return None
    if cmd is None:
        return None
    return wrap_for_export(cmd) if wrap else cmd


def cmd_filter(args) -> int:
    _warn_large_file(args.logfile)
    try:
        flt = Filter(
            component=args.component,
            severity=args.severity,
            namespace=args.ns,
            slow_ms=args.slow,
            ts_from=parse_when(args.ts_from) if args.ts_from else None,
            ts_to=parse_when(args.ts_to) if args.ts_to else None,
            msg_contains=args.msg,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    stats = ParseStats()
    matched = 0
    shown = 0
    tail = deque(maxlen=args.last) if args.last else None

    for entry in iter_entries(args.logfile, stats):
        if not flt.matches(entry):
            continue
        matched += 1
        line = _emit(entry, args.as_explain, args.explain_script)
        if line is None:
            continue
        if tail is not None:
            tail.append(line)
        elif args.limit and shown >= args.limit:
            continue
        else:
            print(line)
            shown += 1

    if tail is not None:
        for raw in tail:
            print(raw)
        shown = len(tail)

    _warn_if_pre44(stats)
    note = f"filter: matched {matched:,} of {stats.parsed:,} parsed lines"
    if shown < matched:
        which = "most recent" if tail is not None else "first"
        note += f"; showing {which} {shown:,} (use --limit/--last to change)"
    print(note, file=sys.stderr)
    return 0


def _warn_large_file(logfile: str) -> None:
    """Warn when a log file is large so users know to be patient."""
    if logfile == "-":
        return
    try:
        mb = os.path.getsize(logfile) / 1024 / 1024
        if mb > 500:
            print(f"note: {logfile} is {mb:.0f} MB — analysis may take several "
                  f"minutes. Ctrl-C to abort.", file=sys.stderr)
        elif mb > 100:
            print(f"note: {logfile} is {mb:.0f} MB — this may take a moment.",
                  file=sys.stderr)
    except OSError:
        pass


def cmd_advise(args) -> int:
    _warn_large_file(args.logfile)
    stats = ParseStats()
    agg = QueryAggregator(min_ms=args.min_ms,
                          include_system=getattr(args, "include_system", False))
    for entry in iter_entries(args.logfile, stats):
        agg.consume(entry)
    _warn_if_pre44(stats)
    indexes = load_indexes(args.indexes) if args.indexes else None
    schema = load_schema(args.schema) if args.schema else None
    results = agg.results()
    if args.ns:
        results = [r for r in results if r.shape.ns == args.ns]
    recs = advise(results, indexes=indexes, schema=schema,
                  min_count=args.min_count)
    if not args.indexes:
        print("note: no --indexes file given; overlap with existing indexes was "
              "not checked. Run: mdbkit export-script indexes  (then pass --indexes)",
              file=sys.stderr)
    if args.json:
        print(dump_json([r.to_dict() for r in recs]))
    else:
        print(render_recommendations(recs, stats, limit=args.limit))
    return 0


def cmd_explain(args) -> int:
    from .explain import analyze_explain, load_explain, render_explain
    try:
        doc = load_explain(args.explainfile)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    indexes = load_indexes(args.indexes) if args.indexes else None
    schema = load_schema(args.schema) if args.schema else None
    report = analyze_explain(doc, indexes=indexes, schema=schema)
    if args.json:
        print(dump_json(report.to_dict()))
    else:
        print(render_explain(report))
    return 0


def cmd_triage(args) -> int:
    from .triage import render_triage, run_triage
    findings, stats, cutoff = run_triage(
        args.logfile, window_min=args.window, dbpath=args.dbpath,
        no_sysprobe=args.no_sysprobe, ftdc_path=args.ftdc)
    _warn_if_pre44(stats)
    if args.report:
        from .report import Report, stamp
        win = ""
        if stats.first_ts and stats.last_ts:
            win = "window %s -> %s" % (
                (cutoff or stats.first_ts).strftime("%Y-%m-%d %H:%M"),
                stats.last_ts.strftime("%H:%M"))
        rep = Report("MongoDB incident triage", stamp(win))
        rep.findings("Findings", findings)
        path = rep.write(args.report)
        print("wrote %s" % path, file=sys.stderr)
        return 0
    if args.json:
        print(dump_json({
            "window": {"from": cutoff or stats.first_ts, "to": stats.last_ts},
            "findings": [f.to_dict() for f in findings],
        }))
    else:
        print(render_triage(findings, stats, cutoff))
    return 0


def parse_duration(text: str) -> int:
    """Parse '90m', '4h', '2d' or a bare number of minutes."""
    t = text.strip().lower()
    mult = 1
    if t.endswith("m"):
        t = t[:-1]
    elif t.endswith("h"):
        t, mult = t[:-1], 60
    elif t.endswith("d"):
        t, mult = t[:-1], 1440
    try:
        return int(float(t) * mult)
    except ValueError:
        raise ValueError("could not parse duration %r (try 90m, 4h, 2d)" % text)


def _ftdc_window(path, args):
    """Resolve the time window for an ftdc command.

    diagnostic.data can hold weeks of history; decoding all of it is a
    minutes-long, CPU-bound job. Commands therefore default to a recent
    window and skip older chunks before decompressing them.
    """
    from .ftdc import ftdc_files, iter_documents, chunk_timestamp
    from datetime import timedelta
    ts_from = parse_when(args.ts_from) if args.ts_from else None
    ts_to = parse_when(args.ts_to) if args.ts_to else None
    if ts_from or ts_to or getattr(args, "all", False):
        return ts_from, ts_to, None
    minutes = parse_duration(args.last) if args.last else 240
    newest = None
    for f in ftdc_files(path):
        for doc in iter_documents(f):
            if doc.get("type") == 1:
                t = chunk_timestamp(doc)
                if t and (newest is None or t > newest):
                    newest = t
    if newest is None:
        return None, None, None
    return newest - timedelta(minutes=minutes), None, minutes


def cmd_ftdc(args) -> int:
    from .ftdc import FtdcReader, ftdc_files
    from .render import render_ftdc_summary, render_ftdc_timeline
    files = ftdc_files(args.path)
    if not files:
        print("error: no FTDC files found at %s (expected a "
              "diagnostic.data directory or a metrics.* file)" % args.path,
              file=sys.stderr)
        return 2
    try:
        ts_from, ts_to, win_min = _ftdc_window(args.path, args)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    if win_min:
        print("note: showing the last %s of recorded metrics "
              "(--last/--from/--to to change, --all for everything)"
              % (args.last or "4h"), file=sys.stderr)

    def progress(done, total):
        if total > 3:
            print("\rreading FTDC: file %d/%d" % (done, total),
                  end="" if done < total else "\n", file=sys.stderr)

    wanted = args.metric or None
    reader = FtdcReader(wanted=wanted, keep_values=False, progress=progress)
    if args.action in ("timeline", "export"):
        reader.keep_values = True
    reader.read(args.path, ts_from=ts_from, ts_to=ts_to)
    if reader.chunks == 0:
        print("warning: no metric chunks decoded from %d file(s)" % len(files),
              file=sys.stderr)
    if args.action == "summary":
        if args.json:
            print(dump_json(reader.summary()))
        else:
            print(render_ftdc_summary(reader, len(files)))
    elif args.action == "timeline":
        if args.json:
            print(dump_json({k: {"times": [t.isoformat() for t in v.times],
                                 "values": v.values}
                             for k, v in reader.series.items()}))
        else:
            print(render_ftdc_timeline(reader, args.step))
    else:  # export
        import csv as _csv
        labels = sorted(reader.series)
        if not labels:
            return 0
        writer = _csv.writer(sys.stdout)
        writer.writerow(["time"] + labels)
        base = reader.series[labels[0]]
        for i, t in enumerate(base.times):
            row = [t.isoformat()]
            for lb in labels:
                vals = reader.series[lb].values
                row.append(vals[i] if i < len(vals) else "")
            writer.writerow(row)
    return 0


def cmd_export_script(args) -> int:
    print(SCHEMA_SCRIPT if args.kind == "schema" else INDEXES_SCRIPT)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mdbkit",
        description=(
            "Offline toolkit for MongoDB 4.4+ structured logs: log summaries, "
            "slow-query analysis, connection churn, log filtering, and "
            "deterministic candidate-index advice. A spiritual successor to "
            "mtools' log tools. Never connects to a network."
        ),
    )
    p.add_argument("--version", action="version", version=f"mdbkit {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def add_common(sp):
        sp.add_argument("logfile", help="path to mongod/mongos log (.log or .gz), or '-' for stdin")
        sp.add_argument("--json", action="store_true", help="machine-readable JSON output")

    sp = sub.add_parser("loginfo", help="overall log summary (versions, restarts, counts)")
    add_common(sp)
    sp.set_defaults(func=cmd_loginfo)

    sp = sub.add_parser("queries", help="group and rank slow queries by shape")
    add_common(sp)
    sp.add_argument("--sort", default="totalMs",
                    choices=["totalMs", "duration", "count", "mean", "max",
                             "docsExamined", "scanRatio"])
    sp.add_argument("--limit", type=int, default=0, help="show top N shapes")
    sp.add_argument("--min-ms", type=int, default=0,
                    help="ignore operations faster than this")
    sp.add_argument("--include-system", action="store_true",
                    help="include internal admin/config/local namespaces "
                         "(hidden by default — they are server housekeeping, "
                         "not your workload)")
    sp.add_argument("--report", metavar="FILE",
                    help="write a shareable .md or .html report instead")
    sp.set_defaults(func=cmd_queries)

    sp = sub.add_parser("connections", help="connection churn by source IP and app")
    add_common(sp)
    sp.set_defaults(func=cmd_connections)

    sp = sub.add_parser("filter", help="stream matching raw log lines (chainable)")
    sp.add_argument("logfile")
    sp.add_argument("--component", help="e.g. COMMAND, NETWORK, REPL")
    sp.add_argument("--severity", help="I, W, E, F")
    sp.add_argument("--ns", help="exact namespace, e.g. shop.orders")
    sp.add_argument("--slow", type=int, help="only ops with durationMillis >= N")
    sp.add_argument("--from", dest="ts_from", help="ISO timestamp lower bound")
    sp.add_argument("--to", dest="ts_to", help="ISO timestamp upper bound")
    sp.add_argument("--msg", help="substring match on the msg field")
    sp.add_argument("--limit", type=int, metavar="N",
                    help="print only the first N matching lines")
    sp.add_argument("--last", type=int, metavar="N",
                    help="print only the LAST N matching lines (most recent — "
                         "usually what you want during an incident)")
    sp.add_argument("--as-explain", action="store_true",
                    help="rebuild each matching slow query as a runnable "
                         "mongosh .explain() command instead of printing the "
                         "raw log line")
    sp.add_argument("--explain-script", action="store_true",
                    help="with --as-explain, wrap output in EJSON.stringify() "
                         "and usage comments so it can be piped to a .js file")
    sp.set_defaults(func=cmd_filter)

    sp = sub.add_parser("advise", help="deterministic candidate-index recommendations")
    add_common(sp)
    sp.add_argument("--indexes", help="indexes.json from `mdbkit export-script indexes`")
    sp.add_argument("--schema", help="schema.json from `mdbkit export-script schema`")
    sp.add_argument("--min-ms", type=int, default=0)
    sp.add_argument("--min-count", type=int, default=1,
                    help="only advise on shapes seen at least N times")
    sp.add_argument("--ns", metavar="NAMESPACE",
                    help="only advise on this namespace, e.g. shop.orders")
    sp.add_argument("--limit", type=int, default=10, metavar="N",
                    help="show only the top N recommendations (default 10; "
                         "0 = all)")
    sp.add_argument("--include-system", action="store_true",
                    help="include internal admin/config/local namespaces")
    sp.set_defaults(func=cmd_advise)

    sp = sub.add_parser("explain",
                        help="analyze a saved explain('executionStats') JSON file")
    sp.add_argument("explainfile",
                    help="explain output saved as JSON (mongosh EJSON.stringify "
                         "or Compass export)")
    sp.add_argument("--json", action="store_true", help="machine-readable output")
    sp.add_argument("--indexes", help="indexes.json for overlap checks")
    sp.add_argument("--schema", help="schema.json for field caveats")
    sp.set_defaults(func=cmd_explain)

    sp = sub.add_parser("triage",
                        help="one-command incident snapshot (beta): elections, "
                             "storms, hot collections, errors + local disk/"
                             "memory/load probes")
    sp.add_argument("logfile", help="mongod log (.log or .gz), or '-'")
    sp.add_argument("--window", type=int, metavar="MINUTES",
                    help="analyze only the last N minutes of log time "
                         "(default: whole file)")
    sp.add_argument("--dbpath", help="override dbPath for the disk probe")
    sp.add_argument("--no-sysprobe", action="store_true",
                    help="skip local disk/memory/load probes")
    sp.add_argument("--ftdc", metavar="PATH",
                    help="diagnostic.data directory — adds CPU, memory, cache "
                         "and connection metrics from MongoDB's own recorder")
    sp.add_argument("--report", metavar="FILE",
                    help="write a shareable report instead of terminal output "
                         "(.md or .html; self-contained, no external assets)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_triage)

    sp = sub.add_parser("ftdc",
                        help="decode diagnostic.data (FTDC): CPU, memory, "
                             "cache, connections and op rates, offline")
    sp.add_argument("action", choices=["summary", "timeline", "export"],
                    help="summary = min/avg/max per metric; timeline = values "
                         "over time; export = CSV to stdout")
    sp.add_argument("path", help="diagnostic.data directory or a metrics.* file")
    sp.add_argument("--metric", action="append",
                    help="restrict to a metric label (repeatable), "
                         "e.g. --metric conns.current")
    sp.add_argument("--step", type=int, default=60, metavar="SECONDS",
                    help="timeline bucket size (default 60)")
    sp.add_argument("--last", metavar="DURATION",
                    help="analyze only the most recent window, e.g. 90m, 4h, "
                         "2d (default 4h)")
    sp.add_argument("--all", action="store_true",
                    help="analyze the entire history — can take minutes and "
                         "is CPU-bound on a large diagnostic.data")
    sp.add_argument("--from", dest="ts_from", help="ISO timestamp lower bound")
    sp.add_argument("--to", dest="ts_to", help="ISO timestamp upper bound")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_ftdc)

    sp = sub.add_parser("export-script",
                        help="print a mongosh script to export schema or indexes")
    sp.add_argument("kind", choices=["schema", "indexes"])
    sp.set_defaults(func=cmd_export_script)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
