# mdbkit

[![CI](https://github.com/saqibameen86/mdbkit/actions/workflows/ci.yml/badge.svg)](https://github.com/saqibameen86/mdbkit/actions)
[![PyPI](https://img.shields.io/pypi/v/mdbkit.svg)](https://pypi.org/project/mdbkit/)
[![Python](https://img.shields.io/pypi/pyversions/mdbkit.svg)](https://pypi.org/project/mdbkit/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**An offline toolkit for MongoDB logs and diagnostics** — slow-query
analysis, deterministic index advice, unused and redundant indexes, incident
triage, sharded clusters, hosts running many mongods, a startup
configuration audit, and diagnostic-data (FTDC) decoding. For MongoDB 4.4
through 9.0, from the terminal, without connecting to anything.
**[Tested against real MongoDB 9.0.2, 8.3.11, 8.0.32, 7.0.43 and 6.0.29](#compatibility)**
— replica sets, standalones and sharded clusters (October 2026).

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
pip install mdbkit          # macOS has no pip by default — see Install below

mdbkit demo --with-extras -o demo.log     # a log + indexes.json, schema.json, explain.json

mdbkit loginfo demo.log                   # what is in this log?
mdbkit queries demo.log                   # which query shapes cost the most?
mdbkit triage demo.log --window 0         # what went wrong, and when?
mdbkit audit demo.log                     # what did mongod warn about at startup?
mdbkit connections demo.log               # who connected, and did anyone fail to?
mdbkit advise demo.log --indexes indexes.json --schema schema.json
mdbkit explain explain.json               # read a saved explain plan
```

The generated log contains a connection storm from one client, a replica set
election, an index build, five failed logins from a service account, and an
aggregation burning 25 million document reads to return 261 documents — then
`advise` tells you which index fixes it.

Output is deterministic: the same `--seed` produces the same log, byte for
byte, on any machine and any Python version, so a demo behaves identically
every time. (Before 0.6.0 this held only within one Python process; across
runs some identifiers changed. Fixed.) Scenarios are `incident`, `healthy` (the
control case — useful for seeing what "nothing wrong" looks like) and `mixed`.

Ready for a real server? Jump to [the workflows](#the-questions-it-answers).

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
grep -rn --include='*.py' "socket\|urllib\|requests\|http\|eval(\|exec(" $(python -c "import mdbkit,os;print(os.path.dirname(mdbkit.__file__))")

# 2. Confirm it has no dependencies
pip show mdbkit | grep Requires

# 3. Watch it make no connections while it runs (Linux)
strace -f -e trace=network mdbkit queries mongod.log 2>&1 | grep -c socket
```

The grep matches exactly one file, `lab.py`: before it starts a *throwaway
test cluster* it tries to bind `127.0.0.1:<port>` to check the port is free.
That is also the only file that starts a process. Every analysis module comes
back clean. The lab is documented as an explicit exception below.

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

### Linux

**Most distributions:**
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

### macOS

macOS ships no `pip` and no `pipx`, so `pip install` fails out of the box.
Pick whichever of these matches your setup.

**With Homebrew (recommended — keeps mdbkit in its own environment):**
```bash
brew install pipx
pipx ensurepath          # then open a new terminal window
pipx install mdbkit
```

**Without Homebrew, using the Python that comes with macOS:**
```bash
python3 --version        # accept the Command Line Tools prompt if it appears
python3 -m pip install --user mdbkit
```

Then put the install location on your `PATH` — macOS does not do this for
you:
```bash
echo 'export PATH="$HOME/Library/Python/'"$(python3 -c 'import sys;print(f"{sys.version_info.major}.{sys.version_info.minor}")')"'/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
mdbkit --version
```

**If you use uv:**
```bash
uv tool install mdbkit
```

**If pip reports `externally-managed-environment`**, add
`--break-system-packages`, or use the pipx route above, which avoids the
problem entirely.

Do not install with `sudo`. mdbkit is a user-level CLI and needs no
elevated privileges — not to install, and not to run.

### Windows

```powershell
py -m pip install mdbkit
py -m mdbkit --version
```

If `mdbkit` is not recognised as a command afterwards, the Scripts directory
is not on your `PATH`; `py -m mdbkit` works regardless.

### Air-gapped database hosts

```bash
pip download mdbkit -d ./wheels          # on a connected machine
# copy ./wheels across, then:
pip install --no-index --find-links ./wheels mdbkit
```

Zero runtime dependencies means this is a single wheel — nothing else to
resolve.

### Upgrading

Use whichever matches how you installed it.

```bash
# Linux — installed with pip
pip install --upgrade mdbkit

# Linux — installed with pipx
pipx upgrade mdbkit

# macOS — installed with pipx (Homebrew route)
pipx upgrade mdbkit

# macOS — installed with python3 -m pip --user
python3 -m pip install --user --upgrade mdbkit

# macOS / Linux — installed with uv
uv tool upgrade mdbkit

# Windows
py -m pip install --upgrade mdbkit
```

Then confirm:
```bash
mdbkit --version
```

**If the version has not changed**, pip is serving a cached index. Force it:

```bash
pipx install --force mdbkit                          # pipx
pip install --upgrade --no-cache-dir mdbkit          # pip
py -m pip install --upgrade --no-cache-dir mdbkit    # Windows
```

Requires Python 3.8+. mdbkit never updates itself and never checks for
updates — upgrades are always explicit.

> mdbkit is a Python package on PyPI. It is **not** in `apt`/`dnf`/`yum`.

---

## Compatibility

**Tested against (October 2026):** MongoDB **9.0.2**, **8.3.11**, **8.0.32**
(LTS), **7.0.43** (LTS) and **6.0.29**, the newest release in each series at
the time: replica sets and standalones on all five, and sharded clusters
(mongos, shards and config servers) on 6.0, 7.0, 8.0 and 9.0. mdbkit reads the structured JSON log format, so anything
from **4.4** onwards works; 4.2 and earlier wrote plain text logs and are not
supported.

| MongoDB | What mdbkit uses from it |
|---|---|
| 4.4 – 7.0 | Everything except the 8.x-only fields below |
| 8.0 | `workingMillis` (time executing, as distinct from waiting), `queues.execution.totalTimeQueuedMicros` (ticket queue time), `queryShapeHash` (usable with `setQuerySettings`), `planCacheShapeHash`, the 8.0 transparent-huge-page guidance, and FTDC's new layout (sharded processes group metrics by role; WiredTiger checkpoint and eviction statistics were renamed) |
| 8.1+ | `<stage>Spills` / `<stage>SpilledBytes` (disk spills per stage) |
| 8.3+ | "Slow in-progress query" lines (id 1794200, component `SLOWPROG`), logged by default for operations still running after 5 s (`--defaultSlowInProgMS`), and `peakTrackedMemBytes` |
| 9.0 | Same log fields as 8.3. Adds a few fields mdbkit does not use yet (`delinquencyInfo`, `queues.ingress`) |

When a field is missing because the log predates it, that part of the output
is left out. It is never guessed. Where MongoDB moved a metric between
versions (concurrency tickets, checkpoint times, eviction), mdbkit reads both
the old and the new location.

**How it was tested.** Official MongoDB builds of each version were run
with `mdbkit lab` as a three-node replica set, a standalone and (6.0, 7.0,
8.0 and 9.0) a sharded cluster with a config server, two shards and a
mongos. Then each one was broken on purpose, in the ways mdbkit is meant to
spot:

* unindexed queries, sorts that spill to disk, writes blocked behind
  `fsyncLock` and long-running operations;
* a clean restart, and a primary killed with `kill -9` (the crash, the
  failover and the recovery);
* a stepdown, and a connection storm of 300 connections at once;
* flow control, with both secondaries frozen by `fsyncLock` while the
  primary took writes;
* cache pressure from a working set four times the cache, and a checkpoint
  that ran for over a minute (the process frozen mid-checkpoint);
* chunk migrations (one failing on purpose), the balancer moving data on its
  own, and a shard killed while queries ran.

Every mdbkit command was run on the resulting logs, `diagnostic.data`,
serverStatus, explain, `getLog` and `$indexStats` output, and compared with
what the servers did. Trimmed copies of that output are in
[`tests/fixtures/real/`](tests/fixtures/real/) and run with every test.

If mdbkit misreads a line from your deployment, please
[open an issue](../../issues) with the line (redact as you like).

---

## The questions it answers

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
`98889:1` is a missing index. `plan` shows what MongoDB actually chose, with
`+SPILL` when the sort or group overflowed to disk.

On MongoDB 8.0+ mdbkit also separates **executing** from **waiting**. A
query that took 2 seconds but worked for 200 ms is not an index problem. It
was queued behind tickets, locks or flow control, and adding an index will
not help.

### 2. What index would fix it?

```bash
# Optional but much sharper: export what already exists.
mdbkit export-script indexes > export_indexes.js
mdbkit export-script schema  > export_schema.js

mongosh --quiet "mongodb://your_db_host:27017/" \
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

Crashes and restarts, elections, connection storms, flow control, hot
collections, index builds, error clusters, slow-query peaks, startup
misconfiguration, time spent waiting rather than working (8.0+),
still-running operations (8.3+), and on sharded clusters scatter-gather
queries, chunk migrations and unreachable shards — plus disk, memory, CPU,
cache pressure and checkpoints from FTDC when run on the database host.
Every finding ends with the next command to run. A shutdown followed by the
server listening again is reported as a restart (WARN); a start with no
clean shutdown before it, as a crash (CRIT).

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

### 5. Is this server configured properly?

```bash
mdbkit audit /var/log/mongodb/mongod.log
```

```
[CRIT] Access control is not enabled
        mongod said: Access control is not enabled for the database. Read and write access to data and configuration is unrestricted
        fix: Anyone who can reach the port can read and write every database. Enable security.authorization ...

[WARN] Transparent huge pages should be re-enabled (8.0+ guidance)
        mongod said: For customers running the current memory allocator, we suggest re-enabling transparent hugepages [...]
        fix: MongoDB 8.0 recommends THP enabled for its new memory allocator, the opposite of the advice for 7.0 and earlier.
```

mongod checks its environment at every start and logs a warning for each
thing it does not like: no access control, low file limits, transparent huge
pages, NUMA, swappiness, running as root. They sit in the log right after
every restart, where almost nobody reads them. `triage` raises them too.

### 6. Why did it die? (and what does serverStatus say?)

```bash
mdbkit oslog /var/log/syslog                  # OOM kills, fd limits, I/O errors
mdbkit triage mongod.log --oslog /var/log/syslog
```

A mongod log cannot record its own OOM kill — the process is gone before it
can write anything. The system log has the answer in one line, and `triage`
will correlate it with the unexplained restart.

```bash
mdbkit export-script serverstatus > export_serverstatus.js
mongosh --quiet --host HOST --eval "$(cat export_serverstatus.js)" > status.json
mdbkit serverstatus status.json
```

```
[CRIT] Concurrency tickets: Exhausted tickets queue every new operation,
       which looks like slowness with no slow query to blame.
        - read: 1 of 128 free (1%)
[CRIT] WiredTiger cache: Above 80% WiredTiger evicts in the background;
       above 95% application threads are made to evict.
        - 31.0 GiB of 32.0 GiB used (96.9%)
```

**Bonus — who connected?**

```bash
mdbkit connections mongod.log
```

Per-IP churn with first/last seen, plus an authenticated-users table showing
successful and failed logins per account and when each last authenticated —
the question that starts most access incidents.

### 7. Is this host running too many mongods for its memory?

```bash
mdbkit host /var/log/mongodb            # every instance's log under here
```

```
== mdbkit host: 8 instance(s) on vm ==
last 1440 minutes of each log | 7.8 GiB RAM (this machine)

[CRIT] Cache sizes vs RAM: WiredTiger caches add up to 11.8 GiB, 150% of 7.8 GiB RAM (this machine).
[CRIT] Instances that crashed: 1 instance(s) started after an unclean stop (crash, kill -9 or OOM kill).
        - vm:29021: 1 unclean start(s)
[WARN] Default cache size on a shared host: 3 of 8 mongod instances run with the default WiredTiger cache (cacheSizeGB not set). [...]

instance  set   version  role        cache    starts  crashes  slow ops  slow time  waiting  worst  first issue
--------  ----  -------  ----------  -------  ------  -------  --------  ---------  -------  -----  --------------------------
vm:29021  rs02  8.0.32   SECONDARY   256 MiB  2       1        0         -          -        CRIT   Process start(s) in window
vm:29030  -     8.0.32   standalone  512 MiB  1       0        9         472ms      0%       WARN   Collection scans
vm:29010  rs01  8.0.32   PRIMARY     3.4 GiB  1       0        3         271ms      0%       WARN   Collection scans
[...]
```

For hosts that run many mongods side by side, typically one member of each
of many replica sets. The usual failure is that every instance was left on
the default WiredTiger cache, which assumes the machine is its alone; the
caches add up to several times the RAM and the OOM killer picks off
instances. (Output above is from a real 8-instance test host.)

### 8. Is my sharded cluster sending queries to every shard?

```bash
mdbkit queries /var/log/mongodb/mongos.log     # how each query was routed
mdbkit triage  /var/log/mongodb/mongos.log     # scatter-gather, unreachable shards
mdbkit triage  /var/log/mongodb/shard1.log     # chunk migrations and why they failed
```

```
namespace    op         count  cumMs  mean  max   shards  to all  shard wait  shape
shop.orders  find       50     520ms  10ms  28ms  2       50/50   97%         {createdAt:gt, status:eq} sort:{createdAt:-1}
shop.orders  aggregate  20     291ms  14ms  20ms  2       20/20   90%         {status:eq}
shop.orders  find       80     19ms   0ms   3ms   1       0/80    21%         {customerId:eq}
```

The router's log shows which query shapes went to every shard (usually no
shard key in the filter) and which went to one. The shards' logs show chunk
migrations, how much they moved and why any failed; the config server's
shows balancer errors. Plans live on the shards, so run `queries` and
`advise` on a shard log for index advice. (Output from a real 8.0 cluster,
sharded on `customerId`.)

### 9. Which indexes are not earning their keep?

```bash
mdbkit export-script indexes > export_indexes.js
mongosh --quiet "mongodb://your_db_host:27017/" --eval "$(cat export_indexes.js)" > indexes.json
mdbkit indexes indexes.json
```

```
exported from: a mongos (usage from each shard's primary)

Unused since the counters started (1)
  shop.orders  customerId_1_status_1 { customerId: 1, status: 1 } — 0 use(s)
      no recorded use since the counters started
      test safely: db.getSiblingDB("shop").getCollection("orders").hideIndex("customerId_1_status_1")

Redundant: a prefix of another index (1)
  shop.orders  status_1 { status: 1 } — 20 use(s)
      its key is a prefix of status_1_createdAt_-1 { status: 1, createdAt: -1 }, which can serve the same queries
      test safely: db.getSiblingDB("shop").getCollection("orders").hideIndex("status_1")

Unused, but not a candidate (1)
  shop.orders  createdAt_1 { createdAt: 1 } — 0 use(s)
      unused for queries, but it is a TTL index: the TTL monitor uses it, which the counters do not show
```

Every index costs write time, memory and disk. This lists the ones with no
recorded use and the ones another index already covers, and explains the
ones it deliberately leaves alone (unique, TTL, hidden, the shard key's
index). It never says "drop": `hideIndex` is the reversible test. (Output
from a real 8.0 cluster, exported through mongos.)

---

## Running it on a schedule

mdbkit is deterministic, offline and read-only, which makes it well suited to
a cron job. It deliberately **cannot send anything anywhere** — there is no
network code and never will be. So mdbkit produces the verdict and your own
script does the talking.

The primitive that makes this work is `--exit-code`:

| Exit code | Meaning |
|---|---|
| `0` | Nothing above INFO |
| `1` | At least one WARN |
| `2` | At least one CRIT |

```bash
#!/usr/bin/env bash
# /usr/local/bin/mdbkit-watch.sh — hourly health check
set -uo pipefail

LOG=/var/log/mongodb/mongod.log
OUT=$(mktemp)

mdbkit triage "$LOG" --window 60 --oslog /var/log/syslog \
       --only CRIT,WARN --exit-code > "$OUT" 2>&1
STATUS=$?

if [ "$STATUS" -ge 2 ]; then
    # CRIT: wake someone up. Your channel, your call — mdbkit stays offline.
    curl -sf -X POST -H 'Content-type: application/json' \
         --data "{\"text\": \"MongoDB CRIT on $(hostname)\n\`\`\`$(cat "$OUT")\`\`\`\"}" \
         "$SLACK_WEBHOOK_URL"
elif [ "$STATUS" -eq 1 ]; then
    mail -s "MongoDB warnings on $(hostname)" dba@example.com < "$OUT"
fi

rm -f "$OUT"
```

```cron
# hourly triage; only speaks up when something is wrong
0 * * * * /usr/local/bin/mdbkit-watch.sh

# daily slow-query digest, kept for trend comparison
30 6 * * * mdbkit queries /var/log/mongodb/mongod.log \
             --report /var/log/mdbkit/$(date +\%F).html
```

Because the reports are dated, `compare` turns them into a trend:

```bash
mdbkit compare /var/log/mongodb/mongod.log.1 --after /var/log/mongodb/mongod.log
```

Two things worth knowing. `--only CRIT,WARN` keeps the mail short, and a run
that finds nothing prints almost nothing — so a silent cron job means a
healthy database rather than a broken script. And because every command is
read-only and never connects to the database, running this hourly on a
production host costs one log read and no risk.

---

## Trying it against a real cluster

`mdbkit lab` starts a **disposable local MongoDB** so you can test against a
real server without touching anything that matters.

```bash
mdbkit lab start                    # 3-node replica set on 127.0.0.1:28110-28112
mdbkit lab seed                     # 50k documents + a deliberately mixed workload
mdbkit queries $(mdbkit lab logs | head -1)
mdbkit lab destroy --yes            # remove it entirely

mdbkit lab start --shards 2         # or a sharded cluster: config server, 2 shards, mongos
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

Overall log summary: server version, what wrote the log (replica set
member, standalone, mongos, shard or config server, and its replica set),
host, restarts, connections accepted, slow-query count, still-running
operations (8.3+), chunk migrations (on a shard), warning/error counts, and a
per-component line breakdown.

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
| `plan` | The plan MongoDB chose: `COLLSCAN` (no index), `IXSCAN{fields}` (index used), `IDHACK` (`_id` lookup), `+SORT` (in-memory sort), `+SPILL` (wrote temporary files to disk). `?` = the plan was not recorded on that line |
| `shape` | Fields and operators queried, with the sort |

Two kinds of row are worth knowing about. A `getMore(find)` or
`getMore(aggregate)` row is a cursor fetching more results; it is grouped
under the shape of the query that opened the cursor. Slow `insert` rows are
included even though no index can speed up an insert: a slow insert is still
latency your application saw, and on 8.0+ the `--shape` view says whether it
was executing or waiting (on locks, flow control or write concern).

**On a mongos (router) log** the table changes: a router records how each
query was routed but not its plan (the shards have that), so `docsEx`,
`scan` and `plan` give way to `shards` (how many shards each execution went
to), `to all` (executions sent to every shard: scatter-gather) and
`shard wait` (the share of time spent waiting for the shards). Cluster
administration commands (`moveRange`, `shardCollection`, ...) are left out;
`triage` on the shard logs reports migrations.

```bash
mdbkit queries mongod.log
mdbkit queries mongod.log --sort scanRatio --limit 10
mdbkit queries mongod.log --min-ms 500 --json
mdbkit queries "mongod.log*"              # rotated logs as one stream
mdbkit queries mongod.log --shape 1       # drill into the worst offender
```

`--shape N` expands one row of the table. On an 8.0+ log it also shows where
the time went and the identifiers MongoDB uses for this shape:

```
namespace : shop.events
operation : aggregate
shape     : {tenantId:eq, ts:gte} sort:{ts:-1}

occurrences   : 53
total time    : 5.7m
mean / max    : 6.5s / 8.7s
docs examined : 47,170,000
docs returned : 477
scan ratio    : 98889 examined per document returned

plans observed
  COLLSCAN                                 53x

flags
  COLLSCAN — no index used for at least one execution
  in-memory SORT — results sorted after retrieval

where the time went (MongoDB 8.0+)
  executing     : 5.5m
  waiting       : 13.3s (4%) — tickets, locks, flow control
  ticket queue  : 12.0s of the wait

resources
  disk spills   : 159 (13.6 GiB written)
  CPU time      : 5.5m total

server identifiers
  queryShapeHash     : BDC6B796B132EBAF671210F40FC5155C05ED250F79996E68DC6C620CBD8972AE
    pin or block this shape without a code change (8.0+):
    db.adminCommand({setQuerySettings: "BDC6...72AE", settings: {...}})
  planCacheShapeHash : 0F877C7D  (queryHash before 8.0)
```

The `queryShapeHash` is the same value MongoDB shows in `explain`, the
profiler, `$currentOp` and `$queryStats`, so you can follow one shape
across all of them. mdbkit only prints the `setQuerySettings` command. It
never runs it.

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
| `--component NAME` | `COMMAND`, `NETWORK`, `REPL`, `ELECTION`, `STORAGE`, `INDEX`, `WRITE`, `QUERY`, `SHARDING`, `CONTROL`, … |
| `--severity S` | `I` info, `W` warning, `E` error, `F` fatal |
| `--ns NAMESPACE` | Exact namespace, e.g. `shop.orders` |
| `--slow N` | Only operations with `durationMillis` >= N |
| `--from TIMESTAMP` | Lower time bound (inclusive) |
| `--to TIMESTAMP` | Upper time bound (inclusive) |
| `--msg TEXT` | Substring match on the message field |
| `--failed` | Only operations that ended in an error (logged with an `errName`) |
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
mdbkit filter mongod.log --component ELECTION      # elections and stepdowns
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
mongosh --quiet --host your_db_host --username your_username \
        --password your_password --authenticationDatabase admin \
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
mongosh --quiet \
  --host your_db_host \
  --port 27017 \
  --username your_username \
  --password your_password \
  --authenticationDatabase admin \
  --eval 'EJSON.stringify(db.getSiblingDB("shop").orders.find({status:"open"}).sort({ts:-1}).explain("executionStats"))' \
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
| `--ftdc PATH` | | `diagnostic.data` directory — adds CPU, memory, cache, checkpoint, eviction, flow-control and connection history from MongoDB's own recorder. Found automatically when the log is from this host (a mongos keeps it next to its log) |
| `--oslog FILE...` | | System log(s): OOM kills, file-descriptor limits and I/O errors that the mongod log cannot record |
| `--only LEVELS` | | Show only these severities, e.g. `--only CRIT,WARN` |
| `--exit-code` | | Exit 2 on CRIT, 1 on WARN, else 0 |
| `--report FILE` | | Write a shareable `.md` or `.html` report instead of terminal output |
| `--json` | | Machine-readable output |

```bash
mdbkit triage /var/log/mongodb/mongod.log
mdbkit triage mongod.log --window 30
mdbkit triage mongod.log --ftdc /var/lib/mongodb/diagnostic.data
mdbkit triage mongod.log --report incident.html
mdbkit triage mongod.log --window 0 --no-sysprobe
```

**Restarts and crashes.** A restart after a clean shutdown is a WARN; a start
with no clean shutdown before it is a CRIT, because that is what a crash,
`kill -9` or OOM kill looks like. mongod itself records whether its previous
shutdown was clean, so mdbkit can tell even when the crash happened before
the log you are reading begins.

**Elections.** An election because a member saw no primary is a CRIT: the
primary was lost (crash, kill, hang or network). A stepdown command ("step
up request") or a higher-priority member taking over is a WARN, worded as
probably deliberate. The first election of a newly initiated replica set is
INFO, not instability.

**Flow control, cache pressure and checkpoints.** Flow control is read from
the log (MongoDB warns every 10 seconds while it throttles writes) and from
FTDC (how long it was engaged and how long writers waited). Cache pressure
comes from FTDC: how long the cache sat past the points where application
threads must help evict (95% full or 20% dirty), and how much time they
spent evicting in the busiest minute. A slow checkpoint is a WARN past 60
seconds; FTDC records every checkpoint's duration, and MongoDB 8.3+ also
logs one that runs past 20 seconds. Each of these was checked on real 6.0,
7.0, 8.0 and 9.0 servers driven into that state.

**Sharded clusters.** On a mongos log, `triage` reports queries sent to every
shard (scatter-gather), time spent waiting on the shards, routing-table
refreshes, and shards or members the router could not reach. On a shard's
log it reports chunk migrations (how many, how much data, and why any
failed, e.g. waiting for the range deleter); on the config server's,
balancer errors. On any log, **failed operations** on your collections (a
query or write that ended in an error such as
`FailedToSatisfyReadPreference` or `MaxTimeMSExpired`) are grouped by
error; `mdbkit filter <log> --failed` prints them.

**Hosts running several mongods.** `triage` reads one instance's log; don't
pass several instances' logs to it, because several files are read as one
stream (that is for the rotated logs of one instance, or the members of one
replica set). For the whole host at once, use [`mdbkit host`](#mdbkit-host-logs).
When several mongods run on the host, `triage` uses the pid and dbPath the
log records (in its startup line, or at the top of a rotated file) to pick
the right process for its memory, uptime and disk checks, and when
`--oslog` shows an OOM kill it says whether the killed pid was this
instance or another one.

---

### `mdbkit host <logs...>`

One host running many mongod instances: one line per instance, and checks
that only make sense for the host as a whole.

| Option | Default | Description |
|---|---|---|
| `--window N` | 1440 | Analyze the last N minutes of each log; `0` = whole logs |
| `--ram SIZE` | | The host's memory (`64G`, `65536M`) when the logs were copied off it. On the host it is read from `/proc` |
| `--oslog FILE...` | | System log(s): each OOM kill is matched to the instance it killed |
| `--limit N` | all | Show only the N worst instances |
| `--exit-code` | | Exit 2 on CRIT, 1 on WARN, else 0 |
| `--json` | | Machine-readable output |

```bash
mdbkit host /var/log/mongodb                          # finds every *.log* below
mdbkit host /var/log/mongodb --oslog /var/log/syslog  # who did the OOM killer hit?
mdbkit host "/mnt/copied/db7/*/mongod.log*" --ram 256G --limit 15
```

Give it files, globs or directories (searched three levels deep, never into
a data directory). Files are grouped into instances by the host and port the
log records, so rotated files join their instance even though they have no
startup line.

**Per instance:** replica set, version, role, WiredTiger cache size, starts
and crashes (unclean stops), slow operations and their total time, the share
of that time spent waiting (8.0+), and its worst finding from the same
detectors `triage` uses. Shard and config server members are labelled as
such, and a mongos on the same host gets a line of its own (it has no cache,
so it is left out of the cache total).

**For the host:**

- **Cache sizes vs RAM.** Each instance's cache comes from its own log
  ("Opening WiredTiger" states the size, even when it is the default). The
  total is compared with the RAM: WARN above 60%, CRIT above 85%. These are
  rules of thumb: MongoDB's single-instance default is about half the RAM,
  leaving the rest to the OS file cache and each process's connections,
  sorts and aggregations.
- **Default cache on a shared host.** Instances without `cacheSizeGB` each
  assume the whole machine is theirs.
- **Crashes** across all instances, and with `--oslog`, every OOM kill
  matched to the instance it hit by pid.
- **Startup warnings counted across instances.** Kernel and limit settings
  belong to the host, so "Open-file limit is too low: 54 of 54 instances" is
  one fix, not 54.
- **On the host itself:** instances with a log but no running mongod, and
  running mongods whose logs were not given.

---

### `mdbkit audit <log>`

Reads the warnings mongod logged when it started and explains each one:
access control, running as root, TLS certificate checks, file and
locked-memory limits, `vm.max_map_count`, transparent huge pages (with the
**8.0 reversal**: 7.0 and earlier want THP off, 8.0's new allocator wants it
on), glibc `rseq`, NUMA, overcommit, zone reclaim, swappiness, filesystem.
Warnings mdbkit does not recognise are still reported, in mongod's own words.

| Option | Description |
|---|---|
| `--exit-code` | Exit 2 on CRIT, 1 on WARN, else 0 |
| `--json` | Machine-readable output |

```bash
mdbkit audit /var/log/mongodb/mongod.log
mdbkit audit "/var/log/mongodb/mongod.log*"     # include rotated logs
```

Startup warnings are written only when mongod starts. If the log covering
the last restart has rotated away, ask the running server instead. The
output of `getLog` works directly, in EJSON, mongosh's printed form, or a
UTF-16 file from Windows PowerShell:

```bash
mongosh --quiet --eval 'EJSON.stringify(db.adminCommand({getLog: "startupWarnings"}))' > startup.json
mdbkit audit startup.json
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
| `--from` / `--to` | | Explicit time bounds, UTC unless the value has an offset (same formats as `filter`) |
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

In `timeline`, gauges (connections, cache bytes, tickets) show the peak in
each bucket and cumulative counters (`ops.*`, `sys.cpu.*`) show their rate
per second over it. FTDC records time in UTC, and these commands show it as
UTC; `triage --ftdc` converts it to the log's own time zone so the two line
up.

Metric labels include `ops.*` (insert/query/update/delete/getmore/command),
`conns.current`, `conns.available`, `queue.readers`, `queue.writers`,
`cache.usedBytes`, `cache.maxBytes`, `cache.dirtyBytes`, `tickets.*`,
`checkpoint.lastMs`, `evict.appThreadMicros`, `flowControl.*`,
`mem.residentMB`, and on Linux `sys.cpu.*` and `sys.mem.availableKB`. The
same labels work across versions where MongoDB renamed the underlying
statistic (checked on 6.0 to 9.0), and on 8.0+ shard servers and mongos,
which group their metrics by role.

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

*window 2026-07-01 08:29 -> 09:29  ·  generated 2026-07-01 09:31*

## Findings

- **[CRIT] Replica set instability** — 2 election/stepdown event(s); at least once a member saw no primary and called an election, which is what losing the primary (crash, kill, hang or network) looks like.
    - 08:50:32  Starting an election, since we've seen no PRIMARY in election timeout period
    - 08:50:34  Election succeeded, assuming primary role
    - *next:* `Find why the primary went away at the first timestamp: its own log (mdbkit triage on it), then the OS log (mdbkit oslog) for OOM kills or restarts.`
- **[WARN] Connection storm** — 1 minute(s) at >= 60 new connections/min (baseline median 0/min); peak 220 at 08:49.
    - 10.20.9.77: 220 in the peak minute
    - *next:* `Identify the client: mdbkit connections <log> — look for pool misconfiguration or crash-loop reconnects.`
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
and seed data; a sharded lab also needs `mongos`). Linux and macOS.

| Action | What it does |
|---|---|
| `start` | Create and start a replica set (or standalone, or sharded cluster), print the connection string and log paths. On an existing lab, start the nodes that are down, with their data and ports (options that would make a different lab are refused) |
| `seed` | Insert sample data and run a workload with deliberately interesting queries (`--workload-only`: just the queries, on the data already there) |
| `status` | Show ports, pids and whether each node is running |
| `logs` | Print the log file paths, ready to pipe into other commands |
| `stop` | Stop the nodes, keep the data |
| `destroy` | Stop and delete the lab (requires `--yes`) |

| Option | Default | Description |
|---|---|---|
| `--dir PATH` | `~/.mdbkit-lab` | Where the lab lives |
| `--nodes N` | 3 | Replica set size (with `--shards`: each shard's replica set, default 1) |
| `--shards N` | | A sharded cluster with N shards, a config server and a mongos |
| `--port N` | 28110 | Base port — deliberately far from 27017 |
| `--slowms N` | 0 | Log every operation, which is what makes the log worth reading |
| `--standalone` | off | Single node, no replica set |
| `--docs N` | 50000 | Documents inserted by `seed` |
| `--workload-only` | | `seed` runs the queries again on the data already there |
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

mdbkit queries $(mdbkit lab logs | head -1)     # node0, the preferred primary
mdbkit triage  $(mdbkit lab logs)               # all three as one stream
mdbkit loginfo $(mdbkit lab logs | sed -n 2p)   # a specific secondary
```

**A sharded cluster**: a config server, N single-node shards (`--nodes 3`
makes each shard a three-node replica set) and a mongos. `seed` shards
`shop.orders` on `customerId`, moves a range to every shard, then runs
targeted and scatter-gather queries through mongos. `logs` lists the mongos
log first:

```bash
mdbkit lab start --shards 2          # config 28110, shards 28111-28112, mongos 28113
mdbkit lab seed
mdbkit queries $(mdbkit lab logs | head -1)     # the router: shards per query
mdbkit triage  $(mdbkit lab logs | sed -n 3p)   # a shard: migrations, its queries
mdbkit lab destroy --yes
```

**A single node**, when you do not need replication. It starts faster, and
`start` works without `mongosh` (`seed` still needs it):

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

**Rehearse a failure.** Kill a node the way a crash or the OOM killer would,
see what mdbkit makes of it, then bring it back. `start` on an existing lab
starts only the nodes that are down, on their old ports:

```bash
kill -9 $(cat ~/.mdbkit-lab/node0/mongod.pid)   # the primary dies
sleep 20                                        # the others elect a new one
mdbkit lab start                                # node0 comes back
mdbkit triage $(mdbkit lab logs) --window 0     # crash, failover, recovery
```

**A complete before/after experiment**, which is what `lab` is really for:

```bash
mdbkit lab start && mdbkit lab seed
cp $(mdbkit lab logs | head -1) before.log
mongosh --port 28110 --quiet --eval 'db.adminCommand({logRotate: 1})'  # start a fresh log

mongosh --port 28110 --eval \
  'db.getSiblingDB("shop").orders.createIndex({status:1, createdAt:-1})'

mdbkit lab seed --workload-only     # the same queries, now with the index
cp $(mdbkit lab logs | head -1) after.log

mdbkit compare before.log --after after.log
mdbkit lab destroy --yes
```

`--workload-only` runs the queries again without reloading the data, so the
index you added stays; a plain `seed` reloads and would drop it. Rotating the
log keeps the second run's lines apart from the first's, because lab logs
are appended to across restarts.

`seed` runs indexed point lookups alongside deliberately unindexed queries —
an equality-plus-range-plus-sort with no supporting index, an aggregation
that scans the collection, and updates whose predicate has no index — so the
log immediately contains something worth analysing.

**Safety.** The lab binds to `127.0.0.1` only, refuses to use or delete any
directory it did not create, and never touches a MongoDB it did not start.
Before it signals a process it checks that the pid still belongs to one of
its own `mongod`s or its `mongos` (the command line names the lab's data
directory or log file), so a stale pid file can never kill something
unrelated. `destroy` refuses to
delete data while a lab node is still running. If a port is already taken it
says so before starting anything. If a start fails halfway, the lab is left
in a state `mdbkit lab destroy --yes` can clean up, and the error says so.
Logs are appended across restarts rather than overwritten.
It is a laptop and scratch-VM tool, not a deployment tool.

---

### `mdbkit oslog [FILE...]`

Scans a system log for the things that affect a database process: OOM kills,
file-descriptor limits, segmentation faults, filesystem and I/O errors,
read-only remounts, conntrack exhaustion, and systemd service exits.

With no argument it reads `/var/log/syslog` or `/var/log/messages` if they are
readable.

| Option | Description |
|---|---|
| `--exit-code` | Exit 2 on CRIT, 1 on WARN, else 0 |
| `--json` | Machine-readable output |

```bash
mdbkit oslog                                  # whichever system log exists
mdbkit oslog /var/log/messages
mdbkit oslog /var/log/syslog.1 /var/log/syslog
```

**On journald systems** there is no text log to read, and mdbkit does not run
commands on your behalf. It tells you what to capture instead:

```bash
journalctl -k --since '4 hours ago' > kern.log
journalctl -u mongod --since '4 hours ago' >> kern.log
mdbkit oslog kern.log
```

The same file can be handed to `triage --oslog`, which correlates it with the
mongod log — so an unexplained restart at 09:14 lines up with the OOM kill
that caused it.

---

### `mdbkit serverstatus FILE [--after FILE]`

Digests a saved `db.adminCommand({serverStatus: 1})` dump. That command
returns several hundred fields; this reports the handful that explain a
struggling server.

| Option | Description |
|---|---|
| `--after FILE` | A second dump taken later — turns cumulative counters into true rates |
| `--report FILE` | Write a shareable `.md` or `.html` report |
| `--exit-code` | Exit 2 on CRIT, 1 on WARN, else 0 |
| `--json` | Machine-readable output |

```bash
mdbkit export-script serverstatus > export_serverstatus.js

mongosh --quiet --host HOST --port PORT \
  --username USER --password PASS --authenticationDatabase admin \
  --eval "$(cat export_serverstatus.js)" > status.json

mdbkit serverstatus status.json
```

What it checks: **concurrency tickets** (exhaustion queues every operation and
looks like slowness with no slow query to blame), **WiredTiger cache** against
the 80% background-eviction and 95% application-thread-eviction thresholds,
**dirty cache**, **application-thread eviction**, **connection headroom**,
**queued readers and writers**, **assertions**, **flow control**, replication
role and process memory. Tickets are read from either the older
`wiredTiger.concurrentTransactions` layout or the newer `queues.execution`.

**Paste-friendly.** The file can be strict JSON, EJSON, or exactly what
mongosh prints when you run `db.serverStatus()` and copy the output:
unquoted keys, `Long('42')`, `ISODate(...)`, `Timestamp({...})`. A file
redirected in Windows PowerShell (UTF-16) works too. Nothing in the file is
ever evaluated; it is tokenised as data. Dumps taken with the 0.6.0 export
script, which wrote 64-bit counters as `{"low": …, "high": …}` objects, are
read correctly too.

**Two dumps give true rates.** Almost everything in serverStatus is cumulative
since process start, so a single dump only yields lifetime averages:

```bash
mongosh ... > before.json ; sleep 60 ; mongosh ... > after.json
mdbkit serverstatus before.json --after after.json
```

```
[INFO] Operation counters: Measured over 60 seconds between the two dumps.
        - query    742,000 in 60s  (12366.7/sec)
```

That is real current load. The same counter read from one dump would have
reported 2,127/sec — the average since the server started ten days ago.

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

### `mdbkit indexes <file>...`

Unused and redundant indexes, from what `mdbkit export-script indexes`
exported (index definitions plus `$indexStats` usage counters).

| Option | Default | Description |
|---|---|---|
| `--ns NAMESPACE` | all | Only this collection |
| `--min-days N` | 7 | Warn when the usage counters cover fewer days than this |
| `--exit-code` | | Exit 1 when there is an unused or redundant index |
| `--json` | | Machine-readable output |

```bash
mdbkit indexes indexes.json
mdbkit indexes primary.json secondary1.json secondary2.json   # combine members
mdbkit indexes indexes.json --ns shop.orders
```

What it reports:

- **Unused:** no recorded use since the counters started.
- **Redundant:** its key is a prefix of another index with the same
  collation, which can serve the same queries (`{a: 1}` when `{a: 1, b: 1}`
  exists). Partial, sparse and special (text, 2dsphere, hashed, wildcard)
  indexes are never treated as covering or covered.
- **Not candidates:** `_id`, unique indexes (they enforce a constraint),
  TTL indexes (the TTL monitor's deletes do not count as use, checked on
  8.0 and 9.0), hidden indexes, and the index the shard key needs.

Usage counters are kept **per member** and **reset when a member restarts**.
An index unused on the primary may serve reads on a secondary, and one used
monthly can look unused after a week. So export from every member (or
through mongos for a sharded cluster) and pass all the files, and check the
counter window mdbkit prints. It never recommends dropping anything: it
prints the `hideIndex` command, which makes the planner ignore the index
while it is still maintained, so `unhideIndex` undoes it instantly.

---

### `mdbkit export-script {schema|indexes|serverstatus}`

Prints a small `mongosh` script to stdout. **mdbkit never connects to your
database**. You run these yourself, so you can read exactly what they do
first. All are read-only. `schema` exports **field names and types only,
never document values**. `indexes` exports the index definitions from
`getIndexes()` (a partial index's filter expression can contain literal
values) and, where your role allows `$indexStats`, how often each index has
been used; through mongos it also records the shard keys.

`schema` and `indexes` cover **every database you can read** (`admin`,
`config` and `local` skipped) and key collections by full namespace, so it
does not matter which database mongosh connects to. To export one database
only, set `ONLY_DB` at the top of the script. They write relaxed Extended
JSON, so 64-bit counters come out as plain numbers.

```bash
mdbkit export-script indexes > export_indexes.js
mdbkit export-script schema  > export_schema.js
mdbkit export-script serverstatus > export_serverstatus.js
```

---

## Roadmap

Terminal output is and will remain first-class — this tool is built for the
Linux box the database actually runs on.

**v0.8 is a long-term release.** Nothing further is scheduled. Bug reports
are still read, and misread log lines are still the most useful thing to
send.

**Shipped in v0.8:**

* **Sharded clusters.** mongos logs show how many shards each query shape
  went to, which ones went to all of them, and the time spent waiting on
  shards and refreshing routing tables. Shard and config server logs show
  chunk migrations (with the reason when one fails), the range deleter, the
  balancer's errors, unreachable shards and failed application operations.
  FTDC from 8.0+ shard servers and mongos decodes. `mdbkit lab --shards N`
  builds a cluster to try it on.
* **`mdbkit indexes`:** unused and redundant indexes from `$indexStats`,
  across replica set members or shards, with the reversible `hideIndex`
  command rather than a drop.
* **The beta detectors checked against real failures.** Elections and
  stepdowns, flow control, cache pressure and slow checkpoints were
  reproduced on real MongoDB 6.0, 7.0, 8.0 and 9.0 servers (a killed
  primary, a stepdown, a connection storm, frozen secondaries, a small
  cache under load, a checkpoint held up for a minute), so none is labelled
  beta any more. That turned up real bugs, now fixed: flow control was never
  spotted in the log, a failover soon after a set was created was taken for
  its set-up, a stepdown was invisible on the primary that stepped down, and
  every chunk migration looked like a shutdown on the receiving shard.
* `filter --failed`, `lab start` restarting only the nodes that are down,
  `lab seed --workload-only`, and FTDC checkpoint and eviction metrics under
  their 8.0 names.

**Shipped before:** v0.7's `host` (hosts running many mongods); v0.6.1's
testing against real servers; v0.6's 8.x/9.0 log fields and `audit`; v0.5's
`oslog` and `serverstatus`; v0.4's `compare`; v0.3's `demo` and `lab`; v0.2's
FTDC decoding, incident triage and shareable reports.

**Ideas, not scheduled:** jumbo chunks and balancer windows; slow oplog
application on secondaries; `lab` on Windows.

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
