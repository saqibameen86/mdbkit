# Real MongoDB output

Captured from official MongoDB builds (6.0.29, 7.0.43, 8.0.32, 8.3.11, 9.0.2)
run with `mdbkit lab` on a scratch VM in October 2026, then trimmed to the
lines the tests need. Nothing here is from a real deployment; hostnames,
paths and data are the lab's own.

| File | What it is |
|---|---|
| `mongod-<version>.log` | Slow queries (getMore with originatingCommand, update WRITE+COMMAND pairs, an insert blocked by fsyncLock, a spilling sort), startup warnings, replica set initiation and first election |
| `slowprog-9.0.2.log` | "Slow in-progress query" lines (id 1794200) from `--defaultSlowInProgMS 200` |
| `clean_restart-8.0.32.log` | One instance stopped with `shutdown` and started again |
| `crash_restart-8.0.32.log` | One instance killed with `kill -9` and started again |
| `rotated_after_crash-8.0.32.log` | The same log cut to begin at the post-crash startup |
| `getlog-startup-*.{json,txt}` | `getLog: "startupWarnings"` as EJSON (9.0.2) and as mongosh prints it (7.0.43) |
| `ftdc-interim-*` | `diagnostic.data/metrics.interim` |
| `serverstatus-9.0.2-jsonstringify-*.json` | serverStatus written by the 0.6.0 export script (`JSON.stringify`, so 64-bit counters are `{low, high}` objects), trimmed to the sections mdbkit reads, 31 s apart |
| `host/` | Eight mongods on one host: two 8.0.32 three-member replica sets (one left on the default WiredTiger cache), an 8.0.32 standalone whose log was rotated, a 7.0.43 standalone restarted cleanly, and one member killed with `kill -9` |
| `indexes/replset-8.0.32.json` | `mdbkit export-script indexes` output from a replica set primary: definitions plus `$indexStats` usage |
| `indexes/sharded-mongos-8.0.32.json` | The same, run through mongos on a two-shard cluster sharded on `customerId`: one usage row per shard, and the shard key |
| `sharded/<version>/manual/` | A config server, two shards and a mongos: a collection sharded on `{customerId: 1}`, a range moved to the second shard, targeted and scatter-gather queries, then a move back that fails because the range deleter has not run yet |
| `sharded/<version>/balancer/` | The balancer moving 1 MB chunks on its own, then the second shard killed with `kill -9` while queries run |
| `sharded/ftdc-shard-8.0.32` | FTDC from an 8.0 shard server, whose metrics are grouped by role (`common.`, `shard.`) |
| `failures/<version>/node{0,1,2}.log.gz` | A three-member replica set put through: the primary killed with `kill -9` and restarted, a stepdown, 300 connections at once, and flow control (both secondaries frozen with `fsyncLock` during 150 s of writes). `scenario.json` says which member played which part. Per-operation lines are dropped (`slowms` 0 logs every operation) |
| `failures/<version>/ftdc-flow` | The primary's FTDC chunk covering the flow-control episode |
| `failures/<version>/ftdc-pressure` | FTDC from a standalone with a 256 MB cache and one eviction thread while four writers rewrote a 1 GB working set |
| `failures/<version>/ftdc-checkpoint` | FTDC from a standalone frozen with SIGSTOP for 70 s in the middle of a checkpoint, so the checkpoint ran for over a minute |
| `failures/9.0.2/checkpoint.log.gz` | That standalone's log: 8.3+ logs "Checkpoint has been running for N seconds" at the default level (6.0-8.0 do not) |
