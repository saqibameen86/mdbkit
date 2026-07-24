"""Tests for `mdbkit explain`."""

import json

from mdbkit.explain import analyze_explain, load_explain

COLLSCAN_EXPLAIN = {
    "explainVersion": "1",
    "queryPlanner": {
        "namespace": "shop.orders",
        "winningPlan": {
            "stage": "SORT",
            "sortPattern": {"createdAt": -1},
            "inputStage": {
                "stage": "COLLSCAN",
                "filter": {"$and": [{"status": {"$eq": "pending"}},
                                     {"createdAt": {"$gt": "2026-06-30"}}]},
                "direction": "forward",
            },
        },
        "rejectedPlans": [],
    },
    "executionStats": {
        "executionSuccess": True,
        "nReturned": 42,
        "executionTimeMillis": 1834,
        "totalKeysExamined": 0,
        "totalDocsExamined": 125000,
    },
    "command": {
        "find": "orders",
        "filter": {"status": "pending", "createdAt": {"$gt": "2026-06-30"}},
        "sort": {"createdAt": -1},
        "$db": "shop",
    },
    "ok": 1,
}

IXSCAN_EXPLAIN = {
    "queryPlanner": {
        "namespace": "shop.users",
        "winningPlan": {
            "stage": "FETCH",
            "inputStage": {"stage": "IXSCAN", "indexName": "email_1",
                            "keyPattern": {"email": 1}},
        },
        "rejectedPlans": [],
    },
    "executionStats": {"nReturned": 1, "executionTimeMillis": 2,
                        "totalKeysExamined": 1, "totalDocsExamined": 1},
    "command": {"find": "users", "filter": {"email": "a@b.com"}, "$db": "shop"},
}


def test_collscan_explain_gets_recommendation():
    report = analyze_explain(COLLSCAN_EXPLAIN)
    assert report.ns == "shop.orders"
    assert report.collscan is True
    assert report.blocking_sort is True
    assert "COLLSCAN" in report.stage_chain and "SORT" in report.stage_chain
    assert any("Full collection scan" in v for v in report.verdicts)
    assert report.recommendation is not None
    assert report.recommendation.candidate == [("status", 1), ("createdAt", -1)]


def test_efficient_ixscan_no_recommendation():
    report = analyze_explain(IXSCAN_EXPLAIN)
    assert report.collscan is False
    assert report.indexes_used == ["email_1"]
    assert any("efficiently" in v for v in report.verdicts)
    assert report.recommendation is None


def test_load_explain_accepts_legacy_shell_output(tmp_path):
    """Old mongo-shell / Compass output is JavaScript, not JSON. Users
    shouldn't have to re-export just to get a plan analyzed."""
    f = tmp_path / "legacy.json"
    f.write_text(
        '{"queryPlanner": {"namespace": "shop.orders",'
        ' "winningPlan": {"stage": "COLLSCAN"}, "rejectedPlans": []},'
        ' "executionStats": {"nReturned": NumberLong(5),'
        ' "executionTimeMillis": NumberInt(900), "totalKeysExamined": 0,'
        ' "totalDocsExamined": NumberLong(90000),'
        ' "since": ISODate("2026-07-01T00:00:00Z"),'
        ' "oid": ObjectId("65f854672d7d6b4a0f6037a6")}}')
    doc = load_explain(str(f))
    assert doc["executionStats"]["nReturned"] == 5
    assert doc["executionStats"]["totalDocsExamined"] == 90000
    report = analyze_explain(doc)
    assert report.collscan is True


def test_load_explain_rejects_true_garbage(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("this is not json at all {{{")
    try:
        load_explain(str(bad))
        assert False, "should have raised"
    except ValueError as exc:
        assert "mongosh" in str(exc)


def test_single_explain_has_no_frequency_caveat():
    """Analyzing one explain: 'shape seen only 1 time' was noise — the count
    is 1 by construction. It should say something useful instead."""
    report = analyze_explain(COLLSCAN_EXPLAIN)
    caveats = " ".join(report.recommendation.caveats)
    assert "seen only 1 time" not in caveats
    assert "single explain document" in caveats


def test_cli_explain_smoke(tmp_path, capsys):
    from mdbkit.cli import main
    f = tmp_path / "explain.json"
    f.write_text(json.dumps(COLLSCAN_EXPLAIN))
    assert main(["explain", str(f)]) == 0
    out = capsys.readouterr().out
    assert "candidate index" in out.lower()
    assert main(["explain", str(f), "--json"]) == 0
