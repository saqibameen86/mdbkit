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

**Upgrading to a newer version:**
```bash
pip install --upgrade mdbkit      # if installed with pip
pipx upgrade mdbkit               # if installed with pipx
mdbkit --version                  # confirm
```
mdbkit never updates itself and never checks for updates — it makes no network
calls at all. Upgrades are always explicit.

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
  --host your_db_host \
  --port 27017 \
  --username your_username \
  --password your_password \
  --authenticationDatabase admin \
  --eval "$(cat export_indexes.js)" > indexes.json

mongosh --quiet \
  --host your_db_host \
  --port 27017 \
  --username your_username \
  --password your_password \
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

## Command reference

Every command reads files or stdin and writes to stdout. `--help` works on any
command (`mdbkit queries --help`). Global: `mdbkit --version`.

All commands that read a log accept a path to a `.log` file, a rotated `.gz`
file, or `-` for stdin.

---

### `mdbkit loginfo <log>`

Overall log summary: server version, host, restarts, connections accepted,
slow-query count, warning/error counts, and a per-component line breakdown.

| Option | Description |
|---|---|
| `--json` | Machine-readable output |

```bash
mdbkit loginfo /var/log/mongodb/mongod.log
mdbkit loginfo mongod.log.2.gz --json
```

---

### `mdbkit queries <log>`

Slow queries grouped by **query shape** — literal values stripped, so the same
query with different parameters is counted once.

| Option | Default | Description |
|---|---|---|
| `--sort FIELD` | `totalMs` | Order by `totalMs`, `count`, `mean`, `max`, `docsExamined`, or `scanRatio` |
| `--limit N` | all | Show only the top N shapes |
| `--min-ms N` | 0 | Ignore operations faster than N milliseconds |
| `--include-system` | off | Include internal `admin`/`config`/`local` namespaces (hidden by default — they are server housekeeping, not your workload) |
| `--report FILE` | | Write a shareable `.md` or `.html` report instead (see [Shareable reports](#shareable-reports----report-file)) |
| `--json` | | Machine-readable output |

**Reading the columns:**

| Column | Meaning |
|---|---|
| `cumMs` | Time summed across **all** occurrences of that shape — not one query |
| `mean` / `max` | Per-occurrence average and worst case |
| `docsEx` | Documents examined, summed across all occurrences |
| `scan` | Documents examined per document returned. `1:1` is ideal; `3444:1` means a missing or weak index |
| `plan` | The plan MongoDB chose: `COLLSCAN` (no index), `IXSCAN{fields}` (index used), `IDHACK` (`_id` lookup), `+SORT` (in-memory sort). `?` = the plan was not recorded on that line |
| `shape` | Fields and operators queried, with the sort |

```bash
mdbkit queries mongod.log
mdbkit queries mongod.log --sort scanRatio --limit 10
mdbkit queries mongod.log --min-ms 500 --json
```

---

### `mdbkit connections <log>`

Connection churn: totals, peak concurrent count, per-source-IP breakdown, and
the client applications and drivers seen.

| Option | Description |
|---|---|
| `--json` | Machine-readable output |

---

### `mdbkit filter <log>`

Streams **matching raw log lines** to stdout. Output stays valid logv2 JSON, so
it chains with other tools (including mdbkit itself).

| Option | Description |
|---|---|
| `--component NAME` | `COMMAND`, `NETWORK`, `REPL`, `STORAGE`, `INDEX`, `WRITE`, `QUERY`, `CONTROL`, … |
| `--severity S` | `I` info, `W` warning, `E` error, `F` fatal |
| `--ns NAMESPACE` | Exact namespace, e.g. `shop.orders` |
| `--slow N` | Only operations with `durationMillis` >= N |
| `--from TIMESTAMP` | Lower time bound (inclusive) |
| `--to TIMESTAMP` | Upper time bound (inclusive) |
| `--msg TEXT` | Substring match on the message field |
| `--limit N` | Print only the **first** N matches |
| `--last N` | Print only the **last** N matches — usually what you want during an incident |
| `--as-explain` | Rebuild each matching slow query as a runnable `mongosh` `.explain()` command instead of printing the raw log line |
| `--explain-script` | With `--as-explain`, wrap in `EJSON.stringify()` plus usage comments so it can be saved as a `.js` file |

**Timestamp formats accepted** by `--from` / `--to`:

```
2026-07-01T08:00:00+04:00     with an explicit offset (production logs)
2026-07-01T08:00:00Z          UTC
2026-07-01T08:00:00           no offset — read as the log's own timezone
2026-07-01 08:00:00           space instead of T
2026-07-01T08:00               minute precision
2026-07-01                     whole day
```

```bash
mdbkit filter mongod.log --severity E --last 20    # errors (most recent 20)
mdbkit filter mongod.log --severity F               # fatal — always investigate
mdbkit filter mongod.log --severity W --last 50     # warnings
mdbkit filter mongod.log --component REPL --msg election
mdbkit filter mongod.log --slow 500 --ns shop.orders --limit 50
mdbkit filter mongod.log --from 2026-07-01T14:30:00+04:00 --to 2026-07-01T15:00:00+04:00
mdbkit filter mongod.log --slow 100 | mdbkit queries -
```

**From a slow query in the log to an explain plan**, without hand-writing the
query — `--as-explain` rebuilds the command that ran:

```bash
# See the actual commands behind your slowest operations
mdbkit filter mongod.log --ns shop.orders --slow 500 --last 3 --as-explain

# Or produce a runnable script, get the plan, and analyze it
mdbkit filter mongod.log --slow 500 --last 1 --as-explain --explain-script > q.js
mongosh --quiet --host your_db_host --username your_username \\
        --password your_password --authenticationDatabase admin \\
        --eval "$(cat q.js)" > explain.json
mdbkit explain explain.json
```

> Rebuilt commands contain the **real values** from your log (not redacted
> shapes) — treat them as sensitive.

---

### `mdbkit advise <log>`

Deterministic **candidate** index recommendations from observed slow-query
shapes, using the ESR guideline (Equality → Sort → Range). Rules, not AI: the
same log always produces the same advice.

| Option | Default | Description |
|---|---|---|
| `--indexes FILE` | | `indexes.json` from `mdbkit export-script indexes` — enables overlap checks against existing indexes |
| `--schema FILE` | | `schema.json` from `mdbkit export-script schema` — enables field-type caveats and confidence adjustment |
| `--ns NAMESPACE` | all | Only advise on one namespace (recommended on large logs) |
| `--limit N` | 10 | Show only the top N recommendations (`0` = all) |
| `--min-ms N` | 0 | Ignore operations faster than N milliseconds |
| `--min-count N` | 1 | Only advise on shapes seen at least N times |
| `--include-system` | off | Include internal `admin`/`config`/`local` namespaces |
| `--json` | | Machine-readable output |

Each recommendation carries a candidate key pattern, the evidence behind it, a
confidence level, caveats, and a validation step. mdbkit never advises dropping
an index — at most it flags an overlap to investigate.

```bash
mdbkit advise mongod.log
mdbkit advise mongod.log --indexes indexes.json --schema schema.json
mdbkit advise mongod.log --ns shop.orders --limit 3
```

---

### `mdbkit explain <file>`

Analyzes a saved `explain("executionStats")` document: the plan chain, the
examined-vs-returned math, plain-English verdicts, and — when the plan needs
help — a candidate index from the same advisor engine.

| Option | Description |
|---|---|
| `--indexes FILE` | Overlap check against existing indexes |
| `--schema FILE` | Field-type caveats |
| `--json` | Machine-readable output |

**Full example.** Get a plan for a query and analyze it:

```bash
# 1. Capture the plan (adjust host/credentials for your deployment)
mongosh --quiet \\
  --host your_db_host \\
  --port 27017 \\
  --username your_username \\
  --password your_password \\
  --authenticationDatabase admin \\
  --eval 'EJSON.stringify(db.getSiblingDB("shop").orders.find({status:"open"}).sort({ts:-1}).explain("executionStats"))' \\
  > explain.json

# 2. Analyze it
mdbkit explain explain.json

# 3. Sharper, with your existing indexes and sampled schema
mdbkit explain explain.json --indexes indexes.json --schema schema.json
```

Don't want to write the query by hand? `mdbkit filter ... --as-explain`
rebuilds it from the log for you (see the `filter` section above).

Legacy `mongo` shell and Compass output containing `NumberLong(...)`,
`ISODate(...)` or `ObjectId(...)` is accepted — mdbkit unwraps those
automatically, so you do not have to re-export.

---

### `mdbkit triage <log>`

**"Triage" means: quickly work out what is wrong and what to look at first.**
Run this when something has gone wrong — or has just gone wrong — and you need
one screen that says what happened, how bad it is, and where to look next.
**Defaults to the last 60 minutes of log time.**

| Option | Default | Description |
|---|---|---|
| `--window N` | 60 | Analyze the last N minutes of log time; `0` = the whole file |
| `--dbpath PATH` | auto | Override the data directory used for the disk check |
| `--no-sysprobe` | off | Skip local disk/memory/CPU probes — use when analyzing a log copied off the host |
| `--ftdc PATH` | | `diagnostic.data` directory — adds CPU, memory, cache, queue and connection history from MongoDB's own recorder |
| `--report FILE` | | Write a shareable `.md` or `.html` report instead of terminal output |
| `--json` | | Machine-readable output |

```bash
mdbkit triage /var/log/mongodb/mongod.log
mdbkit triage mongod.log --window 30
mdbkit triage mongod.log --ftdc /var/lib/mongodb/diagnostic.data
mdbkit triage mongod.log --report incident.html
mdbkit triage mongod.log --window 0 --no-sysprobe
```

---

### `mdbkit ftdc {summary|timeline|export} <path>`

Decodes `diagnostic.data` — **FTDC (Full-Time Diagnostic Data Capture)**, the
metrics recorder every mongod already runs. It holds CPU, memory, WiredTiger
cache, connection, queue and operation history for every node, with no
monitoring agent installed and no database connection. It is compressed BSON,
not encrypted; mdbkit decodes it offline.

| Action | Description |
|---|---|
| `summary` | min / avg / max / last per metric, plus per-second rates for counters |
| `timeline` | Values bucketed over time — shows *when* something spiked |
| `export` | CSV to stdout, for a spreadsheet or your own tooling |

| Option | Default | Description |
|---|---|---|
| `--last DURATION` | `4h` | Analyze only the most recent window — `90m`, `4h`, `2d` |
| `--all` | off | Analyze the entire history (see the performance note below) |
| `--metric LABEL` | all | Restrict to one metric (repeatable), e.g. `--metric conns.current` |
| `--step SECONDS` | 60 | Timeline bucket size |
| `--from` / `--to` | | Explicit time bounds (same formats as `filter`) |
| `--json` | | Machine-readable output |

**Performance note.** `diagnostic.data` can hold weeks of per-second samples —
a few hundred megabytes covering thousands of chunks and several thousand
metrics each. Decoding all of it is CPU-bound and takes minutes, so these
commands **default to the last 4 hours** and skip older chunks before
decompressing them. On a 250 MB directory that is the difference between about
a second and about a minute. Use `--last`/`--from`/`--to` to move the window,
and `--all` when you really do want the whole history.

```bash
mdbkit ftdc summary /var/lib/mongodb/diagnostic.data
mdbkit ftdc timeline diagnostic.data --metric conns.current --step 300
mdbkit ftdc export diagnostic.data > metrics.csv
```

Metric labels include `ops.*` (insert/query/update/delete/getmore/command),
`conns.current`, `conns.available`, `queue.readers`, `queue.writers`,
`cache.usedBytes`, `cache.maxBytes`, `cache.dirtyBytes`, `tickets.*`,
`mem.residentMB`, and on Linux `sys.cpu.*` and `sys.mem.availableKB`.

The data directory can be copied off the host and analyzed elsewhere — it
contains metrics only, never document contents.

---

### Shareable reports — `--report FILE`

`triage` and `queries` can write a self-contained report instead of printing to
the terminal — for a ticket, a handover, or a post-incident review.

```bash
mdbkit triage mongod.log --report incident.html     # styled, self-contained
mdbkit triage mongod.log --report incident.md       # for tickets and PRs
mdbkit queries mongod.log --limit 20 --report slow-queries.md
```

The format follows the file extension: `.html` or `.md`.

Markdown output looks like this:

```markdown
# MongoDB incident triage

*window 2026-07-01 08:10 -> 09:10  ·  generated 2026-07-01 09:12*

## Findings

- **[CRIT] Replica set instability** — 3 election/stepdown event(s) at 08:41:02, 08:58:14
    - Starting an election, since we've seen no PRIMARY in election timeout period
    - *next:* `Correlate with connection storms and slow checkpoints below`
- **[WARN] Connection storm** — 2 minute(s) at >= 60 new connections/min; peak 480 at 08:41
    - 10.2.1.7: 312 in the peak minute
    - *next:* `mdbkit connections <log>`
- **[OK] Errors** — No error/fatal severity lines in window.
```

The HTML version carries the same content with a dark, print-friendly
stylesheet. It is **fully self-contained**: inline CSS, no JavaScript, no
external assets or CDN references, so it opens on an air-gapped machine and
sends nothing anywhere.

Reports contain the same information as the terminal output — query **shapes**
and metrics, never literal values from your documents.

---

### `mdbkit export-script {schema|indexes}`

Prints a small `mongosh` script to stdout. **mdbkit never connects to your
database** — you run these yourself, so you can read exactly what they do
first. Both are read-only and export **field names and types only, never
document values**.

```bash
mdbkit export-script indexes > export_indexes.js
mdbkit export-script schema  > export_schema.js
```

---

## Roadmap

Terminal output is and will remain first-class — this tool is built for the
Linux box the database actually runs on.

**Shipped in v0.2:** FTDC decoding, incident triage with system metrics,
query reconstruction (`--as-explain`), and shareable Markdown/HTML reports.

Next up:
* Per-shape drill-down (`mdbkit queries --shape N` with full detail).
* `serverStatus` snapshot digest for triage (two snapshots for true rates).
* Graduating the remaining beta detectors (checkpoints, eviction, flow
  control) once validated against real incident logs — see
  `docs/TESTING-PLAYBOOK.md`. Real logs very welcome.

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
