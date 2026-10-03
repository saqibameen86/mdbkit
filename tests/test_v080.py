"""0.8 fixes found by reviewing the release, each pinned to an input that
triggered it."""

import contextlib
import io
import json
import os
import shutil
from datetime import datetime, timedelta, timezone

from mdbkit.cli import main
from mdbkit.ftdc import Series
from mdbkit.triage import find_router_ftdc, ftdc_findings, run_triage, sysprobe

HERE = os.path.dirname(__file__)
SHARDED = os.path.join(HERE, "fixtures", "real", "sharded")
FAILURES = os.path.join(HERE, "fixtures", "real", "failures")
MONGOS = os.path.join(SHARDED, "8.0.32", "manual", "mongos-mongos.log")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = main(argv)
        except SystemExit as exc:
            rc = exc.code
    return rc, out.getvalue(), err.getvalue()


def _shift(line, hours):
    d = json.loads(line)
    t = datetime.fromisoformat(d["t"]["$date"])
    d["t"]["$date"] = (t + timedelta(hours=hours)).isoformat(timespec="milliseconds")
    return json.dumps(d)


def _late_mongos(tmp_path):
    """The mongos log, then the same queries two hours later: the startup
    lines that say "this is a mongos" fall outside the default window."""
    lines = open(MONGOS).read().splitlines()
    slow = [l for l in lines if '"Slow query"' in l]
    p = tmp_path / "mongos.log"
    p.write_text("\n".join(lines + [_shift(l, 2) for l in slow]) + "\n")
    return str(p)


def test_router_role_is_known_even_when_startup_is_before_the_window(tmp_path):
    f, _, _ = run_triage(_late_mongos(tmp_path), no_sysprobe=True)
    titles = {x.title: x for x in f}
    assert "all 2 shards" in titles["Scatter-gather queries"].detail


def test_host_keeps_the_mongos_out_of_the_cache_total(tmp_path):
    rc, out, _ = run(["host", _late_mongos(tmp_path), "--window", "60", "--json"])
    inst = json.loads(out)["instances"][0]
    assert (inst["kind"], inst["cacheMB"]) == ("mongos", None)


def test_a_mongos_log_gets_no_guessed_dbpath():
    out = sysprobe(None, router=True)
    titles = [f.title for f in out]
    assert titles[0] == "System probes"
    assert not any(t.startswith("Disk") or t == "mongod process" for t in titles)


def test_router_ftdc_is_found_next_to_the_log_and_through_a_glob(tmp_path):
    shutil.copy(MONGOS, tmp_path / "mongos.log")
    shutil.copy(MONGOS, tmp_path / "mongos.log.1")
    d = tmp_path / "mongos.diagnostic.data"
    d.mkdir()
    shutil.copy(os.path.join(SHARDED, "ftdc-shard-8.0.32"), d / "metrics.2026-10-02T00-00-00Z-00000")
    assert find_router_ftdc(str(tmp_path / "mongos.log")) == str(d)
    assert find_router_ftdc(str(tmp_path / "mongos.log.1.gz")) == str(d)
    f, _, _ = run_triage([str(tmp_path / "mongos.log*")], window_min=0)
    assert any(x.title == "FTDC discovered" and "next to the log" in x.detail for x in f)


def test_busiest_minute_carries_across_short_chunks_but_not_gaps():
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    s = Series("x", "x", "counter")
    v, vals = 0, []
    for i in range(120):
        v += 100 if 40 <= i < 100 else 1
        vals.append(v)
    for k in range(0, 120, 10):                  # twelve 10-sample chunks
        s.observe(vals[k:k + 10], t + timedelta(seconds=k), t + timedelta(seconds=k + 9))
    assert 95 <= s.peak_rate <= 100
    gap = Series("y", "y", "counter")
    gap.observe(list(range(0, 50)), t, t + timedelta(seconds=49))
    # an hour later: 60 samples that span the gap are not one minute
    gap.observe(list(range(10000, 10050)), t + timedelta(hours=1),
                t + timedelta(hours=1, seconds=49))
    assert gap.peak_rate is None or gap.peak_rate < 5


def test_counter_increase_survives_a_restart():
    t = datetime(2026, 1, 1, tzinfo=timezone.utc)
    s = Series("z", "z", "counter")
    s.observe([100, 200, 300], t, t + timedelta(seconds=2))
    s.observe([5, 15], t + timedelta(seconds=3), t + timedelta(seconds=4))  # restarted
    assert s.increase == 200 + 10
    assert s.stats()["change"] == 210


def test_ftdc_times_follow_the_log_or_say_utc():
    flow = os.path.join(FAILURES, "9.0.2", "ftdc-flow")
    plain = {f.title: f for f in ftdc_findings(flow)}
    assert plain["Flow control engaged (FTDC)"].detail.endswith(
        "flow-control tickets.") and " UTC" in plain["Flow control engaged (FTDC)"].detail
    ist = timezone(timedelta(hours=5, minutes=30))
    local = {f.title: f for f in ftdc_findings(flow, tz=ist)}
    detail = local["Flow control engaged (FTDC)"].detail
    assert " UTC" not in detail and "09:13" in detail


def test_advise_json_on_a_router_log_is_still_a_list():
    rc, out, err = run(["advise", MONGOS, "--json"])
    assert rc == 0 and json.loads(out) == [] and "mongos (router) log" in err


def test_seed_workload_only_keeps_the_data():
    from mdbkit.lab import seed_script
    for sharded in (False, True):
        s = seed_script(1000, sharded=sharded, workload_only=True)
        assert ".drop()" not in s and "insertMany" not in s
        assert "running workload" in s and "no data yet" in s


def test_unreachable_set_names_the_read_preference(tmp_path):
    line = {"t": {"$date": "2026-10-03T00:00:00.000+00:00"}, "s": "I", "c": "NETWORK",
            "id": 4333208, "ctx": "x", "msg": "RSM host selection timeout",
            "attr": {"replicaSet": "shB", "error": {
                "code": 133, "codeName": "FailedToSatisfyReadPreference",
                "errmsg": "Could not find host matching read preference "
                          "{ mode: \"secondary\" } for set shB"}}}
    p = tmp_path / "m.log"
    p.write_text(json.dumps(line) + "\n")
    f, _, _ = run_triage(str(p), window_min=0, no_sysprobe=True)
    u = {x.title: x for x in f}["Replica set unreachable"]
    assert "no member for read preference secondary 1x" in u.evidence[0]


def test_hostile_checkpoint_line_does_not_crash(tmp_path):
    line = {"t": {"$date": "2026-10-03T00:00:00.000+00:00"}, "s": "I", "c": "WTCHKPT",
            "id": 22430, "ctx": "x", "msg": "WiredTiger message",
            "attr": {"message": {"msg": "Checkpoint has been running for "
                                        + "9" * 5000 + " seconds"}}}
    p = tmp_path / "c.log"
    p.write_text(json.dumps(line) + "\n")
    run_triage(str(p), window_min=0, no_sysprobe=True)
