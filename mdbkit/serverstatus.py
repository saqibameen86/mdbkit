"""`mdbkit serverstatus` — make sense of a serverStatus dump.

`db.adminCommand({serverStatus: 1})` returns several hundred fields. The
handful that explain a struggling server are scattered through it, and the
numbers that matter most — ticket exhaustion, cache pressure, queue depth —
are the ones people rarely know to look for.

This reads a saved dump and gives verdicts. Two dumps taken a minute apart
give true rates, because almost every counter in there is cumulative since
process start.

Read-only and offline like everything else: you run the command, mdbkit
reads the file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .explain import _relax_shell_json

# Thresholds. WiredTiger begins background eviction at 80% cache usage and
# forces application threads to evict at 95%, so those are the numbers worth
# warning on rather than arbitrary ones.
CACHE_WARN_PCT = 80.0
CACHE_CRIT_PCT = 95.0
DIRTY_WARN_PCT = 5.0
DIRTY_CRIT_PCT = 20.0
CONN_WARN_PCT = 80.0
TICKET_WARN_PCT = 25.0     # of tickets still free
TICKET_CRIT_PCT = 5.0


@dataclass
class Check:
    severity: str          # CRIT | WARN | OK | INFO
    title: str
    detail: str
    evidence: List[str] = field(default_factory=list)
    next_step: str = ""

    def to_dict(self) -> dict:
        return {"severity": self.severity, "title": self.title,
                "detail": self.detail, "evidence": self.evidence,
                "next": self.next_step}


def load(path: str) -> dict:
    """Load a serverStatus dump, tolerating mongosh's extended JSON."""
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    try:
        doc = json.loads(text)
    except ValueError:
        doc = json.loads(_relax_shell_json(text))
    if not isinstance(doc, dict):
        raise ValueError("expected a JSON object from serverStatus")
    # mongosh sometimes wraps output, and some tools nest it.
    for key in ("serverStatus", "result", "value"):
        inner = doc.get(key)
        if isinstance(inner, dict) and ("host" in inner or "uptime" in inner):
            return inner
    return doc


def _num(doc, *path, default=None):
    cur = doc
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    if isinstance(cur, bool):
        return int(cur)
    if isinstance(cur, (int, float)):
        return cur
    if isinstance(cur, dict):
        # extended JSON: {"$numberLong": "42"}
        for k in ("$numberLong", "$numberInt", "$numberDouble"):
            if k in cur:
                try:
                    return float(cur[k]) if "Double" in k else int(cur[k])
                except (TypeError, ValueError):
                    return default
    return default


def _tickets(doc) -> Dict[str, Dict[str, Optional[float]]]:
    """Read/write ticket state across MongoDB versions.

    Pre-7.0 exposes wiredTiger.concurrentTransactions; 7.0+ moved the same
    idea to queues.execution. Both are checked so the digest works on
    whatever the user actually runs.
    """
    out = {}
    for kind in ("read", "write"):
        avail = _num(doc, "wiredTiger", "concurrentTransactions", kind,
                     "available")
        out_ = _num(doc, "wiredTiger", "concurrentTransactions", kind, "out")
        total = _num(doc, "wiredTiger", "concurrentTransactions", kind,
                     "totalTickets")
        if avail is None:
            avail = _num(doc, "queues", "execution", kind, "available")
            out_ = _num(doc, "queues", "execution", kind, "out")
            total = _num(doc, "queues", "execution", kind, "totalTickets")
        if avail is None and out_ is None:
            continue
        if total is None and avail is not None and out_ is not None:
            total = avail + out_
        out[kind] = {"available": avail, "out": out_, "total": total}
    return out


def analyze(doc: dict, after: Optional[dict] = None) -> List[Check]:
    """Produce checks. When `after` is given, counters become true rates."""
    checks: List[Check] = []
    uptime = _num(doc, "uptime", default=0) or 0
    host = doc.get("host") or "unknown"
    version = doc.get("version") or "unknown"

    span = None
    if after is not None:
        a_up = _num(after, "uptime", default=0) or 0
        span = a_up - uptime
        if span <= 0:
            span = None

    # -- identity ---------------------------------------------------------
    proc = doc.get("process") or "mongod"
    checks.append(Check(
        "INFO", "Server",
        "%s %s (%s), up %s." % (host, version, proc, _dur(uptime)),
        next_step="" if span else
        "Take a second dump a minute apart and pass --after for true rates; "
        "the counters below are cumulative since startup."))

    # -- tickets ----------------------------------------------------------
    tickets = _tickets(doc)
    if tickets:
        rows, worst = [], "OK"
        for kind, t in sorted(tickets.items()):
            total = t.get("total") or 0
            avail = t.get("available")
            if avail is None or not total:
                continue
            pct = 100.0 * avail / total
            rows.append("%s: %d of %d free (%.0f%%)"
                        % (kind, avail, total, pct))
            if pct <= TICKET_CRIT_PCT:
                worst = "CRIT"
            elif pct <= TICKET_WARN_PCT and worst != "CRIT":
                worst = "WARN"
        if rows:
            checks.append(Check(
                worst, "Concurrency tickets",
                "Exhausted tickets queue every new operation, which looks "
                "like slowness with no slow query to blame."
                if worst != "OK" else "Plenty of concurrency available.",
                rows,
                "" if worst == "OK" else
                "Find what is holding tickets: mdbkit queries <log> "
                "--sort scanRatio, and check for long-running operations."))

    # -- cache ------------------------------------------------------------
    cache = doc.get("wiredTiger", {}).get("cache", {}) if isinstance(
        doc.get("wiredTiger"), dict) else {}
    used = _num(cache, "bytes currently in the cache")
    maxb = _num(cache, "maximum bytes configured")
    dirty = _num(cache, "tracked dirty bytes in the cache")
    if used is not None and maxb:
        pct = 100.0 * used / maxb
        sev = ("CRIT" if pct >= CACHE_CRIT_PCT else
               "WARN" if pct >= CACHE_WARN_PCT else "OK")
        ev = ["%s of %s used (%.1f%%)" % (_bytes(used), _bytes(maxb), pct)]
        if dirty is not None and maxb:
            dpct = 100.0 * dirty / maxb
            ev.append("dirty: %s (%.1f%%)" % (_bytes(dirty), dpct))
            if dpct >= DIRTY_CRIT_PCT:
                sev = "CRIT"
            elif dpct >= DIRTY_WARN_PCT and sev == "OK":
                sev = "WARN"
        checks.append(Check(
            sev, "WiredTiger cache",
            "Above %.0f%% WiredTiger evicts in the background; above %.0f%% "
            "application threads are made to evict, which slows queries "
            "directly." % (CACHE_WARN_PCT, CACHE_CRIT_PCT)
            if sev != "OK" else "Cache usage is comfortable.", ev,
            "" if sev == "OK" else
            "Either the working set outgrew the cache or eviction cannot "
            "keep up with writes."))

    app_evict = _num(cache, "pages evicted by application threads")
    if app_evict:
        checks.append(Check(
            "WARN", "Application-thread eviction",
            "%s pages have been evicted by application threads since "
            "startup — user operations paying eviction cost."
            % format(int(app_evict), ","),
            next_step="Correlate with cache usage above."))

    # -- connections ------------------------------------------------------
    cur = _num(doc, "connections", "current")
    avail = _num(doc, "connections", "available")
    if cur is not None and avail is not None and (cur + avail):
        total = cur + avail
        pct = 100.0 * cur / total
        sev = "WARN" if pct >= CONN_WARN_PCT else "OK"
        ev = ["%d in use of %d (%.1f%%)" % (cur, total, pct)]
        created = _num(doc, "connections", "totalCreated")
        if created is not None:
            ev.append("%s created since startup" % format(int(created), ","))
            if uptime:
                ev.append("average %.1f new connections/sec"
                          % (created / uptime))
        checks.append(Check(sev, "Connections",
                            "Approaching the connection limit."
                            if sev == "WARN" else "Connection use is healthy.",
                            ev,
                            "" if sev == "OK" else
                            "Check driver pool sizes: mdbkit connections <log>"))

    # -- queues -----------------------------------------------------------
    qr = _num(doc, "globalLock", "currentQueue", "readers", default=0) or 0
    qw = _num(doc, "globalLock", "currentQueue", "writers", default=0) or 0
    active_r = _num(doc, "globalLock", "activeClients", "readers", default=0)
    if qr or qw:
        checks.append(Check(
            "WARN" if (qr + qw) >= 10 else "INFO", "Operations queued",
            "%d reader(s) and %d writer(s) waiting for a lock or ticket "
            "at the moment this dump was taken." % (qr, qw),
            ["active readers: %s" % active_r] if active_r is not None else []))
    else:
        checks.append(Check("OK", "Operations queued",
                            "Nothing waiting on locks or tickets."))

    # -- throughput -------------------------------------------------------
    ops = doc.get("opcounters") or {}
    if ops:
        rows = []
        for key in ("insert", "query", "update", "delete", "getmore",
                    "command"):
            v = _num(ops, key)
            if v is None:
                continue
            if span and after is not None:
                delta = (_num(after.get("opcounters") or {}, key) or 0) - v
                rows.append("%-8s %s in %.0fs  (%.1f/sec)"
                            % (key, format(int(delta), ","), span,
                               delta / span))
            elif uptime:
                rows.append("%-8s %s total  (%.1f/sec average since startup)"
                            % (key, format(int(v), ","), v / uptime))
            else:
                rows.append("%-8s %s total" % (key, format(int(v), ",")))
        if rows:
            checks.append(Check(
                "INFO", "Operation counters",
                "Measured over %.0f seconds between the two dumps." % span
                if span else
                "These are cumulative since startup, so the per-second "
                "figures are lifetime averages, not current load.", rows))

    # -- asserts ----------------------------------------------------------
    asserts = doc.get("asserts") or {}
    fired = {k: _num(asserts, k) for k in asserts
             if isinstance(_num(asserts, k), (int, float))}
    noisy = {k: v for k, v in fired.items() if v}
    if noisy:
        checks.append(Check(
            "WARN" if noisy.get("regular") or noisy.get("warning") else "INFO",
            "Assertions",
            "The server has recorded assertion counts since startup.",
            ["%s: %s" % (k, format(int(v), ",")) for k, v in
             sorted(noisy.items())],
            "Regular assertions usually indicate a real fault; user "
            "assertions are often just rejected client operations."))

    # -- replication ------------------------------------------------------
    repl = doc.get("repl") or {}
    if repl:
        role = ("PRIMARY" if repl.get("isWritablePrimary") or repl.get("ismaster")
                else "SECONDARY" if repl.get("secondary") else "unknown")
        ev = ["set: %s" % repl.get("setName", "?"), "role: %s" % role]
        if repl.get("hosts"):
            ev.append("members: %s" % ", ".join(str(h) for h in repl["hosts"]))
        checks.append(Check("INFO", "Replication", "This node is %s." % role, ev))

    fc_lagged = _num(doc, "flowControl", "isLagged")
    if fc_lagged:
        checks.append(Check(
            "WARN", "Flow control",
            "Flow control is engaged: the primary is throttling writes "
            "because the majority commit point is lagging.",
            next_step="Check secondary health and replication lag."))

    # -- memory -----------------------------------------------------------
    res = _num(doc, "mem", "resident")
    virt = _num(doc, "mem", "virtual")
    if res is not None:
        ev = ["resident: %s" % _bytes(res * 1024 * 1024)]
        if virt:
            ev.append("virtual: %s" % _bytes(virt * 1024 * 1024))
        if maxb:
            ev.append("configured cache: %s" % _bytes(maxb))
        checks.append(Check("INFO", "Memory", "Process memory as reported by "
                                              "the server.", ev))
    return checks


def _dur(seconds) -> str:
    seconds = int(seconds or 0)
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm" % (seconds // 60)
    if seconds < 86400:
        return "%dh %dm" % (seconds // 3600, (seconds % 3600) // 60)
    return "%dd %dh" % (seconds // 86400, (seconds % 86400) // 3600)


def _bytes(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024 or unit == "TiB":
            return "%.1f %s" % (n, unit) if unit != "B" else "%d B" % n
        n /= 1024.0
    return "%.1f TiB" % n


EXPORT_SCRIPT = """// Save this and run it, then analyse with:
//   mdbkit serverstatus status.json
//
//   mongosh --quiet --host HOST --port PORT \\
//     --username USER --password PASS --authenticationDatabase admin \\
//     --eval "$(cat export_serverstatus.js)" > status.json
//
// For true rates rather than lifetime averages, take two dumps a minute
// apart and compare them:
//   ... > before.json ; sleep 60 ; ... > after.json
//   mdbkit serverstatus before.json --after after.json
JSON.stringify(db.adminCommand({ serverStatus: 1 }));
"""
