"""FTDC (Full-Time Diagnostic Data Capture) decoder.

Every mongod continuously writes `<dbPath>/diagnostic.data/` — server counters
AND, on Linux, system metrics (CPU, memory, disks). It is the flight recorder
nobody but MongoDB support usually reads. This module decodes it offline.

Format (per MongoDB's own src/mongo/db/ftdc/README.md):

    archive file  = sequence of BSON documents
    metadata doc  = {_id: Date, type: 0, doc: {...}}
    metrics doc   = {_id: Date, type: 1, data: BinData}
    metadata delta= {_id: Date, type: 2, ...}          (skipped)

    data          = uint32 uncompressed_size + zlib(chunk)
    chunk         = reference BSON doc
                  + uint32 metric_count
                  + uint32 sample_count      (samples AFTER the reference)
                  + varint-encoded, zero-RLE, delta-encoded metrics,
                    stored column-major (all samples of metric 0, then
                    metric 1, ...)

Values are uint64; deltas wrap. Doubles are cast to integers by MongoDB
itself, so fractional parts are already lost in the file — not by us.

Pure stdlib (struct + zlib). No network. Read-only.
"""

from __future__ import annotations

import os
import struct
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterator, List, Optional, Tuple

MASK64 = (1 << 64) - 1
MAX_CHUNK_BYTES = 256 * 1024 * 1024  # refuse absurd/corrupt sizes


# --------------------------------------------------------------- BSON ------

class BsonError(ValueError):
    pass


def _cstring(buf: bytes, pos: int) -> Tuple[str, int]:
    end = buf.index(b"\0", pos)
    return buf[pos:end].decode("utf-8", "replace"), end + 1


def parse_document(buf: bytes, pos: int = 0, depth: int = 0) -> Tuple[dict, int]:
    """Parse one BSON document. Returns (dict, position after document)."""
    if depth > 64:
        raise BsonError("BSON nesting too deep")
    if pos + 4 > len(buf):
        raise BsonError("truncated document header")
    size = struct.unpack_from("<i", buf, pos)[0]
    if size < 5 or pos + size > len(buf):
        raise BsonError("bad document length %d" % size)
    end = pos + size
    out: dict = {}
    p = pos + 4
    while p < end - 1:
        etype = buf[p]
        p += 1
        name, p = _cstring(buf, p)
        if etype == 0x01:  # double
            (val,), p = struct.unpack_from("<d", buf, p), p + 8
        elif etype == 0x02 or etype == 0x0D or etype == 0x0E:  # string/code
            ln = struct.unpack_from("<i", buf, p)[0]
            val = buf[p + 4:p + 4 + ln - 1].decode("utf-8", "replace")
            p += 4 + ln
        elif etype in (0x03, 0x04):  # document / array
            val, p = parse_document(buf, p, depth + 1)
            if etype == 0x04:
                val = list(val.values())
        elif etype == 0x05:  # binary
            ln = struct.unpack_from("<i", buf, p)[0]
            subtype = buf[p + 4]
            val = buf[p + 5:p + 5 + ln]
            if subtype == 0x02:  # legacy wrapped length
                val = val[4:]
            p += 5 + ln
        elif etype == 0x06 or etype == 0x0A:  # undefined / null
            val = None
        elif etype == 0x07:  # objectid
            val, p = buf[p:p + 12].hex(), p + 12
        elif etype == 0x08:  # bool
            val, p = bool(buf[p]), p + 1
        elif etype == 0x09:  # UTC datetime (millis)
            (ms,), p = struct.unpack_from("<q", buf, p), p + 8
            val = ms
        elif etype == 0x0B:  # regex
            _pattern, p = _cstring(buf, p)
            _flags, p = _cstring(buf, p)
            val = None
        elif etype == 0x0C:  # dbpointer
            ln = struct.unpack_from("<i", buf, p)[0]
            p += 4 + ln + 12
            val = None
        elif etype == 0x0F:  # code with scope
            total = struct.unpack_from("<i", buf, p)[0]
            p += total
            val = None
        elif etype == 0x10:  # int32
            (val,), p = struct.unpack_from("<i", buf, p), p + 4
        elif etype == 0x11:  # timestamp: increment then seconds
            inc, sec = struct.unpack_from("<II", buf, p)
            val, p = ("__ts__", sec, inc), p + 8
        elif etype == 0x12:  # int64
            (val,), p = struct.unpack_from("<q", buf, p), p + 8
        elif etype == 0x13:  # decimal128
            val, p = 0, p + 16
        elif etype in (0xFF, 0x7F):  # min/max key
            val = None
        else:
            raise BsonError("unknown BSON type 0x%02x" % etype)
        out[name] = val
    return out, end


# FTDC extracts these BSON types as metrics (per the spec).
def numeric_metrics(doc: dict, prefix: str = "",
                    out: Optional[List[Tuple[str, int]]] = None
                    ) -> List[Tuple[str, int]]:
    """Walk a reference document in FTDC order, collecting numeric leaves.

    Order matters: it defines the column order of the metrics array.
    Timestamps contribute two metrics (seconds, then increment).
    """
    if out is None:
        out = []
    for key, val in doc.items():
        path = "%s.%s" % (prefix, key) if prefix else key
        if isinstance(val, dict):
            numeric_metrics(val, path, out)
        elif isinstance(val, list):
            numeric_metrics({str(i): v for i, v in enumerate(val)}, path, out)
        elif isinstance(val, tuple) and val and val[0] == "__ts__":
            out.append((path, val[1]))
            out.append((path + "._inc", val[2]))
        elif isinstance(val, bool):
            out.append((path, 1 if val else 0))
        elif isinstance(val, int):
            out.append((path, val & MASK64))
        elif isinstance(val, float):
            if val != val:  # NaN
                out.append((path, 0))
            else:
                clamped = max(-(1 << 63), min((1 << 63) - 1, int(val)))
                out.append((path, clamped & MASK64))
    return out


# -------------------------------------------------------------- codec ------

def read_varint(buf: bytes, pos: int) -> Tuple[int, int]:
    """Unsigned LEB128."""
    result = 0
    shift = 0
    n = len(buf)
    while pos < n:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            return result & MASK64, pos
    raise BsonError("truncated varint")


def decode_metrics(buf: bytes, metric_count: int, sample_count: int,
                   base: List[int]) -> List[List[int]]:
    """Decode the column-major delta/RLE/varint array.

    Returns metric_count rows of sample_count values (absolute, not deltas).
    """
    pos = 0
    n = len(buf)
    rows: List[List[int]] = []
    zeros_left = 0
    for m in range(metric_count):
        value = base[m] if m < len(base) else 0
        row: List[int] = []
        for _ in range(sample_count):
            if zeros_left > 0:
                zeros_left -= 1
                delta = 0
            elif pos >= n:
                delta = 0
            else:
                delta, pos = read_varint(buf, pos)
                if delta == 0:
                    if pos < n:
                        extra, pos = read_varint(buf, pos)
                        zeros_left = extra
            value = (value + delta) & MASK64
            row.append(value)
        rows.append(row)
    return rows


# ------------------------------------------------------------- reader ------

@dataclass
class Chunk:
    ts: datetime
    paths: List[str]
    rows: List[List[int]]  # metric-major: rows[metric][sample]

    @property
    def sample_count(self) -> int:
        return len(self.rows[0]) if self.rows else 0


def _to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc)


def iter_documents(path: str) -> Iterator[dict]:
    """Yield top-level BSON documents from an FTDC archive file."""
    with open(path, "rb") as fh:
        data = fh.read()
    pos = 0
    while pos < len(data) - 4:
        try:
            doc, pos = parse_document(data, pos)
        except (BsonError, struct.error, ValueError):
            break  # truncated tail (common on the live/interim file)
        yield doc


def decode_chunk(doc: dict) -> Optional[Chunk]:
    """Decode one type-1 metrics document into a Chunk."""
    blob = doc.get("data")
    if not isinstance(blob, (bytes, bytearray)) or len(blob) < 8:
        return None
    uncompressed_size = struct.unpack_from("<I", blob, 0)[0]
    if uncompressed_size > MAX_CHUNK_BYTES:
        raise BsonError("implausible chunk size %d" % uncompressed_size)
    raw = zlib.decompress(bytes(blob[4:]))
    ref, pos = parse_document(raw, 0)
    metric_count, sample_count = struct.unpack_from("<II", raw, pos)
    pos += 8
    leaves = numeric_metrics(ref)
    paths = [p for p, _ in leaves]
    base = [v for _, v in leaves]
    if metric_count != len(paths):
        # Schema/parse mismatch: trust the file, pad or trim names.
        if metric_count < len(paths):
            paths, base = paths[:metric_count], base[:metric_count]
        else:
            extra = metric_count - len(paths)
            paths += ["_unknown.%d" % i for i in range(extra)]
            base += [0] * extra
    rows = decode_metrics(raw[pos:], metric_count, sample_count, base)
    # Prepend the reference sample so the chunk holds sample_count+1 samples.
    rows = [[base[i]] + rows[i] for i in range(metric_count)]
    return Chunk(ts=_to_dt(doc.get("_id", 0) or 0), paths=paths, rows=rows)


def iter_chunks(path: str) -> Iterator[Chunk]:
    """Yield decoded metric chunks from one archive file."""
    for doc in iter_documents(path):
        if doc.get("type") != 1:
            continue
        try:
            chunk = decode_chunk(doc)
        except (BsonError, zlib.error, struct.error, ValueError):
            continue  # skip corrupt chunk, keep going
        if chunk is not None:
            yield chunk


def ftdc_files(path: str) -> List[str]:
    """Resolve a file or a diagnostic.data directory to a sorted file list."""
    if os.path.isfile(path):
        return [path]
    if not os.path.isdir(path):
        return []
    names = [n for n in os.listdir(path) if n.startswith("metrics.")]
    # interim last: it holds the most recent (possibly partial) samples
    archives = sorted(n for n in names if n != "metrics.interim")
    if "metrics.interim" in names:
        archives.append("metrics.interim")
    return [os.path.join(path, n) for n in archives]


# ------------------------------------------------------------ metrics ------

# Curated view: the handful of metrics a DBA actually triages with.
# (label, ftdc path, kind) where kind: "gauge" | "counter" (per-second rate)
CURATED: List[Tuple[str, str, str]] = [
    ("ops.insert", "serverStatus.opcounters.insert", "counter"),
    ("ops.query", "serverStatus.opcounters.query", "counter"),
    ("ops.update", "serverStatus.opcounters.update", "counter"),
    ("ops.delete", "serverStatus.opcounters.delete", "counter"),
    ("ops.getmore", "serverStatus.opcounters.getmore", "counter"),
    ("ops.command", "serverStatus.opcounters.command", "counter"),
    ("conns.current", "serverStatus.connections.current", "gauge"),
    ("conns.available", "serverStatus.connections.available", "gauge"),
    ("queue.readers", "serverStatus.globalLock.currentQueue.readers", "gauge"),
    ("queue.writers", "serverStatus.globalLock.currentQueue.writers", "gauge"),
    ("cache.usedBytes",
     "serverStatus.wiredTiger.cache.bytes currently in the cache", "gauge"),
    ("cache.maxBytes",
     "serverStatus.wiredTiger.cache.maximum bytes configured", "gauge"),
    ("cache.dirtyBytes",
     "serverStatus.wiredTiger.cache.tracked dirty bytes in the cache", "gauge"),
    ("tickets.readAvail",
     "serverStatus.wiredTiger.concurrentTransactions.read.available", "gauge"),
    ("tickets.writeAvail",
     "serverStatus.wiredTiger.concurrentTransactions.write.available", "gauge"),
    ("mem.residentMB", "serverStatus.mem.resident", "gauge"),
    ("mem.virtualMB", "serverStatus.mem.virtual", "gauge"),
    ("sys.cpu.userMs", "systemMetrics.cpu.user_ms", "counter"),
    ("sys.cpu.systemMs", "systemMetrics.cpu.system_ms", "counter"),
    ("sys.cpu.iowaitMs", "systemMetrics.cpu.iowait_ms", "counter"),
    ("sys.mem.availableKB", "systemMetrics.memory.MemAvailable_kb", "gauge"),
    ("repl.lagSeconds", "replSetGetStatus.members.0.optimeDate", "gauge"),
]


@dataclass
class Series:
    label: str
    path: str
    kind: str
    times: List[datetime]
    values: List[int]

    def stats(self) -> Dict[str, float]:
        if not self.values:
            return {}
        return {"min": min(self.values), "max": max(self.values),
                "avg": sum(self.values) / float(len(self.values)),
                "last": self.values[-1]}


class FtdcReader:
    """Streaming reader that collects curated (or requested) series."""

    def __init__(self, wanted: Optional[List[str]] = None,
                 sample_secs: int = 1):
        self.wanted = wanted
        self.sample_secs = sample_secs
        self.series: Dict[str, Series] = {}
        self.chunks = 0
        self.samples = 0
        self.errors = 0
        self.first_ts: Optional[datetime] = None
        self.last_ts: Optional[datetime] = None
        self.host: Optional[str] = None
        self.version: Optional[str] = None

    def _select(self, paths: List[str]) -> List[Tuple[str, str, str, int]]:
        index = {p: i for i, p in enumerate(paths)}
        chosen = []
        for label, path, kind in CURATED:
            if self.wanted and label not in self.wanted:
                continue
            if path in index:
                chosen.append((label, path, kind, index[path]))
        if self.wanted:
            for w in self.wanted:
                if w in index and not any(c[0] == w for c in chosen):
                    chosen.append((w, w, "gauge", index[w]))
        return chosen

    def read(self, path: str, ts_from: Optional[datetime] = None,
             ts_to: Optional[datetime] = None) -> "FtdcReader":
        for f in ftdc_files(path):
            for doc in iter_documents(f):
                dtype = doc.get("type")
                if dtype == 0:
                    self._meta(doc)
                    continue
                if dtype != 1:
                    continue
                try:
                    chunk = decode_chunk(doc)
                except (BsonError, zlib.error, struct.error, ValueError):
                    self.errors += 1
                    continue
                if chunk is None:
                    continue
                if ts_from and chunk.ts < ts_from:
                    continue
                if ts_to and chunk.ts > ts_to:
                    continue
                self._absorb(chunk)
        return self

    def _meta(self, doc: dict):
        inner = doc.get("doc") or {}
        for coll in inner.values():
            if not isinstance(coll, dict):
                continue
            hi = coll.get("hostinfo") or coll.get("system")
            if isinstance(hi, dict) and not self.host:
                self.host = hi.get("hostname") or hi.get("currentTime")
            bi = coll.get("buildInfo") or coll
            if isinstance(bi, dict) and not self.version:
                v = bi.get("version")
                if isinstance(v, str):
                    self.version = v

    def _absorb(self, chunk: Chunk):
        self.chunks += 1
        chosen = self._select(chunk.paths)
        n = chunk.sample_count
        self.samples += n
        # FTDC samples default to one per second.
        times = [datetime.fromtimestamp(
            chunk.ts.timestamp() + i * self.sample_secs, tz=timezone.utc)
            for i in range(n)]
        if self.first_ts is None:
            self.first_ts = times[0] if times else chunk.ts
        if times:
            self.last_ts = times[-1]
        for label, path, kind, idx in chosen:
            series = self.series.get(label)
            if series is None:
                series = self.series[label] = Series(label, path, kind, [], [])
            series.times.extend(times)
            series.values.extend(chunk.rows[idx])

    # -- derived views ----------------------------------------------------
    def rate(self, label: str) -> Optional[float]:
        """Average per-second rate for a counter series."""
        s = self.series.get(label)
        if not s or len(s.values) < 2:
            return None
        span = (s.times[-1] - s.times[0]).total_seconds() or 1.0
        return (s.values[-1] - s.values[0]) / span

    def cache_pct(self) -> Optional[float]:
        used = self.series.get("cache.usedBytes")
        mx = self.series.get("cache.maxBytes")
        if not used or not mx or not used.values or not mx.values:
            return None
        peak = max(mx.values) or 1
        return 100.0 * max(used.values) / peak

    def summary(self) -> dict:
        out = {
            "chunks": self.chunks, "samples": self.samples,
            "corruptChunks": self.errors,
            "from": self.first_ts.isoformat() if self.first_ts else None,
            "to": self.last_ts.isoformat() if self.last_ts else None,
            "series": {},
        }
        for label, s in sorted(self.series.items()):
            entry = s.stats()
            if s.kind == "counter":
                r = self.rate(label)
                if r is not None:
                    entry["perSecond"] = round(r, 2)
            out["series"][label] = entry
        pct = self.cache_pct()
        if pct is not None:
            out["cachePeakPct"] = round(pct, 1)
        return out
