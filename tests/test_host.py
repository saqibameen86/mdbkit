"""`mdbkit host`, tested on logs from a real host running eight mongods
(fixtures/real/host): two 8.0.32 replica sets of three, one left on the
default WiredTiger cache, an 8.0.32 standalone whose log was rotated, a
7.0.43 standalone restarted cleanly, and one member killed with kill -9."""

import contextlib
import io
import json
import os
from datetime import datetime, timezone

import pytest

from mdbkit.cli import main
from mdbkit.host import (_base_name, default_cache_mb, expand_inputs,
                         group_instances, run_host)
from mdbkit.triage import run_triage

HOST = os.path.join(os.path.dirname(__file__), "fixtures", "real", "host")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = main(argv)
        except SystemExit as exc:
            rc = exc.code
    return rc, out.getvalue(), err.getvalue()


@pytest.fixture(scope="module")
def report():
    return run_host([HOST], window_min=0, ram="8G")


def by_key(rep):
    return {i.key: i for i in rep.instances}


def test_files_are_grouped_into_instances(report):
    inst = by_key(report)
    assert len(inst) == 8
    # the rotated file has no startup line; Process Details ties it in
    assert len(inst["db7:29030"].files) == 2


def test_identity_version_set_and_role(report):
    inst = by_key(report)
    assert inst["db7:29031"].version == "7.0.43"
    assert inst["db7:29010"].repl_set == "rs01"
    assert inst["db7:29010"].role == "PRIMARY"
    assert inst["db7:29011"].role == "SECONDARY"
    assert inst["db7:29030"].role == "standalone"


def test_cache_sizes_come_from_the_log(report):
    inst = by_key(report)
    assert inst["db7:29010"].cache_mb == 3503          # default on that box
    assert inst["db7:29020"].cache_mb == 256
    assert inst["db7:29030"].cache_mb == 512


def test_cache_total_against_ram(report):
    f = {x.title: x for x in report.findings}
    assert f["Cache sizes vs RAM"].severity == "CRIT"     # ~11.8 GiB on 8 GiB
    defaults = f["Default cache size on a shared host"]
    assert "3 of 8 mongod instances" in defaults.detail


def test_cache_total_is_ok_with_enough_ram():
    rep = run_host([HOST], window_min=0, ram="64G")
    f = {x.title: x for x in rep.findings}
    assert f["Cache sizes vs RAM"].severity == "OK"


def test_only_the_killed_instance_crashed(report):
    inst = by_key(report)
    assert inst["db7:29021"].crashes == 1
    assert sum(i.crashes for i in report.instances) == 1
    assert inst["db7:29031"].crashes == 0                 # clean restart


def test_host_settings_are_counted_once_across_instances(report):
    f = {x.title: x for x in report.findings}["Startup configuration"]
    assert any("Access control is not enabled: 8 of 8" in e for e in f.evidence)
    assert not any("other:" in e or "unclean" in e.lower() for e in f.evidence)


def test_oom_kill_is_matched_to_its_instance(tmp_path):
    t = datetime(2026, 10, 2, 17, 57, 38, tzinfo=timezone.utc).astimezone()
    stamp = t.strftime("%b %e %H:%M:%S")
    sys_log = tmp_path / "syslog"
    sys_log.write_text(
        "%s db7 kernel: Out of memory: Killed process 603 (mongod)\n"
        "%s db7 kernel: Out of memory: Killed process 4321 (java)\n" % (stamp, stamp))
    rep = run_host([HOST], window_min=0, ram="8G", oslog=[str(sys_log)])
    oom = {x.title: x for x in rep.findings}["OOM kills"]
    assert any("pid 603" in e and "db7:29012" in e for e in oom.evidence)
    assert any("pid 4321" in e and "not one of these instances (java)" in e
               for e in oom.evidence)


def test_ram_unknown_when_logs_are_from_another_machine():
    rep = run_host([HOST], window_min=0)
    assert rep.ram_mb is None
    f = {x.title: x for x in rep.findings}
    assert f["Cache sizes vs RAM"].severity == "INFO"


def test_cli_text_json_and_exit_code():
    rc, out, _ = run(["host", HOST, "--ram", "8G", "--window", "0", "--exit-code"])
    assert rc == 2
    assert "db7:29021" in out and "8 instance(s) on db7" in out
    rc, out, _ = run(["host", HOST, "--ram", "8G", "--window", "0", "--json"])
    data = json.loads(out)
    assert len(data["instances"]) == 8 and data["ramMB"] == 8192
    rc, out, err = run(["host", HOST, "--ram", "lots"])
    assert rc == 2 and "--ram" in err


def test_limit_shows_the_worst_first():
    rc, out, _ = run(["host", HOST, "--ram", "8G", "--window", "0", "--limit", "2"])
    assert "... 6 more" in out
    table = out[out.index("instance  "):]
    assert table.splitlines()[2].startswith("db7:29021")      # the crash


def test_directory_search_skips_data_directories(tmp_path):
    inst = tmp_path / "rs01"
    (inst / "data" / "diagnostic.data").mkdir(parents=True)
    (inst / "data" / "WiredTiger").write_text("")
    (inst / "data" / "mongod.lock").write_text("123")
    (inst / "mongod.log").write_text("")
    (inst / "mongod.log.2026-10-02T17-54-00").write_text("")
    found = expand_inputs([str(tmp_path)])
    assert sorted(os.path.basename(p) for p in found) == [
        "mongod.log", "mongod.log.2026-10-02T17-54-00"]


@pytest.mark.parametrize("name", ["mongod.log.1", "mongod.log.2026-10-02T17-54-00",
                                  "mongod.log.2026-10-02T17-54-00.gz", "mongod.log.3.gz"])
def test_rotation_suffixes(name):
    assert _base_name("/var/log/rs01/" + name) == "/var/log/rs01/mongod.log"


def test_default_cache_formula():
    assert default_cache_mb(8192) == 3584
    assert default_cache_mb(1024) == 256


def test_triage_on_a_rotated_log_finds_its_process():
    """A rotated log has no startup line; Process Details gives the pid."""
    from mdbkit.triage import TriageEngine
    from mdbkit.parser import iter_entries
    e = TriageEngine()
    rotated = os.path.join(HOST, "solo8", "mongod.log")
    for entry in iter_entries(rotated):
        e.consume(entry)
    assert e.pids and e.dbpath


def test_id_index_of_a_new_collection_is_not_an_index_build_warning():
    f, _, _ = run_triage(os.path.join(HOST, "rs01-1", "mongod.log"),
                         window_min=0, no_sysprobe=True)
    assert "Index build activity" not in {x.title for x in f}
