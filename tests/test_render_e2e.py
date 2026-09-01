"""End-to-end rendering tests.

A user hit `AttributeError: 'int' object has no attribute 'lower'` running
`mdbkit ftdc summary` on a real diagnostic.data. The cause was swapped
arguments in `render_ftdc_summary`, and 139 tests missed it because every
FTDC test stopped at the decoder and never rendered anything.

So these tests deliberately run each command the way a user does — through
the CLI, all the way to printed output — rather than testing internals. The
decoder was the hard part and it was thoroughly covered; the bug was in the
formatting nobody thought worth testing.
"""

import json
import struct
import zlib

import pytest

from mdbkit.cli import main
from mdbkit.demo import DemoLog, write_extras
from mdbkit.render import _human

from test_ftdc import enc_doc, rle_varint


# --------------------------------------------------------------- fixtures ---

def make_ftdc(tmp_path, samples=4):
    """A small but structurally realistic diagnostic.data directory."""
    from mdbkit.ftdc import numeric_metrics, parse_document
    ref = enc_doc([
        ("start", 0),
        ("serverStatus", {
            "connections": {"current": 42, "available": 900,
                            "totalCreated": 5000},
            "opcounters": {"query": 1000, "insert": 50, "update": 7,
                           "delete": 2, "getmore": 9, "command": 300},
            "globalLock": {"currentQueue": {"readers": 0, "writers": 0}},
            "wiredTiger": {
                "cache": {"bytes currently in the cache": 2000000000,
                          "maximum bytes configured": 8589934592,
                          "tracked dirty bytes in the cache": 100000000},
                "transaction": {
                    "transaction checkpoint most recent time (msecs)": 900},
            },
            "mem": {"resident": 4096, "virtual": 9000},
        }),
        ("systemMetrics", {
            "cpu": {"user_ms": 1000, "system_ms": 500, "iowait_ms": 20},
            "disks": {"sda": {"io_time_ms": 100, "reads": 40, "writes": 12,
                              "read_time_ms": 80, "write_time_ms": 30}},
        }),
    ])
    parsed, _ = parse_document(ref)
    leaves = numeric_metrics(parsed)
    deltas = [1] * (len(leaves) * (samples - 1))
    payload = ref + struct.pack("<II", len(leaves), samples - 1) \
        + rle_varint(deltas)
    blob = struct.pack("<I", len(payload)) + zlib.compress(payload)
    d = tmp_path / "diagnostic.data"
    d.mkdir()
    (d / "metrics.2026-08-29T00-00-00Z-00000").write_bytes(
        enc_doc([("_id", 1788000000000), ("type", 1), ("data", blob)]))
    return str(d)


@pytest.fixture
def demo_log(tmp_path):
    p = tmp_path / "demo.log"
    p.write_text("\n".join(DemoLog(minutes=30).build()) + "\n")
    return str(p)


# ------------------------------------------------------- the reported bug ---

def _row(out, label):
    """The cells of one metric row, label stripped off the front."""
    for line in out.splitlines():
        if line.startswith(label + " ") or line.strip() == label:
            return line[len(label):].split()
    raise AssertionError("no row for %s in:\n%s" % (label, out))


def test_ftdc_summary_renders(tmp_path, capsys):
    """The exact command that crashed for a user."""
    path = make_ftdc(tmp_path)
    assert main(["ftdc", "summary", path, "--all"]) == 0
    out = capsys.readouterr().out
    assert "mdbkit ftdc summary" in out
    assert "cache.usedBytes" in out
    assert "conns.current" in out


def test_ftdc_summary_every_cell_is_a_value_not_a_label(tmp_path, capsys):
    """Swapping _human's arguments does not crash now that it is hardened,
    it silently prints the metric name where a number belongs. So assert on
    the cells, not just on the absence of an exception."""
    path = make_ftdc(tmp_path)
    main(["ftdc", "summary", path, "--all"])
    out = capsys.readouterr().out

    cache = _row(out, "cache.usedBytes")
    assert cache[:2] == ["1.9", "GiB"], cache      # min column, formatted
    assert cache.count("GiB") == 4                 # min, avg, max, last

    conns = _row(out, "conns.current")
    assert conns[0] == "42", conns                 # a number, not a label
    for cell in conns:
        assert "conns" not in cell
        assert "GiB" not in cell


def test_ftdc_summary_separates_gauges_from_counters(tmp_path, capsys):
    path = make_ftdc(tmp_path)
    main(["ftdc", "summary", path, "--all"])
    out = capsys.readouterr().out
    assert "current values" in out
    assert "cumulative since server start" in out
    assert "change in window" in out


def test_ftdc_summary_does_not_rescale_non_byte_metrics(tmp_path, capsys):
    """conns.current is a count, not bytes; it must never read as GiB."""
    path = make_ftdc(tmp_path)
    main(["ftdc", "summary", path, "--all"])
    for line in capsys.readouterr().out.splitlines():
        if line.startswith("conns.current"):
            assert "GiB" not in line and "MiB" not in line
            break
    else:
        pytest.fail("conns.current row missing")


def test_ftdc_timeline_and_export_render(tmp_path, capsys):
    path = make_ftdc(tmp_path)
    assert main(["ftdc", "timeline", path, "--all",
                 "--metric", "conns.current"]) == 0
    assert "conns.current" in capsys.readouterr().out
    assert main(["ftdc", "export", path, "--all",
                 "--metric", "conns.current"]) == 0
    assert capsys.readouterr().out.strip()


def test_ftdc_json_is_valid(tmp_path, capsys):
    path = make_ftdc(tmp_path)
    assert main(["ftdc", "summary", path, "--all", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["chunks"] >= 1
    assert "series" in data


# ------------------------------------------- _human, the function that broke ---

def test_human_argument_order_and_tolerance():
    # value first, label second — the order that was swapped
    assert "GiB" in _human(2 * 1024 ** 3, "cache.usedBytes")
    assert "GiB" not in _human(42, "conns.current")
    # a display helper must never raise, whatever it is handed
    assert _human(None) == "-"
    assert _human("already a string") == "already a string"
    assert _human(0, "") == "0"
    assert _human(1234.5, None)


# ------------------------------------ every other command renders end to end ---

def test_all_commands_render(tmp_path, demo_log, capsys):
    """Run each command through the CLI to printed output. This is the test
    that would have caught the ftdc crash."""
    write_extras(str(tmp_path))
    idx = str(tmp_path / "indexes.json")
    sch = str(tmp_path / "schema.json")
    exp = str(tmp_path / "explain.json")

    commands = [
        ["loginfo", demo_log],
        ["queries", demo_log],
        ["queries", demo_log, "--shape", "1"],
        ["connections", demo_log],
        ["advise", demo_log, "--indexes", idx, "--schema", sch],
        ["explain", exp],
        ["triage", demo_log, "--window", "0", "--no-sysprobe"],
        ["filter", demo_log, "--severity", "E"],
        ["compare", demo_log, "--after", demo_log],
        ["export-script", "indexes"],
        ["export-script", "schema"],
        ["export-script", "serverstatus"],
    ]
    for argv in commands:
        assert main(argv) == 0, argv
        out = capsys.readouterr().out
        assert out.strip(), "%s printed nothing" % argv


def test_all_json_modes_are_valid_json(tmp_path, demo_log, capsys):
    write_extras(str(tmp_path))
    for argv in (
        ["loginfo", demo_log, "--json"],
        ["queries", demo_log, "--json"],
        ["connections", demo_log, "--json"],
        ["advise", demo_log, "--indexes", str(tmp_path / "indexes.json"),
         "--json"],
        ["explain", str(tmp_path / "explain.json"), "--json"],
        ["triage", demo_log, "--window", "0", "--no-sysprobe", "--json"],
        ["compare", demo_log, "--after", demo_log, "--json"],
    ):
        assert main(argv) == 0, argv
        json.loads(capsys.readouterr().out)


def test_reports_render(tmp_path, demo_log, capsys):
    for ext in ("md", "html"):
        out = tmp_path / ("r." + ext)
        assert main(["triage", demo_log, "--window", "0", "--no-sysprobe",
                     "--report", str(out)]) == 0
        assert out.exists() and out.stat().st_size > 200
        capsys.readouterr()


def test_serverstatus_and_oslog_render(tmp_path, capsys):
    ss = tmp_path / "s.json"
    ss.write_text(json.dumps({
        "host": "h", "version": "7.0", "uptime": 3600,
        "connections": {"current": 5, "available": 100, "totalCreated": 9},
        "opcounters": {"query": 10, "insert": 2},
        "wiredTiger": {"cache": {"bytes currently in the cache": 1 << 20,
                                 "maximum bytes configured": 1 << 30}},
        "mem": {"resident": 100}}))
    assert main(["serverstatus", str(ss)]) == 0
    assert "mdbkit serverstatus" in capsys.readouterr().out

    sl = tmp_path / "syslog"
    sl.write_text("Aug 29 09:14:03 h kernel: Out of memory: "
                  "Killed process 1 (mongod)\n")
    assert main(["oslog", str(sl)]) == 0
    assert "oom-kill" in capsys.readouterr().out
