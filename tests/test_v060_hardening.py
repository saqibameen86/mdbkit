"""0.6.0 regression tests: one per bug found in the pre-release bug hunt,
plus the hostile-input fuzzers themselves, so these classes of failure stay
fixed.

Every test here corresponds to a confirmed failure in 0.5.5.
"""

import contextlib
import gzip
import io
import json
import os
import shutil
import socket
import stat
import struct
import subprocess
import sys
import time
import zlib

import pytest

from mdbkit import lab
from mdbkit import oslog as OS
from mdbkit import serverstatus as SS
from mdbkit.cli import main
from mdbkit.demo import DemoLog, write_extras
from mdbkit.parser import ParseStats, iter_entries, num, parse_line
from mdbkit.shelljson import loads_lenient, read_text_file, shell_to_json

from test_ftdc import enc_doc, rle_varint

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
POSIX = os.name == "posix"


def run(argv):
    """Run the CLI, returning (rc, stdout, stderr). Never lets an exception
    through silently — an exception is a test failure."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = main(argv)
        except SystemExit as exc:
            rc = exc.code
    return rc, out.getvalue(), err.getvalue()


@pytest.fixture
def demo_lines():
    return DemoLog(scenario="incident", minutes=30).build()


def write(tmp_path, name, lines=None, raw=None):
    p = tmp_path / name
    if raw is not None:
        p.write_bytes(raw)
    else:
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)


# ======================================================= the log fuzzer ===

def _mutate(lines, fn):
    out = []
    for ln in lines:
        d = json.loads(ln)
        fn(d)
        out.append(json.dumps(d))
    return out


def _set_attr(key, value):
    def fn(d):
        a = d.get("attr")
        if isinstance(a, dict) and key in a:
            a[key] = value
    return fn


HOSTILE = {
    "id_string": lambda d: d.__setitem__("id", "abc"),
    "id_dict": lambda d: d.__setitem__("id", {"x": 1}),
    "attr_list": lambda d: d.__setitem__("attr", [1, 2]),
    "attr_string": lambda d: d.__setitem__("attr", "truncated"),
    "ts_naive": lambda d: d.__setitem__("t", {"$date": "2026-07-01T08:00:00"}),
    "ts_garbage": lambda d: d.__setitem__("t", {"$date": "yesterday"}),
    "ts_missing": lambda d: d.pop("t", None),
    "dur_string": _set_attr("durationMillis", "12ms"),
    "dur_dict": _set_attr("durationMillis", {"$numberLong": "99"}),
    "dur_null": _set_attr("durationMillis", None),
    "docs_string": _set_attr("docsExamined", "lots"),
    "nret_string": _set_attr("nreturned", "x"),
    "plan_list": _set_attr("planSummary", ["IXSCAN"]),
    "plan_dict": _set_attr("planSummary", {"a": 1}),
    "ns_int": _set_attr("ns", 5),
    "remote_int": _set_attr("remote", 12),
    "conncount_str": _set_attr("connectionCount", "many"),
    "working_str": _set_attr("workingMillis", "fast"),
    "queues_list": _set_attr("queues", [1, 2]),
}

LOG_COMMANDS = [
    ["loginfo"], ["queries"], ["queries", "--json"], ["queries", "--shape", "1"],
    ["connections"], ["advise"], ["audit"],
    ["triage", "--window", "0", "--no-sysprobe"], ["triage", "--no-sysprobe"],
    ["filter", "--slow", "100", "--as-explain"], ["filter", "--ns", "shop.orders"],
]


@pytest.mark.parametrize("case", sorted(HOSTILE))
def test_no_command_crashes_on_hostile_lines(tmp_path, demo_lines, case):
    """0.5.5 had 31 distinct crash sites across these inputs."""
    path = write(tmp_path, "h.log", _mutate(demo_lines, HOSTILE[case]))
    for cmd in LOG_COMMANDS:
        rc, _out, _err = run([cmd[0], path] + cmd[1:])
        assert rc in (0, 1, 2), (case, cmd, rc)


def test_one_bad_line_does_not_spoil_the_rest(tmp_path, demo_lines):
    bad = json.loads(demo_lines[5])
    bad["id"] = "not-a-number"
    lines = demo_lines[:5] + [json.dumps(bad)] + demo_lines[5:]
    path = write(tmp_path, "m.log", lines)
    rc, out, _ = run(["queries", path, "--json"])
    assert rc == 0
    assert len(json.loads(out)) >= 4


def test_binary_and_garbage_inputs(tmp_path):
    for raw in (os.urandom(100_000), b"\x00" * 5000, b"[1,2]\n42\nnull\n{}\n"):
        path = write(tmp_path, "g.log", raw=raw)
        for cmd in LOG_COMMANDS:
            rc, _o, _e = run([cmd[0], path] + cmd[1:])
            assert rc in (0, 1, 2)


# ====================================================== parse boundary ===

def test_utf8_bom_keeps_the_first_line(tmp_path, demo_lines):
    """A BOM used to cost line 1 — the startup line with version and host."""
    path = write(tmp_path, "b.log",
                 raw=("﻿" + "\n".join(demo_lines) + "\n").encode())
    stats = ParseStats()
    entries = list(iter_entries(path, stats))
    assert stats.unparsed == 0
    assert entries[0].msg == "MongoDB starting"


def test_crlf_line_endings(tmp_path, demo_lines):
    path = write(tmp_path, "c.log", raw=("\r\n".join(demo_lines)).encode())
    stats = ParseStats()
    list(iter_entries(path, stats))
    assert stats.unparsed == 0


def test_timestamps_are_always_timezone_aware():
    for raw in ('{"t":{"$date":"2026-07-01T08:00:00"},"s":"I","c":"X","id":1,"msg":"m"}',
                '{"t":{"$date":"2026-07-01T08:00:00Z"},"s":"I","c":"X","id":1,"msg":"m"}',
                '{"t":{"$date":{"$numberLong":"1782878400000"}},"s":"I","c":"X","id":1,"msg":"m"}'):
        assert parse_line(raw).ts.tzinfo is not None


def test_num_coercion():
    assert num(5) == 5 and num("12") == 12 and num("1.5") == 1.5
    assert num({"$numberLong": "7"}) == 7
    assert num({"$numberDouble": "NaN"}, -1) == -1
    assert num(float("inf"), 0) == 0
    assert num("12ms", 0) == 0 and num(None, 3) == 3 and num([1], 0) == 0


def test_truncated_gzip_keeps_everything_before_the_damage(tmp_path, demo_lines):
    full = gzip.compress("\n".join(demo_lines).encode())
    path = write(tmp_path, "rot.log.gz", raw=full[:-300])
    rc, out, err = run(["loginfo", path])
    assert rc == 0
    assert "truncated or corrupt" in err
    stats = ParseStats()
    got = list(iter_entries(path, stats))
    assert len(got) > len(demo_lines) * 0.8
    assert stats.damaged == [path]


def test_gz_name_without_gzip_content(tmp_path, demo_lines):
    path = write(tmp_path, "notreally.log.gz", demo_lines)
    rc, out, _ = run(["loginfo", path])
    assert rc == 0
    assert "parsed: %d" % len(demo_lines) in out.replace(",", "")


def test_gzip_content_without_gz_name(tmp_path, demo_lines):
    path = write(tmp_path, "mongod.log.1",
                 raw=gzip.compress("\n".join(demo_lines).encode()))
    rc, out, _ = run(["loginfo", path])
    assert rc == 0 and "unparsed: 0" in out


def test_directory_argument_is_a_clean_error(tmp_path):
    rc, out, err = run(["loginfo", str(tmp_path)])
    assert rc == 2 and "directory" in err
    assert "Traceback" not in err


# ================================================= terminal injection ===

def test_escape_sequences_from_client_metadata_are_neutralised(tmp_path, demo_lines):
    """A client sets its own appName. It must never reach the terminal as a
    live escape sequence."""
    evil = "x\x1b]0;PWNED\x07\x1b[2J\x9b31m"

    def poison(d):
        a = d.get("attr")
        if isinstance(a, dict):
            if "appName" in a:
                a["appName"] = evil
            doc = a.get("doc")
            if isinstance(doc, dict) and "application" in doc:
                doc["application"]["name"] = evil
    path = write(tmp_path, "e.log", _mutate(demo_lines, poison))
    for argv in (["connections", path], ["queries", path, "--shape", "1"],
                 ["queries", path, "--json"], ["triage", path, "--window", "0",
                                               "--no-sysprobe"]):
        rc, out, err = run(argv)
        for stream in (out, err):
            assert "\x1b" not in stream and "\x07" not in stream
            assert "\x9b" not in stream
    rc, out, _ = run(["queries", path, "--json"])
    json.loads(out)                          # still valid JSON


# ============================================================ arguments ===

@pytest.mark.parametrize("argv", [
    ["ftdc", "timeline", "x", "--step", "0"],
    ["ftdc", "timeline", "x", "--step", "-60"],
    ["demo", "--minutes", "0"],
    ["demo", "--minutes", "2000000"],
    ["queries", "x", "--limit", "-1"],
    ["queries", "x", "--shape", "0"],
    ["triage", "x", "--window", "-5"],
    ["lab", "start", "--nodes", "50"],
    ["lab", "start", "--port", "80"],
])
def test_invalid_numbers_are_rejected_up_front(argv):
    with pytest.raises(SystemExit) as exc:
        with contextlib.redirect_stderr(io.StringIO()):
            main(argv)
    assert exc.value.code == 2


# ====================================================== demo honesty ===

def test_demo_is_byte_identical_across_processes(tmp_path):
    """The README promises the same seed gives a byte-identical log. Before
    0.6.0 a hash() call made that false across processes."""
    outs = []
    for seed in ("1", "2"):
        env = dict(os.environ, PYTHONHASHSEED=seed)
        res = subprocess.run([sys.executable, "-m", "mdbkit.cli", "demo",
                              "--minutes", "20"], capture_output=True,
                             env=env, check=True)
        outs.append(res.stdout)
    assert outs[0] == outs[1]


# ================================================================ FTDC ===

def _ftdc_dir(tmp_path, server_status, name="diagnostic.data"):
    from mdbkit.ftdc import numeric_metrics, parse_document
    ref = enc_doc([("start", 0), ("serverStatus", server_status)])
    parsed, _ = parse_document(ref)
    leaves = numeric_metrics(parsed)
    payload = ref + struct.pack("<II", len(leaves), 3) + rle_varint([0] * len(leaves) * 3)
    blob = struct.pack("<I", len(payload)) + zlib.compress(payload)
    d = tmp_path / name
    d.mkdir()
    (d / "metrics.2026-08-29T00-00-00Z-00000").write_bytes(
        enc_doc([("_id", 1788000000000), ("type", 1), ("data", blob)]))
    return str(d)


def test_ftdc_reads_tickets_from_the_8_0_location(tmp_path):
    """8.0 moved tickets from wiredTiger.concurrentTransactions to
    queues.execution; 0.5.5 silently showed nothing on 8.0+."""
    d = _ftdc_dir(tmp_path, {"queues": {"execution": {
        "read": {"available": 3, "out": 125, "totalTickets": 128},
        "write": {"available": 120, "out": 8, "totalTickets": 128}}}})
    rc, out, _ = run(["ftdc", "summary", d, "--all", "--json"])
    series = json.loads(out)["series"]
    assert series["tickets.readAvail"]["min"] == 3
    assert series["tickets.writeTotal"]["max"] == 128


def test_ftdc_still_reads_pre_8_0_tickets(tmp_path):
    d = _ftdc_dir(tmp_path, {"wiredTiger": {"concurrentTransactions": {
        "read": {"available": 100, "out": 28, "totalTickets": 128}}}})
    rc, out, _ = run(["ftdc", "summary", d, "--all", "--json"])
    assert json.loads(out)["series"]["tickets.readAvail"]["min"] == 100


def test_ftdc_corrupt_chunk_does_not_crash(tmp_path):
    d = _ftdc_dir(tmp_path, {"connections": {"current": 5}})
    f = os.path.join(d, os.listdir(d)[0])
    data = bytearray(open(f, "rb").read())
    for pos in range(20, len(data), 7):         # shred the body
        data[pos] ^= 0x5A
    open(f, "wb").write(bytes(data))
    for action in ("summary", "timeline", "export"):
        rc, _o, _e = run(["ftdc", action, d, "--all"])
        assert rc in (0, 2)


def test_ftdc_decompression_is_bounded(tmp_path):
    from mdbkit import ftdc
    payload = b"\0" * (ftdc.MAX_CHUNK_BYTES + 1024)
    blob = struct.pack("<I", 100) + zlib.compress(payload)   # lies about size
    with pytest.raises(ftdc.BsonError):
        ftdc.decode_chunk({"_id": 1, "type": 1, "data": blob})


def test_ftdc_rejects_metric_count_larger_than_reference_doc():
    """A corrupt header claiming millions of metrics used to cost seconds."""
    import time
    from mdbkit import ftdc
    payload = enc_doc([("start", 0), ("x", 1)]) + struct.pack("<II", 5_000_000, 299) + b"\0\0"
    blob = struct.pack("<I", len(payload)) + zlib.compress(payload)
    t0 = time.time()
    with pytest.raises(ftdc.BsonError):
        ftdc.decode_chunk({"_id": 1, "type": 1, "data": blob})
    assert time.time() - t0 < 1


# ======================================================== serverstatus ===

MONGOSH_PRINT = """{
  host: 'db1:27017',
  version: '8.0.34',
  uptime: 86400,
  localTime: ISODate('2026-10-01T00:00:00.000Z'),
  connections: { current: 4180, available: 1020, totalCreated: Long('2841993') },
  opcounters: { insert: Long('91827364'), query: Long('1837462910') },
  queues: { execution: {
    read: { out: 127, available: 1, totalTickets: 128 },
    write: { out: 2, available: 126, totalTickets: 128 },
  } },
  globalLock: { currentQueue: { readers: 47, writers: 9 } },
  operationTime: Timestamp({ t: 1782878400, i: 3 }),
  ok: 1
}"""


def test_serverstatus_accepts_mongosh_printed_output(tmp_path):
    """What people paste is mongosh's printout, not strict JSON."""
    p = tmp_path / "s.txt"
    p.write_text(MONGOSH_PRINT)
    rc, out, err = run(["serverstatus", str(p)])
    assert rc == 0, err
    assert "Concurrency tickets" in out and "1 of 128" in out


def test_serverstatus_accepts_powershell_utf16(tmp_path):
    p = tmp_path / "s.json"
    p.write_bytes(json.dumps({
        "host": "h", "uptime": 10, "connections": {"current": 1, "available": 9}}
    ).encode("utf-16"))
    rc, out, err = run(["serverstatus", str(p)])
    assert rc == 0, err


def test_serverstatus_survives_nan_and_odd_types(tmp_path):
    doc = {"host": "h", "uptime": 100,
           "globalLock": {"currentQueue": {"readers": {"$numberDouble": "NaN"}}},
           "queues": {"execution": {"read": {"available": {"$numberDouble": "Infinity"},
                                             "totalTickets": 128}}},
           "asserts": 5, "repl": "standalone", "connections": [1, 2]}
    p = tmp_path / "s.json"
    p.write_text(json.dumps(doc))
    rc, out, err = run(["serverstatus", str(p)])
    assert rc == 0, err


def test_shell_json_converter_edge_cases():
    doc = loads_lenient(
        "{ a: [1, 2,], b: 'it\\'s', c: \"brace { in [ string\", "
        "d: NumberLong(5), e: new Date('2026-01-01'), f: /ab+c/i, "
        "g: NaN, h: -Infinity, i: Timestamp(17, 2), j: UUID('x') }")
    assert doc == {"a": [1, 2], "b": "it's", "c": "brace { in [ string",
                   "d": 5, "e": "2026-01-01", "f": "ab+c", "g": None,
                   "h": None, "i": {"t": 17, "i": 2}, "j": "x"}


def test_read_text_file_encodings(tmp_path):
    for enc, prefix in (("utf-8", b""), ("utf-8", b"\xef\xbb\xbf"),
                        ("utf-16", b""), ("utf-16-le", b"")):
        p = tmp_path / "f.txt"
        p.write_bytes(prefix + '{"x": "é"}'.encode(enc))
        assert json.loads(read_text_file(str(p))) == {"x": "é"}


# =============================================================== oslog ===

def test_oslog_long_line_is_not_quadratic(tmp_path):
    """One pathological 140 KB line took 16 s in 0.5.5."""
    p = tmp_path / "syslog"
    p.write_text("Jul 30 09:14:03 h kernel: oom-kill: x" + " task=a" * 20000 + "\n")
    t = time.time()
    OS.scan(str(p))
    assert time.time() - t < 2


def test_oslog_missing_file_is_an_error_not_an_all_clear(tmp_path):
    rc, out, err = run(["oslog", str(tmp_path / "nope")])
    assert rc == 2 and "No such file" in err


def test_oslog_december_line_read_in_january_is_last_year():
    from datetime import datetime
    from datetime import timedelta
    ts = OS.parse_ts("Dec 31 23:59:59 h kernel: x")
    # A yearless syslog stamp is never placed more than a day in the future.
    assert ts <= datetime.now() + timedelta(days=1)


# ============================================================== triage ===

def test_planned_restart_is_not_reported_as_an_outage(tmp_path):
    lines = [
        {"t": {"$date": "2026-07-01T08:00:00.000+00:00"}, "s": "I", "c": "CONTROL",
         "id": 23138, "ctx": "signal", "msg": "Shutting down", "attr": {}},
        {"t": {"$date": "2026-07-01T08:01:00.000+00:00"}, "s": "I", "c": "CONTROL",
         "id": 4615611, "ctx": "initandlisten", "msg": "MongoDB starting",
         "attr": {"host": "h", "port": 27017}},
        {"t": {"$date": "2026-07-01T08:01:02.000+00:00"}, "s": "I", "c": "NETWORK",
         "id": 23016, "ctx": "listener", "msg": "Waiting for connections",
         "attr": {"port": 27017}},
    ]
    path = write(tmp_path, "r.log", [json.dumps(x) for x in lines])
    from mdbkit.triage import run_triage
    findings, _s, _c = run_triage(path, window_min=0, no_sysprobe=True)
    health = next(f for f in findings if f.title == "Cluster health")
    assert health.severity == "WARN"
    assert "serving again" in health.detail


def test_shutdown_without_restart_is_still_critical(tmp_path):
    line = {"t": {"$date": "2026-07-01T08:00:00.000+00:00"}, "s": "I",
            "c": "CONTROL", "id": 23138, "ctx": "signal", "msg": "Shutting down"}
    path = write(tmp_path, "d.log", [json.dumps(line)])
    from mdbkit.triage import run_triage
    findings, _s, _c = run_triage(path, window_min=0, no_sysprobe=True)
    assert next(f for f in findings if f.title == "Cluster health").severity == "CRIT"


# ======================================================= MongoDB 8.x ===

def _slow(attr, ts="2026-07-01T08:00:00.000+00:00", mid=51803, c="COMMAND",
          msg="Slow query"):
    base = {"type": "command", "ns": "shop.orders",
            "command": {"find": "orders", "filter": {"status": "x"}, "$db": "shop"},
            "planSummary": "COLLSCAN", "docsExamined": 1000, "nreturned": 1}
    base.update(attr)
    return json.dumps({"t": {"$date": ts}, "s": "I", "c": c, "id": mid,
                       "ctx": "conn1", "msg": msg, "attr": base})


def test_8_0_working_vs_waiting_time(tmp_path):
    lines = [_slow({"durationMillis": 1000, "workingMillis": 200,
                    "queues": {"execution": {"totalTimeQueuedMicros": 700000}}})
             for _ in range(5)]
    path = write(tmp_path, "w.log", lines)
    rc, out, _ = run(["queries", path, "--json"])
    shape = json.loads(out)[0]
    assert shape["workingMs"] == 1000 and shape["waitingMs"] == 4000
    assert shape["waitingPct"] == 80.0 and shape["queuedMicros"] == 3_500_000
    from mdbkit.triage import run_triage
    findings, _s, _c = run_triage(path, window_min=0, no_sysprobe=True)
    f = next(f for f in findings if f.title.startswith("Time spent waiting"))
    assert f.severity == "WARN" and "80%" in f.detail


def test_pre_8_0_logs_have_no_timing_claims(tmp_path):
    path = write(tmp_path, "o.log", [_slow({"durationMillis": 500})] * 3)
    rc, out, _ = run(["queries", path, "--json"])
    assert json.loads(out)[0]["waitingMs"] is None
    rc, out, _ = run(["queries", path])
    assert "8.0+ timing" not in out


def test_query_shape_hash_and_plan_cache_hash(tmp_path):
    path = write(tmp_path, "q.log", [_slow({
        "durationMillis": 300, "queryShapeHash": "AB" * 32,
        "planCacheShapeHash": "1234ABCD", "queryHash": "1234ABCD",
        "queryFramework": "sbe"})] * 2)
    rc, out, _ = run(["queries", path, "--shape", "1"])
    assert "AB" * 32 in out and "setQuerySettings" in out
    assert "1234ABCD" in out and "sbe 2x" in out
    # pre-8.0 logs only have queryHash
    path = write(tmp_path, "q7.log", [_slow({"durationMillis": 300,
                                              "queryHash": "DEADBEEF"})])
    rc, out, _ = run(["queries", path, "--json"])
    assert json.loads(out)[0]["planCacheShapeHashes"] == {"DEADBEEF": 1}


def test_spills_8_1_and_legacy_used_disk(tmp_path):
    path = write(tmp_path, "s.log", [
        _slow({"durationMillis": 900, "sortSpills": 3, "sortSpilledBytes": 4096}),
        _slow({"durationMillis": 900, "usedDisk": True}),
    ])
    rc, out, _ = run(["queries", path, "--json"])
    shape = json.loads(out)[0]
    assert shape["spills"] == 4 and shape["spilledBytes"] == 4096
    rc, out, _ = run(["queries", path])
    assert "+SPILL" in out


def test_8_3_peak_memory(tmp_path):
    path = write(tmp_path, "m.log", [
        _slow({"durationMillis": 900, "peakTrackedMemBytes": 50 << 20}),
        _slow({"durationMillis": 900, "peakTrackedMemBytes": 10 << 20})])
    rc, out, _ = run(["queries", path, "--shape", "1"])
    assert "50.0 MiB" in out


def test_8_3_in_progress_entries_are_not_counted_as_slow_queries(tmp_path):
    lines = [_slow({"durationMillis": 400})] + [
        _slow({"durationMillis": 60000 + i}, mid=1794200, c="SLOWPROG",
              msg="Slow in-progress query") for i in range(6)]
    path = write(tmp_path, "p.log", lines)
    rc, out, _ = run(["queries", path, "--json"])
    assert json.loads(out)[0]["count"] == 1
    rc, out, _ = run(["loginfo", path])
    assert "slow queries logged: 1" in out
    assert "still-running operations logged (8.3+): 6" in out
    from mdbkit.triage import run_triage
    findings, _s, _c = run_triage(path, window_min=0, no_sysprobe=True)
    f = next(f for f in findings if f.title == "Long-running operations")
    assert f.severity == "WARN" and "6 operation" in f.detail


# =============================================================== audit ===

def _warn(mid, msg, attr=None, tags=("startupWarnings",), sev="W",
          ctx="initandlisten"):
    d = {"t": {"$date": "2026-07-01T08:00:00.100+00:00"}, "s": sev,
         "c": "CONTROL", "id": mid, "ctx": ctx, "msg": msg}
    if tags:
        d["tags"] = list(tags)
    if attr:
        d["attr"] = attr
    return json.dumps(d)


def test_audit_known_ids_and_severity(tmp_path, demo_lines):
    rc, out, _ = run(["audit", write(tmp_path, "a.log", demo_lines), "--json"])
    keys = {f["key"]: f["severity"] for f in json.loads(out)["findings"]}
    assert keys["access-control"] == "CRIT"
    assert keys["rlimit-nofile"] == "WARN"
    assert keys["thp-8x"] == "WARN" and keys["swappiness"] == "WARN"
    rc, _o, _e = run(["audit", write(tmp_path, "a.log", demo_lines), "--exit-code"])
    assert rc == 2


def test_audit_ignores_runtime_warnings(tmp_path):
    lines = [_warn(22120, "Access control is not enabled"),
             _warn(22430, "WiredTiger message", {"message": "checkpoint took 71s"},
                   tags=(), ctx="Checkpointer")]
    rc, out, _ = run(["audit", write(tmp_path, "r.log", lines), "--json"])
    assert [f["key"] for f in json.loads(out)["findings"]] == ["access-control"]


def test_audit_reports_unknown_tagged_startup_warnings(tmp_path):
    rc, out, _ = run(["audit", write(tmp_path, "u.log",
                                     [_warn(999999, "Something new in 9.1")]), "--json"])
    f = json.loads(out)["findings"][0]
    assert f["key"] == "other:999999" and "9.1" in f["message"]


def test_audit_reads_getlog_output_in_every_format(tmp_path):
    lines = [_warn(22120, "Access control is not enabled"),
             _warn(22184, "Soft rlimits for open file descriptors too low",
                   {"currentValue": 1024})]
    doc = {"totalLinesWritten": 2, "log": lines, "ok": 1}
    shell = "{\n  totalLinesWritten: 2,\n  log: [\n%s\n  ],\n  ok: 1\n}" % (
        ",\n".join("    %r" % ln for ln in lines))
    for name, raw in (("ejson.json", json.dumps(doc).encode()),
                      ("shell.txt", shell.encode()),
                      ("ps.json", json.dumps(doc).encode("utf-16"))):
        path = write(tmp_path, name, raw=raw)
        rc, out, err = run(["audit", path, "--json"])
        res = json.loads(out)
        assert res["source"] == "getLog startupWarnings", name
        assert len(res["findings"]) == 2, name


def test_audit_without_startup_explains_getlog(tmp_path):
    line = _slow({"durationMillis": 200})
    rc, out, _ = run(["audit", write(tmp_path, "n.log", [line])])
    assert "getLog" in out and "startupWarnings" in out


def test_triage_includes_startup_configuration(tmp_path, demo_lines):
    from mdbkit.triage import run_triage
    findings, _s, _c = run_triage(write(tmp_path, "t.log", demo_lines),
                                  window_min=0, no_sysprobe=True)
    f = next(f for f in findings if f.title == "Startup configuration")
    assert f.severity == "CRIT" and "mdbkit audit" in f.next_step


# ================================================================= lab ===

@pytest.fixture
def fake_mongod(tmp_path, monkeypatch):
    if not POSIX:
        pytest.skip("lab lifecycle tests need fork")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    target = bindir / "mongod"
    shutil.copy(os.path.join(FIXTURES, "fake_mongod.py"), target)
    target.chmod(target.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr(lab, "find_binary",
                        lambda name: str(target) if name == "mongod" else None)
    started = []
    yield started
    # never leave daemons behind, even if a test failed
    for d in started:
        try:
            lab.stop(d, echo=lambda *a: None)
        except lab.LabError:
            pass


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port if port < 64990 else 29000


def test_lab_failed_start_is_recoverable(tmp_path, fake_mongod, monkeypatch):
    """0.5.5 left a directory it refused to delete, with mongod still
    running inside it."""
    d = str(tmp_path / "lab")
    fake_mongod.append(d)
    base = _free_port()
    monkeypatch.setenv("FAKE_FAIL_PORT", str(base + 1))
    with pytest.raises(lab.LabError) as exc:
        lab.start(directory=d, base_port=base, echo=lambda *a: None)
    assert "destroy" in str(exc.value)
    assert lab.load_state(d)["status"] == "failed"
    assert not lab.still_running(d)          # node0 was stopped again
    lab.destroy(d, echo=lambda *a: None)
    assert not os.path.exists(d)


def test_lab_busy_port_is_reported_before_anything_starts(tmp_path, fake_mongod):
    base = _free_port()
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", base))
    blocker.listen(1)
    try:
        d = str(tmp_path / "lab")
        with pytest.raises(lab.LabError) as exc:
            lab.start(directory=d, base_port=base, standalone=True,
                      echo=lambda *a: None)
        assert "already in use" in str(exc.value)
        assert not os.path.exists(d)
    finally:
        blocker.close()


def test_lab_full_lifecycle(tmp_path, fake_mongod):
    d = str(tmp_path / "lab")
    fake_mongod.append(d)
    base = _free_port()
    state = lab.start(directory=d, base_port=base, standalone=True,
                      echo=lambda *a: None)
    assert state["status"] == "running"
    assert lab.status(d)["nodes"][0]["running"]
    assert lab.stop(d, echo=lambda *a: None) == 1
    assert not lab.status(d)["nodes"][0]["running"]
    lab.start(directory=d, base_port=base, standalone=True, echo=lambda *a: None)
    lab.destroy(d, echo=lambda *a: None)
    assert not os.path.exists(d)


def test_lab_never_signals_a_process_it_does_not_own(tmp_path, fake_mongod):
    """A stale pid file can name an unrelated process after a reboot."""
    d = str(tmp_path / "lab")
    fake_mongod.append(d)
    lab.start(directory=d, base_port=_free_port(), standalone=True,
              echo=lambda *a: None)
    lab.stop(d, echo=lambda *a: None)
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        with open(os.path.join(d, "node0", "mongod.pid"), "w") as fh:
            fh.write(str(victim.pid))
        assert not lab.status(d)["nodes"][0]["running"]
        lab.destroy(d, echo=lambda *a: None)
        assert victim.poll() is None, "an unrelated process was killed"
    finally:
        victim.kill()
        victim.wait()
