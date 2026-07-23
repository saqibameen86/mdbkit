# mdbkit

**An offline toolkit for MongoDB structured logs — log analysis, slow-query shapes, connection churn, and deterministic index advice.**

A spiritual successor to [mtools](https://github.com/rueckstiess/mtools)' log tools (`mloginfo`, `mlogfilter`) for the structured JSON log format MongoDB has used since 4.4 — the format mtools never supported. Built for DBAs and ops engineers running self-managed MongoDB 4.4 / 5.0 / 6.0 / 7.0 / 8.0.

> **Privacy by design: mdbkit never makes a network call.** It reads log files (or stdin) and writes to stdout. No telemetry, no phoning home, no cloud. Your logs never leave your machine. It is safe to run on air-gapped database hosts.

## Why

MongoDB 4.4 switched to structured JSON logging, and the beloved mtools log commands stopped working — the issue has been open since 2020. Meanwhile, index recommendations from MongoDB's Performance Advisor require a paid Atlas tier or Cloud/Ops Manager. If you run Community Edition on your own infrastructure, you're back to reading raw JSON logs with `grep` and `jq`.

mdbkit fills that gap: a single, dependency-free CLI that turns structured logs into answers.

## Install

**Recommended on any Linux/Mac/Windows:**
```bash
pip install mdbkit
```

**Ubuntu 20.04 / Debian / Amazon Linux 2 (Python 3.8 hosts):**
```bash
# Step 1: install pipx (manages isolated Python tool environments)
sudo apt install pipx        # Ubuntu/Debian
# or: sudo dnf install pipx  # RHEL/Rocky/Amazon Linux

# Step 2: install mdbkit
pipx install mdbkit

# Step 3: if pipx is not on your PATH yet
pipx ensurepath && source ~/.bashrc
```

**If you get "externally-managed-environment" on modern Ubuntu (23.04+):**
```bash
pip install mdbkit --break-system-packages
```

**Air-gapped database hosts (no internet access):**
```bash
# On a connected machine, download the wheel file
pip download mdbkit -d ./wheels

# Copy the ./wheels folder to the database host, then run
pip install --no-index --find-links ./wheels mdbkit
```

Requires Python 3.8+. Zero runtime dependencies — safe to install on production hosts.

> mdbkit is a Python package distributed via PyPI. It is **not** available
> via `apt install`, `dnf install`, or `yum install` — use `pip` or `pipx`.

## Quick start

```bash
# Overall log summary: versions, restarts, connection counts, error/warning totals
mdbkit loginfo /var/log/mongodb/mongod.log

# Slow queries grouped by query shape (literals stripped), ranked by total time
mdbkit queries mongod.log
mdbkit queries mongod.log --sort scanRatio --limit 10 --json

# Columns: cumMs = time summed across ALL occurrences of that shape (not one
# query); docsEx = documents examined; scan = examined per doc returned;
# plan = the plan MongoDB chose (COLLSCAN / IXSCAN{fields} / +SORT)

# Connection churn by source IP, appName, and driver
mdbkit connections mongod.log

# Filter raw log lines (output stays valid logv2 JSON — chainable)
mdbkit filter mongod.log --slow 500 --component COMMAND --ns shop.orders
mdbkit filter mongod.log --severity E --last 20      # 20 most recent errors
mdbkit filter mongod.log --slow 200 --limit 50       # first 50 matches only

# Time ranges — with or without a timezone offset
mdbkit filter mongod.log --from 2026-07-01T08:00:00+04:00 --to 2026-07-01T09:00:00+04:00
mdbkit filter mongod.log --from 2026-07-01T08:00:00Z     # UTC
mdbkit filter mongod.log --from 2026-07-01T08:00:00      # log's own timezone
mdbkit filter mongod.log --from 2026-07-01 | mdbkit queries -

# Rotated/compressed logs work directly
mdbkit queries mongod.log.2.gz
```

### Step 1 — Export your indexes and schema (recommended)

mdbkit never connects to your database directly. Instead it prints small
`mongosh` scripts you run yourself, so you can inspect exactly what they do
before running them. The exports contain **field names and types only — no
document values.**

**Generate the export scripts:**
```bash
mdbkit export-script indexes > export_indexes.js
mdbkit export-script schema  > export_schema.js
```

**Run them against your database** (replace with your actual host, port, and credentials):
```bash
# Basic (local, no auth):
mongosh --quiet "mongodb://localhost/yourdb" export_indexes.js > indexes.json
mongosh --quiet "mongodb://localhost/yourdb" export_schema.js  > schema.json

# With authentication (typical production setup):
mongosh --quiet \
  --host your-db-host \
  --port 27017 \
  --username your-username \
  --password your-password \
  --authenticationDatabase admin \
  --eval "$(cat export_indexes.js)" > indexes.json

mongosh --quiet \
  --host your-db-host \
  --port 27017 \
  --username your-username \
  --password your-password \
  --authenticationDatabase admin \
  --eval "$(cat export_schema.js)" > schema.json
```

> In these examples, replace `yourdb` with your actual database name (e.g. `shop`, `myapp`).
> The `--authenticationDatabase` is usually `admin` unless you use per-db auth.

### Step 2 — Run index advice

```bash
# Basic (without index/schema context — still useful, but lower confidence):
mdbkit advise mongod.log

# Full (with context — sharper recommendations):
mdbkit advise mongod.log --indexes indexes.json --schema schema.json

# Focus on one collection (recommended for large logs):
mdbkit advise mongod.log --indexes indexes.json --schema schema.json --ns shop.orders
```

Sample output:
```
[1] shop.orders  —  confidence: HIGH
    query shape : {createdAt:gt, status:eq} sort:{createdAt:-1}
    candidate   : { status: 1, createdAt: -1 }
    evidence    : COLLSCAN observed in planSummary
    evidence    : examined 251,400 docs to return 73 (3444:1)
    caveat      : Every index adds write and storage overhead ...
    validate    : Re-run the query with .explain('executionStats') ...
```

With `--indexes`, mdbkit checks candidates against your existing indexes —
flagging when an existing index should already cover the query (it flags, never
auto-drops). With `--schema`, it warns about array (multikey) fields,
low-cardinality booleans, and field-name typos, and adjusts confidence
accordingly.

### Incident triage (beta)

```bash
mdbkit triage /var/log/mongodb/mongod.log     # last 60 minutes (default)
mdbkit triage mongod.log --window 30          # last 30 minutes
mdbkit triage mongod.log --window 0           # whole file
mdbkit triage mongod.log --dbpath /data/db    # if auto-discovery misses it
mdbkit triage mongod.log --no-sysprobe        # analyzing a log copied off-host
```

**Defaults to the last 60 minutes of log time**, because triage is for
incidents happening now or just finished. In one command:

- restarts, error clusters, election/stepdown events
- connection storms — with the peak minute and the top source IPs
- slow-query volume and the peak minute, so you know *when* it hurt
- COLLSCAN share of slow operations (the missing-index signal)
- the hot collection plus its top three query shapes inline
- index-build activity (a common cause of surprise load)
- slow checkpoints, cache eviction pressure, flow control
- disk / memory / CPU load, and the running mongod's RSS and uptime

When run on the database host, mdbkit finds `dbPath` automatically — from the
log's startup line, or the running `mongod` process, or `/etc/mongod.conf`,
or common defaults — so the disk check works even when the current log has no
startup event. All probing is stdlib-only (`/proc`, `statvfs`); no shell-outs,
nothing leaves the machine.

Read-only: it never connects to the database, and every finding ends with a
next step for a human to review. Detectors marked beta are pattern-matched and
clearly labeled while broader validation is pending.

### Explain-plan analysis

Got a slow query in hand rather than a log? Save its explain output and ask
mdbkit what's wrong:

```bash
# in mongosh:  EJSON.stringify(db.orders.find({...}).sort({...}).explain("executionStats"))
mdbkit explain explain.json
```

You get the plan chain (`SORT -> COLLSCAN`), the examined/returned math, plain-
English verdicts (full collection scan, blocking in-memory sort, weakly
selective index, covered query), and — when the plan needs help — the same
evidence-backed candidate index the advisor would produce. Works with find and
aggregate explains, classic and SBE (6.0+) plans, and sharded winning plans.

### Design principles

* **Deterministic.** Same log in, same advice out. Rules, not AI. Every
  recommendation shows its evidence and its rule of reasoning.
* **Candidates, not commands.** mdbkit never tells you to blindly run
  `createIndex`, and never advises dropping an index.
* **Honest about uncertainty.** Shapes seen once are labeled low-confidence;
  `$or`, `$regex`, `$in` and low-selectivity operators carry explicit caveats.
* **Offline, always.** No network code exists in this codebase.

## What it reads

Any MongoDB 4.4+ structured log: `mongod.log`, `mongos` logs, rotated `.gz`
files, or stdin (`-`). Slow-query lines (`"msg":"Slow query"`) are logged by
default for operations over `slowms` (100 ms); lower `slowms` or enable
profiling level 1 to capture more:

```js
db.setProfilingLevel(1, { slowms: 50 })
```

Pre-4.4 plain-text logs are detected and politely refused — for those, the
original mtools still works.

## Roadmap

Terminal output is and will remain first-class — this tool is built for the
Linux box the database actually runs on.

* **v0.2** — FTDC (`diagnostic.data`) decoding: offline summaries of the
  metrics MongoDB already records on every node; election/failover timeline
  from REPL events; per-shape drill-down.
* **v0.3** — shareable Markdown/HTML report export (for tickets and
  post-incident reviews — a convenience layer, never a replacement for the
  terminal).

mdbkit is validated against real-world structured logs (tens of thousands of
lines) in addition to its synthetic test fixtures.

## Bugs, feature requests, questions

Please use [GitHub Issues](../../issues) — it keeps problems and fixes public
so the next person can find them. Real-world log lines that parse wrongly are
the most valuable bug reports of all (redact literals first!).

## Security

mdbkit is offline by design: the codebase contains no network code, never
executes or evaluates input, and treats every log line as untrusted data
(strict JSON parsing only — shell constructors are never evaluated). See
[SECURITY.md](SECURITY.md) for the reporting process.

## Non-affiliation

mdbkit is an independent community project. It is **not affiliated with,
endorsed by, or sponsored by MongoDB, Inc.** "MongoDB" is a registered
trademark of MongoDB, Inc., used here only to describe compatibility.

## License

MIT — see [LICENSE](LICENSE).
