"""Tests for mdbkit: parser, analysis, and advisor against a realistic fixture."""

import json
import os

import pytest

from mdbkit.advisor import advise, load_indexes, load_schema
from mdbkit.analysis import (
    ConnectionAggregator,
    QueryAggregator,
    SummaryAggregator,
    extract_shape,
)
from mdbkit.parser import ParseStats, iter_entries, parse_line

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "sample_mongod.log")


def load_all():
    stats = ParseStats()
    entries = list(iter_entries(FIXTURE, stats))
    return entries, stats


# ---------------------------------------------------------------- parser ----

def test_parse_counts():
    entries, stats = load_all()
    assert stats.parsed == len(entries)
    assert stats.unparsed == 2  # plain-text line + truncated JSON line
    assert stats.parsed >= 14
    assert stats.first_ts is not None and stats.last_ts is not None
    assert stats.first_ts < stats.last_ts


def test_parse_line_rejects_garbage():
    assert parse_line("not json at all") is None
    assert parse_line('{"unrelated": true}') is None
    assert parse_line("") is None


def test_gzip_and_stdin_markers_exist():
    # open_log is exercised via iter_entries; here just ensure .gz sniffing
    # doesn't crash on a normal file (magic-byte probe path).
    entries, _ = load_all()
    assert entries


# -------------------------------------------------------------- analysis ----

def test_summary():
    entries, _ = load_all()
    agg = SummaryAggregator()
    for e in entries:
        agg.consume(e)
    s = agg.summary
    assert s.versions == ["7.0.14"]
    assert s.startups == 1
    assert s.connections_accepted == 2
    assert s.slow_queries == 6
    assert s.slowest_ms == 8420
    assert s.warnings == 1
    assert s.errors == 1


def test_query_shapes_group_by_shape_not_values():
    entries, _ = load_all()
    agg = QueryAggregator()
    for e in entries:
        agg.consume(e)
    results = agg.results()
    # Two find-orders queries differ only in literal values -> one shape.
    orders = [r for r in results if r.shape.ns == "shop.orders"]
    assert len(orders) == 1
    assert orders[0].count == 2
    assert orders[0].collscan is True
    assert orders[0].in_memory_sort is True
    assert orders[0].shape.sort_fields == (("createdAt", -1),)


def test_write_and_aggregate_shapes():
    entries, _ = load_all()
    agg = QueryAggregator()
    for e in entries:
        agg.consume(e)
    by_ns = {}
    for r in agg.results():
        by_ns.setdefault(r.shape.ns, []).append(r)
    assert "shop.products" in by_ns  # WRITE update captured
    assert by_ns["shop.products"][0].shape.operation == "update"
    assert "shop.events" in by_ns  # aggregate + getMore
    ops = {r.shape.operation for r in by_ns["shop.events"]}
    assert "aggregate" in ops
    assert any(op.startswith("getMore") for op in ops)


def test_connections():
    entries, _ = load_all()
    agg = ConnectionAggregator()
    for e in entries:
        agg.consume(e)
    d = agg.report.to_dict()
    assert d["totalAccepted"] == 2
    assert d["totalEnded"] == 1
    assert d["appNames"]["OrderService"] == 1
    assert d["drivers"]["PyMongo"] == 1


# --------------------------------------------------------------- advisor ----

def test_advisor_esr_candidate(tmp_path):
    entries, _ = load_all()
    agg = QueryAggregator()
    for e in entries:
        agg.consume(e)
    recs = advise(agg.results())
    by_ns = {r.ns: r for r in recs}

    # find on shop.orders: equality(status) then sort(createdAt desc) -> ESR
    orders = by_ns["shop.orders"]
    assert orders.candidate == [("status", 1), ("createdAt", -1)]
    assert orders.confidence == "high"  # COLLSCAN + huge scan ratio

    # aggregate on shop.events: equality(tenantId) then sort(ts desc)
    events = by_ns["shop.events"]
    assert events.candidate[0] == ("tenantId", 1)
    assert ("ts", -1) in events.candidate

    # update on shop.products: single equality field
    products = by_ns["shop.products"]
    assert products.candidate == [("sku", 1)]

    # well-behaved IXSCAN query must NOT generate a recommendation
    assert "shop.users" not in by_ns


def test_advisor_existing_index_coverage(tmp_path):
    entries, _ = load_all()
    agg = QueryAggregator()
    for e in entries:
        agg.consume(e)

    idx_file = tmp_path / "indexes.json"
    idx_file.write_text(json.dumps({
        "db": "shop",
        "collections": {
            "orders": [
                {"v": 2, "key": {"_id": 1}, "name": "_id_"},
                {"v": 2, "key": {"status": 1, "createdAt": -1}, "name": "status_1_createdAt_-1"},
            ]
        },
    }))
    indexes = load_indexes(str(idx_file))
    recs = advise(agg.results(), indexes=indexes)
    orders = next(r for r in recs if r.ns == "shop.orders")
    assert orders.covered_by == "status_1_createdAt_-1"


def test_advisor_schema_caveats(tmp_path):
    entries, _ = load_all()
    agg = QueryAggregator()
    for e in entries:
        agg.consume(e)

    schema_file = tmp_path / "schema.json"
    schema_file.write_text(json.dumps({
        "db": "shop",
        "collections": {
            "orders": {"sampleSize": 100, "fields": {
                "status": {"types": ["string"], "presence": 1.0},
                "createdAt": {"types": ["date"], "presence": 1.0},
            }},
            "products": {"sampleSize": 100, "fields": {
                # 'sku' deliberately missing -> typo caveat expected
                "name": {"types": ["string"], "presence": 1.0},
            }},
        },
    }))
    schema = load_schema(str(schema_file))
    recs = advise(agg.results(), schema=schema)
    products = next(r for r in recs if r.ns == "shop.products")
    assert any("not seen in the sampled schema" in c for c in products.caveats)


def test_shape_extraction_handles_or_and_regex():
    line = json.dumps({
        "t": {"$date": "2026-07-01T09:00:00.000+00:00"}, "s": "I", "c": "COMMAND",
        "id": 51803, "ctx": "conn1", "msg": "Slow query",
        "attr": {"type": "command", "ns": "app.docs", "command": {
            "find": "docs",
            "filter": {"$or": [{"a": 1}, {"b": {"$regex": "^x"}}]},
            "$db": "app",
        }, "planSummary": "COLLSCAN", "docsExamined": 5000, "nreturned": 2,
            "durationMillis": 300},
    })
    entry = parse_line(line)
    shape = extract_shape(entry)
    assert "$or" in shape.flags
    paths = {p for p, _ in shape.filter_fields}
    assert paths == {"a", "b"}


# ------------------------------------------------------------------- CLI ----

def test_cli_smoke(capsys):
    from mdbkit.cli import main
    assert main(["loginfo", FIXTURE]) == 0
    assert main(["queries", FIXTURE, "--json"]) == 0
    assert main(["connections", FIXTURE]) == 0
    assert main(["advise", FIXTURE]) == 0
    assert main(["export-script", "schema"]) == 0
    out = capsys.readouterr().out
    assert "mdbkit" in out
    assert "COLLSCAN" in out or "candidate" in out


# ------------------------------------------------- 0.1.1 additions ----

def test_parse_when_accepts_offsets_and_naive():
    from mdbkit.filtering import parse_when
    assert parse_when("2026-07-01T08:00:00+04:00").tzinfo is not None
    assert parse_when("2026-07-01T08:00:00Z").tzinfo is not None
    assert parse_when("2026-07-01T08:00:00").tzinfo is None
    assert parse_when("2026-07-01 08:00:00").hour == 8
    assert parse_when("2026-07-01").day == 1
    try:
        parse_when("not-a-date")
        assert False, "should raise"
    except ValueError as exc:
        assert "Could not parse" in str(exc)


def test_filter_naive_bound_does_not_crash():
    """Log timestamps carry +00:00; a naive bound must not raise TypeError."""
    from mdbkit.filtering import Filter, parse_when
    flt = Filter(ts_from=parse_when("2026-07-01T08:05:00"))
    matched = [e for e in iter_entries(FIXTURE) if flt.matches(e)]
    assert matched  # comparison worked
    assert all(e.ts.hour >= 8 for e in matched if e.ts)


def test_filter_limit_and_last(capsys):
    from mdbkit.cli import main
    assert main(["filter", FIXTURE, "--component", "COMMAND", "--limit", "2"]) == 0
    out = capsys.readouterr()
    assert len([l for l in out.out.splitlines() if l.strip()]) == 2
    assert "showing first 2" in out.err

    assert main(["filter", FIXTURE, "--component", "COMMAND", "--last", "2"]) == 0
    out2 = capsys.readouterr()
    assert len([l for l in out2.out.splitlines() if l.strip()]) == 2
    assert "most recent" in out2.err


def test_advise_summary_header_and_limit(capsys):
    from mdbkit.cli import main
    assert main(["advise", FIXTURE, "--limit", "1"]) == 0
    out = capsys.readouterr().out
    assert "candidate(s):" in out
    assert "showing top 1" in out


def test_advise_ns_filter(capsys):
    from mdbkit.cli import main
    assert main(["advise", FIXTURE, "--ns", "shop.products", "--json"]) == 0
    import json as _json
    data = _json.loads(capsys.readouterr().out)
    assert data and all(r["ns"] == "shop.products" for r in data)


def test_bool_field_lowers_confidence(tmp_path):
    """A boolean candidate field must reduce confidence, not stay flat."""
    from mdbkit.advisor import advise, load_schema
    agg = QueryAggregator()
    for e in iter_entries(FIXTURE):
        agg.consume(e)
    base = {r.ns: r for r in advise(agg.results())}
    schema_file = tmp_path / "schema.json"
    schema_file.write_text(json.dumps({
        "db": "shop",
        "collections": {"products": {"sampleSize": 100, "fields": {
            "sku": {"types": ["bool"], "presence": 1.0}}}},
    }))
    with_schema = {r.ns: r for r in advise(agg.results(),
                                            schema=load_schema(str(schema_file)))}
    assert base["shop.products"].confidence != with_schema["shop.products"].confidence
    assert any("boolean" in c for c in with_schema["shop.products"].caveats)
