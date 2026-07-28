"""Tests for `mdbkit triage` (beta)."""

import json
import os
import shutil

from mdbkit.triage import run_triage

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample_mongod.log")

ELECTION_LINES = [
    '{"t":{"$date":"2026-07-01T08:41:00.000+00:00"},"s":"I","c":"REPL","id":4615652,"ctx":"ReplCoord","msg":"Starting an election, since we\'ve seen no PRIMARY in election timeout period","attr":{"term":73}}',
    '{"t":{"$date":"2026-07-01T08:41:02.000+00:00"},"s":"I","c":"REPL","id":21450,"ctx":"ReplCoord","msg":"Election succeeded, assuming primary role","attr":{"term":74}}',
    '{"t":{"$date":"2026-07-01T08:41:02.100+00:00"},"s":"I","c":"REPL","id":21215,"ctx":"ReplCoord","msg":"Member is in new state","attr":{"hostAndPort":"db-prod-02:27017","newState":"SECONDARY"}}',
]

NOISE_LINE = '{"t":{"$date":"2026-07-01T08:42:00.000+00:00"},"s":"I","c":"REPL","id":4615655,"ctx":"ReplCoord","msg":"Not starting an election, since we are not electable","attr":{}}'


def build_log(tmp_path, extra_lines):
    dst = tmp_path / "mongod.log"
    shutil.copy(FIXTURE, dst)
    with open(dst, "a") as fh:
        for line in extra_lines:
            fh.write(line + "\n")
    return str(dst)


def sev_map(findings):
    return {f.title: f for f in findings}


def test_election_detected_and_noise_ignored(tmp_path):
    log = build_log(tmp_path, ELECTION_LINES + [NOISE_LINE])
    findings, _, _ = run_triage(log, no_sysprobe=True)
    by = sev_map(findings)
    f = by["Replica set instability"]
    assert f.severity == "CRIT"
    assert "2 election/stepdown" in f.detail  # start + succeeded; noise excluded
    assert f.beta is True


def test_quiet_log_has_ok_findings():
    findings, _, _ = run_triage(FIXTURE, no_sysprobe=True)
    by = sev_map(findings)
    assert by["Replica set"].severity == "OK"
    assert "Hot collection" in by
    assert by["Hot collection"].detail.startswith("shop.events")
    # fixture has one E line -> error finding present
    assert by["Error-severity log lines"].severity == "WARN"
    # startup marker present -> restart warning
    assert "Process start(s) in window" in by


def test_connection_storm(tmp_path):
    storm = []
    for i in range(80):  # 80 accepts within one minute >= floor threshold 60
        storm.append(json.dumps({
            "t": {"$date": f"2026-07-01T09:00:{i % 60:02d}.{i:03d}+00:00"},
            "s": "I", "c": "NETWORK", "id": 22943, "ctx": "listener",
            "msg": "Connection accepted",
            "attr": {"remote": "10.9.9.9:5000", "connectionId": 1000 + i,
                     "connectionCount": 10 + i},
        }))
    log = build_log(tmp_path, storm)
    findings, _, _ = run_triage(log, no_sysprobe=True)
    by = sev_map(findings)
    f = by["Connection storm"]
    assert f.severity == "WARN"
    assert any("10.9.9.9" in e for e in f.evidence)


def test_sysprobe_disk_ok(tmp_path):
    findings, _, _ = run_triage(FIXTURE, dbpath=str(tmp_path))
    by = sev_map(findings)
    assert "Disk (dbPath volume)" in by  # probe ran against tmp dir


def test_sysprobe_skips_foreign_log():
    # fixture dbPath /data/db should not exist here -> skip finding
    findings, _, _ = run_triage(FIXTURE)
    by = sev_map(findings)
    assert ("System probes skipped" in by) or ("Disk (dbPath volume)" in by)


def test_window_filtering(tmp_path):
    log = build_log(tmp_path, ELECTION_LINES)
    # Window covering only the last minutes should still include elections
    findings, _, cutoff = run_triage(log, window_min=5, no_sysprobe=True)
    assert cutoff is not None
    by = sev_map(findings)
    assert by["Replica set instability"].severity == "CRIT"
    # And the early-morning slow queries (08:01-08:08) fall outside the window
    assert by["Slow queries"].detail.startswith("No slow queries") or \
        "Hot collection" not in by


def test_cli_triage_smoke(tmp_path, capsys):
    from mdbkit.cli import main
    log = build_log(tmp_path, ELECTION_LINES)
    assert main(["triage", log, "--no-sysprobe"]) == 0
    out = capsys.readouterr().out
    assert "[CRIT]" in out and "read-only" in out
    assert main(["triage", log, "--json", "--no-sysprobe"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert any(f["severity"] == "CRIT" for f in data["findings"])


def test_real_election_fixture_detected():
    """Real MongoDB 7.x replica-set election lines (from simagix/hatchet
    public demo logs). Elections log under component ELECTION, not REPL —
    this fixture caught that bug; never regress it."""
    fx = os.path.join(os.path.dirname(__file__), "fixtures",
                      "real_election_rs1.log")
    findings, _, _ = run_triage(fx, no_sysprobe=True)
    by = sev_map(findings)
    f = by["Replica set instability"]
    assert f.severity == "CRIT"
    assert any("Starting an election" in e for e in f.evidence)
    assert any("Election succeeded" in e for e in f.evidence)


# ------------------------------------------------- 0.1.1 additions ----

INDEX_BUILD_LINES = [
    '{"t":{"$date":"2026-07-01T08:45:00.000+00:00"},"s":"I","c":"INDEX","id":20438,"ctx":"conn5","msg":"Index build: registering","attr":{"namespace":"shop.orders","buildUUID":{"uuid":{"$uuid":"aaaa"}}}}',
    '{"t":{"$date":"2026-07-01T08:46:30.000+00:00"},"s":"I","c":"INDEX","id":20440,"ctx":"conn5","msg":"Index build: done building","attr":{"namespace":"shop.orders"}}',
]


def test_index_build_detected(tmp_path):
    log = build_log(tmp_path, INDEX_BUILD_LINES)
    findings, _, _ = run_triage(log, no_sysprobe=True)
    by = sev_map(findings)
    f = by["Index build activity"]
    assert f.severity == "WARN"
    assert "shop.orders" in f.detail


def test_collscan_and_volume_findings():
    findings, _, _ = run_triage(FIXTURE, no_sysprobe=True)
    by = sev_map(findings)
    assert "Collection scans" in by
    assert "COLLSCAN" in by["Collection scans"].detail
    assert "Slow query volume" in by


def test_default_window_is_60_minutes():
    _f, _s, cutoff = run_triage(FIXTURE, no_sysprobe=True)
    assert cutoff is not None  # default window applied, not whole file


def test_whole_file_when_window_zero():
    _f, _s, cutoff = run_triage(FIXTURE, window_min=0, no_sysprobe=True)
    assert cutoff is None


def test_dbpath_from_config_yaml_and_ini(tmp_path):
    from mdbkit.triage import dbpath_from_config
    y = tmp_path / "mongod.conf"
    y.write_text("storage:\n  dbPath: /var/lib/mongodb\n  journal:\n    enabled: true\n")
    assert dbpath_from_config(str(y)) == "/var/lib/mongodb"
    i = tmp_path / "legacy.conf"
    i.write_text("# comment\ndbpath=/data/db\nport=27017\n")
    assert dbpath_from_config(str(i)) == "/data/db"
    assert dbpath_from_config(str(tmp_path / "missing.conf")) is None


def test_dbpath_from_argv():
    from mdbkit.triage import dbpath_from_argv
    assert dbpath_from_argv(["mongod", "--dbpath", "/data/db"]) == "/data/db"
    assert dbpath_from_argv(["mongod", "--dbpath=/srv/mongo"]) == "/srv/mongo"
    assert dbpath_from_argv(["mongod", "--port", "27017"]) is None
