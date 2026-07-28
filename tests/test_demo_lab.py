"""Tests for `mdbkit demo` and `mdbkit lab`.

The lab tests deliberately avoid starting a real mongod: they cover the
safety model (never touch a directory we did not create) and the state
handling, which is where a mistake would actually hurt someone.
"""

import json
import os

import pytest

from mdbkit import lab
from mdbkit.analysis import QueryAggregator, SummaryAggregator
from mdbkit.demo import DemoLog, write_extras
from mdbkit.parser import ParseStats, iter_entries, parse_line
from mdbkit.triage import run_triage


# ------------------------------------------------------------------ demo ---

def build(tmp_path, **kw):
    lines = DemoLog(**kw).build()
    p = tmp_path / "demo.log"
    p.write_text("\n".join(lines) + "\n")
    return str(p), lines


def test_every_line_is_valid_logv2(tmp_path):
    _p, lines = build(tmp_path, minutes=30)
    assert len(lines) > 100
    for ln in lines:
        entry = parse_line(ln)
        assert entry is not None, ln[:120]
        assert entry.ts is not None


def test_demo_is_deterministic(tmp_path):
    a = DemoLog(seed=42, minutes=20).build()
    b = DemoLog(seed=42, minutes=20).build()
    assert a == b
    c = DemoLog(seed=43, minutes=20).build()
    assert a != c


def test_lines_are_chronological(tmp_path):
    path, _ = build(tmp_path, minutes=45)
    times = [e.ts for e in iter_entries(path) if e.ts]
    assert times == sorted(times)


def test_incident_scenario_has_the_full_story(tmp_path):
    path, _ = build(tmp_path, scenario="incident", minutes=60)
    findings, _stats, _cut = run_triage(path, window_min=0, no_sysprobe=True)
    titles = {f.title: f for f in findings}
    assert titles["Replica set instability"].severity == "CRIT"
    assert "Connection storm" in titles
    assert "Index build activity" in titles
    assert "Collection scans" in titles
    assert "Error-severity log lines" in titles


def test_healthy_scenario_is_quiet(tmp_path):
    path, _ = build(tmp_path, scenario="healthy", minutes=60)
    findings, _s, _c = run_triage(path, window_min=0, no_sysprobe=True)
    titles = {f.title: f for f in findings}
    assert titles["Replica set"].severity == "OK"
    assert "Connection storm" not in titles


def test_demo_produces_advisable_shapes(tmp_path):
    path, _ = build(tmp_path, minutes=60)
    agg = QueryAggregator()
    for e in iter_entries(path):
        agg.consume(e)
    shapes = agg.results()
    assert len(shapes) >= 4
    assert any(s.collscan for s in shapes)
    assert any(not s.collscan for s in shapes)     # a healthy one too
    assert any(s.in_memory_sort for s in shapes)

    from mdbkit.advisor import advise
    recs = advise(shapes)
    assert any(r.confidence == "high" for r in recs)


def test_summary_counts_are_sane(tmp_path):
    path, _ = build(tmp_path, minutes=60)
    stats = ParseStats()
    agg = SummaryAggregator()
    for e in iter_entries(path, stats):
        agg.consume(e)
    assert stats.unparsed == 0
    assert agg.summary.versions == ["7.0.14"]
    assert agg.summary.startups == 1
    assert agg.summary.slow_queries > 50
    assert agg.summary.connections_accepted > 10


def test_bad_scenario_rejected():
    with pytest.raises(ValueError):
        DemoLog(scenario="chaos")


def test_extras_are_usable(tmp_path):
    paths = write_extras(str(tmp_path))
    assert len(paths) == 3
    from mdbkit.advisor import load_indexes, load_schema
    from mdbkit.explain import analyze_explain, load_explain
    idx = load_indexes(str(tmp_path / "indexes.json"))
    assert "shop.orders" in idx
    sch = load_schema(str(tmp_path / "schema.json"))
    assert "shop.orders" in sch
    report = analyze_explain(load_explain(str(tmp_path / "explain.json")))
    assert report.collscan is True
    assert report.recommendation is not None


def test_cli_demo(tmp_path, capsys):
    from mdbkit.cli import main
    out = tmp_path / "d.log"
    assert main(["demo", "--minutes", "10", "-o", str(out),
                 "--with-extras"]) == 0
    assert out.exists()
    for name in ("indexes.json", "schema.json", "explain.json"):
        assert (tmp_path / name).exists()
    assert main(["loginfo", str(out)]) == 0


# ------------------------------------------------------------------- lab ---

def test_destroy_refuses_directory_it_did_not_create(tmp_path):
    victim = tmp_path / "precious"
    victim.mkdir()
    (victim / "data.txt").write_text("do not delete me")
    with pytest.raises(lab.LabError) as exc:
        lab.destroy(str(victim))
    assert "marker" in str(exc.value)
    assert (victim / "data.txt").exists()


def test_destroy_removes_a_real_lab(tmp_path):
    d = tmp_path / "lab"
    d.mkdir()
    lab.save_state(str(d), {"createdAt": "now", "dir": str(d),
                            "replicaSet": None, "nodes": []})
    lab.destroy(str(d), echo=lambda *a: None)
    assert not d.exists()


def test_start_refuses_foreign_non_empty_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(lab, "require_mongod", lambda: "/usr/bin/mongod")
    d = tmp_path / "someone-elses-data"
    d.mkdir()
    (d / "WiredTiger") .write_text("x")
    with pytest.raises(lab.LabError) as exc:
        lab.start(directory=str(d), echo=lambda *a: None)
    assert "not created by mdbkit" in str(exc.value)
    assert (d / "WiredTiger").exists()


def test_missing_mongod_message_points_at_demo(monkeypatch):
    monkeypatch.setattr(lab, "find_binary", lambda name: None)
    with pytest.raises(lab.LabError) as exc:
        lab.require_mongod()
    assert "mdbkit demo" in str(exc.value)


def test_default_port_is_far_from_production():
    assert lab.DEFAULT_BASE_PORT >= 28100
    assert lab.DEFAULT_BASE_PORT not in (27017, 27018, 27019, 28017)


def test_connection_string_shapes():
    rs = {"replicaSet": "mdbkitlab",
          "nodes": [{"port": 28110}, {"port": 28111}]}
    cs = lab.connection_string(rs)
    assert cs.startswith("mongodb://127.0.0.1:28110,127.0.0.1:28111/")
    assert "replicaSet=mdbkitlab" in cs
    single = {"replicaSet": None, "nodes": [{"port": 28110}]}
    assert lab.connection_string(single) == "mongodb://127.0.0.1:28110/"


def test_seed_script_is_scoped_and_reasonable():
    script = lab.seed_script(1000)
    assert 'getSiblingDB("shop")' in script
    assert "1000" in script
    # the workload must contain both healthy and deliberately bad patterns
    assert "createIndex" in script
    assert "sort({createdAt: -1})" in script
    for forbidden in ("dropDatabase", "shutdown", "27017"):
        assert forbidden not in script


def test_state_roundtrip(tmp_path):
    d = str(tmp_path)
    assert lab.load_state(d) is None
    lab.save_state(d, {"dir": d, "nodes": [{"index": 0, "port": 28110}]})
    got = lab.load_state(d)
    assert got["nodes"][0]["port"] == 28110
    assert os.path.exists(lab.state_path(d))


def test_status_of_missing_lab(tmp_path):
    assert lab.status(str(tmp_path / "nope")) is None


def test_stop_without_lab_raises(tmp_path):
    with pytest.raises(lab.LabError):
        lab.stop(str(tmp_path / "nope"))


def test_cli_lab_destroy_requires_confirmation(tmp_path, capsys):
    from mdbkit.cli import main
    d = tmp_path / "lab"
    d.mkdir()
    lab.save_state(str(d), {"dir": str(d), "nodes": []})
    assert main(["lab", "destroy", "--dir", str(d)]) == 1
    assert d.exists()                       # nothing removed without --yes
    assert main(["lab", "destroy", "--dir", str(d), "--yes"]) == 0
    assert not d.exists()
