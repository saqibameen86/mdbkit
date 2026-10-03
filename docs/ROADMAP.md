# mdbkit: principles and history

Read this before changing mdbkit, whoever (or whatever model) is doing the
work. The principles are not negotiable.

## Principles

1. **Read-only, forever.** Analysis commands never mutate a cluster, never
   run admin commands and never connect to a database. Where an action would
   help (create an index, hide one, resize the oplog), mdbkit prints the
   command, with caveats, for a human to review and run. `mdbkit lab` is the
   one exception: it starts a disposable local MongoDB, and is bounded as
   SECURITY.md describes.
2. **Offline, forever.** No network code outside `lab.py` (which only probes
   a local port), no telemetry, no update checks. Reading the local machine
   (`/proc`, `statvfs` on the dbPath) is allowed: it never leaves the host.
3. **Zero runtime dependencies.** Python standard library only, Python 3.8+.
   This is a feature (air-gapped installs, no supply chain). If a task seems
   to need a library, implement the small subset needed in-tree, as was done
   for BSON and FTDC.
4. **Terminal-first.** Everything works over SSH as plain text. Markdown and
   HTML reports are a sharing layer, never the primary interface.
5. **Untrusted input.** Logs, FTDC and exports are parsed defensively:
   strict JSON, bounded recursion and decompression, no eval or exec, no
   shell-outs, malformed input skipped and counted, never echoed into errors,
   control characters neutralised before they reach the terminal.
6. **Evidence, confidence, caveats.** Every finding says what was observed,
   how sure mdbkit is, and what could make it wrong. Deterministic rules
   only: same input, same output.
7. **Real output gates everything.** A parser or detector ships only with
   fixtures from a real MongoDB server (`mdbkit lab` makes that easy), not
   just synthetic data. Every bug found in real output so far had passed
   the synthetic tests.

## Compatibility

* Semantic versioning. Breaking CLI changes need a major version.
* `--json` output is a contract: within a minor version, keys are only
  added, never removed or renamed.
* Structured (JSON) logs: MongoDB 4.4 and later. A new server version is
  claimed only after a real server of that version has been run and its
  output added to `tests/fixtures/real/`.

## History

* **0.1** structured log toolkit: `loginfo`, `queries`, `connections`,
  `filter`, `advise`, `explain`, `export-script`.
* **0.2** FTDC decoding, incident `triage`, shareable reports.
* **0.3** `demo` and `lab`. **0.4** `compare`, rotated logs, per-shape
  detail. **0.5** `oslog`, `serverstatus`.
* **0.6** MongoDB 8.x/9.0 fields, `audit`, hardening. **0.6.1** tested
  against real 7.0, 8.0, 8.3 and 9.0 servers; the bugs that turned up fixed.
* **0.7** `host`, for hosts running many mongods.
* **0.8** (long-term release) sharded clusters, `indexes`, and every triage
  detector checked against real failures on 6.0 to 9.0.

## Ideas, not scheduled

* Jumbo chunks and balancer windows.
* Slow oplog application on secondaries ("Applied op" lines). Needs real
  output of a secondary falling behind before it can be built.
* `lab` on Windows.
