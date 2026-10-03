"""`mdbkit indexes`, on a real export (fixtures/real/indexes) taken from an
8.0.32 replica set with a used compound index, a redundant prefix of it, an
unused index, and unique, TTL, hidden and partial indexes."""

import contextlib
import io
import json
import os

import pytest

from mdbkit.cli import main
from mdbkit.indexusage import analyse, hide_command

REAL = os.path.join(os.path.dirname(__file__), "fixtures", "real", "indexes")
RS = os.path.join(REAL, "replset-8.0.32.json")


def run(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = main(argv)
        except SystemExit as exc:
            rc = exc.code
    return rc, out.getvalue(), err.getvalue()


@pytest.fixture(scope="module")
def rep():
    return analyse([RS])


def kinds(rep):
    return {(f.index.name, f.kind) for f in rep.findings}


def test_unused_and_redundant_are_found(rep):
    k = kinds(rep)
    assert ("sku_1", "unused") in k
    assert ("status_1", "redundant") in k


def test_used_indexes_are_not_listed(rep):
    names = {f.index.name for f in rep.findings}
    assert "status_1_createdAt_-1" not in names
    assert "customerId_1" not in names
    assert "n_1" not in names
    assert "_id_" not in names


def test_protected_indexes_are_explained_not_flagged(rep):
    k = kinds(rep)
    assert ("email_1", "not-judged") in k          # unique
    assert ("createdAt_1", "not-judged") in k      # TTL
    assert ("total_1", "not-judged") in k          # hidden


def test_redundant_names_the_index_that_covers_it(rep):
    f = next(f for f in rep.findings if f.index.name == "status_1")
    assert f.covered_by.name == "status_1_createdAt_-1"


def test_partial_index_never_counts_as_covering(tmp_path):
    data = {"db": "", "collections": {"a.b": [
        {"name": "x_1", "key": {"x": 1}},
        {"name": "x_1_y_1", "key": {"x": 1, "y": 1},
         "partialFilterExpression": {"y": {"$gt": 1}}}]}}
    p = tmp_path / "ix.json"
    p.write_text(json.dumps(data))
    r = analyse([str(p)])
    assert not any(f.kind == "redundant" for f in r.findings)


def test_shard_key_index_is_protected(tmp_path):
    data = {"db": "", "collections": {"a.b": [
        {"name": "_id_", "key": {"_id": 1}},
        {"name": "k_1", "key": {"k": 1}},
        {"name": "k_1_t_1", "key": {"k": 1, "t": 1}}]},
        "usage": {"a.b": [{"name": "k_1", "host": "h", "ops": 0,
                           "since": "2026-09-01T00:00:00Z"}]},
        "shardKeys": {"a.b": {"k": 1}}}
    p = tmp_path / "ix.json"
    p.write_text(json.dumps(data))
    r = analyse([str(p)])
    assert not any(f.kind in ("unused", "redundant") and f.index.name == "k_1"
                   for f in r.findings)


def test_usage_from_several_members_is_combined(tmp_path):
    def export(host, ops):
        return {"db": "", "generatedAt": "2026-10-01T00:00:00Z",
                "collections": {"a.b": [{"name": "_id_", "key": {"_id": 1}},
                                        {"name": "c_1", "key": {"c": 1}}]},
                "usage": {"a.b": [{"name": "c_1", "host": host, "ops": ops,
                                   "since": "2026-09-20T00:00:00Z"}]}}
    p1, p2 = tmp_path / "p.json", tmp_path / "s.json"
    p1.write_text(json.dumps(export("primary:27017", 0)))
    p2.write_text(json.dumps(export("secondary:27017", 12)))
    # unused on the primary, but secondaries serve reads too
    assert ("c_1", "unused") in kinds(analyse([str(p1)]))
    assert ("c_1", "unused") not in kinds(analyse([str(p1), str(p2)]))


def test_old_export_without_usage_still_finds_redundancy(tmp_path):
    p = tmp_path / "old.json"
    p.write_text(json.dumps({"db": "shop", "collections": {"orders": [
        {"name": "a_1", "key": {"a": 1}}, {"name": "a_1_b_1", "key": {"a": 1, "b": 1}}]}}))
    rc, out, _ = run(["indexes", str(p)])
    assert rc == 0 and "usage: not in this file" in out and "a_1" in out


def test_short_counter_window_is_warned():
    rc, out, _ = run(["indexes", RS])
    assert "under 7 days" in out


def test_cli_json_and_exit_code():
    rc, out, _ = run(["indexes", RS, "--json"])
    data = json.loads(out)
    assert data["usageAvailable"] is True and data["indexes"] == 11
    rc, _, _ = run(["indexes", RS, "--exit-code"])
    assert rc == 1
    rc, out, _ = run(["indexes", RS, "--ns", "shop.users", "--exit-code"])
    assert rc == 0


def test_hide_command_is_mongosh_syntax(rep):
    f = next(f for f in rep.findings if f.index.name == "sku_1")
    assert hide_command(f.index) == \
        'db.getSiblingDB("shop").getCollection("orders").hideIndex("sku_1")'


def test_not_an_index_export_is_an_error(tmp_path):
    p = tmp_path / "x.json"
    p.write_text('{"host": "h"}')
    rc, _, err = run(["indexes", str(p)])
    assert rc == 2 and "not an" in err


SHARDED = os.path.join(REAL, "sharded-mongos-8.0.32.json")


def test_export_through_mongos_combines_the_shards():
    """A real export through mongos (8.0.32, two shards, sharded on
    customerId): $indexStats returns one row per shard, and the shard key
    comes from config.collections."""
    rep = analyse([SHARDED])
    k = kinds(rep)
    # customerId_1 was used on one shard only (targeted queries): used
    assert not any(n == "customerId_1" for n, _ in k)
    assert ("status_1", "redundant") in k
    assert ("createdAt_1", "not-judged") in k            # TTL
    # starts with the shard key, but customerId_1 is the index the shard
    # key needs, so this one is judged like any other
    assert ("customerId_1_status_1", "unused") in k
    rc, out, _ = run(["indexes", SHARDED])
    assert "a mongos (usage from each shard's primary)" in out


def test_special_index_never_counts_as_covering(tmp_path):
    """0.8 review: {a: 1} was called redundant next to a text index on
    {a: 1, _fts: "text"}, which cannot serve a query on a alone."""
    p = tmp_path / "ix.json"
    p.write_text(json.dumps({"db": "", "collections": {"s.c": [
        {"name": "a_1", "key": {"a": 1}},
        {"name": "a_text", "key": {"a": 1, "_fts": "text", "_ftsx": 1}},
        {"name": "b_1", "key": {"b": 1}},
        {"name": "b_geo", "key": {"b": 1, "loc": "2dsphere"}}]},
        "usage": {"s.c": [{"name": n, "host": "h", "ops": 5}
                          for n in ("a_1", "a_text", "b_1", "b_geo")]}}))
    assert not analyse([str(p)]).findings


def test_an_unused_index_never_counts_as_covering(tmp_path):
    """Second 0.8 review: {a: 1} (used) was called redundant next to an
    unused {a: 1, b: 1}, which was also listed as unused; hiding both would
    leave queries on a with no index."""
    p = tmp_path / "ix.json"
    p.write_text(json.dumps({"db": "", "collections": {"s.c": [
        {"name": "a_1", "key": {"a": 1}}, {"name": "a_1_b_1", "key": {"a": 1, "b": 1}}]},
        "usage": {"s.c": [{"name": "a_1", "host": "h", "ops": 5000},
                          {"name": "a_1_b_1", "host": "h", "ops": 0}]}}))
    k = kinds(analyse([str(p)]))
    assert k == {("a_1_b_1", "unused")}
