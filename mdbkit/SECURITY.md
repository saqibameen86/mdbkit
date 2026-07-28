# Security Policy

## Design posture

Every **analysis** command — `loginfo`, `queries`, `connections`, `filter`,
`advise`, `explain`, `triage`, `ftdc`, `demo`, `export-script` — is safe to
run on a production database host:

* **No network code.** The tool never opens a socket, phones home, checks for
  updates, or sends telemetry. Verify it yourself:
  `grep -rn "socket\|urllib\|http" mdbkit/` finds nothing.
* **No database connection.** Analysis commands read files and write to your
  terminal. They never connect to MongoDB.
* **No code execution.** Log lines, explain files, and schema/index exports
  are parsed with strict `json.loads` only. MongoDB shell constructors
  (`ObjectId(...)`, `ISODate(...)`) are unwrapped textually, never evaluated.
* **Untrusted input by default.** Malformed, truncated, adversarial, or
  binary input is counted and skipped, never echoed into exceptions.
* **Read-only.** Analysis commands never mutate a deployment. Where an action
  would help, mdbkit prints the command for a human to review and run.
* **Zero runtime dependencies.** Nothing in the supply chain but the Python
  standard library.

### The one exception: `mdbkit lab`

`mdbkit lab` exists to create a **disposable local MongoDB for testing**, so
it necessarily starts external processes. It is the only part of mdbkit that
does, and it is bounded:

* It runs only `mongod` and `mongosh` from your `PATH`. Nothing else, ever.
* It binds to `127.0.0.1` only, on a base port of **28110** — deliberately
  far from 27017–27019 so a lab can never be mistaken for a real deployment.
* It refuses to use or delete a directory it did not create. Every lab
  directory carries a `.mdbkit-lab.json` marker, and `lab destroy` aborts
  without one.
* It never connects to, reads, or modifies any MongoDB it did not start.
* `lab seed` writes sample data only into the lab it created.

If you want the guarantee that mdbkit never starts a process on a given
machine, simply do not run `lab` there — no other command can reach that
code path. `mdbkit demo` covers the same "let me try this" need with no
MongoDB and no subprocesses at all.

## Reporting a vulnerability

Please report suspected vulnerabilities privately via GitHub's
**Security → Report a vulnerability** on this repository (preferred), or open
an issue asking for a private contact channel if you cannot use it.
Reports will be acknowledged within a few days. Please do not open public
issues containing exploit details before a fix is released.

## Supported versions

The latest released minor version receives security fixes.
