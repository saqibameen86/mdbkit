# DESIGN v0.2 — Incident Triage (`mdbkit triage`)

Status: approved design, not yet implemented.
Implementer notes: follow docs/ROADMAP.md principles. Read-only. Offline.

## The promise

At 3 a.m., on a box with nothing installed but mdbkit, one command answers:
*what is unhealthy right now, what changed, and where do I look next?*

```
mdbkit triage /var/log/mongodb/mongod.log \
    [--ftdc /data/db/diagnostic.data] \
    [--serverstatus ss1.json [ss2.json]] \
    [--top top.json] [--window 60m] [--json]
```

Only the log is required. Every extra input sharpens the picture. mdbkit
NEVER connects to the database — serverStatus/top files are produced by the
operator with printed one-liners (extend `export-script` with
`serverstatus` and `top` kinds: `mongosh --quiet --eval
'EJSON.stringify(db.serverStatus())' > ss1.json`, run twice ~30 s apart).

## Output: severity-ordered findings

```
== mdbkit triage ==  window: last 60m of log (08:10 -> 09:10)

[CRIT] Replica set instability: 2 elections in window (08:41, 08:58).
       Node stepped down at 08:41:02 ("Stepping down"); new term 74.
[WARN] Disk: dbPath /data/db volume 91% full (statvfs), 18 GiB free.
[WARN] Connection storm: 480 connections accepted 08:40-08:42
       (baseline 6/min). Top source: 10.2.1.7 (312). appName: OrderService.
[WARN] Hot collection: shop.orders — 71% of slow-query time in window
       (214 ops, 3 shapes, worst {status:eq,createdAt:gt} sort:{createdAt:-1}).
[INFO] WiredTiger: 3 slow checkpoints (max 12.4 s at 08:39).
[OK]   Memory: no eviction-pressure or OOM indicators found in log.

next: mdbkit queries --min-ms 100 <log> | mdbkit advise <log>
```

Findings are deterministic, each backed by counted evidence. `[OK]` lines
matter: an incident tool must say what it checked and found healthy.

## Architecture

```
mdbkit/triage/__init__.py
mdbkit/triage/detectors.py   # log-side detectors (one class per detector)
mdbkit/triage/sysprobe.py    # local OS probes, stdlib only
mdbkit/triage/serverstatus.py# snapshot digest (1 or 2 files)
mdbkit/triage/engine.py      # runs detectors, merges, ranks
mdbkit/triage/render_triage.py
```

### Detector interface

```python
class Detector(Protocol):
    name: str
    def consume(self, entry: LogEntry) -> None
    def findings(self) -> list[Finding]

@dataclass
class Finding:
    severity: str        # CRIT | WARN | INFO | OK
    title: str
    detail: str
    evidence: list[str]  # timestamps/counts, never raw payloads
    next_step: str       # a command or doc pointer, never auto-executed
```

### Log-side detectors (single pass, share the v0.1 parser)

1. **ElectionDetector** — REPL component. Match on msg text (robust across
   versions): "Starting an election", "election succeeded", "Stepping
   down", "Member is in new state", "transition to" with PRIMARY/SECONDARY,
   "Replica set state transition". IMPLEMENTATION GATE: collect real
   election log fixtures (kill a primary in a local 3-node mlaunch set for
   each supported major version) and record the exact msg ids seen into
   tests BEFORE trusting text matching. Timeline output: ordered state
   changes with timestamps and terms. >=1 election in window = CRIT.
2. **ConnectionStormDetector** — reuse ConnectionAggregator, add per-minute
   bucketing. Storm = a minute exceeding max(10x median-nonzero-minute,
   configurable floor 60). Report top source IPs and appNames in the storm
   window.
3. **SlowQueryBurstDetector / HotCollection** — per-minute slow-query
   counts and per-namespace total durationMillis; hot collection = ns with
   largest share of slow time (report share %, top shapes via existing
   QueryAggregator).
4. **CheckpointDetector** — STORAGE/WTCHKPT messages containing
   "checkpoint" with duration; slow if > 60 s per MongoDB guidance —
   verify threshold and msg ids against real logs (fixture gate as above).
5. **EvictionPressureDetector** — WT eviction/cache-pressure messages and
   slow-query `storage` sections with large `timeWaitingMicros.cache`;
   also "application threads performing eviction" style messages.
6. **FlowControlDetector** — "Flow control is engaged" REPL messages.
7. **OplogWindowDetector** — from repl startup lines + any oplog messages;
   if oplog window can be derived, WARN when < 1 h. If not derivable from
   log alone, emit nothing (no guessing).
8. **FatalErrorDetector** — severity E/F lines grouped by msg id, top 5.
9. **RestartDetector** — startup markers inside the window = WARN with
   timestamps (unexpected restarts are incidents).

### sysprobe.py (local, stdlib, no shell-outs — keeps SECURITY.md true)

* dbPath discovered from the log's startup line (attr.dbPath); fall back to
  `--dbpath` flag.
* Disk: `os.statvfs(dbPath)` -> %used, bytes free. CRIT >= 95%, WARN >= 85%.
* Memory: parse `/proc/meminfo` (MemAvailable/MemTotal). WARN < 10%.
* Load: `os.getloadavg()` vs `os.cpu_count()`. WARN load1 > 2x cores.
* Graceful degradation on non-Linux or missing paths: emit INFO "probe
  unavailable", never crash. All probes wrapped in try/except.

### serverstatus.py

* One snapshot: connections current/available (WARN < 20% available),
  wt cache used % vs configured (WARN > 95% of max, dirty > 20%),
  tickets available (pre-7.0), globalLock queues, asserts, uptime,
  flowControl.isLagged, mem.resident vs system RAM if sysprobe ran.
* Two snapshots taken N seconds apart: true rates — opcounters/s,
  connection creation rate, cache eviction rates, queue trend. Rates are
  labeled with the measured interval. (Cumulative counters make a single
  snapshot weak for rates — hence the two-file design; the export
  one-liner prints both commands with a sleep between.)
* Accept mongosh EJSON output; reject legacy-shell non-JSON with the same
  helpful error as `mdbkit explain`.

### engine.py

Single pass over the log window feeding all detectors (reuse iter_entries;
default window = last 60 min of log time, `--window` to change). Merge
findings + sysprobe + serverstatus + (if --ftdc given) FTDC summary deltas
for the same window (cache %, iowait, disk util from DESIGN-ftdc metrics).
Rank CRIT > WARN > INFO > OK; stable ordering within a class by first
timestamp. `--json` emits the Finding list verbatim (schema documented
here before merge).

## Acceptance criteria

1. Election fixture logs (one per supported major version, generated from
   real replica sets) produce a correct, complete timeline — zero missed
   elections, zero false elections on the quiet sample log.
2. Runs on the v0.1 sample corpus + Hatchet demo log without a single
   false CRIT.
3. Full triage of a 1 GB log completes < 60 s, one pass, streaming.
4. Works with log alone; every optional input degrades gracefully.
5. No probe or parser can crash the command; worst case is an INFO
   "unavailable" finding.
6. Every finding includes a next_step; no finding auto-executes anything.
7. Zero new dependencies; no network; no shell-outs (CI greps enforce).

## Explicit non-goals

No daemon/watch mode (that is monitoring — future platform, not the CLI).
No thresholds tuned per-customer (flags exist, defaults documented).
No mutation of anything, ever.
