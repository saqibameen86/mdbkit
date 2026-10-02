"""`mdbkit audit` — the configuration problems mongod already told you about.

Every time mongod starts it checks its environment and logs a warning for
each thing it does not like: access control off, file-descriptor limits too
low, transparent huge pages in the wrong mode, NUMA, swappiness, running as
root. These are classic production misconfigurations, and they sit in the
log right after every restart where almost nobody reads them.

This reads them — from a mongod log, or from the output of
`db.adminCommand({getLog: "startupWarnings"})` when the log covering the
last restart has already rotated away — and explains each one.

The message ids below are taken from the MongoDB server source
(startup_warnings_mongod.cpp / startup_warnings_common.cpp) for 6.0, 7.0 and
8.x, and checked against what real 7.0.43, 8.0.32, 8.3.11 and 9.0.2 servers
log. Unknown startup warnings are still reported, with MongoDB's own text.

Offline and read-only like every other analysis command.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Optional

from .parser import (ID_BUILD_INFO, ID_STARTUP, LogEntry, ParseStats,
                     iter_entries_multi, parse_line, text)

# id -> (key, severity, title, advice)
KNOWN: Dict[int, tuple] = {
    # security
    22120: ("access-control", "CRIT", "Access control is not enabled",
            "Anyone who can reach the port can read and write every database. "
            "Enable security.authorization and create users before this node "
            "is reachable from anything but localhost."),
    22138: ("running-as-root", "WARN", "mongod is running as root",
            "Run mongod as an unprivileged service user (the packaged "
            "'mongod' user). A compromise of the process should not be a "
            "compromise of the host."),
    22124: ("x509-invalid-certs", "WARN", "Invalid X.509 certificates are allowed",
            "net.tls.allowInvalidCertificates is on. Acceptable for testing, "
            "not for production."),
    22128: ("x509-no-hostname-check", "WARN", "X.509 hostname validation is disabled",
            "net.tls.allowInvalidHostnames is on, which allows "
            "man-in-the-middle connections."),
    22147: ("keyfile-multiple-keys", "INFO", "Multiple keys in the security key file",
            "Expected only during a keyfile rollover; remove the old key once "
            "every member has the new one."),
    22140: ("bound-to-localhost", "INFO", "Bound to localhost only",
            "Remote clients and other replica set members cannot connect. "
            "Intended on a laptop, a misconfiguration on a server."),
    # resource limits
    22184: ("rlimit-nofile", "WARN", "Open-file limit is too low",
            "Raise the soft and hard nofile limit (MongoDB recommends 64000). "
            "Low limits surface later as 'too many open files' under load — "
            "see `mdbkit oslog`."),
    22188: ("rlimit-memlock", "WARN", "Locked-memory limit is too low",
            "Raise the memlock limit for the mongod user."),
    5123300: ("max-map-count", "WARN", "vm.max_map_count is too low",
              "Raise vm.max_map_count; WiredTiger can run out of memory map "
              "areas on large deployments."),
    # memory / kernel tuning
    22178: ("thp-enabled", "WARN", "Transparent huge pages are 'always' (pre-8.0 guidance)",
            "On MongoDB 7.0 and earlier, set "
            "/sys/kernel/mm/transparent_hugepage/enabled to 'never'. Note: "
            "MongoDB 8.0 reversed this advice — see the 8.0 warnings."),
    22181: ("thp-defrag", "WARN", "Transparent huge page defrag is 'always' (pre-8.0 guidance)",
            "On MongoDB 7.0 and earlier, set "
            "/sys/kernel/mm/transparent_hugepage/defrag to 'never'."),
    9068900: ("thp-8x", "WARN", "Transparent huge page setting (8.0+ guidance)",
              "MongoDB 8.0's new TCMalloc performs best with THP enabled; "
              "follow the exact change named in the message text."),
    9068901: ("thp-8x", "WARN", "Transparent huge pages should be re-enabled (8.0+ guidance)",
              "MongoDB 8.0 recommends THP enabled for its new memory "
              "allocator, the opposite of the advice for 7.0 and earlier."),
    9068902: ("thp-8x", "INFO", "Could not determine the transparent huge page setting",
              "Check /sys/kernel/mm/transparent_hugepage by hand."),
    8640302: ("thp-8x", "WARN", "A transparent huge page kernel setting should be 0",
              "Follow the file named in the message text (8.0+ guidance)."),
    8718500: ("glibc-rseq", "WARN", "glibc rseq is enabled",
              "MongoDB 8.0's TCMalloc needs rseq for itself; set "
              "GLIBC_TUNABLES=glibc.pthread.rseq=0 for the mongod service."),
    22167: ("numa", "WARN", "Running on a NUMA machine without interleaving",
            "Start mongod with `numactl --interleave=all` (the packaged "
            "service file can do this)."),
    22192: ("numa", "WARN", "NUMA is enabled",
            "Disable NUMA in the BIOS, or run mongod under numactl "
            "--interleave=all."),
    22171: ("overcommit", "WARN", "vm.overcommit_memory is not recommended",
            "Set vm.overcommit_memory to the value named in the message."),
    22174: ("zone-reclaim", "WARN", "vm.zone_reclaim_mode is not 0",
            "Set vm.zone_reclaim_mode to 0; zone reclaim causes latency "
            "spikes on NUMA hardware."),
    8386700: ("swappiness", "WARN", "vm.swappiness is too high",
              "Set vm.swappiness to 0 or 1; swapping the WiredTiger cache "
              "is far slower than evicting from it."),
    22161: ("openvz", "INFO", "Running under OpenVZ", "Known issues on old RHEL kernels."),
    22297: ("xfs", "WARN", "WiredTiger is not on XFS",
            "MongoDB strongly recommends XFS for WiredTiger; ext4 is known to "
            "stall under heavy checkpointing."),
    # build / mode
    22123: ("32-bit", "WARN", "32-bit binary", "Unsupported for production."),
    22135: ("32-bit", "WARN", "32-bit binary on a 64-bit OS", "Unsupported for production."),
    22152: ("32-bit", "WARN", "32-bit binary", "Unsupported for production."),
    6260401: ("restore-mode", "INFO", "Running with --restore",
              "Only for restoring from a backup."),
    8892401: ("restore-mode", "INFO", "Running with --magicRestore",
              "Only for restoring from a backup."),
    21558: ("read-only", "INFO", "Running in read-only mode", "Writes are rejected."),
}

# Warnings identified by text, for messages whose id is not stable across
# versions.
TEXT_RULES = [
    ("xfs", "WARN", "WiredTiger is not on XFS",
     "XFS filesystem is strongly recommended",
     "MongoDB strongly recommends XFS for WiredTiger; ext4 is known to stall "
     "under heavy checkpointing."),
    ("thp", "WARN", "Transparent huge page setting", "transparent_hugepage",
     "Follow the exact change in the message; the right value depends on the "
     "MongoDB version (8.0 reversed the earlier advice)."),
]

SEV_ORDER = {"CRIT": 0, "WARN": 1, "INFO": 2}


@dataclass
class AuditItem:
    key: str
    severity: str
    title: str
    advice: str
    message: str
    ids: List[int] = field(default_factory=list)
    count: int = 0               # messages
    startups: int = 0            # distinct startups that reported it
    last_seen: Optional[datetime] = None
    changes: List[str] = field(default_factory=list)   # "file: now -> want"
    _seen_in: set = field(default_factory=set, repr=False)

    def to_dict(self) -> dict:
        return {"key": self.key, "severity": self.severity, "title": self.title,
                "advice": self.advice, "message": self.message,
                "ids": self.ids, "count": self.count,
                "startups": self.startups, "changes": self.changes,
                "lastSeen": self.last_seen.isoformat() if self.last_seen else None}


@dataclass
class AuditResult:
    items: List[AuditItem]
    startups: int
    versions: List[str]
    source: str

    def to_dict(self) -> dict:
        return {"startups": self.startups, "versions": self.versions,
                "source": self.source,
                "findings": [i.to_dict() for i in self.items]}


def _is_startup_warning(entry: LogEntry, after_startup: bool) -> bool:
    """mongod tags its startup warnings with "startupWarnings"; older lines
    and hand-trimmed logs are recognised by id or by the startup thread."""
    if entry.msg_id in KNOWN or "startupWarnings" in entry.tags:
        return True
    return entry.severity == "W" and entry.ctx == "initandlisten"


def _full_text(entry: LogEntry) -> str:
    details = ["%s=%s" % (k, v) for k, v in entry.attr.items()
               if isinstance(v, (str, int, float)) and not isinstance(v, bool)]
    return entry.msg + (" [%s]" % ", ".join(details) if details else "")


def _change_of(entry: LogEntry) -> str:
    """The concrete fix a kernel-setting warning asks for, e.g.
    "/sys/kernel/mm/transparent_hugepage/enabled: never -> always"."""
    a = entry.attr
    target = text(a.get("sysfsFile") or a.get("file"))
    if not target:
        return ""
    now = a.get("currentValue")
    want = a.get("desiredValue")
    if want is None and "max_ptes_none" in target:
        want = 0      # 8640302 says "should be 0" in its message text
    out = "%s: %s" % (target, now if now is not None else "?")
    if want is not None:
        out += " -> %s" % want
    return out


def audit_entries(entries: Iterable[LogEntry], source: str = "") -> AuditResult:
    found: Dict[str, AuditItem] = {}
    startups = 0
    versions: List[str] = []
    seen_startup = False

    for entry in entries:
        if entry.msg_id == ID_STARTUP:
            startups += 1
            seen_startup = True
        elif entry.msg_id == ID_BUILD_INFO:
            bi = entry.attr.get("buildInfo")
            version = text(bi.get("version")) if isinstance(bi, dict) else ""
            if version and version not in versions:
                versions.append(version)
        if not _is_startup_warning(entry, seen_startup):
            continue

        msg_text = _full_text(entry)
        if entry.msg_id in KNOWN:
            key, sev, title, advice = KNOWN[entry.msg_id]
        else:
            for k, s, t, needle, a in TEXT_RULES:
                if needle.lower() in msg_text.lower():
                    key, sev, title, advice = k, s, t, a
                    break
            else:
                key = "other:%d" % entry.msg_id
                sev, title = "INFO", entry.msg[:80] or "Startup warning"
                advice = "Reported by mongod at startup; see the message text."
        item = found.get(key)
        if item is None:
            item = found[key] = AuditItem(key, sev, title, advice,
                                          msg_text[:300])
        item.count += 1
        change = _change_of(entry)
        if change and change not in item.changes:
            item.changes.append(change)
        if startups not in item._seen_in:
            item._seen_in.add(startups)
            item.startups += 1
        if entry.msg_id and entry.msg_id not in item.ids:
            item.ids.append(entry.msg_id)
        if entry.ts is not None:
            item.last_seen = entry.ts

    items = sorted(found.values(),
                   key=lambda i: (SEV_ORDER.get(i.severity, 9), i.title))
    return AuditResult(items, startups, versions, source)


def load_startup_warnings(path: str) -> Optional[List[LogEntry]]:
    """Read `db.adminCommand({getLog: "startupWarnings"})` output, if `path`
    is one. Returns None when the file is an ordinary log."""
    from .shelljson import read_text_file, loads_lenient
    try:
        raw = read_text_file(path)
    except (OSError, UnicodeError):
        return None
    head = raw.lstrip()[:200]
    if not head.startswith("{") or '"log"' not in raw[:5000] and "log:" not in raw[:5000]:
        return None
    try:
        doc = loads_lenient(raw)
    except ValueError:
        return None
    lines = doc.get("log") if isinstance(doc, dict) else None
    if not isinstance(lines, list):
        return None
    out = []
    for line in lines:
        if isinstance(line, dict):
            line = json.dumps(line)
        entry = parse_line(text(line))
        if entry is not None:
            out.append(entry)
    return out


def run_audit(paths) -> tuple:
    """Audit one or more logs (or a startupWarnings dump)."""
    if isinstance(paths, str):
        paths = [paths]
    if len(paths) == 1 and paths[0] != "-":
        entries = load_startup_warnings(paths[0])
        if entries is not None:
            stats = ParseStats(total_lines=len(entries), parsed=len(entries))
            res = audit_entries(entries, "getLog startupWarnings")
            # getLog output is, by definition, from the current startup.
            res.startups = max(res.startups, 1)
            return res, stats
    stats = ParseStats()
    return audit_entries(iter_entries_multi(paths, stats), "log"), stats


GETLOG_HINT = (
    "Startup warnings are written only when mongod starts. If the log "
    "covering the last restart has rotated away, capture them from the "
    "running server and audit that instead:\n"
    "  mongosh --quiet --eval 'EJSON.stringify(db.adminCommand({getLog: "
    "\"startupWarnings\"}))' > startup.json\n"
    "  mdbkit audit startup.json"
)
