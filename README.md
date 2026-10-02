# mdbkit

[![CI](https://github.com/saqibameen86/mdbkit/actions/workflows/ci.yml/badge.svg)](https://github.com/saqibameen86/mdbkit/actions)
[![PyPI](https://img.shields.io/pypi/v/mdbkit.svg)](https://pypi.org/project/mdbkit/)
[![Python](https://img.shields.io/pypi/pyversions/mdbkit.svg)](https://pypi.org/project/mdbkit/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**An offline toolkit for MongoDB structured logs** — slow-query analysis,
deterministic index advice, incident triage, a startup configuration audit,
and diagnostic-data decoding. For MongoDB 4.4 through 8.x and 9.0, from the
terminal, without connecting to anything.
**[Tested against real MongoDB 9.0.2, 8.3.11, 8.0.32 and 7.0.43](#compatibility)**
(October 2026).

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
(LTS) and **7.0.43** (LTS), the newest release in each series at the time.
mdbkit reads the structured JSON log format, so anything from **4.4** onwards
works; 4.2 and earlier wrote plain text logs and are not supported.

| MongoDB | What mdbkit uses from it |
|---|---|
| 4.4 – 7.0 | Everything except the 8.x-only fields below |
| 8.0 | `workingMillis` (time executing, as distinct from waiting), `queues.execution.totalTimeQueuedMicros` (ticket queue time), `queryShapeHash` (usable with `setQuerySettings`), `planCacheShapeHash`, the 8.0 transparent-huge-page guidance |
| 8.1+ | `<stage>Spills` / `<stage>SpilledBytes` (disk spills per stage) |
| 8.3+ | "Slow in-progress query" lines (id 1794200, component `SLOWPROG`), logged by default for operations still running after 5 s (`--defaultSlowInProgMS`), and `peakTrackedMemBytes` |
| 9.0 | Same log fields as 8.3. Adds a few fields mdbkit does not use yet (`delinquencyInfo`, `queues.ingress`) |

When a field is missing because the log predates it, that part of the output
is left out. It is never guessed. FTDC decoding and `serverstatus` read
concurrency tickets from either the older `wiredTiger.concurrentTransactions`
or the newer `queues.execution` layout.

**How it was tested.** Official MongoDB builds of each version were run with
`mdbkit lab` (a three-node replica set and a standalone), seeded, and put
through a workload built to produce every field above: unindexed queries,
sorts and groups forced to spill to disk, writes blocked behind `fsyncLock` so
they wait rather than work, long-running server-side JavaScript, a clean
restart and a `kill -9`. Every mdbkit command was then run on the resulting
logs, `diagnostic.data`, serverStatus and explain output and `getLog`
captures, and compared with what the server actually did. Trimmed copies of
that output are in [`tests/fixtures/real/`](tests/fixtures/real/) and run with
every test.

> **Correction.** The 0.6.0 README and release notes said "last checked
> against 9.0.2, 8.3.13, 8.0.34 and 7.0.45". Three of those version numbers
> were wrong: MongoDB's release index has no 8.3.13, 8.0.34 or 7.0.45. That
> release was also checked against release notes and server source only, not
> a running server. Testing against real servers in 0.6.1 found several bugs
> 0.6.0 had missed (see the release notes).

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

Cluster health, elections, connection storms, hot collections, index builds,
error clusters, slow-query peaks, startup misconfiguration, time spent
waiting rather than working (8.0+), and still-running operations (8.3+) —
plus disk, memory, CPU and FTDC metrics when run on the database host. Every
finding ends with the next command to run. A shutdown followed by the server
listening again is reported as a restart (WARN), not an outage (CRIT).

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
| `plan` | The plan MongoDB chose: `COLLSCAN` (no index), `IXSCAN{fields}` (index used), `IDHACK` (`_id` lookup), `+SORT` (in-memory sort), `+SPILL` (wrote temporary files to disk). `?` = the plan was not recorded on that line |
| `shape` | Fields and operators queried, with the sort |

Two kinds of row are worth knowing about. A `getMore(find)` or
`getMore(aggregate)` row is a cursor fetching more results; it is grouped
under the shape of the query that opened the cursor. Slow `insert` rows are
included even though no index can speed up an insert: a slow insert is still
latency your application saw, and on 8.0+ the `--shape` view says whether it
was executing or waiting (on locks, flow control or write concern).

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

**Restarts and crashes.** A restart after a clean shutdown is a WARN; a start
with no clean shutdown before it is a CRIT, because that is what a crash,
`kill -9` or OOM kill looks like. mongod itself records whether its previous
shutdown was clean, so mdbkit can tell even when the crash happened before
the log you are reading begins. The first election of a newly initiated
replica set is reported as INFO, not as instability.

**Hosts running several mongods.** Run mdbkit once per instance, on that
instance's own log; don't pass several instances' logs in one command,
because several files are read as one stream (that is for the rotated logs of
one instance, or the members of one replica set). When several mongods run on
the host, `triage` uses the pid and dbPath in the log's startup line to pick
the right process for its memory, uptime and disk checks, and when
`--oslog` shows an OOM kill it says whether the killed pid was this
instance or another one.

```bash
for log in /var/log/mongodb/*/mongod.log; do
  echo "== $log"
  mdbkit triage "$log" --only CRIT,WARN
done
```

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

In `timeline`, gauges (connections, cache bytes, tickets) show the peak in
each bucket and cumulative counters (`ops.*`, `sys.cpu.*`) show their rate
per second over it.

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
Before it signals a process it checks that the pid still belongs to one of
its own `mongod`s (its command line names the lab's data directory), so a
stale pid file can never kill something unrelated. `destroy` refuses to
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

### `mdbkit export-script {schema|indexes}`

Prints a small `mongosh` script to stdout. **mdbkit never connects to your
database**. You run these yourself, so you can read exactly what they do
first. Both are read-only. `schema` exports **field names and types only,
never document values**. `indexes` exports the index definitions from
`getIndexes()`; a partial index's filter expression can contain literal
values.

Both cover **every database you can read** (`admin`, `config` and `local`
skipped) and key collections by full namespace, so it does not matter which
database mongosh connects to. To export one database only, set `ONLY_DB` at
the top of the script. The `serverstatus` script writes relaxed Extended
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

**Shipped in v0.6.1:** tested against real MongoDB 7.0, 8.0, 8.3 and 9.0
servers, with the bugs that turned up fixed; crash vs clean restart
detection; correct behaviour on hosts running many mongods.

**Shipped in v0.6:** MongoDB 8.x/9.0 support (executing vs waiting time,
query shape hashes, disk spills, peak memory, in-progress operations, the
8.0 ticket layout in FTDC), the `audit` command, mongosh-paste input, and a
hardening pass. Before that: v0.5's `oslog` and `serverstatus`, v0.4's
`compare`, rotated-log globbing and per-shape drill-down, v0.3's `demo` and
`lab`, and v0.2's FTDC decoding, incident triage, query reconstruction and
shareable reports.

Next up, roughly in order:

* **Hosts running many mongods.** One line per instance (health, restarts,
  slow time, startup warnings), the WiredTiger cache sizes of every instance
  added up against the host's RAM (54 instances on default settings would
  each claim about half of it), and each OOM kill matched to its instance.
* **Sharded clusters.** `mongos` logs are a different shape, and the classic
  sharded failure — a query with no shard key fanning out to every shard — is
  visible in the log. Also chunk migrations, balancer windows and jumbo
  chunks. Would come with `mdbkit lab --sharded` so it can be tested.
* **Index usage candidates.** Prefix-redundant indexes (an index on `{a: 1}`
  when `{a: 1, b: 1}` exists) are worth *examining*, but static analysis is
  not sufficient grounds to drop one — the planner may still be choosing it.
  So mdbkit will flag candidates and print an `$indexStats` script to confirm
  real usage first, never a drop recommendation.
* **Confirming the FTDC-based checkpoint, eviction and flow-control
  detectors** against real `diagnostic.data` — see
  `docs/TESTING-PLAYBOOK.md`. Real logs and metrics very welcome.

mdbkit is validated against real-world structured logs (tens of thousands of
lines) in addition to its synthetic test fixtures, and its tests include real
output from MongoDB 7.0.43, 8.0.32, 8.3.11 and 9.0.2.

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
