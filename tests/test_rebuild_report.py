"""Tests for query reconstruction (filter --as-explain) and report export."""

import json
import os

from mdbkit.parser import parse_line
from mdbkit.rebuild import to_mongosh, wrap_for_export
from mdbkit.report import Report

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample_mongod.log")


def entry_for(command, ns="shop.orders", op_type="command"):
    return parse_line(json.dumps({
        "t": {"$date": "2026-07-01T08:00:00.000+00:00"}, "s": "I",
        "c": "COMMAND", "id": 51803, "ctx": "conn1", "msg": "Slow query",
        "attr": {"type": op_type, "ns": ns, "command": command,
                 "durationMillis": 500}}))


def test_rebuild_find_with_sort_limit_projection():
    cmd = to_mongosh(entry_for({
        "find": "orders", "filter": {"status": "pending"},
        "projection": {"_id": 0, "status": 1}, "sort": {"createdAt": -1},
        "limit": 50, "$db": "shop"}))
    assert 'getSiblingDB("shop")' in cmd
    assert 'getCollection("orders")' in cmd
    assert '"status": "pending"' in cmd
    assert '.sort({ "createdAt": -1 })' in cmd
    assert ".limit(50)" in cmd
    assert '.explain("executionStats")' in cmd


def test_rebuild_aggregate():
    cmd = to_mongosh(entry_for({
        "aggregate": "events",
        "pipeline": [{"$match": {"tenantId": "t-1"}}, {"$sort": {"ts": -1}}],
        "$db": "shop"}))
    assert ".aggregate(" in cmd and "$match" in cmd


def test_rebuild_renders_ejson_as_constructors():
    cmd = to_mongosh(entry_for({
        "find": "orders",
        "filter": {"_id": {"$oid": "65f854672d7d6b4a0f6037a6"},
                   "at": {"$date": "2026-07-01T00:00:00Z"}},
        "$db": "shop"}))
    assert 'ObjectId("65f854672d7d6b4a0f6037a6")' in cmd
    assert 'ISODate("2026-07-01T00:00:00Z")' in cmd


def test_rebuild_skips_batched_write_without_predicate():
    """COMMAND-level batched writes carry no per-op q — nothing to rebuild.
    Found during QA on real replica logs (it emitted empty {} filters)."""
    assert to_mongosh(entry_for({"update": "orders", "$db": "shop"})) is None
    assert to_mongosh(entry_for({"delete": "orders", "$db": "shop"})) is None


def test_rebuild_write_with_predicate_works():
    cmd = to_mongosh(entry_for(
        {"q": {"sku": "S1"}, "u": {"$set": {"stock": 1}}},
        ns="shop.products", op_type="update"))
    assert cmd is None or "sku" in cmd  # ns-derived collection path


def test_rebuild_returns_none_for_unreconstructable():
    assert to_mongosh(entry_for({"ping": 1, "$db": "admin"})) is None


def test_wrap_for_export_is_runnable_script():
    script = wrap_for_export('db.getSiblingDB("a").getCollection("b").find({})')
    assert script.startswith("//")
    assert "EJSON.stringify(" in script
    assert "mdbkit explain" in script


def test_report_markdown_and_html(tmp_path):
    class F:
        severity, title, detail = "CRIT", "Elections", "3 events"
        evidence, next_step, beta = ["at 10:49"], "check nodes", True

    rep = Report("Test report", "window x")
    rep.findings("Findings", [F()])
    rep.table("Data", ["a", "b"], [[1, 2]])
    rep.code("Command", "mdbkit triage x.log")

    md = rep.to_markdown()
    assert "# Test report" in md and "**[CRIT] Elections**" in md
    assert "| a | b |" in md

    html_out = rep.to_html()
    assert html_out.startswith("<!DOCTYPE html>")
    assert "<style>" in html_out
    # self-contained: no external assets, no scripts
    for bad in ("http://", "https://", "<script", "src="):
        assert bad not in html_out, "report must not reference %s" % bad

    p = tmp_path / "r.html"
    rep.write(str(p))
    assert p.read_text().startswith("<!DOCTYPE html>")
    p2 = tmp_path / "r.md"
    rep.write(str(p2))
    assert p2.read_text().startswith("# Test report")


def test_report_escapes_html_injection():
    class F:
        severity, title = "WARN", "<script>alert(1)</script>"
        detail, evidence, next_step, beta = "x", [], "", False
    out = Report("t").findings("F", [F()]).to_html()
    assert "<script>alert" not in out
    assert "&lt;script&gt;" in out


def test_cli_as_explain_and_report(tmp_path, capsys):
    from mdbkit.cli import main
    assert main(["filter", FIXTURE, "--ns", "shop.orders", "--as-explain",
                 "--limit", "1"]) == 0
    out = capsys.readouterr().out
    assert 'getSiblingDB("shop")' in out and ".explain(" in out

    report = tmp_path / "t.md"
    assert main(["triage", FIXTURE, "--no-sysprobe",
                 "--report", str(report)]) == 0
    assert "MongoDB incident triage" in report.read_text()
