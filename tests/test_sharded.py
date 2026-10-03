"""Sharded clusters, on logs from real 6.0.29, 7.0.43, 8.0.32 and 9.0.2
clusters (fixtures/real/sharded):

* manual/   a config server, two shards and a mongos; a collection sharded
            on {customerId: 1}; a range moved to the second shard (a real
            migration); targeted and scatter-gather queries; then a move
            back that fails because the range deleter has not run yet.
* balancer/ the balancer moving data on its own (1 MB chunks), then the
            second shard killed with kill -9 while queries run.
"""

import contextlib
import io
import json
import os

import pytest

from mdbkit.analysis import SummaryAggregator
from mdbkit.cli import main
from mdbkit.parser import iter_entries
from mdbkit.triage import run_triage

SHARDED = os.path.join(os.path.dirname(__file__), "fixtures", "real", "sharded")
VERSIONS = ("6.0.29", "7.0.43", "8.0.32", "9.0.2")


def log(version, scenario, name):
    return os.path.join(SHARDED, version, scenario, name)


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = main(argv)
        except SystemExit as exc:
            rc = exc.code
    return rc, out.getvalue(), err.getvalue()


def findings(path):
    f, _, _ = run_triage(path, window_min=0, no_sysprobe=True)
    return {x.title: x for x in f}


def summary(path):
    agg = SummaryAggregator()
    for e in iter_entries(path):
        agg.consume(e)
    return agg.finish()


@pytest.mark.parametrize("version", VERSIONS)
def test_roles_are_recognised(version):
    assert summary(log(version, "manual", "mongos-mongos.log")).role == "mongos"
    s = summary(log(version, "manual", "shA-mongod.log"))
    assert (s.role, s.repl_set) == ("shard", "shA")
    s = summary(log(version, "balancer", "cfg-mongod.log"))
    assert (s.role, s.repl_set) == ("config", "cfgRS")


@pytest.mark.parametrize("version", VERSIONS)
def test_scatter_gather_is_reported_on_the_router(version):
    f = findings(log(version, "manual", "mongos-mongos.log"))
    sg = f["Scatter-gather queries"]
    assert "all 2 shards" in sg.detail
    assert any("{status:eq" in e for e in sg.evidence)
    # the targeted query (shard key in the filter) is not listed
    assert not any("{customerId:eq}" in e for e in sg.evidence)


@pytest.mark.parametrize("version", VERSIONS)
def test_router_queries_table_shows_routing_not_plans(version):
    rc, out, _ = run(["queries", log(version, "manual", "mongos-mongos.log")])
    assert rc == 0
    assert "shards" in out and "to all" in out and "shard wait" in out
    assert "COLLSCAN" not in out and " scan " not in out
    rows = {l.split()[2] + l.split()[-1]: l for l in out.splitlines()
            if l.startswith("shop.orders")}
    targeted = [l for l in out.splitlines() if "{customerId:eq}" in l]
    assert targeted and " 1 " in targeted[0]


@pytest.mark.parametrize("version", VERSIONS)
def test_advise_on_a_router_points_to_the_shards(version):
    rc, out, _ = run(["advise", log(version, "manual", "mongos-mongos.log")])
    assert rc == 0 and "mongos (router) log" in out and "shard" in out


@pytest.mark.parametrize("version", VERSIONS)
def test_migrations_on_the_donor_shard(version):
    f = findings(log(version, "manual", "shA-mongod.log"))
    m = f["Chunk migrations"]
    assert "1 moved" in m.detail
    assert "1 incoming migration(s) failed" in m.detail
    assert "Migrations waiting for the range deleter" in f


@pytest.mark.parametrize("version", VERSIONS)
def test_failed_migration_reason_is_explained(version):
    f = findings(log(version, "manual", "shB-mongod.log"))
    m = f["Chunk migrations"]
    assert m.severity == "WARN" and "1 failed" in m.detail
    assert any("range deleter" in e for e in m.evidence)


@pytest.mark.parametrize("version", VERSIONS)
def test_balancer_migrations_are_counted(version):
    s = summary(log(version, "balancer", "shA-mongod.log"))
    assert s.migrations and s.migrations["moved"] >= 5 and s.migrations["failed"] == 0


@pytest.mark.parametrize("version", VERSIONS)
def test_shard_outage_seen_from_the_config_server(version):
    f = findings(log(version, "balancer", "cfg-mongod.log"))
    assert f["Balancer errors"].severity == "WARN"
    assert f["Replica set unreachable"].severity == "CRIT"
    assert "shB" in f["Replica set unreachable"].detail


@pytest.mark.parametrize("version", VERSIONS)
def test_failed_queries_during_the_outage(version):
    f = findings(log(version, "balancer", "mongos1-mongos.log"))
    failed = f["Failed operations"]
    assert failed.severity == "WARN"
    assert any("FailedToSatisfyReadPreference" in e for e in failed.evidence)


def test_internal_errors_are_not_failed_operations():
    """Real replica set logs are full of failed internal commands (mongosh
    handshake probes, admin.$cmd); none of them is an application failure."""
    real = os.path.join(os.path.dirname(__file__), "fixtures", "real")
    for name in ("mongod-9.0.2.log", "mongod-8.0.32.log", "mongod-7.0.43.log"):
        f, _, _ = run_triage(os.path.join(real, name), window_min=0, no_sysprobe=True)
        assert "Failed operations" not in {x.title for x in f}


def test_ftdc_on_a_shard_server_reads_role_prefixed_metrics():
    """8.0+ shard servers write common.serverStatus... instead of
    serverStatus...; mdbkit read nothing from them before 0.8."""
    rc, out, _ = run(["ftdc", "summary", os.path.join(SHARDED, "ftdc-shard-8.0.32"),
                      "--all", "--json"])
    data = json.loads(out)
    text = json.dumps(data)
    assert "cache.maxBytes" in text and "conns.current" in text


def test_lab_sharded_topology_plan():
    from mdbkit.lab import plan_nodes
    plan = plan_nodes(nodes=1, standalone=False, shards=2)
    assert [p["role"] for p in plan] == ["config", "shard", "shard", "mongos"]
    assert [p["set"] for p in plan][:3] == ["mdbkitcfg", "shard1", "shard2"]
    plan = plan_nodes(nodes=3, standalone=False, shards=2)
    assert len(plan) == 1 + 6 + 1


def test_lab_logs_list_the_router_first():
    from mdbkit.lab import connection_string, log_paths
    state = {"nodes": [
        {"index": 0, "role": "config", "port": 1, "log": "c.log"},
        {"index": 1, "role": "shard", "port": 2, "log": "s1.log"},
        {"index": 2, "role": "mongos", "port": 3, "log": "m.log"}]}
    assert log_paths(state)[0] == "m.log"
    assert connection_string(state) == "mongodb://127.0.0.1:3/"


def test_lab_rejects_shards_with_standalone():
    rc, _, err = run(["lab", "start", "--shards", "2", "--standalone",
                      "--dir", "/nonexistent-mdbkit-lab"])
    assert rc == 2 and "cannot be combined" in err


# A real line from the recipient shard of every chunk migration (8.0.32).
MIGRATION_SHUTDOWN_LINE = '{"t":{"$date":"2026-10-02T23:51:33.222+05:30"},"s":"I",  "c":"SHARDING", "id":6718401, "svc":"S", "ctx":"migrateThread","msg":"Shutting down and joining inserter threads for migration {migrationId}","attr":{"migrationId":{"uuid":{"$uuid":"b25d089a-00d8-4e2d-a019-5c180aa805b3"}},"namespace":"shop.orders"}}'


def test_a_migration_is_not_a_shutdown(tmp_path):
    """The recipient logs "Shutting down and joining inserter threads" for
    each migration. 0.7 took any message starting "Shutting down" for the
    process going down, and reported a CRIT "nothing after it shows the node
    serving again" on a healthy shard."""
    src = log("8.0.32", "manual", "shB-mongod.log")
    dst = tmp_path / "shB.log"
    dst.write_text(open(src).read() + MIGRATION_SHUTDOWN_LINE + "\n")
    f = findings(str(dst))
    assert f["Cluster health"].severity == "OK"
