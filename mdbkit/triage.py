"""`mdbkit triage` — one-command incident snapshot (beta).

Answers the question a DBA has at 3 a.m.: *what happened in the last hour?*

Defaults to the last 60 minutes of log time, because triage is for incidents
happening now or just finished. Use --window N to widen, --window 0 for the
whole file.

Read-only, offline, single pass over the log plus optional local OS probes
(/proc and statvfs — nothing leaves the machine, no shell-outs). Never
connects to a database, never mutates anything; findings carry next steps
for a HUMAN to run.

Detector validation status:
  stable : restarts, fatal errors, connection storms, hot collections,
           slow-query bursts, COLLSCAN volume, index builds, noise filtering
  beta   : elections/stepdowns, slow checkpoints, eviction pressure,
           flow control — pattern-matched from documented log messages,
           pending broader validation (see docs/TESTING-PLAYBOOK.md).
"""

from __future__ import annotations

import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Dict, List, Optional, Tuple

from .analysis import QueryAggregator
from .parser import (ID_CONN_ACCEPTED, ID_LISTENING, ID_SHUTDOWN,
                     ID_STARTUP, LogEntry, ParseStats, iter_entries,
                     iter_entries_multi)

SEV_ORDER = {"CRIT": 0, "WARN": 1, "INFO": 2, "OK": 3}
DEFAULT_WINDOW_MIN = 60

COMMON_DBPATHS = ("/var/lib/mongodb", "/var/lib/mongo", "/data/db",
                  "/opt/mongodb/data", "/mongodb/data")


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


def _fmt_min(m: int, tz=None) -> str:
    """Format a minute bucket in the log's own timezone (not UTC).

    Log timestamps carry an offset (e.g. -04:00); showing peaks in UTC while
    the window header shows local time is confusing during an incident.
    """
    from datetime import datetime, timezone
    return datetime.fromtimestamp(m * 60, tz=tz or timezone.utc).strftime("%H:%M")


def _ms(v: float) -> str:
    if v >= 60_000:
        return "%.1fm" % (v / 60_000)
    if v >= 1_000:
        return "%.1fs" % (v / 1_000)
    return "%dms" % int(v)


class TriageEngine:
    """Single-pass log consumer feeding all detectors."""

    ELECTION_EVENT = ("starting an election", "election succeeded",
                      "stepping down", "stepped down")
    ELECTION_STATE = ("member is in new state", "replica set state transition",
                      "transition to")
    INDEX_BUILD = ("index build", "build index", "index builds")

    def __init__(self):
        self.qagg = QueryAggregator()
        self.startups: List = []
        self.errors: Counter = Counter()
        self.error_examples: Dict = {}
        self.conn_minutes: Counter = Counter()
        self.conn_ips: defaultdict = defaultdict(Counter)
        self.elections: List = []
        self.state_changes: List = []
        self.checkpoints: List = []
        self.evictions = 0
        self.flow_control = 0
        self.dbpath: Optional[str] = None
        self.slow_minutes: Counter = Counter()
        self.collscan_count = 0
        self.collscan_minutes: Counter = Counter()
        self.slow_total = 0
        self.index_builds: List = []
        self.log_tz = None
        self.system_index_builds = 0
        self.self_state = None            # this node's latest role
        self.self_state_at = None
        self.member_states = {}           # host -> (state, ts)
        self.heartbeat_errors = Counter()
        self.listening = False
        self.shutdown_at = None

    # ---------------------------------------------------------- consume ----
    def consume(self, entry: LogEntry):
        if self.log_tz is None and entry.ts is not None:
            self.log_tz = entry.ts.tzinfo
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

        if entry.is_slow_query and not (
                not self.qagg.include_system
                and QueryAggregator.is_system_ns(str(entry.attr.get("ns", "")))):
            self.slow_total += 1
            if entry.ts is not None:
                self.slow_minutes[_minute(entry.ts)] += 1
            if "COLLSCAN" in str(entry.attr.get("planSummary", "")):
                self.collscan_count += 1
                if entry.ts is not None:
                    self.collscan_minutes[_minute(entry.ts)] += 1

        if entry.msg_id == ID_LISTENING or "waiting for connections" in msg_l:
            self.listening = True
        if entry.msg_id == ID_SHUTDOWN or msg_l.startswith("shutting down"):
            self.shutdown_at = entry.ts

        if entry.component in ("REPL", "ELECTION", "REPL_HB"):
            new_state = entry.attr.get("newState")
            host = entry.attr.get("hostAndPort") or entry.attr.get("host")
            if new_state and host:
                self.member_states[str(host)] = (str(new_state), entry.ts)
            elif new_state and "state transition" in msg_l:
                self.self_state = str(new_state)
                self.self_state_at = entry.ts
            if "heartbeat" in msg_l and (entry.severity in ("W", "E")
                                         or "error" in msg_l
                                         or "failed" in msg_l):
                tgt = str(entry.attr.get("target")
                          or entry.attr.get("hostAndPort") or "unknown")
                self.heartbeat_errors[tgt] += 1

        if entry.component in ("REPL", "ELECTION"):
            if any(p in msg_l for p in self.ELECTION_EVENT) and \
                    "not starting" not in msg_l:
                self.elections.append((entry.ts, entry.msg))
            elif any(p in msg_l for p in self.ELECTION_STATE):
                self.state_changes.append(
                    (entry.ts, entry.msg,
                     str(entry.attr.get("newState",
                                        entry.attr.get("memberState", "")))))
            if "flow control" in msg_l or "flow control" in wt_msg.lower():
                self.flow_control += 1

        if entry.component == "INDEX" and any(p in msg_l for p in self.INDEX_BUILD):
            ns = str(entry.attr.get("namespace") or entry.attr.get("ns") or "")
            if QueryAggregator.is_system_ns(ns):
                self.system_index_builds += 1
            else:
                self.index_builds.append((entry.ts, entry.msg, ns))

        if "checkpoint" in msg_l or "checkpoint" in wt_msg.lower():
            dur = entry.attr.get("durationMillis")
            if dur is None:
                m = re.search(r"took (\d+) second", wt_msg.lower())
                dur = int(m.group(1)) * 1000 if m else None
            self.checkpoints.append((entry.ts, dur))

        if "eviction" in msg_l or "eviction" in wt_msg.lower():
            self.evictions += 1

    # --------------------------------------------------------- findings ----
    def _health_finding(self) -> Finding:
        """Synthesise cluster health from the log alone — no connection.

        Everything here is 'what the log last said', which is the honest
        limit of an offline tool. It answers the 3am question 'is this node
        even serving, and what does it think of its peers?'
        """
        bits = []
        role = self.self_state or "unknown"
        if self.self_state_at:
            bits.append("this node last reported %s at %s"
                        % (role, self.self_state_at.strftime("%H:%M:%S")))
        elif self.self_state:
            bits.append("this node last reported %s" % role)

        unhealthy = []
        for host, (state, ts) in sorted(self.member_states.items()):
            label = "%s = %s" % (host, state)
            if ts:
                label += " (at %s)" % ts.strftime("%H:%M:%S")
            bits.append(label)
            low = state.lower()
            if any(w in low for w in ("not reachable", "unhealthy", "down",
                                      "removed", "rollback", "recovering",
                                      "startup")):
                unhealthy.append(host)

        sev = "OK"
        detail_head = "Serving connections; no problems visible in the log."
        next_step = ""

        if self.shutdown_at:
            sev = "CRIT"
            detail_head = ("A shutdown was logged at %s — this node stopped "
                           "serving." % self.shutdown_at.strftime("%H:%M:%S"))
            next_step = "Check whether the process was restarted afterwards."
        elif unhealthy:
            sev = "CRIT"
            detail_head = ("%d member(s) last reported an unhealthy state: %s."
                           % (len(unhealthy), ", ".join(unhealthy)))
            next_step = ("Check those hosts directly: process alive, disk, "
                         "network reachability from this node.")
        elif self.heartbeat_errors:
            sev = "WARN"
            total = sum(self.heartbeat_errors.values())
            worst = self.heartbeat_errors.most_common(1)[0]
            detail_head = ("%d heartbeat error(s) in window; most to %s (%d)."
                           % (total, worst[0], worst[1]))
            next_step = "Network or peer health between replica set members."
        elif self.elections:
            sev = "WARN"
            detail_head = ("The set re-elected during this window; it may be "
                           "healthy now but it was not stable.")
        elif not self.listening and not self.member_states and not self.self_state:
            sev = "INFO"
            detail_head = ("Not enough replication detail in this window to "
                           "judge cluster health.")
            next_step = ("Widen with --window 0, or point at a log that "
                         "covers a restart.")

        return Finding(sev, "Cluster health", detail_head, bits, next_step)

    def findings(self) -> List[Finding]:
        out: List[Finding] = [self._health_finding()]

        if self.elections:
            times = [t.strftime("%H:%M:%S") if t else "?" for t, _ in self.elections]
            out.append(Finding(
                "CRIT", "Replica set instability",
                "%d election/stepdown event(s) at %s." % (
                    len(self.elections), ", ".join(times[:6])),
                [m for _, m in self.elections[:6]],
                "Correlate with connection storms and slow checkpoints below; "
                "check node health and network at those timestamps.",
                beta=True))
        else:
            out.append(Finding("OK", "Replica set",
                               "No election or stepdown messages in window.",
                               beta=True))

        if self.startups:
            ts = [t.strftime("%H:%M:%S") if t else "?" for t in self.startups]
            out.append(Finding(
                "CRIT" if len(self.startups) > 1 else "WARN",
                "Process start(s) in window",
                "mongod startup marker seen %dx (at %s). Unplanned restarts "
                "are incidents." % (len(self.startups), ", ".join(ts[:5])),
                next_step="If unexpected: check the OOM killer (dmesg -T | "
                          "grep -i oom) and the Errors finding below."))

        if self.errors:
            total = sum(self.errors.values())
            out.append(Finding(
                "WARN", "Error-severity log lines",
                "%d E/F line(s) in window." % total,
                ["%dx [%s] %s" % (n, c, m)
                 for (c, m), n in self.errors.most_common(5)],
                "Read them: mdbkit filter <log> --severity E --last 20"))
        else:
            out.append(Finding("OK", "Errors",
                               "No error/fatal severity lines in window."))

        out.append(self._storm_finding())
        out.extend(self._slow_query_findings())

        if self.index_builds:
            times = [t.strftime("%H:%M:%S") if t else "?"
                     for t, _, _ in self.index_builds]
            namespaces = sorted({ns for _, _, ns in self.index_builds if ns})
            out.append(Finding(
                "WARN", "Index build activity",
                "%d index-build message(s) at %s%s. Index builds consume CPU, "
                "memory and I/O and can slow the whole node." % (
                    len(self.index_builds), ", ".join(times[:4]),
                    " on " + ", ".join(namespaces[:3]) if namespaces else ""),
                [m for _, m, _ in self.index_builds[:4]],
                "If unexpected during an incident, find who started it: "
                "db.currentOp({'command.createIndexes': {$exists: true}})"))
        elif self.system_index_builds:
            out.append(Finding(
                "INFO", "Index builds (system only)",
                "%d index-build message(s), all on internal namespaces "
                "(admin/config/local) — normal startup housekeeping." %
                self.system_index_builds))

        slow_cp = [(t, d) for t, d in self.checkpoints if d and d >= 60_000]
        if slow_cp:
            worst = max(d for _, d in slow_cp)
            out.append(Finding(
                "WARN", "Slow WiredTiger checkpoints (log)",
                "%d checkpoint(s) over 60s (worst %.1fs). Usually disk I/O "
                "saturation or a large dirty cache." % (
                    len(slow_cp), worst / 1000.0),
                next_step="Check disk latency/utilisation at those times.",
                beta=True))

        if self.evictions:
            out.append(Finding(
                "WARN", "Cache eviction pressure",
                "%d eviction-related message(s) — application threads may be "
                "doing eviction work (cache too small or workload spike)." %
                self.evictions,
                next_step="Compare WT cache used vs configured: "
                          "db.serverStatus().wiredTiger.cache", beta=True))

        if self.flow_control:
            out.append(Finding(
                "WARN", "Flow control engaged",
                "%d flow-control message(s): the primary throttled writes "
                "because the majority-commit point lagged." % self.flow_control,
                next_step="Check secondary health and replication lag.",
                beta=True))
        return out

    def _storm_finding(self) -> Finding:
        if not self.conn_minutes:
            return Finding("INFO", "Connections",
                           "No connection-accepted events in window.")
        counts = sorted(self.conn_minutes.values())
        if len(counts) >= 2:
            # The busiest minute must not be its own baseline.
            baseline = counts[:-1]
            median = baseline[(len(baseline) - 1) // 2]
        else:
            median = 0  # too little history — rely on the absolute floor
        threshold = max(60, 10 * max(1, median))
        storms = {m: n for m, n in self.conn_minutes.items() if n >= threshold}
        peak_min = max(self.conn_minutes, key=self.conn_minutes.get)
        peak_n = self.conn_minutes[peak_min]
        if not storms:
            return Finding(
                "OK", "Connections",
                "No connection storms. Peak %d/min at %s (median %d/min)."
                % (peak_n, _fmt_min(peak_min, self.log_tz), median))
        top_ips = self.conn_ips[peak_min].most_common(3)
        return Finding(
            "WARN", "Connection storm",
            "%d minute(s) at >= %d new connections/min (baseline median "
            "%d/min); peak %d at %s." % (
                len(storms), threshold, median, peak_n, _fmt_min(peak_min, self.log_tz)),
            ["%s: %d in the peak minute" % (ip or "unknown", n)
             for ip, n in top_ips],
            "Identify the client: mdbkit connections <log> — look for pool "
            "misconfiguration or crash-loop reconnects.")

    def _slow_query_findings(self) -> List[Finding]:
        out: List[Finding] = []
        shapes = self.qagg.results()
        if not shapes:
            detail = "None logged in window (slowms default 100 ms)."
            if self.qagg.skipped_system:
                detail += (" %d internal operation(s) on admin/config/local "
                           "were excluded." % self.qagg.skipped_system)
            out.append(Finding("OK", "Slow queries", detail))
            return out

        if self.slow_minutes:
            peak_min = max(self.slow_minutes, key=self.slow_minutes.get)
            peak_n = self.slow_minutes[peak_min]
            counts = sorted(self.slow_minutes.values())
            median = counts[len(counts) // 2] or 1
            sev = "WARN" if peak_n >= max(50, 5 * median) else "INFO"
            out.append(Finding(
                sev, "Slow query volume",
                "%d slow operations in window; peak %d in the minute at %s "
                "(median %d/min)." % (self.slow_total, peak_n,
                                      _fmt_min(peak_min, self.log_tz), median),
                next_step="Zoom in on the peak: mdbkit filter <log> --slow 100 "
                          "--last 20"))

        if self.collscan_count:
            pct = 100.0 * self.collscan_count / max(1, self.slow_total)
            sev = "WARN" if pct >= 25 else "INFO"
            peak = ""
            if self.collscan_minutes:
                pm = max(self.collscan_minutes, key=self.collscan_minutes.get)
                peak = " Peak %d at %s." % (self.collscan_minutes[pm],
                                                _fmt_min(pm, self.log_tz))
            out.append(Finding(
                sev, "Collection scans",
                "%d of %d slow operations used COLLSCAN (%.0f%%).%s" % (
                    self.collscan_count, self.slow_total, pct, peak),
                next_step="mdbkit advise <log> --limit 5"))

        by_ns: Counter = Counter()
        for s in shapes:
            by_ns[s.shape.ns] += s.total_ms
        total = sum(by_ns.values()) or 1
        ns, ms = by_ns.most_common(1)[0]
        share = 100.0 * ms / total
        # A dominant share of a trivial total is not an incident.
        sev = ("WARN" if share >= 50 and len(shapes) >= 3 and ms >= 5_000
               else "INFO")
        out.append(Finding(
            sev, "Hot collection",
            "%s accounts for %.0f%% of slow-query time (%.1fs)." % (
                ns, share, ms / 1000.0),
            ["%s | %s | %dx | %s cumulative | %s" % (
                s.shape.ns, s.shape.operation, s.count, _ms(s.total_ms),
                s.shape.pretty()[:60])
             for s in shapes[:3]],
            "mdbkit advise <log> --ns %s" % ns))
        return out


# ------------------------------------------------------------- sysprobe ----

def _read(path: str, limit: int = 65536) -> str:
    try:
        with open(path, "r", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def find_mongod() -> Optional[Tuple[int, List[str]]]:
    """Locate a running mongod by reading /proc — no shell-outs."""
    if not os.path.isdir("/proc"):
        return None
    try:
        pids = [d for d in os.listdir("/proc") if d.isdigit()]
    except OSError:
        return None
    for pid in pids:
        cmdline = _read("/proc/%s/cmdline" % pid, 8192)
        if not cmdline:
            continue
        argv = [a for a in cmdline.split("\0") if a]
        if argv and os.path.basename(argv[0]) == "mongod":
            try:
                return int(pid), argv
            except ValueError:
                continue
    return None


def dbpath_from_config(path: str) -> Optional[str]:
    """Parse dbPath out of a mongod.conf (YAML or legacy ini). No deps."""
    text = _read(path)
    if not text:
        return None
    for line in text.splitlines():
        stripped = line.split("#", 1)[0].strip()
        if not stripped:
            continue
        m = re.match(r"^dbPath\s*:\s*(\S+)", stripped, re.IGNORECASE)
        if m:
            return m.group(1).strip("\"'")
        m = re.match(r"^dbpath\s*=\s*(\S+)", stripped, re.IGNORECASE)
        if m:
            return m.group(1).strip("\"'")
    return None


def dbpath_from_argv(argv: List[str]) -> Optional[str]:
    """Extract dbPath from a mongod command line, or from its config file."""
    for i, arg in enumerate(argv):
        if arg in ("--dbpath", "--dbPath") and i + 1 < len(argv):
            return argv[i + 1]
        if arg.startswith("--dbpath=") or arg.startswith("--dbPath="):
            return arg.split("=", 1)[1]
    for i, arg in enumerate(argv):
        conf = None
        if arg in ("-f", "--config") and i + 1 < len(argv):
            conf = argv[i + 1]
        elif arg.startswith("--config="):
            conf = arg.split("=", 1)[1]
        if conf:
            return dbpath_from_config(conf)
    return None


def discover_dbpath(from_log: Optional[str]) -> Tuple[Optional[str], str]:
    """Resolve dbPath through a fallback chain. Returns (path, how)."""
    if from_log and os.path.isdir(from_log):
        return from_log, "startup line in log"
    found = find_mongod()
    if found:
        _pid, argv = found
        candidate = dbpath_from_argv(argv)
        if candidate and os.path.isdir(candidate):
            return candidate, "running mongod process"
    for conf in ("/etc/mongod.conf", "/etc/mongodb.conf",
                 "/usr/local/etc/mongod.conf"):
        candidate = dbpath_from_config(conf)
        if candidate and os.path.isdir(candidate):
            return candidate, conf
    for candidate in COMMON_DBPATHS:
        if os.path.isdir(candidate):
            return candidate, "common default location"
    return (from_log, "log (not present on this host)") if from_log else (None, "")


def sysprobe(dbpath_from_log: Optional[str],
             explicit: Optional[str] = None) -> List[Finding]:
    """Local OS probes. Stdlib only, no shell-outs, all failures soft."""
    out: List[Finding] = []
    if explicit:
        dbpath, how = explicit, "--dbpath"
    else:
        dbpath, how = discover_dbpath(dbpath_from_log)

    found = find_mongod()
    if found:
        pid, _argv = found
        rss_kb = 0
        for line in _read("/proc/%d/status" % pid).splitlines():
            if line.startswith("VmRSS:"):
                try:
                    rss_kb = int(line.split()[1])
                except (IndexError, ValueError):
                    pass
                break
        uptime_note = ""
        try:
            boot = float(_read("/proc/uptime").split()[0])
            starttime = float(_read("/proc/%d/stat" % pid).split()[21])
            hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
            age_s = boot - (starttime / hz)
            if age_s > 0:
                uptime_note = ", up %.1f h" % (age_s / 3600.0)
        except (IndexError, ValueError, ZeroDivisionError, OSError):
            pass
        out.append(Finding(
            "INFO", "mongod process",
            "pid %d, RSS %.1f GiB%s." % (pid, rss_kb / 1048576.0, uptime_note)))

    if not dbpath or not os.path.isdir(dbpath):
        out.append(Finding(
            "INFO", "System probes skipped",
            "Could not locate a dbPath on this machine (checked the log's "
            "startup line, any running mongod, /etc/mongod.conf and common "
            "defaults). Pass --dbpath /your/data/dir to enable the disk check."))
        return out

    try:
        st = os.statvfs(dbpath)
        free = st.f_bavail * st.f_frsize
        totalb = st.f_blocks * st.f_frsize or 1
        used_pct = 100.0 * (1 - st.f_bavail / float(st.f_blocks or 1))
        sev = "CRIT" if used_pct >= 95 else "WARN" if used_pct >= 85 else "OK"
        out.append(Finding(
            sev, "Disk (dbPath volume)",
            "%s [%s]: %.0f%% used, %.1f GiB free of %.1f GiB." % (
                dbpath, how, used_pct, free / 2.0 ** 30, totalb / 2.0 ** 30),
            next_step="" if sev == "OK" else
            "Free space or extend the volume — a full dbPath stops writes."))
    except OSError as exc:
        out.append(Finding("INFO", "Disk probe unavailable", str(exc)))

    try:
        info = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, _, rest = line.partition(":")
                info[k] = int(rest.strip().split()[0])
        avail, total = info.get("MemAvailable", 0), info.get("MemTotal", 1)
        pct = 100.0 * avail / total
        sev = "WARN" if pct < 10 else "OK"
        out.append(Finding(sev, "Memory",
                           "%.0f%% available (%.1f GiB of %.1f GiB)." % (
                               pct, avail / 1048576.0, total / 1048576.0),
                           next_step="" if sev == "OK" else
                           "Check WT cache sizing and other processes; watch "
                           "for the OOM killer."))
    except (OSError, ValueError, KeyError):
        out.append(Finding("INFO", "Memory probe unavailable",
                           "/proc/meminfo not readable (non-Linux?)."))

    try:
        load1, load5, _ = os.getloadavg()
        cores = os.cpu_count() or 1
        sev = "WARN" if load1 > 2 * cores else "OK"
        out.append(Finding(sev, "CPU load",
                           "load1=%.1f load5=%.1f on %d core(s)." % (
                               load1, load5, cores),
                           next_step="" if sev == "OK" else
                           "Find the cost: mdbkit queries <log> --sort totalMs "
                           "--limit 10"))
    except OSError:
        out.append(Finding("INFO", "Load probe unavailable", ""))
    return out


# ------------------------------------------------------------------ run ----

def ftdc_findings(path: str, ts_from=None, ts_to=None) -> List[Finding]:
    """Turn decoded FTDC metrics into triage findings.

    FTDC is MongoDB's own flight recorder: it already holds CPU, memory,
    cache and connection history for every node, with no monitoring stack
    installed. This reads it offline.
    """
    from .ftdc import FtdcReader, ftdc_files
    out: List[Finding] = []
    files = ftdc_files(path)
    if not files:
        return [Finding("INFO", "FTDC", "No metrics.* files found at %s." % path)]
    reader = FtdcReader(keep_values=False).read(path, ts_from=ts_from,
                                                ts_to=ts_to)
    if reader.chunks == 0:
        detail = "Found %d file(s) but decoded no metric chunks." % len(files)
        if reader.skipped:
            detail = ("Found %d file(s), but all %d metric chunks fall outside "
                      "the triage window — the log and diagnostic.data appear "
                      "to cover different time ranges. Widen with --window, or "
                      "inspect the metrics directly with `mdbkit ftdc summary`."
                      % (len(files), reader.skipped))
        return [Finding("INFO", "FTDC", detail)]

    span = ""
    if reader.first_ts and reader.last_ts:
        span = " (%s -> %s)" % (reader.first_ts.strftime("%H:%M"),
                                reader.last_ts.strftime("%H:%M"))
    out.append(Finding(
        "INFO", "FTDC metrics",
        "Decoded %d chunk(s), %d sample(s) from %d file(s)%s." % (
            reader.chunks, reader.samples, len(files), span)))

    pct = reader.cache_pct()
    if pct is not None:
        sev = "CRIT" if pct >= 95 else "WARN" if pct >= 90 else "OK"
        out.append(Finding(
            sev, "WiredTiger cache",
            "Peak usage %.0f%% of configured maximum." % pct,
            next_step="" if sev == "OK" else
            "Sustained pressure above 95%% forces application threads to "
            "evict, which shows up as slow queries."))

    conns = reader.series.get("conns.current")
    if conns and conns.values:
        avail = reader.series.get("conns.available")
        detail = "peak %d concurrent, last %d." % (max(conns.values),
                                                   conns.values[-1])
        sev = "OK"
        if avail and avail.values:
            total = max(conns.values) + min(avail.values)
            used_pct = 100.0 * max(conns.values) / max(1, total)
            detail = ("peak %d of ~%d available (%.0f%%)." %
                      (max(conns.values), total, used_pct))
            sev = "WARN" if used_pct >= 80 else "OK"
        out.append(Finding(sev, "Connections (FTDC)", detail))

    for label, title in (("queue.readers", "Read queue"),
                         ("queue.writers", "Write queue")):
        s_ = reader.series.get(label)
        if s_ and s_.values and max(s_.values) > 0:
            peak = max(s_.values)
            sev = "WARN" if peak >= 10 else "INFO"
            out.append(Finding(
                sev, title + " (FTDC)",
                "Peak %d operation(s) queued waiting for a lock/ticket." % peak,
                next_step="" if sev == "INFO" else
                "Queueing means the server ran out of capacity — correlate "
                "with the slow-query peak above."))

    # Checkpoint / eviction / flow control are measured here rather than in
    # the log: mongod does not log checkpoint duration at default verbosity,
    # and eviction pressure has no log line at all. FTDC records all three.
    cp = reader.series.get("checkpoint.lastMs")
    if cp and cp.vmax is not None:
        worst = cp.vmax / 1000.0
        sev = "WARN" if worst >= 60 else "INFO"
        out.append(Finding(
            sev, "Checkpoints (FTDC)",
            "Longest checkpoint in window %.1fs." % worst,
            next_step="" if sev == "INFO" else
            "Sustained long checkpoints usually mean disk saturation or a "
            "large dirty cache; correlate with disk metrics."))

    evict = reader.rate("evict.appThreadPages")
    if evict is not None and evict > 0:
        sev = "WARN" if evict >= 1 else "INFO"
        out.append(Finding(
            sev, "Cache eviction pressure (FTDC)",
            "Application threads evicted ~%.1f pages/s. When user operations "
            "have to evict, the cache is not keeping up." % evict,
            next_step="" if sev == "INFO" else
            "Compare cache used vs configured above; consider a larger "
            "WiredTiger cache or reducing the working set."))

    lagged = reader.series.get("flowControl.isLagged")
    fc_wait = reader.rate("flowControl.waitMicros")
    if lagged and lagged.vmax:
        out.append(Finding(
            "WARN", "Flow control engaged (FTDC)",
            "The primary throttled writes because the majority-commit point "
            "lagged%s." % ("; ~%.0f ms/s spent waiting" % (fc_wait / 1000.0)
                           if fc_wait else ""),
            next_step="Check secondary health and replication lag."))

    mem = reader.series.get("mem.residentMB")
    if mem and mem.values:
        out.append(Finding("INFO", "mongod memory (FTDC)",
                           "Resident peak %.1f GiB." % (max(mem.values) / 1024.0)))

    for label, title in (("sys.cpu.iowaitMs", "CPU iowait (FTDC)"),
                         ("sys.cpu.userMs", "CPU user (FTDC)")):
        r = reader.rate(label)
        if r is not None and r > 0:
            pct_cpu = r / 10.0  # ms/s across all cores -> rough %
            sev = ("WARN" if label.endswith("iowaitMs") and pct_cpu > 20
                   else "INFO")
            out.append(Finding(
                sev, title, "~%.0f%% of one core equivalent." % pct_cpu,
                next_step="" if sev == "INFO" else
                "High iowait points at disk saturation rather than CPU."))

    rates = []
    for label in ("ops.query", "ops.insert", "ops.update", "ops.delete",
                  "ops.getmore", "ops.command"):
        r = reader.rate(label)
        if r is not None and r >= 1:
            rates.append("%s ~%.0f/s" % (label.split(".")[1], r))
    if rates:
        out.append(Finding("INFO", "Throughput (FTDC)",
                           "Average over the window: " + ", ".join(rates) + "."))
    return out


def find_diagnostic_data(dbpath: Optional[str]) -> Optional[str]:
    """diagnostic.data always lives inside the dbPath, so if we found the
    dbPath we already know where the metrics are — no need to ask."""
    if not dbpath:
        return None
    candidate = os.path.join(dbpath, "diagnostic.data")
    if os.path.isdir(candidate):
        try:
            if any(n.startswith("metrics.") for n in os.listdir(candidate)):
                return candidate
        except OSError:
            return None
    return None


def run_triage(logfile: str, window_min: Optional[int] = None,
               dbpath: Optional[str] = None, no_sysprobe: bool = False,
               ftdc_path: Optional[str] = None):
    """Analyze the last `window_min` minutes of log time (default 60).

    window_min=0 analyzes the whole file.
    """
    if window_min is None:
        window_min = DEFAULT_WINDOW_MIN

    paths = logfile if isinstance(logfile, list) else [logfile]
    cutoff = None
    if window_min and paths != ["-"]:
        pre = ParseStats()
        for _ in iter_entries_multi(paths, pre):
            pass
        if pre.last_ts:
            cutoff = pre.last_ts - timedelta(minutes=window_min)

    stats = ParseStats()
    engine = TriageEngine()
    for entry in iter_entries_multi(paths, stats):
        if cutoff and entry.ts and entry.ts < cutoff:
            continue
        engine.consume(entry)

    findings = engine.findings()
    resolved_dbpath = None
    if not no_sysprobe:
        resolved_dbpath = dbpath or discover_dbpath(engine.dbpath)[0]
        findings += sysprobe(engine.dbpath, explicit=dbpath)
    if not ftdc_path:
        auto = find_diagnostic_data(resolved_dbpath or dbpath or engine.dbpath)
        if auto:
            ftdc_path = auto
            findings.append(Finding(
                "INFO", "FTDC discovered",
                "Using %s (found next to the dbPath). Pass --ftdc to override."
                % auto))
    if ftdc_path:
        try:
            findings += ftdc_findings(ftdc_path, ts_from=cutoff)
        except Exception as exc:  # never let metrics break log triage
            findings.append(Finding("INFO", "FTDC unavailable", str(exc)[:200]))
    findings.sort(key=lambda f: SEV_ORDER.get(f.severity, 9))
    return findings, stats, cutoff


def render_triage(findings: List[Finding], stats: ParseStats, cutoff) -> str:
    parts = ["== mdbkit triage: what happened recently? =="]
    if stats.first_ts and stats.last_ts:
        start = cutoff or stats.first_ts
        parts.append("window: %s -> %s   (%s lines scanned)" % (
            start.strftime("%Y-%m-%d %H:%M"),
            stats.last_ts.strftime("%H:%M"), format(stats.parsed, ",")))
    counts = Counter(f.severity for f in findings)
    parts.append("findings: %d critical, %d warning, %d ok/info" % (
        counts.get("CRIT", 0), counts.get("WARN", 0),
        counts.get("OK", 0) + counts.get("INFO", 0)))
    parts.append("")
    for f in findings:
        parts.append("[%s] %s: %s" % (f.severity, f.title, f.detail))
        for e in f.evidence:
            parts.append("        - %s" % e)
        if f.next_step:
            parts.append("        next: %s" % f.next_step)
    parts.append("")
    parts.append("Window defaults to the last 60 minutes of log time "
                 "(--window N, or --window 0 for the whole file).")
    parts.append("Checkpoint, eviction and flow-control detectors are marked "
                 "[beta] and pending validation against real incident logs.")
    parts.append("mdbkit is read-only: it never runs commands against your "
                 "cluster. Review every next step before acting.")
    return "\n".join(parts)
