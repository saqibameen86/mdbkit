"""`mdbkit oslog` — what the operating system saw.

A mongod log often just stops. It does not say "the kernel killed me", and
it cannot: the process was gone before it could write anything. That
explanation lives in the system log, and it is usually one line.

This reads a plain text system log — `/var/log/messages`, `/var/log/syslog`,
`/var/log/kern.log`, or output you captured from `journalctl` — and reports
the things that affect a database process.

No shell-outs: mdbkit does not run `journalctl` for you, it tells you the
command to run. That keeps the guarantee that analysis commands never start
a process. See SECURITY.md.
"""

from __future__ import annotations

import gzip
import io
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

# Where a system log usually lives, most specific first.
COMMON_OSLOGS = (
    "/var/log/syslog",
    "/var/log/messages",
    "/var/log/kern.log",
)

JOURNAL_HINT = (
    "This system appears to use journald rather than text logs. Capture what\n"
    "  mdbkit needs with:\n"
    "    journalctl -k --since '4 hours ago' > kern.log\n"
    "    journalctl -u mongod --since '4 hours ago' >> kern.log\n"
    "  then re-run with:  --oslog kern.log"
)

# Syslog:  "Jul 30 10:15:23 host kernel: ..."
_SYSLOG_TS = re.compile(
    r"^([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})\s+(\S+)\s+(.*)$")
# journalctl short-iso / ISO-ish: "2026-07-30T10:15:23+0400 host unit: ..."
_ISO_TS = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}):(\d{2}):(\d{2})\S*\s+(\S+)\s+(.*)$")

_MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}


@dataclass
class OsEvent:
    kind: str
    severity: str
    line: str
    ts: Optional[datetime] = None
    detail: Dict[str, str] = field(default_factory=dict)


# Each rule: (kind, severity, compiled pattern, human explanation)
# Ordered most-specific first; a line matches at most one rule.
RULES = [
    ("oom-kill", "CRIT",
     re.compile(r"Out of memory:\s*Kill(?:ed)? process\s+(\d+)\s+\(([^)]+)\)",
                re.I),
     "The kernel ran out of memory and killed a process."),
    ("oom-kill", "CRIT",
     re.compile(r"oom-kill:.*?task=(\S+).*?pid=(\d+)", re.I),
     "The kernel OOM killer terminated a task."),
    ("oom-pressure", "WARN",
     re.compile(r"(invoked oom-killer|page allocation (?:failure|stall))", re.I),
     "Memory pressure severe enough to invoke the OOM killer."),
    ("open-files", "CRIT",
     re.compile(r"(too many open files|EMFILE|VFS: file-max limit)", re.I),
     "A file-descriptor limit was reached."),
    ("segfault", "CRIT",
     re.compile(r"(segfault at|general protection fault|traps:\s*mongod)", re.I),
     "A process crashed on a memory fault."),
    ("disk-error", "CRIT",
     re.compile(r"(I/O error|Buffer I/O error|blk_update_request|"
                r"EXT4-fs error|XFS \(.*\): (?:metadata )?I/O error|"
                r"critical medium error)", re.I),
     "The storage layer reported an I/O error."),
    ("filesystem-ro", "CRIT",
     re.compile(r"(Remounting filesystem read-only|"
                r"filesystem has been set read-only)", re.I),
     "A filesystem was remounted read-only, which stops all writes."),
    ("service-exit", "WARN",
     re.compile(r"mongod\.service:.*(Main process exited|Failed with result|"
                r"Killed process|Scheduled restart|Start request repeated)",
                re.I),
     "systemd reported the mongod service stopping or restarting."),
    ("conntrack", "WARN",
     re.compile(r"nf_conntrack:\s*(?:table full|nf_conntrack: table full)", re.I),
     "The connection-tracking table filled, which drops new connections."),
    ("network-drop", "WARN",
     re.compile(r"(TCP: out of memory|possible SYN flooding|"
                r"neighbour table overflow)", re.I),
     "The kernel network stack shed load."),
    ("thp", "INFO",
     re.compile(r"transparent_hugepage", re.I),
     "Transparent huge pages activity — MongoDB recommends these disabled."),
    ("numa", "INFO",
     re.compile(r"\bnuma_balancing\b", re.I),
     "NUMA balancing activity."),
]

EXPLANATIONS = {kind: why for kind, _sev, _pat, why in RULES}


def _open(path: str):
    if path.endswith(".gz"):
        return io.TextIOWrapper(gzip.open(path, "rb"), errors="replace")
    return open(path, "r", encoding="utf-8", errors="replace")


def parse_ts(line: str, year: Optional[int] = None):
    """Best-effort timestamp. Syslog omits the year, so assume the current
    one unless a caller knows better."""
    m = _ISO_TS.match(line)
    if m:
        try:
            d = [int(x) for x in m.group(1).split("-")]
            return datetime(d[0], d[1], d[2], int(m.group(2)),
                            int(m.group(3)), int(m.group(4)))
        except (ValueError, IndexError):
            return None
    m = _SYSLOG_TS.match(line)
    if m:
        try:
            return datetime(year or datetime.now().year,
                            _MONTHS.get(m.group(1), 1), int(m.group(2)),
                            int(m.group(3)), int(m.group(4)), int(m.group(5)))
        except ValueError:
            return None
    return None


def scan(paths, ts_from=None, ts_to=None) -> List[OsEvent]:
    """Scan one or more system logs for events that matter to a database."""
    if isinstance(paths, str):
        paths = [paths]
    events: List[OsEvent] = []
    for path in paths:
        try:
            fh = _open(path)
        except OSError:
            continue
        with fh:
            for raw in fh:
                line = raw.rstrip("\n")
                if not line:
                    continue
                matched = None
                for kind, sev, pattern, _why in RULES:
                    m = pattern.search(line)
                    if m:
                        matched = (kind, sev, m)
                        break
                if not matched:
                    continue
                kind, sev, m = matched
                ts = parse_ts(line)
                if ts_from and ts and ts < ts_from:
                    continue
                if ts_to and ts and ts > ts_to:
                    continue
                detail = {}
                groups = [g for g in (m.groups() or ()) if g]
                if kind == "oom-kill" and groups:
                    for g in groups:
                        if g.isdigit():
                            detail["pid"] = g
                        else:
                            detail["process"] = g
                events.append(OsEvent(kind=kind, severity=sev, line=line[:400],
                                      ts=ts, detail=detail))
    return events


def discover() -> List[str]:
    """System logs readable on this host, if any."""
    return [p for p in COMMON_OSLOGS
            if os.path.isfile(p) and os.access(p, os.R_OK)]


def uses_journald() -> bool:
    """True when journald is present but no readable text log is."""
    return os.path.isdir("/run/systemd/journal") and not discover()


def summarize(events: List[OsEvent]) -> List[dict]:
    """Group events by kind, newest occurrence first."""
    by_kind: Dict[str, List[OsEvent]] = {}
    for e in events:
        by_kind.setdefault(e.kind, []).append(e)
    out = []
    order = {"CRIT": 0, "WARN": 1, "INFO": 2}
    for kind, items in by_kind.items():
        sev = min((i.severity for i in items), key=lambda s: order.get(s, 9))
        stamped = [i for i in items if i.ts]
        out.append({
            "kind": kind,
            "severity": sev,
            "count": len(items),
            "explanation": EXPLANATIONS.get(kind, ""),
            "first": min((i.ts for i in stamped), default=None),
            "last": max((i.ts for i in stamped), default=None),
            "processes": sorted({i.detail.get("process") for i in items
                                 if i.detail.get("process")}),
            "examples": [i.line for i in items[:3]],
        })
    out.sort(key=lambda d: (order.get(d["severity"], 9), -d["count"]))
    return out


def mongod_was_oom_killed(events: List[OsEvent]) -> bool:
    return any(e.kind == "oom-kill"
               and "mongo" in (e.detail.get("process") or "").lower()
               for e in events)
