"""Tests for the 0.5 additions: OS log correlation, serverStatus digest,
exit codes, and the dbPath-discovery fixes."""

import json

import pytest

from mdbkit import oslog as OS
from mdbkit import serverstatus as SS
from mdbkit.cli import main
from mdbkit.demo import DemoLog


SYSLOG = """\
Jul 30 09:14:02 dbprod01 kernel: mongod invoked oom-killer: gfp_mask=0x100cca, order=0
Jul 30 09:14:03 dbprod01 kernel: Out of memory: Killed process 24815 (mongod) total-vm:62914560kB
Jul 30 09:14:05 dbprod01 systemd[1]: mongod.service: Main process exited, code=killed, status=9/KILL
Jul 30 10:02:11 dbprod01 kernel: EXT4-fs error (device sdb1): ext4_find_entry:1455: inode #2
Jul 30 11:31:44 dbprod01 mongod[9931]: too many open files
Jul 30 12:00:00 dbprod01 kernel: nf_conntrack: table full, dropping packet
Jul 30 12:05:00 dbprod01 CRON[123]: (root) CMD (some harmless cron job)
"""


def _syslog(tmp_path):
    p = tmp_path / "syslog"
    p.write_text(SYSLOG)
    return str(p)


# ----------------------------------------------------------------- oslog ---

def test_oslog_detects_the_oom_kill(tmp_path):
    events = OS.scan(_syslog(tmp_path))
    kinds = {e.kind for e in events}
    assert "oom-kill" in kinds
    assert OS.mongod_was_oom_killed(events)
    oom = next(e for e in events if e.kind == "oom-kill")
    assert oom.severity == "CRIT"
    assert oom.detail.get("process") == "mongod"
    assert oom.detail.get("pid") == "24815"


def test_oslog_detects_limits_and_disk_errors(tmp_path):
    kinds = {e.kind for e in OS.scan(_syslog(tmp_path))}
    for expected in ("open-files", "disk-error", "service-exit", "conntrack",
                     "oom-pressure"):
        assert expected in kinds, expected


def test_oslog_ignores_ordinary_lines(tmp_path):
    events = OS.scan(_syslog(tmp_path))
    assert not any("harmless cron job" in e.line for e in events)


def test_oslog_timestamps_parse(tmp_path):
    events = OS.scan(_syslog(tmp_path))
    stamped = [e for e in events if e.ts]
    assert stamped
    assert all(e.ts.month == 7 and e.ts.day == 30 for e in stamped)


def test_oslog_parses_iso_format():
    ts = OS.parse_ts("2026-07-30T09:14:03+0400 host kernel: Out of memory: "
                     "Killed process 1 (mongod)")
    assert ts is not None and ts.year == 2026 and ts.hour == 9


def test_oslog_summary_ranks_critical_first(tmp_path):
    groups = OS.summarize(OS.scan(_syslog(tmp_path)))
    assert groups[0]["severity"] == "CRIT"
    assert all(g["explanation"] for g in groups)


def test_oslog_time_window_filters(tmp_path):
    from datetime import datetime
    events = OS.scan(_syslog(tmp_path),
                     ts_from=datetime(2026, 7, 30, 11, 0, 0))
    kinds = {e.kind for e in events}
    assert "oom-kill" not in kinds        # 09:14 is before the window
    assert "open-files" in kinds          # 11:31 is inside it


def test_oslog_missing_file_is_soft(tmp_path):
    assert OS.scan(str(tmp_path / "nope.log")) == []


def test_cli_oslog_and_exit_code(tmp_path, capsys):
    path = _syslog(tmp_path)
    assert main(["oslog", path]) == 0
    out = capsys.readouterr().out
    assert "oom-kill" in out
    assert "The kernel" in out
    assert main(["oslog", path, "--exit-code"]) == 2
    capsys.readouterr()                      # discard, buffer accumulates
    assert main(["oslog", path, "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert any(f["kind"] == "oom-kill" for f in data["findings"])


def test_triage_correlates_oslog(tmp_path):
    from mdbkit.triage import run_triage
    log = tmp_path / "m.log"
    log.write_text("\n".join(DemoLog(minutes=20).build()) + "\n")
    findings, _s, _c = run_triage(str(log), window_min=0, no_sysprobe=True,
                                  oslog=[_syslog(tmp_path)])
    titles = {f.title for f in findings}
    assert "System: oom-kill" in titles
    oom = next(f for f in findings if f.title == "System: oom-kill")
    assert oom.severity == "CRIT"
    assert "restart" in oom.next_step.lower()


# ---------------------------------------------------------- serverstatus ---

def _status(**over):
    doc = {
        "host": "db1:27017", "version": "7.0.14", "process": "mongod",
        "uptime": 86400,
        "connections": {"current": 100, "available": 4900,
                        "totalCreated": 50000},
        "opcounters": {"insert": 1000, "query": 200000, "update": 500,
                       "delete": 10, "getmore": 900, "command": 300000},
        "globalLock": {"activeClients": {"readers": 4, "writers": 1},
                       "currentQueue": {"readers": 0, "writers": 0}},
        "wiredTiger": {
            "concurrentTransactions": {
                "read": {"out": 4, "available": 124, "totalTickets": 128},
                "write": {"out": 2, "available": 126, "totalTickets": 128}},
            "cache": {"bytes currently in the cache": 1 << 30,
                      "maximum bytes configured": 8 << 30,
                      "tracked dirty bytes in the cache": 1 << 26}},
        "mem": {"resident": 4096, "virtual": 8192},
        "asserts": {"regular": 0, "warning": 0, "msg": 0, "user": 0},
    }
    doc.update(over)
    return doc


def sev_for(checks, title):
    return next(c.severity for c in checks if c.title == title)


def test_serverstatus_healthy_server():
    checks = SS.analyze(_status())
    assert sev_for(checks, "Concurrency tickets") == "OK"
    assert sev_for(checks, "WiredTiger cache") == "OK"
    assert sev_for(checks, "Connections") == "OK"
    assert sev_for(checks, "Operations queued") == "OK"


def test_serverstatus_flags_ticket_exhaustion():
    doc = _status()
    doc["wiredTiger"]["concurrentTransactions"]["read"] = {
        "out": 127, "available": 1, "totalTickets": 128}
    checks = SS.analyze(doc)
    assert sev_for(checks, "Concurrency tickets") == "CRIT"
    tick = next(c for c in checks if c.title == "Concurrency tickets")
    assert any("read" in e for e in tick.evidence)


def test_serverstatus_supports_modern_queues_layout():
    """7.0+ moved tickets from wiredTiger.concurrentTransactions to
    queues.execution; the digest must read either."""
    doc = _status()
    del doc["wiredTiger"]["concurrentTransactions"]
    doc["queues"] = {"execution": {
        "read": {"out": 120, "available": 8, "totalTickets": 128},
        "write": {"out": 10, "available": 118, "totalTickets": 128}}}
    checks = SS.analyze(doc)
    assert sev_for(checks, "Concurrency tickets") in ("WARN", "CRIT")


def test_serverstatus_flags_cache_pressure():
    doc = _status()
    doc["wiredTiger"]["cache"]["bytes currently in the cache"] = int(7.9 * (1 << 30))
    checks = SS.analyze(doc)
    assert sev_for(checks, "WiredTiger cache") == "CRIT"


def test_serverstatus_flags_dirty_cache():
    doc = _status()
    doc["wiredTiger"]["cache"]["tracked dirty bytes in the cache"] = int(2 * (1 << 30))
    assert sev_for(SS.analyze(doc), "WiredTiger cache") in ("WARN", "CRIT")


def test_serverstatus_flags_connection_pressure():
    doc = _status(connections={"current": 4500, "available": 500,
                               "totalCreated": 10})
    assert sev_for(SS.analyze(doc), "Connections") == "WARN"


def test_serverstatus_flags_queued_operations():
    doc = _status()
    doc["globalLock"]["currentQueue"] = {"readers": 40, "writers": 5}
    assert sev_for(SS.analyze(doc), "Operations queued") == "WARN"


def test_serverstatus_flags_flow_control():
    doc = _status(flowControl={"isLagged": True, "isLaggedCount": 3})
    assert sev_for(SS.analyze(doc), "Flow control") == "WARN"


def test_serverstatus_two_snapshots_give_true_rates():
    before = _status()
    after = _status(uptime=86460)
    after["opcounters"]["query"] = before["opcounters"]["query"] + 60000
    checks = SS.analyze(before, after)
    ops = next(c for c in checks if c.title == "Operation counters")
    assert "60 seconds" in ops.detail
    assert any("1000.0/sec" in e for e in ops.evidence)


def test_serverstatus_single_snapshot_says_counters_are_cumulative():
    checks = SS.analyze(_status())
    ops = next(c for c in checks if c.title == "Operation counters")
    assert "cumulative" in ops.detail
    server = next(c for c in checks if c.title == "Server")
    assert "--after" in server.next_step


def test_serverstatus_loads_extended_json(tmp_path):
    p = tmp_path / "s.json"
    p.write_text('{"host":"a","uptime":{"$numberLong":"3600"},'
                 '"connections":{"current":1,"available":9}}')
    doc = SS.load(str(p))
    assert SS._num(doc, "uptime") == 3600


def test_serverstatus_unwraps_nested_output(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"serverStatus": _status()}))
    assert SS.load(str(p))["host"] == "db1:27017"


def test_cli_serverstatus(tmp_path, capsys):
    doc = _status()
    doc["wiredTiger"]["concurrentTransactions"]["read"] = {
        "out": 128, "available": 0, "totalTickets": 128}
    p = tmp_path / "s.json"
    p.write_text(json.dumps(doc))
    assert main(["serverstatus", str(p)]) == 0
    out = capsys.readouterr().out
    assert "Concurrency tickets" in out
    assert main(["serverstatus", str(p), "--exit-code"]) == 2
    capsys.readouterr()                      # discard, buffer accumulates
    assert main(["serverstatus", str(p), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert any(c["title"] == "Concurrency tickets" for c in data["checks"])


def test_export_script_serverstatus(capsys):
    assert main(["export-script", "serverstatus"]) == 0
    out = capsys.readouterr().out
    assert "serverStatus" in out
    assert "--after" in out


# --------------------------------------------------- discovery + exit codes ---

def test_triage_skips_local_metrics_for_a_foreign_log(tmp_path, monkeypatch):
    """A log copied from another host must not be correlated with this
    machine's diagnostic.data — that produced confident, wrong output."""
    from mdbkit import triage as T
    log = tmp_path / "copied.log"
    log.write_text("\n".join(DemoLog(minutes=15, host="other-host").build())
                   + "\n")
    monkeypatch.setattr(T, "local_hostname", lambda: "this-host")
    called = []
    monkeypatch.setattr(T, "find_diagnostic_data",
                        lambda p: called.append(p) or None)
    findings, _s, _c = T.run_triage(str(log), window_min=0, no_sysprobe=True)
    assert not called, "must not go looking for local metrics"
    note = next(f for f in findings if f.title == "Metrics not collected")
    assert "other-host" in note.detail and "this-host" in note.detail
    assert "--ftdc" in note.next_step


def test_triage_uses_local_metrics_when_host_matches(tmp_path, monkeypatch):
    from mdbkit import triage as T
    log = tmp_path / "local.log"
    log.write_text("\n".join(DemoLog(minutes=15, host="samehost").build()) + "\n")
    monkeypatch.setattr(T, "local_hostname", lambda: "samehost")
    seen = []
    monkeypatch.setattr(T, "find_diagnostic_data",
                        lambda p: seen.append(p) or None)
    T.run_triage(str(log), window_min=0, no_sysprobe=True)
    assert seen, "same host should still auto-discover"


def test_discover_dbpath_refuses_to_guess_between_instances(monkeypatch):
    from mdbkit import triage as T
    monkeypatch.setattr(T, "find_mongods", lambda: [
        (101, ["/usr/bin/mongod", "--dbpath", "/data/shard1", "--port", "27018"]),
        (102, ["/usr/bin/mongod", "--dbpath", "/data/config", "--port", "27019"]),
    ])
    monkeypatch.setattr(T, "dbpath_from_config", lambda p: None)
    monkeypatch.setattr(T.os.path, "isdir", lambda p: False)
    path, how = T.discover_dbpath(None)
    assert path is None
    assert "multiple" in how.lower()


def test_sysprobe_lists_multiple_instances(monkeypatch):
    from mdbkit import triage as T
    monkeypatch.setattr(T, "find_mongods", lambda: [
        (101, ["/usr/bin/mongod", "--dbpath", "/data/shard1", "--port", "27018"]),
        (102, ["/usr/bin/mongod", "--dbpath", "/data/config", "--port", "27019"]),
    ])
    findings = T.sysprobe(None)
    f = next(x for x in findings if x.title == "Multiple mongod processes")
    assert "27018" in " ".join(f.evidence)
    assert "--dbpath" in f.next_step


def test_port_from_argv():
    from mdbkit.triage import port_from_argv
    assert port_from_argv(["mongod", "--port", "28110"]) == "28110"
    assert port_from_argv(["mongod", "--port=28111"]) == "28111"
    assert port_from_argv(["mongod"]) is None


def test_triage_exit_codes(tmp_path):
    quiet = tmp_path / "q.log"
    quiet.write_text("\n".join(
        DemoLog(scenario="healthy", minutes=15).build()) + "\n")
    bad = tmp_path / "b.log"
    bad.write_text("\n".join(
        DemoLog(scenario="incident", minutes=30).build()) + "\n")
    assert main(["triage", str(bad), "--window", "0", "--no-sysprobe",
                 "--exit-code"]) == 2
    rc = main(["triage", str(quiet), "--window", "0", "--no-sysprobe",
               "--exit-code"])
    assert rc in (0, 1)
    assert main(["triage", str(bad), "--window", "0", "--no-sysprobe"]) == 0


def test_triage_only_filter(tmp_path, capsys):
    bad = tmp_path / "b.log"
    bad.write_text("\n".join(
        DemoLog(scenario="incident", minutes=30).build()) + "\n")
    main(["triage", str(bad), "--window", "0", "--no-sysprobe",
          "--only", "CRIT"])
    out = capsys.readouterr().out
    assert "[CRIT]" in out
    assert "[OK]" not in out and "[INFO]" not in out
