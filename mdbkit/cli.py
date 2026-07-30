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
from .parser import (PRE_44_HINT, ParseStats, expand_paths, iter_entries,
                     iter_entries_multi)
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
    for entry in iter_entries_multi(args.logfile, stats):
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
    for entry in iter_entries_multi(args.logfile, stats):
        agg.consume(entry)
    _warn_if_pre44(stats)
    results = agg.results(sort_by=args.sort, limit=args.limit)
    if agg.skipped_system:
        print("note: excluded %d internal operation(s) on admin/config/local "
              "(--include-system to show them)" % agg.skipped_system,
              file=sys.stderr)
    if getattr(args, "shape", None):
        from .render import render_shape_detail
        idx = args.shape - 1
        if idx < 0 or idx >= len(results):
            print("error: --shape %d is out of range (1..%d)"
                  % (args.shape, len(results)), file=sys.stderr)
            return 2
        if args.json:
            print(dump_json(results[idx].to_dict()))
        else:
            print(render_shape_detail(results[idx], stats))
        return 0
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
    for entry in iter_entries_multi(args.logfile, stats):
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

    for entry in iter_entries_multi(args.logfile, stats):
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


def _warn_large_file(logfile) -> None:
    """Warn when the input is large so users know to be patient."""
    paths = logfile if isinstance(logfile, list) else [logfile]
    paths = [p for p in paths if p != "-"]
    if not paths:
        return
    try:
        mb = sum(os.path.getsize(p) for p in paths) / 1024 / 1024
        if len(paths) > 1:
            print("note: reading %d files as one stream" % len(paths),
                  file=sys.stderr)
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
    for entry in iter_entries_multi(args.logfile, stats):
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
        no_sysprobe=args.no_sysprobe, ftdc_path=args.ftdc,
        oslog=getattr(args, "oslog", None))
    if getattr(args, "only", None):
        keep = {s.strip().upper() for s in args.only.split(",")}
        findings = [f for f in findings if f.severity in keep]
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
    if getattr(args, "exit_code", False):
        sev = {f.severity for f in findings}
        return 2 if "CRIT" in sev else (1 if "WARN" in sev else 0)
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


def cmd_oslog(args) -> int:
    from . import oslog as OS
    from .render import render_oslog
    paths = args.logfile if args.logfile else OS.discover()
    if not paths:
        if OS.uses_journald():
            print("error: no readable text system log found.\n  " +
                  OS.JOURNAL_HINT, file=sys.stderr)
        else:
            print("error: no system log given and none found in %s"
                  % ", ".join(OS.COMMON_OSLOGS), file=sys.stderr)
        return 2
    try:
        events = OS.scan(paths)
    except OSError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    groups = OS.summarize(events)
    if args.json:
        print(dump_json({"scanned": list(paths), "findings": [
            {**g, "first": g["first"].isoformat() if g["first"] else None,
             "last": g["last"].isoformat() if g["last"] else None}
            for g in groups]}))
    else:
        print(render_oslog(groups, list(paths)))
    if args.exit_code:
        return 2 if any(g["severity"] == "CRIT" for g in groups) else (
            1 if any(g["severity"] == "WARN" for g in groups) else 0)
    return 0


def cmd_serverstatus(args) -> int:
    from . import serverstatus as SS
    from .render import render_serverstatus
    try:
        doc = SS.load(args.statusfile)
        after = SS.load(args.after) if args.after else None
    except (OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        print("  Produce a dump with: mdbkit export-script serverstatus",
              file=sys.stderr)
        return 2
    checks = SS.analyze(doc, after)
    if args.json:
        print(dump_json({"checks": [c.to_dict() for c in checks]}))
    elif args.report:
        from .report import Report, stamp
        rep = Report("MongoDB serverStatus digest", stamp())
        rep.table("Checks", ["severity", "check", "detail"],
                  [[c.severity, c.title, c.detail] for c in checks])
        print("wrote %s" % rep.write(args.report), file=sys.stderr)
    else:
        print(render_serverstatus(checks))
    if args.exit_code:
        sev = {c.severity for c in checks}
        return 2 if "CRIT" in sev else (1 if "WARN" in sev else 0)
    return 0


def cmd_compare(args) -> int:
    from .compare import aggregate_file, compare
    from .render import render_compare
    _warn_large_file(args.before)
    _warn_large_file(args.after)
    try:
        before, sb = aggregate_file(args.before, min_ms=args.min_ms,
                                    include_system=args.include_system)
        after, sa = aggregate_file(args.after, min_ms=args.min_ms,
                                   include_system=args.include_system)
    except FileNotFoundError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    result = compare(before, after, min_count=args.min_count, ns=args.ns)
    if args.json:
        print(dump_json(result.to_dict()))
    elif args.report:
        from .report import Report, stamp
        rep = Report("MongoDB before/after comparison", stamp())
        rows = []
        for d in result.deltas:
            if d.status == "unchanged":
                continue
            rows.append([d.ns, d.status, d.shape[:60],
                         round(d.before.mean_ms) if d.before else "-",
                         round(d.after.mean_ms) if d.after else "-",
                         "%+.0f%%" % d.mean_pct if d.before and d.after else "-"])
        rep.table("Query shapes", ["namespace", "status", "shape",
                                   "mean before (ms)", "mean after (ms)",
                                   "change"], rows)
        print("wrote %s" % rep.write(args.report), file=sys.stderr)
    else:
        print(render_compare(result, sb, sa, limit=args.limit))
    return 0


def cmd_demo(args) -> int:
    from .demo import DemoLog, write_extras
    try:
        log = DemoLog(scenario=args.scenario, minutes=args.minutes,
                      seed=args.seed)
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    lines = log.build()
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        msg = ["wrote %s (%d lines, %s scenario, %d minutes)" % (
            args.out, len(lines), args.scenario, args.minutes)]
        if args.with_extras:
            directory = os.path.dirname(os.path.abspath(args.out)) or "."
            for path in write_extras(directory):
                msg.append("wrote %s" % path)
        msg.append("")
        msg.append("Try:")
        msg.append("  mdbkit loginfo %s" % args.out)
        msg.append("  mdbkit queries %s" % args.out)
        msg.append("  mdbkit triage %s --window 0 --no-sysprobe" % args.out)
        if args.with_extras:
            msg.append("  mdbkit advise %s --indexes indexes.json "
                       "--schema schema.json" % args.out)
            msg.append("  mdbkit explain explain.json")
        print("\n".join(msg), file=sys.stderr)
    else:
        for ln in lines:
            print(ln)
    return 0


def _lab_echo(msg=""):
    print(msg, file=sys.stderr)


def cmd_lab(args) -> int:
    from . import lab
    try:
        if args.lab_action == "start":
            state = lab.start(directory=args.dir, nodes=args.nodes,
                              base_port=args.port, slowms=args.slowms,
                              standalone=args.standalone, echo=_lab_echo)
            print("")
            print("lab ready: %s" % lab.connection_string(state))
            print("directory: %s" % state["dir"])
            for n in state["nodes"]:
                print("  node%d  port %d  log %s"
                      % (n["index"], n["port"], n["log"]))
            print("")
            print("Next:")
            print("  mdbkit lab seed                     # sample data + workload")
            print("  mdbkit queries %s" % state["nodes"][0]["log"])
            print("  mdbkit lab destroy                  # remove it all")
        elif args.lab_action == "status":
            state = lab.status(args.dir)
            if not state:
                print("no lab found in %s" % args.dir)
                return 0
            print("lab in %s (created %s)" % (state["dir"], state["createdAt"]))
            print("connection: %s" % lab.connection_string(state))
            for n in state["nodes"]:
                print("  node%d  port %-6d pid %-8s %s"
                      % (n["index"], n["port"], n.get("pid") or "-",
                         "running" if n.get("running") else "stopped"))
        elif args.lab_action == "seed":
            ok = lab.seed(args.dir, docs=args.docs, echo=_lab_echo)
            if ok:
                state = lab.status(args.dir)
                print("")
                print("Now analyse what it produced:")
                print("  mdbkit queries %s" % state["nodes"][0]["log"])
                print("  mdbkit advise %s" % state["nodes"][0]["log"])
        elif args.lab_action == "stop":
            n = lab.stop(args.dir, echo=_lab_echo)
            print("stopped %d node(s)" % n)
        elif args.lab_action == "destroy":
            if not args.yes:
                print("This deletes %s and all data in it." % args.dir,
                      file=sys.stderr)
                print("Re-run with --yes to confirm.", file=sys.stderr)
                return 1
            lab.destroy(args.dir, echo=_lab_echo)
        elif args.lab_action == "logs":
            state = lab.status(args.dir)
            if not state:
                print("no lab found in %s" % args.dir, file=sys.stderr)
                return 1
            for path in lab.log_paths(state):
                print(path)
    except lab.LabError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    return 0


def cmd_export_script(args) -> int:
    if args.kind == "serverstatus":
        from .scripts import SERVERSTATUS
        print(SERVERSTATUS)
        return 0
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
        sp.add_argument("logfile", nargs="+",
                        help="mongod/mongos log file(s) or a glob (.log, .gz), "
                             "or '-' for stdin. Rotated logs are read as one "
                             "stream, e.g. \"mongod.log*\"")
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
    sp.add_argument("--shape", type=int, metavar="N",
                    help="show full detail for shape N from the table "
                         "(plans, clients, timings, scan ratio)")
    sp.set_defaults(func=cmd_queries)

    sp = sub.add_parser("connections", help="connection churn by source IP and app")
    add_common(sp)
    sp.set_defaults(func=cmd_connections)

    sp = sub.add_parser("filter", help="stream matching raw log lines (chainable)")
    sp.add_argument("logfile", nargs="+",
                    help="log file(s) or a glob, or '-' for stdin")
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
    sp.add_argument("logfile", nargs="+",
                    help="mongod log file(s) or a glob, or '-' for stdin")
    sp.add_argument("--window", type=int, metavar="MINUTES",
                    help="analyze only the last N minutes of log time "
                         "(default: whole file)")
    sp.add_argument("--dbpath", help="override dbPath for the disk probe")
    sp.add_argument("--no-sysprobe", action="store_true",
                    help="skip local disk/memory/load probes")
    sp.add_argument("--ftdc", metavar="PATH",
                    help="diagnostic.data directory — adds CPU, memory, cache "
                         "and connection metrics from MongoDB's own recorder. "
                         "Auto-discovered when the log came from this host")
    sp.add_argument("--oslog", nargs="+", metavar="FILE",
                    help="system log(s) to correlate (/var/log/syslog, "
                         "/var/log/messages, or captured journalctl output). "
                         "Reveals OOM kills and file-descriptor limits that "
                         "the mongod log cannot record")
    sp.add_argument("--only", metavar="LEVELS",
                    help="show only these severities, e.g. --only CRIT,WARN")
    sp.add_argument("--exit-code", action="store_true",
                    help="exit 2 if any CRIT finding, 1 if any WARN, else 0 "
                         "— for cron and monitoring wrappers")
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

    sp = sub.add_parser("oslog",
                        help="scan a system log for OOM kills, fd limits, "
                             "I/O errors and service restarts")
    sp.add_argument("logfile", nargs="*",
                    help="system log file(s); defaults to /var/log/syslog "
                         "or /var/log/messages when readable")
    sp.add_argument("--exit-code", action="store_true",
                    help="exit 2 on CRIT, 1 on WARN, else 0")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_oslog)

    sp = sub.add_parser("serverstatus",
                        help="digest a saved db.adminCommand({serverStatus:1}) "
                             "dump: tickets, cache, queues, connections")
    sp.add_argument("statusfile", help="saved serverStatus JSON")
    sp.add_argument("--after", metavar="FILE",
                    help="a second dump taken later; turns cumulative "
                         "counters into true rates")
    sp.add_argument("--report", metavar="FILE",
                    help="write a shareable .md or .html report instead")
    sp.add_argument("--exit-code", action="store_true",
                    help="exit 2 on CRIT, 1 on WARN, else 0")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_serverstatus)

    sp = sub.add_parser("compare",
                        help="diff two logs: did the index actually help?")
    sp.add_argument("before", nargs="+",
                    help="log file(s) from before the change")
    sp.add_argument("--after", nargs="+", required=True,
                    help="log file(s) from after the change")
    sp.add_argument("--ns", metavar="NAMESPACE",
                    help="only compare this namespace")
    sp.add_argument("--min-count", type=int, default=3,
                    help="ignore shapes seen fewer than N times in a log "
                         "(default 3, so noise is not read as a regression)")
    sp.add_argument("--min-ms", type=int, default=0,
                    help="ignore operations faster than this")
    sp.add_argument("--limit", type=int, default=15,
                    help="shapes to print (default 15, 0 = all)")
    sp.add_argument("--include-system", action="store_true",
                    help="include internal admin/config/local namespaces")
    sp.add_argument("--report", metavar="FILE",
                    help="write a shareable .md or .html report instead")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_compare)

    sp = sub.add_parser("demo",
                        help="generate a realistic sample log so you can try "
                             "mdbkit without a MongoDB")
    sp.add_argument("--scenario", default="mixed",
                    choices=["healthy", "incident", "mixed"],
                    help="what the log should contain (default mixed)")
    sp.add_argument("--minutes", type=int, default=90,
                    help="how much log time to generate (default 90)")
    sp.add_argument("--seed", type=int, default=7,
                    help="deterministic seed — same seed, same log")
    sp.add_argument("-o", "--out", metavar="FILE",
                    help="write to a file instead of stdout")
    sp.add_argument("--with-extras", action="store_true",
                    help="also write indexes.json, schema.json and "
                         "explain.json alongside --out")
    sp.set_defaults(func=cmd_demo)

    sp = sub.add_parser("lab",
                        help="start a disposable local MongoDB for testing "
                             "(the only command that runs external processes)")
    sp.add_argument("lab_action",
                    choices=["start", "seed", "status", "stop", "destroy",
                             "logs"])
    sp.add_argument("--dir", default=None,
                    help="lab directory (default ~/.mdbkit-lab)")
    sp.add_argument("--nodes", type=int, default=3,
                    help="replica set size (default 3)")
    sp.add_argument("--port", type=int, default=28110,
                    help="base port (default 28110, deliberately far from 27017)")
    sp.add_argument("--slowms", type=int, default=0,
                    help="slow query threshold in ms (default 0 = log every "
                         "operation, which is what makes the log interesting)")
    sp.add_argument("--standalone", action="store_true",
                    help="single node, no replica set")
    sp.add_argument("--docs", type=int, default=50000,
                    help="documents to insert for `seed` (default 50000)")
    sp.add_argument("--yes", action="store_true",
                    help="confirm destructive actions")
    sp.set_defaults(func=cmd_lab)

    sp = sub.add_parser("export-script",
                        help="print a mongosh script to export schema or indexes")
    sp.add_argument("kind", choices=["schema", "indexes", "serverstatus"])
    sp.set_defaults(func=cmd_export_script)

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "command", None) == "lab" and not args.dir:
        from .lab import DEFAULT_DIR
        args.dir = DEFAULT_DIR
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
