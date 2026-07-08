# HANDOFF — Continuing mdbkit with any model (Opus/Sonnet/other)

## Project state
- v0.1 complete & tested (24 tests green): loginfo, queries, connections,
  filter, advise, explain, export-script. Zero deps, offline, MIT.
- `triage` shipped BETA: engine + detectors + sysprobe done; election
  detector validated against real 7.x logs; checkpoint/eviction/flow-control
  detectors pending real fixtures (see TESTING-PLAYBOOK.md).
- Validated against 28k-line real log (100% parse) + real replica-set logs.

## How to resume (paste this to the model)
"Read docs/ROADMAP.md first — its principles are non-negotiable (read-only,
offline, stdlib-only, terminal-first, evidence+caveats, fixtures gate
shipping). Then read docs/DESIGN-<feature>.md and implement exactly that.
Before and after changes run: pip install -e '.[dev]' && pytest. All 24+
existing tests must stay green. New parsers/detectors require REAL fixtures
per docs/TESTING-PLAYBOOK.md. Update README and the design doc's acceptance
checklist. Never add dependencies, network code, shell-outs, or eval."

## Work order
1. Graduate triage betas using fixtures Saqib brings from the playbook
   (add tests/fixtures/real_*, remove beta labels per detector).
2. DESIGN-ftdc.md — the v0.2 flagship (FTDC includes system CPU/mem/disk;
   wire `triage --ftdc` per DESIGN-triage.md).
3. serverstatus digest (section in DESIGN-triage.md) + export-script kinds.
4. DESIGN-reports.md (v0.3).
5. PyPI release: fix CHANGEME URLs in pyproject.toml, `python -m build`,
   `twine upload dist/*`.

## Conventions
- One module per feature area; findings/recommendations are dataclasses
  with to_dict(); every command has --json (schemas are contracts).
- Errors never echo raw input. Untrusted-input rules in SECURITY.md must
  stay literally true (CI-greppable: no socket/urllib/subprocess/eval).
- Real-log bugs found so far (do not regress): elections log under
  component ELECTION not REPL; batched update/delete at COMMAND level omit
  q (WRITE twins carry it); deletes report ndeleted; noise ops filtered.
