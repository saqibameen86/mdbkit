# Security Policy

## Design posture

mdbkit is built to be safe to run on production database hosts:

* **No network code.** The tool never opens a socket, phones home, checks for
  updates, or sends telemetry. Verify it yourself: `grep -rn "socket\|urllib\|http" mdbkit/` finds nothing.
* **No code execution.** Log lines, explain files, and schema/index exports
  are parsed with strict `json.loads` only. MongoDB shell constructors
  (`ObjectId(...)`, `ISODate(...)`) are never evaluated — files containing
  them are rejected with guidance, not interpreted.
* **Untrusted input by default.** Malformed, truncated, adversarial, or
  binary input is counted and skipped, never echoed into exceptions.
* **No shell-outs.** mdbkit never invokes external commands.
* **Zero runtime dependencies.** Nothing in the supply chain but the Python
  standard library.

## Reporting a vulnerability

Please report suspected vulnerabilities privately via GitHub's
**Security → Report a vulnerability** on this repository (preferred), or open
an issue asking for a private contact channel if you cannot use it.
Reports will be acknowledged within a few days. Please do not open public
issues containing exploit details before a fix is released.

## Supported versions

The latest released minor version receives security fixes.
