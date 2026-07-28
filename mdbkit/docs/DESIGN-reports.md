# DESIGN v0.3 — Shareable Reports (`--report`)

Status: approved design, not yet implemented. Depends on v0.2 landing.

## Scope

A convenience layer that renders existing analyses (loginfo, queries,
advise, explain, triage, ftdc summary) into a single shareable document.
Terminal output remains primary; this exists for tickets, hand-offs, and
post-incident reviews.

## CLI

    mdbkit report <log> [--ftdc DIR] [--serverstatus f1 f2] \
        --format md|html [-o incident-2026-07-07.md]

Also a `--report md|html -o FILE` flag on individual commands.

## Rules

* Markdown first (tickets/Slack/GitHub paste); HTML is the same content
  through a tiny inline template.
* HTML must be fully self-contained: inline CSS, no JS required, NO
  external assets/CDN (offline principle). ASCII/inline-SVG sparklines only.
* Reports contain query SHAPES and metrics only — same redaction posture
  as terminal output; literals never appear (they never enter shapes).
* Deterministic: same inputs -> byte-identical report (fixed ordering,
  timestamp of the LOG WINDOW not of generation, unless --stamp).
* Footer: mdbkit version + "generated offline; not affiliated with
  MongoDB, Inc."

## Acceptance criteria

1. Golden-file tests: fixture inputs -> committed expected md/html.
2. HTML passes a no-network check (grep for http/src=/href= whitelist).
3. A triage report of the sample corpus fits in one screen of Markdown.
