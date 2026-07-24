# mdbkit Roadmap & Design Principles

This document governs all future versions. Any model or contributor
implementing a design doc in this folder MUST honor these principles.

## Non-negotiable principles

1. **Read-only, forever.** mdbkit never mutates a cluster, never runs
   admin commands, never connects to a database. Where an action would help
   (create index, resize oplog, step down), mdbkit PRINTS the command with
   caveats for a human to review and run. Advice, never action.
2. **Offline, forever.** No network code of any kind. No telemetry, no
   update checks, no phoning home. Local OS inspection (reading /proc,
   statvfs on the dbPath) is allowed — it never leaves the machine.
3. **Zero runtime dependencies.** Python stdlib only. This is a feature
   (air-gapped installs, no supply chain) and must not be traded away for
   convenience. If a task seems to need a library (BSON, zlib), implement
   the minimal subset in-tree (zlib IS stdlib; BSON needs ~150 lines).
4. **Terminal-first.** Every feature must be fully usable over SSH with
   plain-text output. HTML/Markdown export is a sharing convenience layer,
   never the primary interface.
5. **Untrusted input.** All files (logs, FTDC, serverStatus exports) are
   parsed defensively: strict parsing, bounded recursion, no eval/exec, no
   shell-outs, malformed input skipped and counted, never echoed into
   errors.
6. **Evidence, confidence, caveats.** Every diagnostic or recommendation
   states what was observed, how sure we are, and what could make it wrong.
   Deterministic rules only — same input, same output.
7. **Tests gate everything.** New parsers require fixtures from REAL
   MongoDB output (redacted), not just synthetic data. A feature without
   real-world fixtures does not ship.

## Version plan

* **v0.1 (shipped)** — structured log toolkit: loginfo, queries,
  connections, filter, advise, explain, export-script.
* **v0.2 (shipped)** — the incident release:
  * FTDC decoder (`DESIGN-ftdc.md`) — offline metrics from diagnostic.data,
    including system CPU/memory/disk that FTDC already records.
  * Triage command (`DESIGN-triage.md`) — one-command incident snapshot:
    log detectors + local OS probes + optional serverStatus digest + hot
    collection ranking.
  * Election/failover timeline (part of triage design).
* **v0.3 (shipped early, in v0.2)** — shareable Markdown/HTML reports.
* **Later / separate product** — GUI control plane, continuous backup
  health, scheduling (the commercial platform). The CLI stays free and
  fully functional forever; it is the trust anchor, not a crippled demo.

## Naming & compatibility

* Follow semver. Breaking CLI-flag changes require a major bump.
* `--json` output schemas are contracts: additive changes only within a
  minor version; document every schema in the design docs.
* Support MongoDB 4.4 through current; new server versions get a fixture
  and a CI entry before we claim support.
