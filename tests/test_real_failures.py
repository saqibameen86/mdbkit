"""Triage on real failures, on MongoDB 6.0.29, 7.0.43, 8.0.32 and 9.0.2
(fixtures/real/failures). Each version ran the same script on a three-node
replica set started with `mdbkit lab`:

1. the primary (node0) killed with kill -9, restarted 30 s later;
2. a replSetStepDown;
3. 300 connections opened at once to node0;
4. flow control: both secondaries frozen with fsyncLock while the primary
   took 150 s of writes.

scenario.json records which node played which part. Separately, on a
standalone: cache pressure (a 256 MB cache, one eviction thread, four
writers) and a checkpoint held up for over a minute (the process frozen
with SIGSTOP mid-checkpoint). Logs are trimmed (slowms 0 logs every
operation) and gzipped; FTDC files are cut to the chunks around the event.
"""

import json
import os

import pytest

from mdbkit.triage import ftdc_findings, run_triage

HERE = os.path.join(os.path.dirname(__file__), "fixtures", "real", "failures")
VERSIONS = ("6.0.29", "7.0.43", "8.0.32", "9.0.2")


def scenario(version):
    with open(os.path.join(HERE, version, "scenario.json")) as fh:
        return json.load(fh)


def triage(version, node):
    f, _, _ = run_triage(os.path.join(HERE, version, node + ".log.gz"),
                         window_min=0, no_sysprobe=True)
    return {x.title: x for x in f}


@pytest.mark.parametrize("version", VERSIONS)
def test_killed_primary_is_reported_as_an_unclean_restart(version):
    f = triage(version, scenario(version)["crashed"])
    start = f["Process start(s) in window"]
    assert start.severity == "CRIT" and "unclean" in start.detail


@pytest.mark.parametrize("version", VERSIONS)
def test_failover_after_the_kill_is_instability(version):
    """The kill came seconds after the set was created, so the new primary's
    election is term 2. Before 0.8 that was taken for part of the set-up."""
    f = triage(version, scenario(version)["failover_winner"])
    inst = f["Replica set instability"]
    assert inst.severity == "CRIT"
    assert any("no PRIMARY" in e for e in inst.evidence)


@pytest.mark.parametrize("version", VERSIONS)
def test_stepdown_is_visible_on_the_member_that_stepped_up(version):
    f = triage(version, scenario(version)["stepped_up"])
    found = [x for t, x in f.items() if t in ("Elections", "Replica set instability")]
    assert found and any("step up request" in e for x in found for e in x.evidence)


@pytest.mark.parametrize("version", VERSIONS)
def test_connection_storm(version):
    f = triage(version, scenario(version)["storm"])
    storm = f["Connection storm"]
    assert storm.severity == "WARN"
    assert any(e.startswith("127.0.0.1: ") and int(e.split(": ")[1].split()[0]) >= 300
               for e in storm.evidence)


@pytest.mark.parametrize("version", VERSIONS)
def test_flow_control_in_the_log(version):
    """MongoDB logs it as id 22225 under STORAGE; before 0.8 mdbkit only
    looked at replication messages and never saw it."""
    f = triage(version, scenario(version)["flow_primary"])
    fc = f["Flow control engaged"]
    assert fc.severity == "WARN" and "warning(s) between" in fc.detail


@pytest.mark.parametrize("version", VERSIONS)
def test_the_secondaries_do_not_report_flow_control(version):
    s = scenario(version)
    for node in ("node0", "node1", "node2"):
        if node != s["flow_primary"]:
            assert "Flow control engaged" not in triage(version, node)


@pytest.mark.parametrize("version", VERSIONS)
def test_flow_control_in_ftdc(version):
    f = {x.title: x for x in ftdc_findings(os.path.join(HERE, version, "ftdc-flow"))}
    fc = f["Flow control engaged (FTDC)"]
    assert fc.severity == "WARN" and "lagged for" in fc.detail


@pytest.mark.parametrize("version", VERSIONS)
def test_cache_pressure_in_ftdc(version):
    f = {x.title: x for x in ftdc_findings(os.path.join(HERE, version, "ftdc-pressure"))}
    # application threads evicted for hundreds of ms per second
    ev = f["Cache eviction pressure (FTDC)"]
    assert ev.severity == "WARN"
    assert int(ev.detail.split(": ")[1].split(" ms")[0]) >= 300
    # the cache sat at the 20% dirty trigger for about a minute or more
    # (59 s on 6.0, just under the 60 s that makes it a WARN on its own)
    assert "application threads start helping to evict" in f["WiredTiger cache"].detail


@pytest.mark.parametrize("version", VERSIONS)
def test_long_checkpoint_in_ftdc(version):
    m = {x.title: x for x in ftdc_findings(os.path.join(HERE, version, "ftdc-checkpoint"))}
    cp = m["Checkpoints (FTDC)"]
    assert cp.severity == "WARN"
    assert float(cp.detail.split("window ")[1].split("s")[0]) >= 70


def test_long_checkpoint_in_the_log():
    """8.3+ logs WiredTiger's checkpoint progress at the default level
    ("Checkpoint has been running for N seconds, wrote ..."); 6.0-8.0 do
    not, so for them FTDC is the only source."""
    f = triage("9.0.2", "checkpoint")
    cp = f["Slow WiredTiger checkpoints (log)"]
    assert cp.severity == "WARN"
    assert "1 checkpoint(s) ran for 20s or more, 1 of them over 60s" in cp.detail


def test_short_checkpoints_reported_by_wiredtiger_are_ignored():
    """9.0 also reports quick checkpoints ("running for 0 seconds") at
    startup; the first version of the log detector listed those."""
    f = triage("9.0.2", "node1")
    assert "Slow WiredTiger checkpoints (log)" not in f


# ---------------------------------------------------------------------------
# Several members' logs as one stream (`mdbkit triage $(mdbkit lab logs)`)

REAL = os.path.dirname(HERE)


def _stream(*paths):
    f, _, _ = run_triage(list(paths), window_min=0, no_sysprobe=True)
    return {x.title: x for x in f}


def test_other_members_first_start_is_not_a_restart():
    """0.7 read node2's startup, after node1's whole log, as node1 restarting."""
    f = _stream(os.path.join(HERE, "9.0.2", "node1.log.gz"),
                os.path.join(HERE, "9.0.2", "node2.log.gz"))
    assert "Process start(s) in window" not in f


def test_a_crash_inside_another_members_log_is_still_found(tmp_path):
    """The second file is a different instance whose log starts with history
    (its first startup cut off), then crashes and restarts: still a CRIT."""
    src = open(os.path.join(REAL, "crash_restart-8.0.32.log")).read().splitlines()
    cut = tmp_path / "other.log"
    cut.write_text("\n".join(src[19:]) + "\n")       # from after its first start
    f = _stream(os.path.join(HERE, "9.0.2", "node1.log.gz"), str(cut))
    start = f["Process start(s) in window"]
    assert start.severity == "CRIT" and "1x after an unclean stop" in start.detail


def test_a_clean_restart_inside_another_members_log_is_still_found(tmp_path):
    src = open(os.path.join(REAL, "clean_restart-8.0.32.log")).read().splitlines()
    cut = tmp_path / "other.log"
    cut.write_text("\n".join(src[19:]) + "\n")
    f = _stream(os.path.join(HERE, "9.0.2", "node1.log.gz"), str(cut))
    start = f["Process start(s) in window"]
    assert start.severity == "WARN" and "1x after a clean shutdown" in start.detail


@pytest.mark.parametrize("version", VERSIONS)
def test_stepdown_is_visible_on_the_primary_that_stepped_down(version):
    """The primary logs the replSetStepDown command under COMMAND (21579);
    0.7 only read REPL/ELECTION lines and said "No election or stepdown"."""
    s = scenario(version)
    stepped = "node0"                         # node0 was primary when stepped down
    assert s["stepped_up"] != stepped
    f = triage(version, stepped)
    found = [x for t, x in f.items() if t in ("Elections", "Replica set instability")]
    assert any("replSetStepDown" in e for x in found for e in x.evidence)
