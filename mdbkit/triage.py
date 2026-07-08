"""`mdbkit triage` — one-command incident snapshot (beta).

At 3 a.m., on a box with nothing installed but mdbkit, one command answers:
what is unhealthy, what changed, and where to look next.

Read-only, offline, single pass over the log plus optional local OS probes
(statvfs/proc — nothing leaves the machine). Never connects to a database,
never mutates anything; findings include next steps for a HUMAN to run.

Detector validation status:
  stable : restarts, fatal errors, connection storms, hot collections
  beta   : elections/stepdowns, slow checkpoints, eviction pressure,
           flow control — pattern-matched from documented log messages,
           pending validation against real-cluster fixtures
           (see docs/TESTING-PLAYBOOK.md).
"""

from __future__ import annotations

import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from typing import List, Optional

from .analysis import QueryAggregator
from .parser import ID_CONN_ACCEPTED, ID_STARTUP, LogEntry, ParseStats, iter_entries

SEV_ORDER = {"CRIT": 0, "WARN": 1, "INFO": 2, "OK": 3}


@dataclass
class Finding:
    severity: str  # CRIT | WARN | INFO | OK
    title: str
    detail: str
    evidence: List[str] = field(default_factory=list)
    next_step: str = ""
    beta: bool = False

    def to_dict(self) -> dict:
        return {"severity": self.severity, "title": self.title,
                "detail": self.detail, "evidence": self.evidence,
                "nextStep": self.next_step, "beta": self.beta}


def _minute(ts) -> int:
    return int(ts.timestamp() // 60)


def _fmt_min(m: int) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(m * 60, tz=timezone.utc).strftime("%H:%M")


class TriageEngine:
    """Single-pass log consumer feeding all detectors."""

    ELECTION_EVENT = ("starting an election", "election succeeded",
                      "stepping down", "stepped down")
    ELECTION_STATE = ("member is in new state", "replica set state transition",
                      "transition to")

    def __init__(self):
        self.qagg = QueryAggregator()
        self.startups: List = []
        self.errors: Counter = Counter()
        self.error_examples = {}
        self.conn_minutes: Counter = Counter()
        self.conn_ips: defaultdict = defaultdict(Counter)
        self.elections: List = []      # (ts, kind, msg)
        self.state_changes: List = []  # (ts, msg, newState)
        self.checkpoints: List = []    # (ts, duration_ms or None)
        self.evictions = 0
        self.flow_control = 0
        self.dbpath: Optional[str] = None

    # ---------------------------------------------------------- consume ----
    def consume(self, entry: LogEntry):
        self.qagg.consume(entry)
        msg_l = entry.msg.lower()
        wt_msg = str(entry.attr.get("message", "")) if entry.attr else ""

        if entry.msg_id == ID_STARTUP:
            self.startups.append(entry.ts)
            self.dbpath = entry.attr.get("dbPath") or self.dbpath
        elif entry.severity in ("E", "F"):
            key = (entry.component, entry.msg)
            self.errors[key] += 1
            self.error_examples.setdefault(key, entry.ts)
        if entry.msg_id == ID_CONN_ACCEPTED and entry.ts is not None:
            m = _minute(entry.ts)
            self.conn_minutes[m] += 1
            remote = str(entry.attr.get("remote", "")).rsplit(":", 1)[0]
            self.conn_ips[m][remote] += 1
        if entry.component in ("REPL", "ELECTION"):
            if any(p in msg_l for p in self.ELECTION_EVENT) and \
                    "not starting" not in msg_l:
                self.elections.append((entry.ts, entry.msg))
            elif any(p in msg_l for p in self.ELECTION_STATE):
                new_state = str(entry.attr.get("newState",
                                entry.attr.get("memberState", "")))
                self.state_changes.append((entry.ts, entry.msg, new_state))
            if "flow control" in msg_l or "flow control" in wt_msg.lower():
                self.flow_control += 1
        if "checkpoint" in msg_l or "checkpoint" in wt_msg.lower():
            dur = entry.attr.get("durationMillis")
            if dur is None:
                m = re.search(r"took (\d+) second", wt_msg.lower())
                dur = int(m.group(1)) * 1000 if m else None
            self.checkpoints.append((entry.ts, dur))
        if "eviction" in msg_l or "eviction" in wt_msg.lower():
            self.evictions += 1

    # --------------------------------------------------------- findings ----
    def findings(self) -> List[Finding]:
        out: List[Finding] = []

        # Elections / stepdowns (beta).
        if self.elections:
            times = [t.strftime("%H:%M:%S") if t else "?" for t, _ in self.elections]
            out.append(Finding(
                "CRIT", "Replica set instability",
                f"{len(self.elections)} election/stepdown event(s) in window "
                f"at {', '.join(times[:6])}.",
                [m for _, m in self.elections[:6]],
                "Check node health/network at those timestamps; correlate "
                "with connection storms and slow checkpoints below. Timeline "
                "of state changes: see --json output.", beta=True))
        else:
            out.append(Finding("OK", "Replica set",
                               "No election or stepdown messages found.",
                               beta=True))

        # Restarts.
        if self.startups:
            ts = [t.strftime("%H:%M:%S") if t else "?" for t in self.startups]
            out.append(Finding(
                "WARN", "Process start(s) in window",
                f"mongod startup marker seen {len(self.startups)}x "
                f"(at {', '.join(ts[:5])}). Unplanned restarts are incidents.",
                next_step="If unexpected, check system OOM killer "
                          "(journalctl / dmesg) and FatalError findings."))

        # Fatal / error lines.
        if self.errors:
            total = sum(self.errors.values())
            top = self.errors.most_common(5)
            out.append(Finding(
                "WARN", "Error-severity log lines",
                f"{total} E/F line(s) in window.",
                [f"{n}x [{c}] {m}" for (c, m), n in top],
                "Read the raw lines: mdbkit filter <log> --severity E"))
        else:
            out.append(Finding("OK", "Errors",
                               "No error/fatal severity lines in window."))

        # Connection storm.
        storm = self._storm_finding()
        out.append(storm)

        # Hot collection.
        out.append(self._hot_collection_finding())

        # Checkpoints (beta).
        slow_cp = [(t, d) for t, d in self.checkpoints if d and d >= 60_000]
        if slow_cp:
            worst = max(d for _, d in slow_cp)
            out.append(Finding(
                "WARN", "Slow WiredTiger checkpoints",
                f"{len(slow_cp)} checkpoint(s) over 60s (worst "
                f"{worst/1000:.1f}s). Often disk I/O saturation or huge "
                "dirty cache.",
                next_step="Check disk latency/utilization at those times "
                          "(FTDC will cover this in v0.2).", beta=True))
        elif self.checkpoints:
            out.append(Finding("INFO", "WiredTiger checkpoints",
                               f"{len(self.checkpoints)} checkpoint "
                               "message(s), none flagged slow (>60s).",
                               beta=True))

        # Eviction / flow control (beta).
        if self.evictions:
            out.append(Finding(
                "WARN", "Cache eviction pressure indicators",
                f"{self.evictions} eviction-related message(s) — application "
                "threads may be doing eviction work (cache too small or "
                "workload spike).",
                next_step="Check WT cache usage vs configured max "
                          "(db.serverStatus().wiredTiger.cache).", beta=True))
        if self.flow_control:
            out.append(Finding(
                "WARN", "Flow control engaged",
                f"{self.flow_control} flow-control message(s): the primary "
                "throttled writes because majority-commit point lagged.",
                next_step="Check secondary health/lag and network.",
                beta=True))
        return out

    def _storm_finding(self) -> Finding:
        if not self.conn_minutes:
            return Finding("INFO", "Connections",
                           "No connection-accepted events in window.")
        counts = sorted(self.conn_minutes.values())
        baseline = counts[:-1] or counts  # the busiest minute can't be its own baseline
        median = baseline[(len(baseline) - 1) // 2]
        threshold = max(60, 10 * max(1, median))
        storms = {m: n for m, n in self.conn_minutes.items() if n >= threshold}
        if not storms:
            return Finding("OK", "Connections",
                           f"No connection storms (peak "
                           f"{max(counts)}/min, median {median}/min).")
        worst_min = max(storms, key=storms.get)
        top_ips = self.conn_ips[worst_min].most_common(3)
        return Finding(
            "WARN", "Connection storm",
            f"{len(storms)} minute(s) at >= {threshold} new connections/min "
            f"(baseline median {median}/min); worst {storms[worst_min]} at "
            f"{_fmt_min(worst_min)} UTC.",
            [f"{ip or 'unknown'}: {n} in worst minute" for ip, n in top_ips],
            "Identify the client (appName via `mdbkit connections`); check "
            "for pool misconfiguration or crash-loop reconnects.")

    def _hot_collection_finding(self) -> Finding:
        shapes = self.qagg.results()
        if not shapes:
            return Finding("INFO", "Slow queries",
                           "No slow queries logged in window (slowms "
                           "default is 100 ms).")
        by_ns: Counter = Counter()
        for s in shapes:
            by_ns[s.shape.ns] += s.total_ms
        total = sum(by_ns.values()) or 1
        ns, ms = by_ns.most_common(1)[0]
        share = 100.0 * ms / total
        ns_shapes = [s for s in shapes if s.shape.ns == ns]
        worst = max(ns_shapes, key=lambda s: s.total_ms)
        sev = "WARN" if share >= 50 and len(shapes) >= 3 else "INFO"
        return Finding(
            sev, "Hot collection",
            f"{ns} accounts for {share:.0f}% of slow-query time "
            f"({ms/1000:.1f}s across {len(ns_shapes)} shape(s)).",
            [f"worst shape: {worst.shape.pretty()} "
             f"({worst.count}x, {worst.total_ms/1000:.1f}s total)"],
            f"mdbkit advise <log>  # candidates for {ns}")


# ------------------------------------------------------------- sysprobe ----

def sysprobe(dbpath: Optional[str]) -> List[Finding]:
    """Local OS probes. Stdlib only, no shell-outs, everything try/except."""
    out: List[Finding] = []

    if dbpath and os.path.isdir(dbpath):
        try:
            st = os.statvfs(dbpath)
            free = st.f_bavail * st.f_frsize
            totalb = st.f_blocks * st.f_frsize or 1
            used_pct = 100.0 * (1 - st.f_bavail / (st.f_blocks or 1))
            sev = "CRIT" if used_pct >= 95 else "WARN" if used_pct >= 85 else "OK"
            out.append(Finding(
                sev, "Disk (dbPath volume)",
                f"{dbpath}: {used_pct:.0f}% used, "
                f"{free / 2**30:.1f} GiB free of {totalb / 2**30:.1f} GiB.",
                next_step="" if sev == "OK" else
                "Free space or extend the volume; a full dbPath volume "
                "stops writes and can corrupt shutdown."))
        except OSError as exc:
            out.append(Finding("INFO", "Disk probe unavailable", str(exc)))
    else:
        out.append(Finding(
            "INFO", "System probes skipped",
            "dbPath from the log was not found on this machine — the log "
            "appears to be from another host. Run triage on the DB host "
            "for disk/memory/load checks, or pass --dbpath."))
        return out

    try:
        info = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, _, rest = line.partition(":")
                info[k] = int(rest.strip().split()[0])  # kB
        avail, total = info.get("MemAvailable", 0), info.get("MemTotal", 1)
        pct = 100.0 * avail / total
        sev = "WARN" if pct < 10 else "OK"
        out.append(Finding(sev, "Memory",
                           f"{pct:.0f}% available "
                           f"({avail / 2**20:.1f} GiB of {total / 2**20:.1f} GiB).",
                           next_step="" if sev == "OK" else
                           "Check for OOM risk: page cache squeeze, other "
                           "processes, or WT cache oversized."))
    except (OSError, ValueError, KeyError):
        out.append(Finding("INFO", "Memory probe unavailable",
                           "/proc/meminfo not readable (non-Linux?)."))

    try:
        load1, _, _ = os.getloadavg()
        cores = os.cpu_count() or 1
        sev = "WARN" if load1 > 2 * cores else "OK"
        out.append(Finding(sev, "CPU load",
                           f"load1={load1:.1f} on {cores} core(s).",
                           next_step="" if sev == "OK" else
                           "Identify hot queries: mdbkit queries <log> "
                           "--sort totalMs"))
    except OSError:
        out.append(Finding("INFO", "Load probe unavailable", ""))
    return out


# ------------------------------------------------------------------ run ----

def run_triage(logfile: str, window_min: Optional[int] = None,
               dbpath: Optional[str] = None, no_sysprobe: bool = False):
    cutoff = None
    if window_min and logfile != "-":
        pre = ParseStats()
        for _ in iter_entries(logfile, pre):
            pass
        if pre.last_ts:
            cutoff = pre.last_ts - timedelta(minutes=window_min)

    stats = ParseStats()
    engine = TriageEngine()
    for entry in iter_entries(logfile, stats):
        if cutoff and entry.ts and entry.ts < cutoff:
            continue
        engine.consume(entry)

    findings = engine.findings()
    if not no_sysprobe:
        findings += sysprobe(dbpath or engine.dbpath)
    findings.sort(key=lambda f: SEV_ORDER.get(f.severity, 9))
    return findings, stats, cutoff


def render_triage(findings: List[Finding], stats: ParseStats, cutoff) -> str:
    parts = ["== mdbkit triage (beta) =="]
    span = ""
    if stats.first_ts and stats.last_ts:
        start = cutoff or stats.first_ts
        span = f"window: {start.strftime('%H:%M')} -> " \
               f"{stats.last_ts.strftime('%H:%M')} ({stats.parsed:,} lines)"
    parts.append(span)
    parts.append("beta detectors (elections/checkpoints/eviction/flow-control) "
                 "are pattern-matched pending real-cluster validation — "
                 "see docs/TESTING-PLAYBOOK.md")
    parts.append("")
    for f in findings:
        tag = f"[{f.severity}]"
        parts.append(f"{tag:<6} {f.title}: {f.detail}")
        for e in f.evidence:
            parts.append(f"        - {e}")
        if f.next_step:
            parts.append(f"        next: {f.next_step}")
    parts.append("")
    parts.append("mdbkit is read-only: it never runs commands against your "
                 "cluster. Review every next step before acting.")
    return "\n".join(parts)
