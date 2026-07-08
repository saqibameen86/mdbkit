# mdbkit

**An offline toolkit for MongoDB structured logs — log analysis, slow-query shapes, connection churn, and deterministic index advice.**

A spiritual successor to [mtools](https://github.com/rueckstiess/mtools)' log tools (`mloginfo`, `mlogfilter`) for the structured JSON log format MongoDB has used since 4.4 — the format mtools never supported. Built for DBAs and ops engineers running self-managed MongoDB 4.4 / 5.0 / 6.0 / 7.0 / 8.0.

> **Privacy by design: mdbkit never makes a network call.** It reads log files (or stdin) and writes to stdout. No telemetry, no phoning home, no cloud. Your logs never leave your machine. It is safe to run on air-gapped database hosts.

## Why

MongoDB 4.4 switched to structured JSON logging, and the beloved mtools log commands stopped working — the issue has been open since 2020. Meanwhile, index recommendations from MongoDB's Performance Advisor require a paid Atlas tier or Cloud/Ops Manager. If you run Community Edition on your own infrastructure, you're back to reading raw JSON logs with `grep` and `jq`.

mdbkit fills that gap: a single, dependency-free CLI that turns structured logs into answers.

## Install

```bash
pipx install mdbkit        # recommended
# or
pip install mdbkit
```

Zero runtime dependencies (pure Python stdlib), so it also installs cleanly on air-gapped hosts from a single wheel:

```bash
pip download mdbkit -d ./wheels     # on a connected machine
pip install --no-index --find-links ./wheels mdbkit   # on the DB host
```

Requires Python 3.9+.

## Quick start

```bash
# Overall log summary: versions, restarts, connection counts, error/warning totals
mdbkit loginfo /var/log/mongodb/mongod.log

# Slow queries grouped by query shape (literals stripped), ranked by total time
mdbkit queries mongod.log
mdbkit queries mongod.log --sort scanRatio --limit 10 --json

# Connection churn by source IP, appName, and driver
mdbkit connections mongod.log

# Filter raw log lines (output stays valid logv2 JSON — chainable)
mdbkit filter mongod.log --slow 500 --component COMMAND --ns shop.orders
mdbkit filter mongod.log --from 2026-07-01T08:00:00Z --to 2026-07-01T09:00:00Z | mdbkit queries -

# Rotated/compressed logs work directly
mdbkit queries mongod.log.2.gz
```

### Index advice

```bash
mdbkit advise mongod.log
```

Produces **candidate** indexes from observed slow-query shapes using the ESR
(Equality → Sort → Range) guideline, with evidence, confidence, caveats, and a
validation step for every recommendation:

```
[1] shop.orders  —  confidence: HIGH
    query shape : {createdAt:gt, status:eq} sort:{createdAt:-1}
    candidate   : { status: 1, createdAt: -1 }
    evidence    : COLLSCAN observed in planSummary
    evidence    : in-memory sort (hasSortStage) observed
    evidence    : examined 251,400 docs to return 73 (3444:1)
    caveat      : Every index adds write and storage overhead ...
    validate    : Re-run the query with .explain('executionStats') ...
```

The advice gets sharper if you export your existing indexes and a sampled
schema. mdbkit never connects to your database — instead it prints small
`mongosh` scripts you run yourself, so you can read exactly what they do:

```bash
mdbkit export-script indexes > export_indexes.js
mdbkit export-script schema  > export_schema.js
mongosh --quiet "mongodb://localhost/shop" export_indexes.js > indexes.json
mongosh --quiet "mongodb://localhost/shop" export_schema.js  > schema.json

mdbkit advise mongod.log --indexes indexes.json --schema schema.json
```

With `--indexes`, mdbkit checks each candidate against your existing indexes
(flagging when an existing index should already cover the query, or when a
candidate would make an existing index redundant — it flags, never suggests
dropping). With `--schema`, it warns about multikey (array) fields,
low-cardinality fields, and field-name typos. The schema export records field
**names and types only — no values.**

### Incident triage (beta)

```bash
mdbkit triage /var/log/mongodb/mongod.log        # or a copied log + --no-sysprobe
```

One command during an incident: election/stepdown timeline, connection
storms, hot collections, error clusters, slow checkpoints — plus local
disk/memory/load probes when run on the DB host. Read-only: it never
connects to the database, and every finding ends with a next step for a
human to review. Detectors marked beta are validated against real logs
where available and clearly labeled where broader validation is pending.

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

## Roadmap

Terminal output is and will remain first-class — this tool is built for the
Linux box the database actually runs on.

* **v0.2** — FTDC (`diagnostic.data`) decoding: offline summaries of the
  metrics MongoDB already records on every node; election/failover timeline
  from REPL events; per-shape drill-down.
* **v0.3** — shareable Markdown/HTML report export (for tickets and
  post-incident reviews — a convenience layer, never a replacement for the
  terminal).

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
