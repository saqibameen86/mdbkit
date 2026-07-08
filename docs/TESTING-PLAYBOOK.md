# Real-Cluster Testing Playbook

Purpose: generate REAL log fixtures that graduate beta triage detectors to
stable. Run each scenario, capture logs, note timestamps, send back. Each
becomes a `tests/fixtures/real_*` file and a regression test.

## Status
- Elections: VALIDATED against real 7.x replica logs (found & fixed the
  ELECTION-component bug). Still wanted: 6.0 and 8.0 samples, and a
  network-partition election (not just clean shutdown).
- Checkpoints, eviction, flow control: NOT yet validated — scenarios below.

## Setup (per MongoDB version: 6.0, 7.0, 8.0)
3-node replica set on one machine: `mlaunch init --replicaset --nodes 3`
(original mtools still works for this) or three mongod processes/docker.

## Scenarios (note wall-clock time of each action!)
1. **Election (hard):** `kill -9` the primary; wait 30s; restart it.
2. **Election (clean):** on primary: `rs.stepDown()`.
3. **Connection storm:** `for i in $(seq 250); do mongosh --eval 1 --quiet & done` within one minute.
4. **Slow checkpoint + eviction:** start nodes with `--wiredTigerCacheSizeGB 0.25`; run 10 min of heavy inserts/updates (1KB docs, several threads).
5. **Flow control:** under sustained writes, `kill -STOP` a secondary for 2-3 min, then `kill -CONT`.
6. **FTDC (for v0.2):** after the above, copy each node's `diagnostic.data/` directory (contains NO user documents — metrics only; verify with `strings` spot-check).

## Capture & delivery
- Full mongod logs from ALL nodes + `mongod --version` output.
- A note per scenario: "scenario 3 at 14:22 local".
- Redaction (only for non-lab logs): `sed -E 's/[0-9]{1,3}(\.[0-9]{1,3}){3}/10.0.0.1/g; s/yourhost[a-z0-9.-]*/host-1/g'`.

## Acceptance mapping
| Detector | Scenario | Graduates when |
|---|---|---|
| Elections | 1, 2 | detected on all versions, zero false positives on quiet logs |
| Connection storm | 3 | storm minute flagged, source IP correct |
| Slow checkpoint | 4 | >60s checkpoint flagged with duration |
| Eviction pressure | 4 | pressure messages detected during cache squeeze |
| Flow control | 5 | engagement detected during SIGSTOP window |
| FTDC decoder | 6 | DESIGN-ftdc.md acceptance criteria |
