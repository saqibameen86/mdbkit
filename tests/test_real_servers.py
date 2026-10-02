"""Regression tests built from real MongoDB 7.0.43, 8.0.32, 8.3.11 and 9.0.2
output (see fixtures/real/README.md). Each test names the bug that real
output exposed in 0.6.0 or earlier."""

import contextlib
import io
import json
import os
import struct
import zlib
from datetime import datetime, timedelta, timezone

import pytest

from mdbkit import ftdc
from mdbkit import scripts
from mdbkit.analysis import BatchDedup, QueryAggregator
from mdbkit.cli import main
from mdbkit.parser import iter_entries, num, parse_line
from mdbkit.shelljson import loads_lenient
from mdbkit.triage import TriageEngine, pick_mongod, run_triage

REAL = os.path.join(os.path.dirname(__file__), "fixtures", "real")
VERSIONS = ("7.0.43", "8.0.32", "9.0.2")


def real(name):
    return os.path.join(REAL, name)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = main(argv)
        except SystemExit as exc:
            rc = exc.code
    return rc, out.getvalue(), err.getvalue()


def shapes(version):
    agg = QueryAggregator()
    for e in iter_entries(real("mongod-%s.log" % version)):
        agg.consume(e)
    return {(s.shape.ns, s.shape.operation, s.shape.pretty()): s
            for s in agg.results()}


def by_op(version, ns, op):
    return [s for (n, o, _), s in shapes(version).items() if n == ns and o == op]


# ------------------------------------------------------------ queries ---

@pytest.mark.parametrize("version", VERSIONS)
def test_every_command_runs_on_real_logs(version):
    log = real("mongod-%s.log" % version)
    for argv in (["loginfo", log], ["queries", log], ["queries", log, "--shape", "1"],
                 ["connections", log], ["advise", log], ["audit", log],
                 ["triage", log, "--window", "0", "--no-sysprobe"],
                 ["filter", log, "--slow", "100"], ["queries", log, "--json"]):
        rc, out, err = run(argv)
        assert rc == 0, (argv, err)
        assert "Traceback" not in out + err


@pytest.mark.parametrize("version", VERSIONS)
def test_getmore_takes_its_shape_from_originating_command(version):
    """Real logs put originatingCommand beside the command, not inside it;
    every getMore used to collapse into one empty shape."""
    gm = [k for k in shapes(version) if k[1].startswith("getMore")]
    assert gm, "fixture has getMore lines"
    assert any(op == "getMore(find)" for _, op, _ in gm)
    assert any("$where" in shape for _, _, shape in gm)


@pytest.mark.parametrize("version", VERSIONS)
def test_slow_update_is_counted_once(version):
    """A slow update logs a WRITE line and a COMMAND batch line. 9.0 puts an
    opid on both, 8.0 and 7.0 on neither; pairing must work for all."""
    raw = [json.loads(l) for l in open(real("mongod-%s.log" % version))]
    writes = sum(1 for d in raw if d["c"] == "WRITE" and d.get("msg") == "Slow query")
    ups = by_op(version, "shop.orders", "update")
    assert writes >= 2
    assert sum(s.count for s in ups) == writes


def test_dedup_does_not_swallow_unrelated_commands():
    d = BatchDedup()
    w = parse_line(json.dumps({"t": {"$date": "2026-10-02T10:00:00Z"}, "s": "I",
        "c": "WRITE", "id": 51803, "ctx": "conn1", "msg": "Slow query",
        "attr": {"ns": "shop.orders", "durationMillis": 200}}))
    other = parse_line(json.dumps({"t": {"$date": "2026-10-02T10:00:01Z"}, "s": "I",
        "c": "COMMAND", "id": 51803, "ctx": "conn1", "msg": "Slow query",
        "attr": {"ns": "shop.$cmd", "durationMillis": 200,
                 "command": {"update": "users", "updates": [], "$db": "shop"}}}))
    assert d.is_duplicate(w) is False
    assert d.is_duplicate(other) is False      # different collection


@pytest.mark.parametrize("version", VERSIONS)
def test_slow_insert_blocked_by_a_lock_is_reported(version):
    """Inserts used to be dropped from `queries`, hiding write-latency
    problems: here an insert waited ~3 s behind fsyncLock."""
    waits = by_op(version, "shop.waits", "insert")
    assert waits and max(max(s.durations) for s in waits) >= 2500


def test_waiting_vs_working_on_real_8x():
    for version in ("8.0.32", "9.0.2"):
        s = max(by_op(version, "shop.waits", "insert"), key=lambda s: s.total_ms)
        assert s.waiting_pct is not None and s.waiting_pct > 80
    s7 = max(by_op("7.0.43", "shop.waits", "insert"), key=lambda s: s.total_ms)
    assert s7.waiting_pct is None          # no workingMillis before 8.0


def test_insert_detail_has_no_index_advice_or_query_settings():
    rc, out, _ = run(["queries", real("mongod-9.0.2.log"), "--json"])
    rows = json.loads(out)
    idx = next(i for i, r in enumerate(rows)
               if r["ns"] == "shop.waits" and r["operation"] == "insert")
    rc, out, _ = run(["queries", real("mongod-9.0.2.log"), "--shape", str(idx + 1)])
    assert "setQuerySettings" not in out
    assert "mdbkit advise" not in out
    assert "an index cannot speed up an insert" in out


def test_spill_detected_on_real_8x():
    rc, out, _ = run(["queries", real("mongod-9.0.2.log")])
    assert "+SPILL" in out


def test_in_progress_lines_are_counted_not_treated_as_slow_queries():
    rc, out, _ = run(["loginfo", real("slowprog-9.0.2.log")])
    assert "still-running operations logged (8.3+): 5" in out


# ------------------------------------------------------------- triage ---

def test_first_election_of_a_new_set_is_not_instability():
    findings, _, _ = run_triage(real("mongod-9.0.2.log"), window_min=0,
                                no_sysprobe=True)
    titles = {f.title: f for f in findings}
    assert "Replica set instability" not in titles
    assert titles["Replica set created"].severity == "INFO"


def test_clean_restart_is_warn_and_crash_is_crit():
    f, _, _ = run_triage(real("clean_restart-8.0.32.log"), window_min=0,
                         no_sysprobe=True)
    start = next(x for x in f if x.title == "Process start(s) in window")
    assert start.severity == "WARN" and "clean shutdown" in start.detail
    f, _, _ = run_triage(real("crash_restart-8.0.32.log"), window_min=0,
                         no_sysprobe=True)
    start = next(x for x in f if x.title == "Process start(s) in window")
    assert start.severity == "CRIT" and "unclean" in start.detail


def test_log_that_begins_after_a_crash_is_flagged():
    """mongod's 'Startup from clean shutdown?' flag catches a crash even
    when the shutdown itself is in an older, rotated log."""
    f, _, _ = run_triage(real("rotated_after_crash-8.0.32.log"), window_min=0,
                         no_sysprobe=True)
    crash = next(x for x in f if x.title == "Started after a crash")
    assert crash.severity == "CRIT"


def test_log_that_begins_at_a_clean_first_start_is_info():
    f, _, _ = run_triage(real("mongod-9.0.2.log"), window_min=0, no_sysprobe=True)
    titles = {x.title: x for x in f}
    assert titles["Log begins at a startup"].severity == "INFO"
    assert "Process start(s) in window" not in titles


def _engine_with_start(pid, at):
    e = TriageEngine()
    e.consume(parse_line(json.dumps({
        "t": {"$date": at.isoformat()}, "s": "I", "c": "CONTROL", "id": 4615611,
        "ctx": "initandlisten", "msg": "MongoDB starting",
        "attr": {"pid": pid, "port": 27017, "dbPath": "/data/rs2", "host": "db1"}})))
    return e


def test_pick_mongod_uses_the_log_pid_not_the_first_process():
    running = [(100, ["mongod", "--dbpath", "/data/rs1"]),
               (200, ["mongod", "--dbpath", "/data/rs2"]),
               (300, ["mongod", "--dbpath", "/data/rs3"])]
    assert pick_mongod(running, [200], None)[0] == 200
    assert pick_mongod(running, [], "/data/rs3")[0] == 300
    assert pick_mongod(running, [], None) is None
    assert pick_mongod(running[:1], [], "/data/other") is None


def test_oom_kill_of_another_instance_is_not_blamed_on_this_log(tmp_path):
    start = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)
    log = tmp_path / "rs2.log"
    lines = [{"t": {"$date": start.isoformat()}, "s": "I", "c": "CONTROL",
              "id": 4615611, "ctx": "initandlisten", "msg": "MongoDB starting",
              "attr": {"pid": 2222, "port": 27018, "dbPath": "/data/rs2", "host": "db1"}},
             {"t": {"$date": (start + timedelta(minutes=5)).isoformat()}, "s": "I",
              "c": "NETWORK", "id": 22943, "ctx": "listener",
              "msg": "Connection accepted", "attr": {"remote": "10.0.0.1:5000"}}]
    log.write_text("\n".join(json.dumps(l) for l in lines) + "\n")
    stamp = (start + timedelta(minutes=4)).strftime("%b %e %H:%M:%S")
    for killed, expect in ((3333, "another mongod on this host"),
                           (2222, "was this log's mongod")):
        sys = tmp_path / ("syslog%d" % killed)
        sys.write_text("%s db1 kernel: Out of memory: Killed process %d (mongod)\n"
                       % (stamp, killed))
        f, _, _ = run_triage(str(log), window_min=0, no_sysprobe=True,
                             oslog=[str(sys)])
        oom = next(x for x in f if x.title == "System: oom-kill")
        assert expect in oom.detail, oom.detail


# -------------------------------------------------------------- audit ---

@pytest.mark.parametrize("name,expect", [
    ("getlog-startup-9.0.2.json", {"Access control is not enabled",
                                   "WiredTiger is not on XFS",
                                   "glibc rseq is enabled"}),
    ("getlog-startup-7.0.43.txt", {"Access control is not enabled",
                                   "mongod is running as root"}),
])
def test_audit_reads_real_getlog_output(name, expect):
    rc, out, _ = run(["audit", real(name), "--json"])
    titles = {f["title"] for f in json.loads(out)["findings"]}
    assert expect <= titles
    assert not any(t.startswith("other:") for t in titles)


def test_every_real_startup_warning_id_is_known():
    from mdbkit.audit import KNOWN
    for name in ("getlog-startup-9.0.2.json",):
        for line in json.load(open(real(name)))["log"]:
            assert json.loads(line)["id"] in KNOWN


# --------------------------------------------------------------- ftdc ---

@pytest.mark.parametrize("version", ("7.0.43", "9.0.2"))
def test_real_ftdc_header_matches_reference_document(version):
    files = ftdc.ftdc_files(real("ftdc-interim-%s" % version))
    for doc in ftdc.iter_documents(files[0]):
        if doc.get("type") != 1:
            continue
        raw = zlib.decompress(bytes(doc["data"][4:]))
        ref, pos = ftdc.parse_document(raw, 0)
        metric_count, _ = struct.unpack_from("<II", raw, pos)
        assert metric_count == len(ftdc.numeric_metrics(ref))
        chunk = ftdc.decode_chunk(doc)
        assert chunk is not None and chunk.rows
        return
    pytest.fail("no metrics chunk in fixture")


@pytest.mark.parametrize("version", ("7.0.43", "9.0.2"))
def test_real_ftdc_tickets_and_cache(version):
    rc, out, _ = run(["ftdc", "summary", real("ftdc-interim-%s" % version),
                      "--all", "--json"])
    data = json.loads(out)
    text = json.dumps(data)
    assert "tickets.readTotal" in text and "cache.maxBytes" in text


def test_duplicate_bson_keys_keep_every_column():
    """BSON allows repeated keys (FTDC can list a mount twice). Collapsing
    them shifts every later metric onto the wrong column."""
    from test_ftdc import enc_doc
    body = (b"\x10a\x00" + struct.pack("<i", 1) + b"\x10a\x00" + struct.pack("<i", 2)
            + b"\x10b\x00" + struct.pack("<i", 3))
    raw = struct.pack("<i", len(body) + 5) + body + b"\x00"
    doc, _ = ftdc.parse_document(raw, 0)
    assert list(doc.values()) == [1, 2, 3]


def test_timeline_hides_helper_columns_and_shows_rates():
    rc, out, _ = run(["ftdc", "timeline", real("ftdc-interim-9.0.2"), "--all",
                      "--step", "10"])
    assert rc == 0
    assert "_disk." not in out and "_repl." not in out
    assert "/s" in out


# ------------------------------------------------------- serverstatus ---

def test_long_written_by_json_stringify_is_a_number():
    assert num({"low": 40022, "high": 0, "unsigned": False}) == 40022
    assert num({"low": -1, "high": 0}) == 4294967295
    assert num({"low": 0, "high": 1}) == 1 << 32
    assert num({"low": 5, "high": 0, "other": 1}) == 0


def test_rates_from_dumps_made_by_the_old_export_script():
    """0.6.0's export script wrote counters as {low, high}; --after then
    silently dropped the operation counters."""
    rc, out, _ = run(["serverstatus", real("serverstatus-9.0.2-jsonstringify-1.json"),
                      "--after", real("serverstatus-9.0.2-jsonstringify-2.json")])
    assert rc == 0
    assert "Operation counters: Measured over 31 seconds" in out
    assert "command  145 in 31s" in out


def test_export_scripts_write_relaxed_ejson_for_all_databases():
    assert "EJSON.stringify(db.adminCommand({ serverStatus: 1 }), { relaxed: true })" \
        in scripts.SERVERSTATUS
    for script in (scripts.INDEXES_SCRIPT, scripts.SCHEMA_SCRIPT):
        assert "listDatabases" in script and "JSON.stringify(out)" not in script
        assert 'name + "." + coll' in script


def test_index_file_keyed_by_namespace_is_read(tmp_path):
    from mdbkit.advisor import load_indexes
    p = tmp_path / "ix.json"
    p.write_text(json.dumps({"db": "", "databases": ["shop"], "collections": {
        "shop.orders": [{"v": 2, "key": {"_id": 1}, "name": "_id_"},
                        {"v": 2, "key": {"status": 1, "createdAt": -1}, "name": "s_c"}]}}))
    ix = load_indexes(str(p))
    assert [i["name"] for i in ix["shop.orders"]] == ["_id_", "s_c"]


def test_mongosh_string_concatenation_is_joined():
    doc = loads_lenient("{ s: 'line one\\n' +\n    'line two', n: Long('7') }")
    assert doc == {"s": "line one\nline two", "n": 7}


# ------------------------------------------ fixes found on real output ---

def _line(**kw):
    base = {"t": {"$date": "2026-10-02T10:00:00Z"}, "s": "I", "c": "COMMAND",
            "id": 51803, "ctx": "conn1", "msg": "Slow query", "attr": {}}
    base.update(kw)
    return json.dumps(base)


def test_loginfo_slowest_ignores_internal_long_polls(tmp_path):
    """An awaitable hello or oplog getMore waits ~10 s by design; it used to
    be reported as the slowest query."""
    p = tmp_path / "m.log"
    p.write_text("\n".join([
        _line(attr={"ns": "local.oplog.rs", "durationMillis": 10000,
                    "command": {"getMore": 1, "collection": "oplog.rs"}}),
        _line(attr={"ns": "shop.orders", "durationMillis": 250,
                    "command": {"find": "orders", "filter": {"a": 1}}}),
    ]) + "\n")
    rc, out, _ = run(["loginfo", str(p)])
    assert "1 on your collections, slowest 250ms" in out


def test_routine_eviction_lines_are_not_eviction_pressure():
    e = TriageEngine()
    e.consume(parse_line(_line(c="STORAGE", id=22430, msg="WiredTiger message",
                               attr={"message": "starting eviction threads"})))
    assert e.evictions == 0
    e.consume(parse_line(_line(c="STORAGE", id=22430, s="W", msg="WiredTiger message",
                               attr={"message": "cache stuck for too long"})))
    assert e.evictions == 1


def test_audit_lists_the_exact_kernel_changes():
    rc, out, _ = run(["audit", real("getlog-startup-9.0.2.json")])
    assert "/sys/kernel/mm/transparent_hugepage/enabled: never -> always" in out
