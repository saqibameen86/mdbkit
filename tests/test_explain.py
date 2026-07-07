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


def test_load_explain_rejects_shell_constructors(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"n": NumberLong(5)}')
    try:
        load_explain(str(bad))
        assert False, "should have raised"
    except ValueError as exc:
        assert "mongosh" in str(exc)


def test_cli_explain_smoke(tmp_path, capsys):
    from mdbkit.cli import main
    f = tmp_path / "explain.json"
    f.write_text(json.dumps(COLLSCAN_EXPLAIN))
    assert main(["explain", str(f)]) == 0
    out = capsys.readouterr().out
    assert "candidate index" in out.lower()
    assert main(["explain", str(f), "--json"]) == 0
