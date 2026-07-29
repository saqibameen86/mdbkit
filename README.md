# mdbkit

[![CI](https://github.com/saqibameen86/mdbkit/actions/workflows/ci.yml/badge.svg)](https://github.com/saqibameen86/mdbkit/actions)
[![PyPI](https://img.shields.io/pypi/v/mdbkit.svg)](https://pypi.org/project/mdbkit/)
[![Python](https://img.shields.io/pypi/pyversions/mdbkit.svg)](https://pypi.org/project/mdbkit/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**An offline toolkit for MongoDB structured logs** — slow-query analysis,
deterministic index advice, incident triage, and diagnostic-data decoding.
For MongoDB 4.4 – 8.0, from the terminal, without connecting to anything.

A spiritual successor to mtools' log tools, which never learned to read the
JSON log format introduced in 4.4.

```
namespace    op         count  cumMs  docsEx      scan     plan           shape
shop.events  aggregate  29     3.2m   25,810,000  98889:1  COLLSCAN+SORT  {tenantId:eq, ts:gte} sort:{ts:-1}
shop.orders  find       48     1.4m   6,000,000   2976:1   COLLSCAN+SORT  {status:eq, createdAt:gt}
shop.users   find       31     3.7s   31          1:1      IXSCAN{email}  {email:eq}
```

Two of those need an index. One is already fine. That distinction is the
whole point.

---

## Try it right now — no MongoDB required

`mdbkit demo` writes a realistic log containing a real incident, so you can
evaluate the tool in about a minute without touching a cluster:

```bash
pip install mdbkit

mdbkit demo --with-extras -o demo.log     # a log + indexes.json, schema.json, explain.json

mdbkit loginfo demo.log                   # what is in this log?
mdbkit queries demo.log                   # which query shapes cost the most?
mdbkit triage demo.log --window 0         # what went wrong, and when?
mdbkit connections demo.log               # who connected, and did anyone fail to?
mdbkit advise demo.log --indexes indexes.json --schema schema.json
mdbkit explain explain.json               # read a saved explain plan
```

The generated log contains a connection storm from one client, a replica set
election, an index build, five failed logins from a service account, and an
aggregation burning 25 million document reads to return 261 documents — then
`advise` tells you which index fixes it.

Output is deterministic: the same `--seed` always produces the same log, so a
demo behaves identically every time. Scenarios are `incident`, `healthy` (the
control case — useful for seeing what "nothing wrong" looks like) and `mixed`.

Ready for a real server? Jump to [the workflows](#the-four-questions-it-answers).

---

## Is it safe to run on a production server?

This is the right question to ask of any tool someone hands you. The honest
answer, and how to check it yourself.

**What mdbkit never does:**

| | |
|---|---|
| Connect to your database | Analysis commands read **files**. There is no driver, no URI, no connection. |
| Send anything anywhere | There is no network code at all. No telemetry, no update check, no crash reporting. |
| Change anything | It is strictly read-only. Where an action would help, it **prints the command** for you to review and run. |
| Execute what it reads | Log lines and explain files are parsed as data with `json.loads`. Nothing is ever evaluated. |
| Pull in dependencies | Zero runtime dependencies. Nothing in the supply chain but the Python standard library. |

**Verify it yourself in 60 seconds** — this is a small, dependency-free
codebase specifically so that you can:

```bash
# 1. No network, no shell-outs, no eval anywhere in the analysis code
pip show -f mdbkit | head -3
grep -rn "socket\|urllib\|requests\|http\|eval(\|exec(" $(python -c "import mdbkit,os;print(os.path.dirname(mdbkit.__file__))")

# 2. Confirm it has no dependencies
pip show mdbkit | grep Requires

# 3. Watch it make no connections while it runs (Linux)
strace -f -e trace=network mdbkit queries mongod.log 2>&1 | grep -c socket
```

The grep returns nothing for every analysis module. The only file that starts
a process is `lab.py`, which exists to create a *throwaway test cluster* and
is documented as an explicit exception below.

**What it does read:** the log file you point it at; optionally
`diagnostic.data` (metrics only, never documents); optionally `indexes.json`
and `schema.json` that **you** generate with scripts mdbkit prints for you to
inspect first. On the database host it also reads `/proc` and calls `statvfs`
for disk and memory figures — nothing that leaves the machine.

**What leaves your machine: nothing.** There is no server to send it to.

**Still cautious?** That is reasonable. Run `mdbkit demo` first and see what
the output looks like on synthetic data, or `mdbkit lab` to try it against a
disposable local cluster before you point it at anything real. Both exist for
exactly this reason.

Full detail: [SECURITY.md](SECURITY.md).

---

## Install

**Most systems:**
```bash
pip install mdbkit
```

**Ubuntu 20.04 / Debian / Amazon Linux 2 (Python 3.8 hosts):**
```bash
sudo apt install pipx        # or: sudo dnf install pipx
pipx install mdbkit
pipx ensurepath && source ~/.bashrc
```

**Modern Ubuntu/Debian complaining about "externally-managed-environment":**
```bash
pip install mdbkit --break-system-packages
```

**Air-gapped database hosts:**
```bash
pip download mdbkit -d ./wheels          # on a connected machine
# copy ./wheels across, then:
pip install --no-index --find-links ./wheels mdbkit
```

**Upgrading:**
```bash
pip install --upgrade mdbkit             # or: pipx upgrade mdbkit
mdbkit --version
```

Requires Python 3.8+. mdbkit never updates itself and never checks for
updates — upgrades are always explicit.

> mdbkit is a Python package on PyPI. It is **not** in `apt`/`dnf`/`yum`.

---

## The four questions it answers

Every command reads files or stdin and accepts several files or a glob, so
rotated logs work as one stream: `mdbkit queries "mongod.log*"`.

### 1. Why is my database slow?

```bash
mdbkit queries mongod.log                       # shapes ranked by total time
mdbkit queries mongod.log --sort scanRatio      # worst examined:returned first
mdbkit queries mongod.log --shape 1             # full detail on one shape
```

`cumMs` is time summed across **all** occurrences of a shape, not one query.
`scan` is documents examined per document returned — `1:1` is healthy,
`98889:1` is a missing index. `plan` shows what MongoDB actually chose.

### 2. What index would fix it?

```bash
# Optional but much sharper: export what already exists.
mdbkit export-script indexes > export_indexes.js
mdbkit export-script schema  > export_schema.js

mongosh --quiet --host your_db_host --port 27017 \
  --username your_username --password your_password \
  --authenticationDatabase admin \
  --eval "$(cat export_indexes.js)" > indexes.json      # repeat for schema

mdbkit advise mongod.log --indexes indexes.json --schema schema.json --ns shop.orders
```

Every recommendation states the evidence it reasoned from, a confidence
level, the caveats, and how to validate it. It says *candidate*, not
*command*, and it never tells you to drop an index.

### 3. What happened at 3am?

```bash
mdbkit triage /var/log/mongodb/mongod.log       # last 60 minutes by default
mdbkit triage mongod.log --window 0             # the whole file
mdbkit triage mongod.log --report incident.html # something to attach to a ticket
```

Cluster health, elections, connection storms, hot collections, index builds,
error clusters, slow-query peaks — plus disk, memory, CPU and FTDC metrics
when run on the database host. Every finding ends with the next command to
run.

### 4. Did my change actually help?

```bash
mdbkit compare before.log --after after.log
```

```
slow-query time DOWN 32%  (6.0m -> 4.1m across compared shapes)
shapes: 1 improved, 0 regressed, 0 new, 0 gone, 4 unchanged

IMPROVED
  shop.orders {createdAt:gt, status:eq} sort:{createdAt:-1}
    mean 1.7s -> 33ms (-98%)   scan 2976:1 -> 1:1  [COLLSCAN -> index, in-memory sort gone]
```

The natural follow-up to `advise`: you created the index, a day passed, and
this tells you whether it worked.

**Bonus — who connected?**

```bash
mdbkit connections mongod.log
```

Per-IP churn with first/last seen, plus an authenticated-users table showing
successful and failed logins per account and when each last authenticated —
the question that starts most access incidents.

---

## Trying it against a real cluster

`mdbkit lab` starts a **disposable local MongoDB** so you can test against a
real server without touching anything that matters.

```bash
mdbkit lab start                    # 3-node replica set on 127.0.0.1:28110-28112
mdbkit lab seed                     # 50k documents + a deliberately mixed workload
mdbkit queries $(mdbkit lab logs | head -1)
mdbkit lab destroy --yes            # remove it entirely
```

It binds to localhost only, uses ports far from 27017 so it can never be
confused with a real deployment, and refuses to touch any directory it did
not create. It is the one command that starts external processes — see
[SECURITY.md](SECURITY.md).

Full options and more examples: [`mdbkit lab`](#mdbkit-lab) in the reference.

---

## Command reference

Every command reads files or stdin and writes to stdout. `--help` works on any
command (`mdbkit queries --help`). Global: `mdbkit --version`.

All commands that read a log accept one or more paths, a shell glob, a
rotated `.gz` file, or `-` for stdin. Several files are read as a single
stream in filename order, which matches MongoDB's rotation naming:

```bash
mdbkit queries mongod.log                       # one file
mdbkit queries mongod.log.1 mongod.log          # explicit list
mdbkit queries "mongod.log*"                    # glob (quote it)
mdbkit queries /var/log/mongodb/mongod.log.*.gz # compressed archives
cat mongod.log | mdbkit queries -               # stdin
```

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
mdbkit queries "mongod.log*"              # rotated logs as one stream
mdbkit queries mongod.log --shape 1       # drill into the worst offender
```

`--shape N` expands one row of the table:

```
namespace : shop.events
shape     : {tenantId:eq, ts:gte} sort:{ts:-1}

occurrences   : 29
total time    : 3.2m
mean / max    : 6.6s / 8.9s
docs examined : 25,810,000
docs returned : 261
scan ratio    : 98889 examined per document returned

plans observed
  COLLSCAN                                 29x

flags
  COLLSCAN — no index used for at least one execution
  in-memory SORT — results sorted after retrieval

client applications
  ReportWorker                   15x
  OrderService                   7x
```

---

### `mdbkit connections <log>`

Connection churn and **who authenticated**: totals, peak concurrent count,
per-source-IP breakdown with first/last seen, the client applications and
drivers, and a per-user table.

| Option | Description |
|---|---|
| `--json` | Machine-readable output |

```bash
mdbkit connections mongod.log
```

```
source ip   accepted  ended  first seen           last seen            appName
----------  --------  -----  -------------------  -------------------  ------------
10.20.9.77  220       0      2026-07-01 08:49:30  2026-07-01 08:49:30  checkout-api
10.20.4.11  4         1      2026-07-01 08:00:15  2026-07-01 09:29:30  OrderService

authenticated users
user          auth db  ok   failed  last authenticated   from
------------  -------  ---  ------  -------------------  -----------
svc_checkout  admin    221  0       2026-07-01 08:49:30  10.20.9.77
etl_batch     admin    0    5       2026-07-01 08:51:54  10.20.11.40

  etl_batch: 5 failed authentication(s) — last error: AuthenticationFailed
```

This answers the question that starts most access incidents: *did that
account connect, from where, and when last?* If the log shows no
authentication events at all, mdbkit says so — either auth is disabled, or
the window contains no new logins because clients are reusing connections.

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

### `mdbkit demo`

Generates a realistic MongoDB structured log so you can evaluate mdbkit — or
run a live demo — without a cluster. Output is deterministic for a given
seed, so a demo behaves identically every time, including on a projector.

| Option | Default | Description |
|---|---|---|
| `--scenario` | `mixed` | `healthy`, `incident`, or `mixed` |
| `--minutes N` | 90 | How much log time to generate |
| `--seed N` | 7 | Same seed produces byte-identical output |
| `-o, --out FILE` | stdout | Write to a file |
| `--with-extras` | off | Also write `indexes.json`, `schema.json` and `explain.json` beside the log |

```bash
mdbkit demo -o demo.log                          # 90 minutes, mixed
mdbkit demo --scenario incident --minutes 30 -o incident.log
mdbkit demo --scenario healthy -o quiet.log      # nothing wrong: the control case
mdbkit demo | mdbkit queries -                   # straight down a pipe
```

The `incident` scenario contains an index build, a connection storm from a
single client, a replica set election, plan-executor errors, a slow
WiredTiger checkpoint, and a burst of unindexed queries afterwards — the
shape of a real bad afternoon.

---

### `mdbkit lab`

Starts a **disposable local MongoDB** for testing, reproducing a slow query,
or rehearsing a demo. This is the only command that starts external
processes; see [SECURITY.md](SECURITY.md) for exactly how it is bounded.

Requires `mongod` on your `PATH` (and `mongosh` to initiate the replica set
and seed data). Linux and macOS.

| Action | What it does |
|---|---|
| `start` | Create and start a replica set, print the connection string and log paths |
| `seed` | Insert sample data and run a workload with deliberately interesting queries |
| `status` | Show ports, pids and whether each node is running |
| `logs` | Print the log file paths, ready to pipe into other commands |
| `stop` | Stop the nodes, keep the data |
| `destroy` | Stop and delete the lab (requires `--yes`) |

| Option | Default | Description |
|---|---|---|
| `--dir PATH` | `~/.mdbkit-lab` | Where the lab lives |
| `--nodes N` | 3 | Replica set size |
| `--port N` | 28110 | Base port — deliberately far from 27017 |
| `--slowms N` | 0 | Log every operation, which is what makes the log worth reading |
| `--standalone` | off | Single node, no replica set |
| `--docs N` | 50000 | Documents inserted by `seed` |
| `--yes` | | Confirm `destroy` |

**The full loop:**

```bash
mdbkit lab start                    # 3-node replica set on 28110-28112
mdbkit lab seed                     # sample data + a mixed workload

mdbkit queries $(mdbkit lab logs | head -1)
mdbkit advise  $(mdbkit lab logs | head -1)

mdbkit lab destroy --yes            # remove everything
```

**`mdbkit lab logs`** prints the log file path of every node, one per line,
so it composes with the other commands instead of you hunting for paths:

```bash
mdbkit lab logs
# /home/you/.mdbkit-lab/node0/mongod.log
# /home/you/.mdbkit-lab/node1/mongod.log
# /home/you/.mdbkit-lab/node2/mongod.log

mdbkit queries $(mdbkit lab logs | head -1)     # just the primary
mdbkit triage  $(mdbkit lab logs)               # all three as one stream
mdbkit loginfo $(mdbkit lab logs | sed -n 2p)   # a specific secondary
```

**A single node**, when you do not need replication — faster to start and it
does not require `mongosh`:

```bash
mdbkit lab start --standalone
mdbkit lab seed --docs 5000
mdbkit queries $(mdbkit lab logs)
mdbkit lab destroy --yes
```

**Several labs side by side**, for example to compare two MongoDB versions or
keep one running while you break another:

```bash
mdbkit lab start --dir ~/lab-a --port 28110
mdbkit lab start --dir ~/lab-b --port 28210 --standalone

mdbkit lab status --dir ~/lab-a
mdbkit lab destroy --dir ~/lab-b --yes
```

**Pause without losing data** — `stop` leaves the data directory intact so
you can start again later; only `destroy` deletes anything:

```bash
mdbkit lab stop                     # nodes down, data kept
mdbkit lab start                    # back up with the same data
mdbkit lab status                   # ports, pids, running or not
```

**A complete before/after experiment**, which is what `lab` is really for:

```bash
mdbkit lab start && mdbkit lab seed
cp $(mdbkit lab logs | head -1) before.log

mongosh --port 28110 --eval \
  'db.getSiblingDB("shop").orders.createIndex({status:1, createdAt:-1})'

mdbkit lab seed                     # run the workload again with the index
cp $(mdbkit lab logs | head -1) after.log

mdbkit compare before.log --after after.log
mdbkit lab destroy --yes
```

`seed` runs indexed point lookups alongside deliberately unindexed queries —
an equality-plus-range-plus-sort with no supporting index, an aggregation
that scans the collection, and updates whose predicate has no index — so the
log immediately contains something worth analysing.

**Safety.** The lab binds to `127.0.0.1` only, refuses to use or delete any
directory it did not create, and never touches a MongoDB it did not start.
It is a laptop and scratch-VM tool, not a deployment tool.

---

### `mdbkit compare BEFORE --after AFTER`

Diffs query shapes between two logs and reports what improved, what
regressed, and what is new. The natural follow-up to `advise`: you created an
index, a day passed, and this answers whether it worked.

| Option | Default | Description |
|---|---|---|
| `--after FILE...` | required | The log(s) from after the change |
| `--ns NAMESPACE` | all | Compare only one namespace |
| `--min-count N` | 3 | Ignore shapes seen fewer than N times, so noise in a quiet log does not read as a regression |
| `--min-ms N` | 0 | Ignore operations faster than this |
| `--limit N` | 15 | Shapes to print (`0` = all) |
| `--include-system` | off | Include internal `admin`/`config`/`local` namespaces |
| `--report FILE` | | Write a shareable `.md` or `.html` report |
| `--json` | | Machine-readable output |

```bash
mdbkit compare before.log --after after.log
mdbkit compare before.log --after after.log --ns shop.orders
mdbkit compare "old/mongod.log*" --after "new/mongod.log*" --report change.html
```

```
slow-query time DOWN 32%  (6.0m -> 4.1m across compared shapes)
shapes: 1 improved, 0 regressed, 0 new, 0 gone, 4 unchanged

IMPROVED
  shop.orders {createdAt:gt, status:eq} sort:{createdAt:-1}
    mean 1.7s -> 33ms (-98%)   scan 2976:1 -> 1:1  [COLLSCAN -> index, in-memory sort gone]
```

A shape counts as improved or regressed on a plan change (COLLSCAN becoming
an index scan, or the reverse), on an in-memory sort disappearing, or on mean
duration moving by more than 20%.

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

**Shipped in v0.4:** `compare`, rotated-log globbing, per-shape drill-down —
on top of v0.3's `demo` and `lab`, and v0.2's FTDC decoding, incident triage,
query reconstruction and shareable reports.

Next up, roughly in order:

* **Sharded clusters.** `mongos` logs are a different shape, and the classic
  sharded failure — a query with no shard key fanning out to every shard — is
  visible in the log. Also chunk migrations, balancer windows and jumbo
  chunks. Would come with `mdbkit lab --sharded` so it can be tested.
* **Startup configuration audit.** mongod logs warnings at startup about
  transparent huge pages, readahead, ulimits, NUMA and filesystem choice.
  These are classic production misconfigurations and they are already in
  your log — nothing new needs collecting.
* **Redundant index detection** from `indexes.json` alone: an index on
  `{a: 1}` is redundant when `{a: 1, b: 1}` exists. Purely offline, no
  connection, no `$indexStats` needed.
* **Confirming the FTDC-based checkpoint, eviction and flow-control
  detectors** against real `diagnostic.data` — see
  `docs/TESTING-PLAYBOOK.md`. Real logs and metrics very welcome.

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
