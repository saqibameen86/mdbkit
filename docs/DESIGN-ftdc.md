# DESIGN v0.2 — FTDC Decoder (`mdbkit ftdc`)

Status: approved design, not yet implemented.
Implementer notes: follow docs/ROADMAP.md principles. Stdlib only.

## Why

Every mongod continuously records Full-Time Diagnostic Data Capture into
`<dbPath>/diagnostic.data/` — server counters AND (on Linux) system metrics:
CPU, memory, disks, network. It is the flight recorder nobody but MongoDB
support reads. Decoding it offline gives self-managed users the "what
happened at 3 a.m." answer without any monitoring stack installed.

## File format (verify against real files before coding)

`diagnostic.data/` contains files named `metrics.YYYY-MM-DDTHH-MM-SSZ-00000`
plus `metrics.interim`. Each file is a sequence of BSON documents:

* **type 0** — metadata document: `{_id: <Date>, type: 0, doc: {...}}` with
  buildInfo, getCmdLineOpts, hostInfo. Parse once per file for context.
* **type 1** — metrics chunk: `{_id: <Date>, type: 1, data: <BinData>}`.
  `data` layout: 4-byte little-endian uint32 (uncompressed length), then a
  zlib stream. Decompressed payload:
  1. one reference BSON document (a full serverStatus-like sample),
  2. uint32 `metricCount`, uint32 `sampleCount`,
  3. for each metric (in depth-first traversal order of numeric leaves of
     the reference doc): `sampleCount` deltas, varint-encoded, with
     zero-run-length compression — a varint 0 is followed by a varint count
     of ADDITIONAL consecutive zeros.

Numeric leaves include: int32, int64, double (stored via integer
transform), bool (0/1), UTC datetime (millis), timestamp (t and i counted
as two metrics). Strings/objectids are skipped (not metrics). EXACT
per-type encoding must be validated against real files — write the codec
against fixtures first (see Test strategy).

## Module layout

```
mdbkit/ftdc/__init__.py
mdbkit/ftdc/bsonlite.py    # minimal read-only BSON parser (~150 lines)
mdbkit/ftdc/codec.py       # varint + zero-RLE delta decoder
mdbkit/ftdc/reader.py      # file iteration -> chunks -> samples
mdbkit/ftdc/metrics.py     # curated metric map + derived rates
mdbkit/ftdc/render_ftdc.py # tables/CSV/JSON
```

### bsonlite.py

`parse_document(buf, offset) -> (dict, next_offset)` supporting element
types: 0x01 double, 0x02 string, 0x03 doc, 0x04 array, 0x05 binary (skip
content, keep for `data`), 0x08 bool, 0x09 datetime, 0x0A null, 0x10 int32,
0x11 timestamp, 0x12 int64. Anything else: skip via length rules. Must be
iterative or depth-capped (<= 64) — FTDC reference docs nest ~6 deep.
Also expose `numeric_leaves(doc) -> list[(path, value)]` preserving
insertion order (dicts preserve order in py3.7+), matching FTDC's traversal.

### codec.py

`read_varint(buf, pos) -> (int, pos)` (LEB128, unsigned) and
`decode_deltas(buf, metric_count, sample_count) -> list[list[int]]`.
Deltas are cumulative: `value[i] = value[i-1] + delta[i]`, seeded from the
reference doc leaf value.

### reader.py

```
@dataclass
class FtdcSample:  ts: datetime; values: dict[str, int|float]
def iter_samples(path_or_dir, ts_from=None, ts_to=None,
                 keys: set[str] | None = None) -> Iterator[FtdcSample]
```
Accepts a single metrics file, a diagnostic.data directory (sorted file
order), or a copied/tarred directory. Streaming: never hold all samples in
memory; decode chunk by chunk. Bounded: max decompressed chunk 64 MiB,
refuse beyond (corrupt/hostile file).

### metrics.py — curated view (the value layer)

Raw FTDC has thousands of metrics. Ship a curated map of the ones a DBA
triages with, grouped:

* ops: `serverStatus.opcounters.*` (derive per-second rates from deltas)
* connections: `serverStatus.connections.current/available`
* wt cache: `...wiredTiger.cache.bytes currently in the cache` vs
  `maximum bytes configured` (compute %), dirty bytes %
* tickets: `...concurrentTransactions.read/write.available` (pre-7.0)
* queues: `serverStatus.globalLock.currentQueue.readers/writers`
* flow control: `serverStatus.flowControl.isLagged`
* repl lag inputs: `serverStatus.repl` + oplog timestamps
* system (Linux): `systemMetrics.cpu.*` (user/sys/iowait ticks -> %),
  `systemMetrics.memory.MemAvailable`, `systemMetrics.disks.<dev>.*`
  (io time, reads/writes -> derive utilization %)

Every derived rate documents its formula in code comments.

## CLI surface

```
mdbkit ftdc summary <path> [--from T --to T] [--json]
    # one table: min/avg/max/last for curated metrics over the window
mdbkit ftdc timeline <path> --metric wtCachePct --metric opsInsert [--step 1m]
    # per-interval rows, terminal-friendly sparkline optional (pure ASCII)
mdbkit ftdc export <path> --format csv|json [--metric ...] [--raw]
    # --raw exports uncurated full metric names for power users
```

## Acceptance criteria

1. Decodes real diagnostic.data from MongoDB 4.4, 5.0, 6.0, 7.0, 8.0
   fixtures byte-for-byte without error; unknown metrics ignored gracefully.
2. Cross-check: for one fixture, values for opcounters/connections match
   `keyhole`'s or Grafana/mongo-ftdc's output within rounding (manual
   one-time verification, documented in the PR).
3. A 24h diagnostic.data directory (~200 MB) summarizes in < 30 s and
   < 300 MB RSS (streaming decode).
4. Corrupt/truncated chunk: skipped with a counted warning, never a crash,
   never a traceback echoing file bytes.
5. `--json` schema documented in this file before merge.
6. Zero new dependencies; SECURITY.md claims remain true (`grep` test).

## Test strategy

* Fixtures: spin up throwaway mongod versions locally (mlaunch or docker),
  run a small workload, copy diagnostic.data, commit ONE small real file
  per major version (a few hundred KB each; FTDC contains no user data —
  verify before committing, document verification).
* Unit-test codec with hand-built varint/RLE cases including zero runs
  spanning chunk boundaries.
* Property test: encode->decode round trip for the delta codec.

## Explicit non-goals

No live tailing of diagnostic.data in v0.2 (that's monitoring, later).
No writing FTDC. No charting beyond ASCII (HTML belongs to v0.3 reports).
