"""Tests for the FTDC decoder.

The codec is validated against the worked example in MongoDB's own
src/mongo/db/ftdc/README.md, and a synthetic archive is round-tripped
through a miniature encoder written here (an independent implementation
of the spec — if decoder and encoder agree on the spec's example, the
decoder is reading real files correctly).
"""

import struct
import zlib

import pytest

from mdbkit.ftdc import (
    Chunk,
    FtdcReader,
    decode_chunk,
    decode_metrics,
    numeric_metrics,
    parse_document,
    read_varint,
)


# ------------------------------------------------------- tiny BSON encoder --

def enc_cstr(s):
    return s.encode() + b"\0"


def enc_doc(pairs):
    """pairs: list of (name, python value). Supports int, float, bool, str, dict."""
    body = b""
    for name, val in pairs:
        if isinstance(val, bool):
            body += b"\x08" + enc_cstr(name) + (b"\x01" if val else b"\x00")
        elif isinstance(val, int):
            if -2**31 <= val < 2**31:
                body += b"\x10" + enc_cstr(name) + struct.pack("<i", val)
            else:
                body += b"\x12" + enc_cstr(name) + struct.pack("<q", val)
        elif isinstance(val, float):
            body += b"\x01" + enc_cstr(name) + struct.pack("<d", val)
        elif isinstance(val, str):
            raw = val.encode() + b"\0"
            body += b"\x02" + enc_cstr(name) + struct.pack("<i", len(raw)) + raw
        elif isinstance(val, dict):
            sub = enc_doc(list(val.items()))
            body += b"\x03" + enc_cstr(name) + sub
        elif isinstance(val, bytes):
            body += (b"\x05" + enc_cstr(name) + struct.pack("<i", len(val))
                     + b"\x00" + val)
        else:
            raise TypeError(name)
    return struct.pack("<i", len(body) + 5) + body + b"\x00"


def enc_varint(n):
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            out += bytes([b])
            return out


def rle_varint(deltas):
    """Zero run-length encode then varint, per the spec."""
    out = b""
    i = 0
    while i < len(deltas):
        d = deltas[i]
        if d == 0:
            run = 0
            j = i + 1
            while j < len(deltas) and deltas[j] == 0:
                run += 1
                j += 1
            out += enc_varint(0) + enc_varint(run)
            i = j
        else:
            out += enc_varint(d)
            i += 1
    return out


# ------------------------------------------------------------- unit tests --

def test_varint_roundtrip():
    for n in (0, 1, 127, 128, 300, 2**20, 2**35):
        val, pos = read_varint(enc_varint(n), 0)
        assert val == n
        assert pos == len(enc_varint(n))


def test_spec_walkthrough_example():
    """MongoDB's README walkthrough:

        {"a":1,"x":2,"s":"t"}  (reference)
        {"a":2,"x":2,"s":"t"}
        {"a":3,"x":2,"s":"t"}
        {"a":4,"x":2,"s":"t"}

    metrics array  -> [2, 3, 4, 2, 2, 2]   (column-major: a then x)
    delta encoded  -> [1, 1, 1, 0, 0, 0]
    zero RLE       -> [1, 1, 1, 0, 2]
    """
    encoded = rle_varint([1, 1, 1, 0, 0, 0])
    assert encoded == enc_varint(1) * 3 + enc_varint(0) + enc_varint(2)

    rows = decode_metrics(encoded, metric_count=2, sample_count=3,
                          base=[1, 2])
    assert rows[0] == [2, 3, 4]   # 'a' climbs
    assert rows[1] == [2, 2, 2]   # 'x' unchanged (the RLE run)


def test_numeric_metrics_order_and_types():
    doc, _ = parse_document(enc_doc([
        ("a", 1),
        ("s", "ignored"),
        ("sub", {"b": 2, "c": True}),
        ("d", 3.9),
    ]))
    leaves = numeric_metrics(doc)
    assert [p for p, _ in leaves] == ["a", "sub.b", "sub.c", "d"]
    assert [v for _, v in leaves] == [1, 2, 1, 3]  # bool->1, double truncated


def test_zero_run_spanning_metric_boundary():
    """A zero run can continue across the boundary between two metrics."""
    deltas = [0, 0, 0, 0, 5, 0]          # metric0: 0,0,0  metric1: 0,5,0
    rows = decode_metrics(rle_varint(deltas), metric_count=2, sample_count=3,
                          base=[10, 20])
    assert rows[0] == [10, 10, 10]
    assert rows[1] == [20, 25, 25]


def test_negative_delta_wraps_correctly():
    """Values are uint64; a decreasing counter encodes as a wrapped delta."""
    mask = (1 << 64) - 1
    deltas = [(-3) & mask]
    rows = decode_metrics(rle_varint(deltas), metric_count=1, sample_count=1,
                          base=[10])
    assert rows[0] == [7]


# --------------------------------------------------- full file round-trip --

def build_ftdc_file(tmp_path, samples):
    """Build a real-shaped FTDC archive from a list of flat metric dicts."""
    ref = samples[0]
    ref_doc = enc_doc([
        ("start", 0),
        ("serverStatus", {
            "opcounters": {"insert": ref["insert"], "query": ref["query"]},
            "connections": {"current": ref["current"], "available": 999},
        }),
    ])
    ref_parsed, _ = parse_document(ref_doc)
    leaves = numeric_metrics(ref_parsed)
    paths = [p for p, _ in leaves]
    base = [v for _, v in leaves]

    def value_for(path, s):
        if path.endswith("opcounters.insert"):
            return s["insert"]
        if path.endswith("opcounters.query"):
            return s["query"]
        if path.endswith("connections.current"):
            return s["current"]
        if path.endswith("connections.available"):
            return 999
        return 0

    metric_count = len(paths)
    sample_count = len(samples) - 1
    deltas = []
    for m, path in enumerate(paths):
        prev = base[m]
        for s in samples[1:]:
            cur = value_for(path, s)
            deltas.append((cur - prev) & ((1 << 64) - 1))
            prev = cur

    payload = (ref_doc + struct.pack("<II", metric_count, sample_count)
               + rle_varint(deltas))
    blob = struct.pack("<I", len(payload)) + zlib.compress(payload)
    doc = enc_doc([("_id", 1700000000000), ("type", 1), ("data", blob)])

    path = tmp_path / "metrics.2026-07-01T00-00-00Z-00000"
    path.write_bytes(doc)
    return str(path)


def test_full_file_roundtrip(tmp_path):
    samples = [
        {"insert": 100, "query": 500, "current": 10},
        {"insert": 110, "query": 505, "current": 12},
        {"insert": 125, "query": 505, "current": 12},
        {"insert": 140, "query": 600, "current": 9},
    ]
    path = build_ftdc_file(tmp_path, samples)

    reader = FtdcReader().read(path)
    assert reader.chunks == 1
    assert reader.errors == 0
    assert reader.samples == 4          # 3 deltas + the reference sample

    ins = reader.series["ops.insert"]
    # reference sample is prepended, so all four values are present
    assert ins.values == [100, 110, 125, 140]
    conns = reader.series["conns.current"]
    assert conns.values == [10, 12, 12, 9]

    s = reader.summary()
    assert s["series"]["conns.current"]["max"] == 12
    assert s["series"]["conns.current"]["last"] == 9
    assert "perSecond" in s["series"]["ops.insert"]


def test_corrupt_chunk_is_skipped_not_fatal(tmp_path):
    good = build_ftdc_file(tmp_path, [
        {"insert": 1, "query": 1, "current": 1},
        {"insert": 2, "query": 1, "current": 1},
    ])
    with open(good, "ab") as fh:
        fh.write(enc_doc([("_id", 1700000001000), ("type", 1),
                          ("data", b"\x10\x00\x00\x00garbagegarbage")]))
    reader = FtdcReader().read(good)
    assert reader.chunks == 1      # the good one
    assert reader.errors == 1      # the corrupt one counted, not raised


def test_directory_and_interim_ordering(tmp_path):
    from mdbkit.ftdc import ftdc_files
    d = tmp_path / "diagnostic.data"
    d.mkdir()
    for name in ("metrics.2026-07-01T00-00-00Z-00000",
                 "metrics.2026-07-02T00-00-00Z-00000",
                 "metrics.interim", "some-other-file"):
        (d / name).write_bytes(b"")
    files = [f.rsplit("/", 1)[-1] for f in ftdc_files(str(d))]
    assert files == ["metrics.2026-07-01T00-00-00Z-00000",
                     "metrics.2026-07-02T00-00-00Z-00000",
                     "metrics.interim"]


def test_empty_and_missing_paths():
    from mdbkit.ftdc import ftdc_files
    assert ftdc_files("/nonexistent/path") == []


def test_truncated_file_stops_cleanly(tmp_path):
    path = build_ftdc_file(tmp_path, [
        {"insert": 1, "query": 1, "current": 1},
        {"insert": 2, "query": 2, "current": 2},
    ])
    data = open(path, "rb").read()
    truncated = tmp_path / "metrics.truncated-00000"
    truncated.write_bytes(data[:len(data) // 2])
    reader = FtdcReader().read(str(truncated))
    assert reader.chunks == 0   # nothing usable, but no exception


# --------------------------------------------- 0.2.1 performance tests ----

def test_selective_decode_matches_full_decode():
    """Skipping unwanted columns must not shift the stream: the values of
    the columns we do keep have to be identical either way."""
    from mdbkit.ftdc import decode_selective
    deltas = [3, 0, 0, 5,   0, 0, 0, 0,   7, 2, 0, 1]  # 3 metrics x 4 samples
    buf = rle_varint(deltas)
    base = [10, 20, 30]
    full = decode_metrics(buf, 3, 4, base)
    part = decode_selective(buf, 3, 4, base, {2})
    assert part[2] == full[2]
    part0 = decode_selective(buf, 3, 4, base, {0})
    assert part0[0] == full[0]


def test_chunk_timestamp_without_decompressing(tmp_path):
    from mdbkit.ftdc import chunk_timestamp, iter_documents
    path = build_ftdc_file(tmp_path, [
        {"insert": 1, "query": 1, "current": 1},
        {"insert": 2, "query": 2, "current": 2},
    ])
    docs = [d for d in iter_documents(path) if d.get("type") == 1]
    assert docs
    ts = chunk_timestamp(docs[0])
    assert ts is not None and ts.year >= 2023


def test_time_window_skips_chunks_before_decode(tmp_path):
    from datetime import timedelta
    path = build_ftdc_file(tmp_path, [
        {"insert": 1, "query": 1, "current": 1},
        {"insert": 2, "query": 2, "current": 2},
    ])
    r = FtdcReader(keep_values=False).read(path)
    assert r.chunks == 1 and r.skipped == 0
    far_future = r.last_ts + timedelta(days=365)
    r2 = FtdcReader(keep_values=False).read(path, ts_from=far_future)
    assert r2.chunks == 0 and r2.skipped == 1


def test_streaming_stats_without_keeping_values(tmp_path):
    path = build_ftdc_file(tmp_path, [
        {"insert": 100, "query": 500, "current": 10},
        {"insert": 110, "query": 505, "current": 12},
        {"insert": 125, "query": 505, "current": 9},
    ])
    r = FtdcReader(keep_values=False).read(path)
    s = r.series["conns.current"]
    assert s.values == []          # nothing retained
    assert s.vmin == 9 and s.vmax == 12 and s.last == 9
    assert r.summary()["series"]["conns.current"]["max"] == 12


def test_curated_covers_the_beta_detector_metrics():
    """Checkpoint duration, application-thread eviction and flow control are
    not reliably present in the mongod log (checkpoint timing is LOGV2_DEBUG
    level 4 on modern versions), so FTDC must carry them."""
    from mdbkit.ftdc import CURATED
    labels = {lb for lb, _p, _k in CURATED}
    for needed in ("checkpoint.lastMs", "evict.appThreadPages",
                   "flowControl.isLagged", "flowControl.waitMicros"):
        assert needed in labels, needed
    paths = {p for _lb, p, _k in CURATED}
    assert any("transaction checkpoint most recent time" in p for p in paths)
    assert any("pages evicted by application threads" in p for p in paths)
    assert any(p.startswith("serverStatus.flowControl.") for p in paths)


def test_ftdc_findings_flag_checkpoint_and_flow_control(tmp_path):
    """A chunk carrying an 80s checkpoint and an engaged flow control must
    produce WARN findings without any log at all."""
    import struct, zlib
    from mdbkit.triage import ftdc_findings

    ref = enc_doc([
        ("start", 0),
        ("serverStatus", {
            "wiredTiger": {
                "transaction": {
                    "transaction checkpoint most recent time (msecs)": 80000},
                "cache": {"pages evicted by application threads": 0},
            },
            "flowControl": {"isLagged": 1, "timeAcquiringMicros": 0},
        }),
    ])
    parsed, _ = parse_document(ref)
    leaves = numeric_metrics(parsed)
    base = [v for _, v in leaves]
    deltas = []
    for m, (_path, _v) in enumerate(leaves):
        for _ in range(2):
            deltas.append(0)
    payload = ref + struct.pack("<II", len(leaves), 2) + rle_varint(deltas)
    blob = struct.pack("<I", len(payload)) + zlib.compress(payload)
    d = tmp_path / "diagnostic.data"
    d.mkdir()
    (d / "metrics.2026-07-01T00-00-00Z-00000").write_bytes(
        enc_doc([("_id", 1782000000000), ("type", 1), ("data", blob)]))

    findings = ftdc_findings(str(d))
    titles = {f.title: f for f in findings}
    assert "Checkpoints (FTDC)" in titles
    assert titles["Checkpoints (FTDC)"].severity == "WARN"
    assert "80.0s" in titles["Checkpoints (FTDC)"].detail
    assert titles["Flow control engaged (FTDC)"].severity == "WARN"
