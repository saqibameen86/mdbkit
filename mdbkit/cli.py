"""mdbkit command-line interface.

Fully offline: reads files/stdin, writes stdout. No network, no telemetry.
"""

from __future__ import annotations

import argparse
import sys

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
    stats = ParseStats()
    agg = QueryAggregator(min_ms=args.min_ms)
    for entry in iter_entries(args.logfile, stats):
        agg.consume(entry)
    _warn_if_pre44(stats)
    results = agg.results(sort_by=args.sort, limit=args.limit)
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


def cmd_filter(args) -> int:
    flt = Filter(
        component=args.component,
        severity=args.severity,
        namespace=args.ns,
        slow_ms=args.slow,
        ts_from=parse_when(args.ts_from) if args.ts_from else None,
        ts_to=parse_when(args.ts_to) if args.ts_to else None,
        msg_contains=args.msg,
    )
    stats = ParseStats()
    matched = 0
    for entry in iter_entries(args.logfile, stats):
        if flt.matches(entry):
            print(entry.raw)
            matched += 1
    _warn_if_pre44(stats)
    print(f"filter: matched {matched:,} of {stats.parsed:,} parsed lines",
          file=sys.stderr)
    return 0


def cmd_advise(args) -> int:
    stats = ParseStats()
    agg = QueryAggregator(min_ms=args.min_ms)
    for entry in iter_entries(args.logfile, stats):
        agg.consume(entry)
    _warn_if_pre44(stats)
    indexes = load_indexes(args.indexes) if args.indexes else None
    schema = load_schema(args.schema) if args.schema else None
    recs = advise(agg.results(), indexes=indexes, schema=schema,
                  min_count=args.min_count)
    if not args.indexes:
        print("note: no --indexes file given; overlap with existing indexes was "
              "not checked. Export with: mdbkit export-script indexes",
              file=sys.stderr)
    if args.json:
        print(dump_json([r.to_dict() for r in recs]))
    else:
        print(render_recommendations(recs, stats))
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
        no_sysprobe=args.no_sysprobe)
    _warn_if_pre44(stats)
    if args.json:
        print(dump_json({
            "window": {"from": cutoff or stats.first_ts, "to": stats.last_ts},
            "findings": [f.to_dict() for f in findings],
        }))
    else:
        print(render_triage(findings, stats, cutoff))
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
    sp.set_defaults(func=cmd_filter)

    sp = sub.add_parser("advise", help="deterministic candidate-index recommendations")
    add_common(sp)
    sp.add_argument("--indexes", help="indexes.json from `mdbkit export-script indexes`")
    sp.add_argument("--schema", help="schema.json from `mdbkit export-script schema`")
    sp.add_argument("--min-ms", type=int, default=0)
    sp.add_argument("--min-count", type=int, default=1,
                    help="only advise on shapes seen at least N times")
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
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_triage)

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
