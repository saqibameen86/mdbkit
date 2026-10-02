# Real MongoDB output

Captured from official MongoDB builds (7.0.43, 8.0.32, 8.3.11, 9.0.2) run with
`mdbkit lab` on a scratch VM in October 2026, then trimmed to the lines the
tests need. Nothing here is from a real deployment; hostnames, paths and data
are the lab's own.

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
