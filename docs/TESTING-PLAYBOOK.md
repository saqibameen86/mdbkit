# Real-Cluster Testing Playbook

Purpose: generate REAL log fixtures that graduate the remaining beta triage
detectors to stable. Since v0.3 you no longer need mlaunch or a separate
cluster — `mdbkit lab` creates and destroys everything.

Each scenario takes a few minutes. Capture the logs, note the wall-clock time
of each action, and the result becomes a `tests/fixtures/real_*` file plus a
regression test.

## Detector status

| Detector | Status |
|---|---|
| Elections / stepdowns | **validated** against real 7.x replica logs |
| Restarts, fatal errors | validated |
| Connection storms | validated |
| Hot collections, COLLSCAN volume | validated |
| Index builds | validated |
| Cluster health | validated on synthetic + real state transitions |
| Slow checkpoints | now measured from **FTDC**, not the log — needs scenario 4 to confirm |
| Cache eviction pressure | now measured from **FTDC** — needs scenario 4 to confirm |
| Flow control | now measured from **FTDC** — needs scenario 5 to confirm |
| FTDC decoder | spec-correct and round-trip tested, **never run against a real `diagnostic.data`** — scenario 6 |

## Setup

```bash
mdbkit lab start                 # 3-node replica set, 127.0.0.1:28110-28112
mdbkit lab seed                  # 50k docs + a mixed workload
mdbkit lab logs                  # paths you will be collecting
```

Everything below assumes that lab. Tear it down at the end with
`mdbkit lab destroy --yes`.

---

## Scenario 1 — Election (hard kill)

```bash
# note the time
date
# find the primary's pid and kill it
mdbkit lab status
kill -9 <pid of node0>
sleep 45
mdbkit lab start                 # restarts the stopped node
```

Collect: all three node logs. Expect: election messages on the surviving
nodes, a state transition to PRIMARY on one of them.

```bash
mdbkit triage $(mdbkit lab logs | sed -n 2p) --window 0 --no-sysprobe
```

## Scenario 2 — Election (clean stepdown)

```bash
date
mongosh --port 28110 --eval 'rs.stepDown(60)'
sleep 20
```

Collect: all three logs. This produces different messages from scenario 1 —
both are wanted.

## Scenario 3 — Connection storm

```bash
date
for i in $(seq 250); do mongosh --port 28110 --quiet --eval 1 & done; wait
```

Expect: `mdbkit triage` reports a storm minute with the right source IP, and
`mdbkit connections` shows the churn.

> **Why these moved to FTDC.** MongoDB's own source shows checkpoint timing is
> logged at `LOGV2_DEBUG` level 4 and via `LOGV2_FOR_RECOVERY`, so it does not
> appear in a default log at all; application-thread eviction has no log line
> whatsoever. Both are recorded in `diagnostic.data` every second. Scenarios 4
> and 5 therefore check the **FTDC** numbers, and the log-based detectors
> remain only as corroboration.

## Scenario 4 — Slow checkpoints and cache eviction  ← needed

Start a lab with a deliberately small cache so WiredTiger has to work:

```bash
mdbkit lab destroy --yes
mdbkit lab start --standalone
# edit nothing: the lab already uses a 0.25 GB cache, which is small enough
date
mdbkit lab seed --docs 400000        # sustained write pressure
```

Then check the metrics rather than the log:

```bash
mdbkit ftdc summary ~/.mdbkit-lab/node0/data/diagnostic.data --all \
  --metric checkpoint.lastMs --metric evict.appThreadPages
mdbkit triage $(mdbkit lab logs | head -1) --window 0
```

Expect `checkpoint.lastMs` to climb and `evict.appThreadPages` to become
non-zero under pressure. Collect the whole `diagnostic.data` directory even if
nothing is flagged — a negative result tells us the metric names differ on
your version, which is exactly what needs fixing.

## Scenario 5 — Flow control  ← needed

```bash
mdbkit lab destroy --yes && mdbkit lab start && mdbkit lab seed
mdbkit lab status                    # note the secondary pids
date
kill -STOP <pid of node1> <pid of node2>
# now generate sustained writes for 2-3 minutes
mongosh --port 28110 --eval 'for (let i=0;i<500000;i++) db.getSiblingDB("shop").load.insertOne({i, t:new Date()})'
kill -CONT <pid of node1> <pid of node2>
```

Then:

```bash
mdbkit ftdc summary ~/.mdbkit-lab/node0/data/diagnostic.data --all \
  --metric flowControl.isLagged --metric flowControl.waitMicros
```

Expect `flowControl.isLagged` to reach 1 and the wait time to climb during the
SIGSTOP window.

## Scenario 6 — FTDC against real diagnostic.data  ← the important one

```bash
mdbkit lab start && mdbkit lab seed
sleep 300                            # let FTDC accumulate a few chunks
mdbkit ftdc summary ~/.mdbkit-lab/node0/data/diagnostic.data --all
mdbkit triage $(mdbkit lab logs | head -1) --window 0
```

This is the single most valuable test in this document. The FTDC decoder is
written against MongoDB's published format specification and round-trips
through an independent encoder, but it has never decoded a file produced by a
real mongod. What to check:

* Does it decode without errors, and is `corruptChunks` zero?
* Do `conns.current` and `ops.*` look plausible for the workload you ran?
* Does `cache.usedBytes` sit below `cache.maxBytes`?
* Does `mdbkit triage` pick up `diagnostic.data` automatically?

If anything is wrong, the most useful thing to send back is the first 4 KB of
one metrics file:

```bash
head -c 4096 ~/.mdbkit-lab/node0/data/diagnostic.data/metrics.* | xxd | head -60
```

FTDC contains metrics only, never document contents — but glance at the
output before sharing, as with any production artifact.

## Collecting and sharing

```bash
mkdir -p /tmp/mdbkit-fixtures
cp $(mdbkit lab logs) /tmp/mdbkit-fixtures/
mongod --version >> /tmp/mdbkit-fixtures/versions.txt
# one line per scenario: "scenario 4 at 14:22 local"
```

Redaction, only needed for logs from a real deployment rather than the lab:

```bash
sed -E 's/[0-9]{1,3}(\.[0-9]{1,3}){3}/10.0.0.1/g; s/yourhost[a-z0-9.-]*/host-1/g' in.log > out.log
```

## Acceptance mapping

| Detector | Scenario | Graduates when |
|---|---|---|
| Elections | 1, 2 | Detected on 6.0/7.0/8.0, zero false positives on a quiet log |
| Connection storm | 3 | Storm minute flagged, source IP correct |
| Slow checkpoint | 4 | A >60s checkpoint is flagged with its duration |
| Eviction pressure | 4 | Pressure detected during the cache squeeze |
| Flow control | 5 | Engagement detected during the SIGSTOP window |
| FTDC decoder | 6 | Real `diagnostic.data` decodes with zero corrupt chunks and plausible values |

## Cleaning up

```bash
mdbkit lab destroy --yes
```
