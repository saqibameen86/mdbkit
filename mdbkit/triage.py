"""`mdbkit triage` — one-command incident snapshot.

Answers the question a DBA has at 3 a.m.: *what happened in the last hour?*

Defaults to the last 60 minutes of log time, because triage is for incidents
happening now or just finished. Use --window N to widen, --window 0 for the
whole file.

Read-only, offline, single pass over the log plus optional local OS probes
(/proc and statvfs — nothing leaves the machine, no shell-outs). Never
connects to a database, never mutates anything; findings carry next steps
for a HUMAN to run.

The detectors have been checked against real MongoDB 6.0, 7.0, 8.0 and 9.0
servers put through the failures they look for: a primary killed with
kill -9, a stepdown, a connection storm, flow control (secondaries frozen
with fsyncLock), cache pressure, a checkpoint held up for over a minute,
chunk migrations and a shard outage. That output, trimmed, is in
tests/fixtures/real/. WiredTiger does not log eviction pressure at the
default level, so that comes from FTDC; the log is only scanned for its
"cache stuck" style errors.
"""

from __future__ import annotations

import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Dict, List, Optional, Tuple

from .analysis import BatchDedup, QueryAggregator
from .audit import KNOWN as _AUDIT_KNOWN
from .parser import (ID_CONN_ACCEPTED, ID_LISTENING, ID_SHUTDOWN,
                     ID_SLOW_IN_PROGRESS, ID_STARTUP, LogEntry, ParseStats,
                     expand_paths, iter_entries, iter_entries_multi, num, text)

AUDIT_IDS = frozenset(_AUDIT_KNOWN)
# "mongod startup complete", whose attr says whether the previous shutdown
# was clean. Logged by 7.0.43, 8.0.32, 8.3.11 and 9.0.2.
ID_STARTUP_COMPLETE = 8423403
# Re-logged at the top of every file after a log rotation: pid (as a
# string), port and host; and the startup options (with storage.dbPath).
ID_PROCESS_DETAILS = 20721
ID_OPTIONS = 21951
ID_FLOW_CONTROL = 22225
ID_STEPDOWN_CMD = 21579      # "Attempting to step down in response to replSetStepDown"
CHECKPOINT_PROGRESS = re.compile(
    r"[Cc]heckpoint (has been running|ran) for (\d{1,9}) seconds?")


def dbpath_of_options(entry: LogEntry) -> Optional[str]:
    opts = entry.attr.get("options")
    storage = opts.get("storage") if isinstance(opts, dict) else None
    path = storage.get("dbPath") if isinstance(storage, dict) else None
    return text(path) or None

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

    EVICTION_PRESSURE = ("cache stuck", "eviction stuck", "unable to reach eviction goal",
                         "application thread", "cache full", "cache overflow")
    ELECTION_EVENT = ("starting an election", "election succeeded",
                      "stepping down", "stepped down")
    # reasons that mean the primary was lost or moved on purpose, as opposed
    # to a higher-priority member taking over a newly created set
    FAILOVER_REASONS = ("no primary", "election timeout", "step up", "stepdown",
                        "in response to heartbeat")
    ELECTION_STATE = ("member is in new state", "replica set state transition",
                      "transition to")
    INDEX_BUILD = ("index build", "build index", "index builds")

    def __init__(self):
        self.qagg = QueryAggregator()
        self.dedup = BatchDedup()
        from .sharding import ShardingState
        self.sharding = ShardingState()
        self.startups: List = []
        self.errors: Counter = Counter()
        self.error_examples: Dict = {}
        self.conn_minutes: Counter = Counter()
        self.conn_ips: defaultdict = defaultdict(Counter)
        self.elections: List = []
        self.election_terms: List = []   # term per election event, if logged
        self.pids: List[int] = []    # this log's mongod, one pid per start
        self.pid_starts: List = []   # (startup time, pid)
        self.initiated_at = None     # replSetInitiate: a brand-new set
        self.state_changes: List = []
        self.checkpoints: List = []
        self.evictions = 0
        self.flow_control = 0
        self.flow_control_first = None
        self.flow_control_last = None
        self.dbpath: Optional[str] = None
        self.slow_minutes: Counter = Counter()
        self.collscan_count = 0
        self.collscan_minutes: Counter = Counter()
        self.slow_total = 0
        self.index_builds: List = []
        self.log_tz = None
        self.last_seen = None
        self.seen = 0                # entries consumed
        self.history_before_start = 0
        self.at_file_start = True    # the window begins at the log's start
        self.system_index_builds = 0
        self.self_state = None            # this node's latest role
        self.self_state_at = None
        self.member_states = {}           # host -> (state, ts)
        self.heartbeat_errors = Counter()
        self.listening = False
        self.listening_at = None
        self.shutdown_at = None
        self.start_clean: List[bool] = []   # per startup: was it a clean restart?
        self.start_reported: List[bool] = []  # ...as stated by mongod itself
        # Several members' logs read as one stream: the first start of each
        # further instance is where its log begins, not a restart.
        self.start_new_instance: List[bool] = []
        self._start_keys: set = set()
        self._file_history = 0          # non-startup lines so far in this file
        self._file_shutdown = False     # a clean shutdown seen in this file
        self._shutdown_pending = False
        self.log_host = None
        self.in_progress: List = []       # 8.3+ "Slow in-progress query"
        self.audit_entries: List[LogEntry] = []

    # ---------------------------------------------------------- consume ----
    def begin_file(self):
        """A new file starts in a multi-file stream (rotated logs, or the
        logs of several members)."""
        self._file_history = 0
        self._file_shutdown = False

    def note_skipped(self, entry: LogEntry):
        """An entry before the window: not analysed, but it is history."""
        if entry.msg_id != ID_STARTUP and entry.ctx not in ("main", "-"):
            self._file_history += 1

    def consume(self, entry: LogEntry):
        self.sharding.consume(entry)
        self.seen += 1
        if not self.startups and entry.msg_id != ID_STARTUP and \
                entry.ctx not in ("main", "-"):
            # mongod writes a few lines from its "main" thread (6.0 also
            # from "-") before "MongoDB starting"; anything else means the
            # log has history.
            self.history_before_start += 1
        if entry.msg_id != ID_STARTUP and entry.ctx not in ("main", "-"):
            self._file_history += 1
        if entry.ts is not None:
            self.last_seen = entry.ts
        if self.log_tz is None and entry.ts is not None:
            self.log_tz = entry.ts.tzinfo
        self.qagg.consume(entry)
        msg_l = entry.msg.lower()
        wt_msg = str(entry.attr.get("message", "")) if entry.attr else ""

        if entry.msg_id == ID_STARTUP and entry.attr.get("host"):
            self.log_host = text(entry.attr.get("host")).split(":")[0]
        if entry.severity == "W" or entry.msg_id in AUDIT_IDS or \
                entry.msg_id == ID_STARTUP:
            self.audit_entries.append(entry)
        if entry.msg_id == ID_SLOW_IN_PROGRESS:
            self.in_progress.append(entry)
        if entry.msg_id == ID_STARTUP_COMPLETE and self.start_clean:
            # mongod says itself whether the previous shutdown was clean,
            # even when that shutdown is in an older, rotated log.
            summary = entry.attr.get("Summary of time elapsed")
            flag = summary.get("Startup from clean shutdown?") \
                if isinstance(summary, dict) else None
            if isinstance(flag, bool):
                self.start_clean[-1] = flag
                self.start_reported[-1] = True
        if entry.msg_id == ID_PROCESS_DETAILS:
            # A rotated log has no startup line; this says which process
            # (pid) and port wrote it.
            self.note_pid(entry)
            if entry.attr.get("host") and not self.log_host:
                self.log_host = text(entry.attr.get("host")).split(":")[0]
        elif entry.msg_id == ID_OPTIONS and not self.dbpath:
            self.dbpath = dbpath_of_options(entry)
        if entry.msg_id == ID_STARTUP:
            # Which instance: its dbPath, else host and port.
            key = text(entry.attr.get("dbPath")) or "%s:%s" % (
                text(entry.attr.get("host")), text(entry.attr.get("port")))
            new_here = bool(self._start_keys) and key not in self._start_keys
            # Another instance's log (members read as one stream) that
            # begins with its startup: where that log begins, not a restart.
            self.start_new_instance.append(new_here and self._file_history == 0)
            self._start_keys.add(key)
            self.start_clean.append(self._file_shutdown if new_here
                                    else self._shutdown_pending)
            self.start_reported.append(False)
            self._shutdown_pending = False
            self._file_shutdown = False
            self.startups.append(entry.ts)
            self.dbpath = text(entry.attr.get("dbPath")) or self.dbpath
            self.note_pid(entry)
        elif entry.severity in ("E", "F"):
            key = (entry.component, entry.msg)
            self.errors[key] += 1
            self.error_examples.setdefault(key, entry.ts)

        if entry.msg_id == ID_CONN_ACCEPTED and entry.ts is not None:
            m = _minute(entry.ts)
            self.conn_minutes[m] += 1
            remote = str(entry.attr.get("remote", "")).rsplit(":", 1)[0]
            self.conn_ips[m][remote] += 1

        if entry.is_slow_query and not self.dedup.is_duplicate(entry) and not (
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
            self.listening_at = entry.ts
        if entry.msg_id == ID_SHUTDOWN:
            # 23138 "Shutting down" is the process going down. Many other
            # messages start with "Shutting down ..." in normal operation:
            # every chunk migration logs one on the recipient shard.
            self.shutdown_at = entry.ts
            self._shutdown_pending = True
            self._file_shutdown = True

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
            if "replsetinitiate admin command received" in msg_l:
                self.initiated_at = entry.ts
            elif entry.msg_id == 21392 and self.initiated_at is None:
                # "New replica set config in use" with term 0: a set that
                # has never held an election, i.e. one being created. Only
                # the member that ran replSetInitiate logs that command;
                # the others see this.
                cfg = entry.attr.get("config")
                if isinstance(cfg, dict) and num(cfg.get("term"), -1) == 0:
                    self.initiated_at = entry.ts
            if entry.msg_id == ID_STEPDOWN_CMD:
                pass        # recorded below, whatever the component
            elif any(p in msg_l for p in self.ELECTION_EVENT) and \
                    "not starting" not in msg_l and "shutdown" not in msg_l:
                # (a primary stepping down because it is shutting down is
                # part of the shutdown, which health reports on its own)
                self.elections.append((entry.ts, entry.msg))
                term = entry.attr.get("term")
                self.election_terms.append(
                    int(num(term)) if term is not None else None)
            elif any(p in msg_l for p in self.ELECTION_STATE):
                self.state_changes.append(
                    (entry.ts, entry.msg,
                     str(entry.attr.get("newState",
                                        entry.attr.get("memberState", "")))))

        if entry.msg_id == ID_STEPDOWN_CMD:
            # 21579 (COMMAND) on the primary that was asked to step down
            self.elections.append((entry.ts, "Stepping down in response to a "
                                   "replSetStepDown command (%s)" % entry.ctx))
            self.election_terms.append(None)

        if entry.msg_id == ID_FLOW_CONTROL or "flow control is engaged" in msg_l:
            # 22225 (STORAGE, W), every 10 s while the primary throttles
            # writes because the majority-commit point is not moving.
            self.flow_control += 1
            self.flow_control_first = self.flow_control_first or entry.ts
            self.flow_control_last = entry.ts

        if entry.component == "INDEX" and any(p in msg_l for p in self.INDEX_BUILD):
            ns = str(entry.attr.get("namespace") or entry.attr.get("ns") or "")
            # Every new collection gets an _id index built inline, and a
            # secondary logs it as collections replicate; that is not an
            # index build anyone started.
            if QueryAggregator.is_system_ns(ns) or entry.attr.get("index") == "_id_":
                self.system_index_builds += 1
            else:
                self.index_builds.append((entry.ts, entry.msg, ns))

        if "checkpoint" in wt_msg.lower() or "checkpoint" in msg_l:
            # WiredTiger reports a checkpoint every 20 s while it runs:
            # "Checkpoint has been running for 40 seconds and wrote: ...",
            # and "Checkpoint ran for 61 seconds ..." when it ends.
            m = CHECKPOINT_PROGRESS.search(wt_msg) or CHECKPOINT_PROGRESS.search(entry.msg)
            if m:
                self.checkpoints.append((entry.ts, int(m.group(2)), m.group(1) == "ran"))

        text_l = msg_l + " " + wt_msg.lower()
        if "evict" in text_l or any(p in text_l for p in self.EVICTION_PRESSURE):
            # Routine lines ("starting eviction threads" at every 8.x startup,
            # eviction settings in the WiredTiger config string) say nothing
            # about pressure. Count warnings/errors and the phrases
            # WiredTiger uses when eviction is actually struggling.
            if entry.severity in ("W", "E", "F") or any(
                    p in text_l for p in self.EVICTION_PRESSURE):
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

        restarted = bool(self.shutdown_at and self.listening_at
                         and self.listening_at > self.shutdown_at)
        if self.shutdown_at and restarted:
            sev = "WARN"
            detail_head = ("Shut down at %s and serving again from %s."
                           % (self.shutdown_at.strftime("%H:%M:%S"),
                              self.listening_at.strftime("%H:%M:%S")))
            next_step = ("If the restart was not planned, check why: "
                         "mdbkit oslog /var/log/syslog")
        elif self.shutdown_at:
            sev = "CRIT"
            detail_head = ("A shutdown was logged at %s and nothing after it "
                           "shows the node serving again."
                           % self.shutdown_at.strftime("%H:%M:%S"))
            next_step = "Check whether the process was restarted afterwards."
        elif unhealthy:
            sev = "CRIT"
            detail_head = ("%d member(s) last reported an unhealthy state: %s."
                           % (len(unhealthy), ", ".join(unhealthy)))
            next_step = ("Check those hosts directly: process alive, disk, "
                         "network reachability from this node.")
        elif self.sharding.no_primary or self.sharding.host_failures:
            # a mongos (or any process with a replica set monitor) that
            # could not reach some members: serving, but not cleanly
            sev = "WARN"
            detail_head = ("Serving connections, but it could not reach some "
                           "replica set members (see \"Members unreachable\" "
                           "below).")
            next_step = "Check those members: mdbkit triage <their logs>"
        elif self.heartbeat_errors:
            sev = "WARN"
            total = sum(self.heartbeat_errors.values())
            worst = self.heartbeat_errors.most_common(1)[0]
            detail_head = ("%d heartbeat error(s) in window; most to %s (%d)."
                           % (total, worst[0], worst[1]))
            next_step = "Network or peer health between replica set members."
        elif self._unplanned_elections():
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

    def note_pid(self, entry: LogEntry) -> None:
        pid = int(num(entry.attr.get("pid"), 0))
        if pid > 0 and pid not in self.pids:
            self.pids.append(pid)
            self.pid_starts.append((entry.ts, pid))

    def pid_at(self, when) -> Optional[int]:
        """The pid this log's mongod had at `when`, if the log shows it."""
        best = None
        if when is not None and when.tzinfo is None:
            # syslog has no zone; read it in the log's zone (same host).
            for ts, _pid in self.pid_starts:
                if ts is not None and ts.tzinfo is not None:
                    when = when.replace(tzinfo=ts.tzinfo)
                    break
        if when is not None and self.last_seen is not None:
            # A process killed shortly after its last log line is the normal
            # shape of an OOM kill; much later, it may have restarted since.
            limit = self.last_seen
            if limit.tzinfo is None and when.tzinfo is not None:
                limit = limit.replace(tzinfo=when.tzinfo)
            elif limit.tzinfo is not None and when.tzinfo is None:
                when = when.replace(tzinfo=limit.tzinfo)
            if when > limit + timedelta(minutes=10):
                return None
        for ts, pid in self.pid_starts:
            if ts is not None and ts.tzinfo is None and when is not None \
                    and when.tzinfo is not None:
                ts = ts.replace(tzinfo=when.tzinfo)
            if ts is not None and when is not None and ts <= when:
                if best is None or ts >= best[0]:
                    best = (ts, pid)
        return best[1] if best else None

    def _checkpoint_runs(self) -> List:
        """One (first report, seconds) per long checkpoint: WiredTiger
        reports a running checkpoint every 20 s, with a growing count."""
        runs: List = []
        prev = None
        for ts, secs, ended in self.checkpoints:
            if prev is None or secs < prev[1] or prev[2]:
                runs.append([ts, secs])
            else:
                runs[-1][1] = secs
            prev = (ts, secs, ended)
        # WiredTiger also reports some short checkpoints (at startup, for
        # example, "running for 0 seconds"); only long ones matter here.
        return [(t, secs) for t, secs in runs if secs >= 20]

    def _unplanned_elections(self) -> List:
        """Elections other than the first one of a newly initiated set."""
        if self.initiated_at is None:
            return list(self.elections)
        # A new set's first election is term 1. A member with a higher
        # priority may then take over (term 2): also part of setting the
        # set up. A term-2 election because no primary was seen is a real
        # failover (the first primary died), so the reason decides.
        # "Starting an election" lines carry no term: they take the term of
        # the election that follows within a few seconds.
        terms = list(self.election_terms)
        for i in range(len(terms) - 1, -1, -1):
            if terms[i] is None and i + 1 < len(terms) and terms[i + 1] is not None:
                t0, t1 = self.elections[i][0], self.elections[i + 1][0]
                if t0 and t1 and 0 <= (t1 - t0).total_seconds() <= 15:
                    terms[i] = terms[i + 1]
        known = any(t is not None for t in terms)
        settle = self.initiated_at + timedelta(seconds=120)
        term2 = " ".join(m.lower() for (_, m), t in zip(self.elections, terms) if t == 2)
        term2_failover = any(w in term2 for w in self.FAILOVER_REASONS)
        out = []
        for (ts, msg), term in zip(self.elections, terms):
            after = ts is not None and ts >= self.initiated_at
            if after and term == 1:
                continue
            if after and term == 2 and ts <= settle and not term2_failover:
                continue
            if after and term is None and not known and ts <= settle:
                continue
            out.append((ts, msg))
        return out

    def findings(self) -> List[Finding]:
        out: List[Finding] = [self._health_finding()]

        elections = self._unplanned_elections()
        if elections:
            msgs = " ".join(m.lower() for _, m in elections)
            deliberate = ("priority takeover" in msgs or "step up" in msgs
                          or "replsetstepdown" in msgs)

            primaries = [t for t, _m, state in self.state_changes
                         if t is not None and state == "PRIMARY"]

            def after_own_start(ts):
                # A member that starts and never sees a primary calls an
                # election: a restart, not a lost primary. One that saw a
                # primary first and then saw none, lost it.
                for st in self.startups:
                    if st is None or ts is None or not 0 <= (ts - st).total_seconds() <= 45:
                        continue
                    if not any(st <= p < ts for p in primaries):
                        return True
                return False
            no_primary = [(t, m) for t, m in elections
                          if "no primary" in m.lower() or "election timeout" in m.lower()]
            lost = any(not after_own_start(t) for t, _ in no_primary)
            # several members' logs are read one after another: show in time order
            ordered = sorted(elections, key=lambda e: e[0].timestamp() if e[0]
                             else float("inf"))
            evidence = ["%s  %s" % (t.strftime("%H:%M:%S") if t else "?", m)
                        for t, m in ordered[:8]]
            if len(elections) > 8:
                evidence.append("... and %d more" % (len(elections) - 8))
            if lost:
                out.append(Finding(
                    "CRIT", "Replica set instability",
                    "%d election/stepdown event(s); at least once a member saw "
                    "no primary and called an election, which is what losing "
                    "the primary (crash, kill, hang or network) looks like."
                    % len(elections),
                    evidence,
                    "Find why the primary went away at the first timestamp: "
                    "its own log (mdbkit triage on it), then the OS log "
                    "(mdbkit oslog) for OOM kills or restarts."))
            elif deliberate:
                out.append(Finding(
                    "WARN", "Elections",
                    "%d election/stepdown event(s) that look deliberate: a "
                    "stepdown command (replSetStepDown, \"step up request\") or "
                    "a higher-priority member taking over. Confirm they were "
                    "planned." % len(elections),
                    evidence,
                    "If nobody stepped the primary down, look for what made the "
                    "higher-priority member return (a restart) at those times."))
            elif no_primary:
                out.append(Finding(
                    "WARN", "Elections",
                    "%d election/stepdown event(s) right after this node started: "
                    "a member that starts and finds no primary calls an election, "
                    "as after a restart of the whole set or of a one-member set. "
                    "The start itself is reported separately." % len(elections),
                    evidence,
                    "If the restart was not planned, find out why it happened."))
            else:
                out.append(Finding(
                    "WARN", "Elections",
                    "%d election/stepdown event(s), with no reason recorded in "
                    "this log. The member that called the election logs it: "
                    "\"no PRIMARY\" means the primary was lost; \"priority "
                    "takeover\" or \"step up request\" means it was moved."
                    % len(elections),
                    evidence,
                    "Run mdbkit triage on the other members' logs for the same "
                    "time."))
        elif self.elections:
            out.append(Finding(
                "INFO", "Replica set created",
                "The set was initiated at %s and elected its first primary "
                "straight after. That is how a new replica set starts, not "
                "instability." % self.initiated_at.strftime("%H:%M:%S"),
                [m for _, m in sorted(self.elections, key=lambda e: e[0].timestamp()
                                      if e[0] else float("inf"))[:4]]))
        else:
            out.append(Finding("OK", "Replica set",
                               "No election or stepdown messages in window."))

        log_begins_with_start = (len(self.startups) == 1 and self.at_file_start
                                 and self.history_before_start == 0)
        if log_begins_with_start and self.start_reported[:1] == [True] \
                and self.start_clean[:1] == [False]:
            t = self.startups[0]
            out.append(Finding(
                "CRIT", "Started after a crash",
                "The log begins with mongod starting at %s, and mongod reported "
                "that its previous shutdown was not clean: it crashed or was "
                "killed (kill -9, OOM killer) before this log began." % (
                    t.strftime("%H:%M:%S") if t else "?"),
                next_step="Find out why it stopped: mdbkit oslog "
                          "/var/log/syslog, and the previous rotated log."))
        elif log_begins_with_start:
            t = self.startups[0]
            out.append(Finding(
                "INFO", "Log begins at a startup",
                "The log starts with mongod starting at %s. Nothing before it "
                "is in this file, so whether that start was planned is not "
                "visible here." % (t.strftime("%H:%M:%S") if t else "?"),
                next_step="If it was not planned: mdbkit oslog /var/log/syslog "
                          "(OOM kills, fd limits), or the previous rotated log."))
        elif self.startups:
            starts = list(zip(self.startups, self.start_clean, self.start_reported,
                              self.start_new_instance))
            first_crashed = []
            if self.at_file_start and self.history_before_start == 0:
                # Where the log begins: only a crash if mongod says so.
                if self.start_reported[:1] == [True] and not self.start_clean[0]:
                    first_crashed = [self.startups[0]]
                starts = starts[1:]
            # Where another member's log begins: likewise.
            first_crashed += [t for t, clean, said, new in starts
                              if new and said and not clean]
            starts = [(t, clean) for t, clean, _, new in starts if not new]
            crashed = first_crashed + [t for t, clean in starts if not clean]
            planned = [t for t, clean in starts if clean]
            crashed.sort(key=lambda t: t.timestamp() if t else float("inf"))
            fmt = lambda ts: ", ".join(t.strftime("%H:%M:%S") if t else "?"
                                       for t in ts[:5])
            if crashed:
                out.append(Finding(
                    "CRIT", "Process start(s) in window",
                    "mongod started %dx after an unclean stop (at %s): it "
                    "died without shutting down, which is what a crash, "
                    "kill -9 or OOM kill looks like.%s" % (
                        len(crashed), fmt(crashed),
                        " %d other start(s) followed a clean shutdown."
                        % len(planned) if planned else ""),
                    next_step="Find out why it stopped: mdbkit oslog "
                              "/var/log/syslog (OOM kills, fd limits)."))
            elif planned:
                out.append(Finding(
                    "WARN", "Process start(s) in window",
                    "mongod restarted %dx after a clean shutdown (at %s). "
                    "Planned restarts are routine; confirm these were."
                    % (len(planned), fmt(planned)),
                    next_step="If not planned: 'Terminating via shutdown "
                              "command' names the connection that sent it "
                              "(connN); 'Received signal' means the OS or "
                              "service manager stopped it."))

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
        out.extend(self._in_progress_finding())
        out.extend(self._audit_finding())

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

        runs = self._checkpoint_runs()
        if runs:
            slow = [(t, secs) for t, secs in runs if secs >= 60]
            worst = max(secs for _, secs in runs)
            evidence = ["%s  a checkpoint that ran for at least %ds"
                        % (t.strftime("%H:%M:%S") if t else "?", secs)
                        for t, secs in runs[:6]]
            out.append(Finding(
                "WARN" if slow else "INFO", "Slow WiredTiger checkpoints (log)",
                "%d checkpoint(s) ran for 20s or more, %d of them over 60s; "
                "the longest at least %ds. Checkpoints normally start every "
                "60s, so a long one delays the next and leaves more dirty "
                "data in the cache." % (len(runs), len(slow), worst),
                evidence,
                next_step="Check disk latency and utilisation at those times "
                          "(mdbkit ftdc timeline on diagnostic.data shows both)."))

        if self.evictions:
            out.append(Finding(
                "WARN", "Cache eviction pressure",
                "%d eviction-related message(s) — application threads may be "
                "doing eviction work (cache too small or workload spike)." %
                self.evictions,
                next_step="Compare WT cache used vs configured: "
                          "db.serverStatus().wiredTiger.cache"))

        if self.flow_control:
            first, last = self.flow_control_first, self.flow_control_last
            out.append(Finding(
                "WARN", "Flow control engaged",
                "The primary throttled writes because the majority-commit "
                "point stopped moving: %d warning(s) between %s and %s (it "
                "repeats every 10s while engaged)." % (
                    self.flow_control,
                    first.strftime("%H:%M:%S") if first else "?",
                    last.strftime("%H:%M:%S") if last else "?"),
                next_step="Find the secondary that fell behind: its log and "
                          "metrics at that time (disk, CPU, a long-running "
                          "operation, fsyncLock), or a network problem."))
        from .sharding import findings as sharding_findings
        out.extend(sharding_findings(self.sharding, self.qagg.results()))
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
        if ms < 100:
            # (e.g. slowms 0 logs every operation, most of them 0 ms)
            out.extend(self._waiting_findings(shapes))
            return out
        out.append(Finding(
            sev, "Hot collection",
            "%s accounts for %.0f%% of slow-query time (%.1fs)." % (
                ns, share, ms / 1000.0),
            ["%s | %s | %dx | %s cumulative | %s" % (
                s.shape.ns, s.shape.operation, s.count, _ms(s.total_ms),
                s.shape.pretty()[:60])
             for s in shapes[:3]],
            ("Plans are in the shard logs: mdbkit advise <shard log> --ns %s" % ns
             if self.sharding.role == "mongos" else "mdbkit advise <log> --ns %s" % ns)))

        out.extend(self._waiting_findings(shapes))
        return out

    def _waiting_findings(self, shapes) -> List[Finding]:
        """MongoDB 8.0+: was the time spent working, or waiting?

        8.0 logs workingMillis alongside durationMillis. The difference is
        time spent queued for an execution ticket, waiting on locks or held
        back by flow control — slowness that no index will fix.
        """
        timed = [s for s in shapes if s.timed_count]
        total = sum(s.timed_total_ms for s in timed)
        if not timed or total < 1000:
            return []
        waiting = sum(s.waiting_ms for s in timed)
        pct = 100.0 * waiting / total
        worst = sorted(timed, key=lambda s: s.waiting_ms, reverse=True)[:3]
        evidence = ["%s %s: %.0f%% of %s waiting" % (
            s.shape.ns, s.shape.pretty()[:50], s.waiting_pct or 0,
            _ms(s.timed_total_ms)) for s in worst if s.waiting_ms]
        queued_s = sum(s.queued_us for s in timed) / 1e6
        if queued_s >= 1:
            evidence.append("%.1fs of that was queued for execution tickets"
                            % queued_s)
        if pct >= 40:
            return [Finding(
                "WARN", "Time spent waiting, not working",
                "%.0f%% of slow-query time (%s of %s) was spent waiting for "
                "tickets, locks or flow control rather than executing. An "
                "index will not fix that part." % (pct, _ms(waiting), _ms(total)),
                evidence,
                "Check concurrency: mdbkit serverstatus <dump> (tickets, "
                "queues), and what held them: mdbkit queries <log> --sort "
                "scanRatio")]
        return [Finding(
            "INFO", "Time spent waiting",
            "%.0f%% of slow-query time was waiting rather than executing "
            "(MongoDB 8.0+ workingMillis)." % pct, evidence)]

    def _in_progress_finding(self) -> List[Finding]:
        """MongoDB 8.3+: operations logged while still running."""
        if not self.in_progress:
            return []
        longest = max(self.in_progress,
                      key=lambda e: num(e.attr.get("durationMillis")))
        by_ns = Counter(text(e.attr.get("ns")) or "?" for e in self.in_progress)
        evidence = ["%dx %s" % (n, ns) for ns, n in by_ns.most_common(3)]
        evidence.append("longest still running at %s after %s (%s)" % (
            longest.ts.strftime("%H:%M:%S") if longest.ts else "?",
            _ms(num(longest.attr.get("durationMillis"))),
            text(longest.ctx)))
        return [Finding(
            "WARN" if len(self.in_progress) >= 5 else "INFO",
            "Long-running operations",
            "%d operation(s) were logged as still running past "
            "slowOpInProgressThreshold (MongoDB 8.3+). If they never logged "
            "a completed 'Slow query', they were killed or are still going."
            % len(self.in_progress), evidence,
            "Find them live with db.currentOp({secs_running: {$gt: 30}}); "
            "in the log: mdbkit filter <log> --component SLOWPROG")]

    def _audit_finding(self) -> List[Finding]:
        from .audit import audit_entries
        res = audit_entries(self.audit_entries)
        if not res.items:
            return []
        crit = [i for i in res.items if i.severity == "CRIT"]
        warn = [i for i in res.items if i.severity == "WARN"]
        sev = "CRIT" if crit else ("WARN" if warn else "INFO")
        return [Finding(
            sev, "Startup configuration",
            "the server reported %d configuration warning(s) at startup "
            "(%d critical)." % (len(res.items), len(crit)),
            ["%s (%s)" % (i.title, i.severity.lower()) for i in res.items[:5]],
            "Details and fixes: mdbkit audit <log>")]


# ------------------------------------------------------------- sysprobe ----

def _read(path: str, limit: int = 65536) -> str:
    try:
        with open(path, "r", errors="replace") as fh:
            return fh.read(limit)
    except OSError:
        return ""


def find_mongods() -> List[Tuple[int, List[str]]]:
    """Locate every running mongod by reading /proc — no shell-outs.

    Returning all of them matters: a host can run a shard and a config
    server, or several test instances. Silently picking the first one found
    means analysing the wrong deployment's dbPath.
    """
    out: List[Tuple[int, List[str]]] = []
    if not os.path.isdir("/proc"):
        return out
    try:
        pids = sorted(int(d) for d in os.listdir("/proc") if d.isdigit())
    except OSError:
        return out
    for pid in pids:
        cmdline = _read("/proc/%d/cmdline" % pid, 8192)
        if not cmdline:
            continue
        argv = [a for a in cmdline.split("\0") if a]
        if argv and os.path.basename(argv[0]) == "mongod":
            out.append((pid, argv))
    return out


def find_mongod() -> Optional[Tuple[int, List[str]]]:
    """First running mongod, or None. Kept for callers that want one."""
    found = find_mongods()
    return found[0] if found else None


def port_from_argv(argv: List[str]) -> Optional[str]:
    for i, a in enumerate(argv):
        if a == "--port" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--port="):
            return a.split("=", 1)[1]
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
        return from_log, "recorded in the log"
    running = find_mongods()
    if len(running) > 1:
        # Ambiguous: do not guess which deployment the caller means.
        return None, "multiple mongod processes"
    if running:
        _pid, argv = running[0]
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


def _same_path(a: Optional[str], b: Optional[str]) -> bool:
    if not a or not b:
        return False
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except (OSError, ValueError):
        return a.rstrip("/") == b.rstrip("/")


def pick_mongod(running: List[Tuple[int, List[str]]],
                log_pids: Optional[List[int]] = None,
                dbpath: Optional[str] = None) -> Optional[Tuple[int, List[str]]]:
    """The running mongod this log belongs to, or None if that is unclear.

    On a host running several mongods, reporting the first one found gives
    another instance's memory and uptime. Match by the pid in the log's
    startup line, then by dbPath; only a lone mongod is assumed.
    """
    log_pids = log_pids or []
    for pid, argv in running:
        if pid in log_pids:
            return pid, argv
    if dbpath:
        for pid, argv in running:
            if _same_path(dbpath_from_argv(argv), dbpath):
                return pid, argv
    if len(running) == 1:
        argv_db = dbpath_from_argv(running[0][1])
        if not (dbpath and argv_db and not _same_path(argv_db, dbpath)):
            return running[0]
    return None


def sysprobe(dbpath_from_log: Optional[str],
             explicit: Optional[str] = None,
             log_pids: Optional[List[int]] = None,
             router: bool = False) -> List[Finding]:
    """Local OS probes. Stdlib only, no shell-outs, all failures soft."""
    out: List[Finding] = []
    if router and not explicit:
        # A mongos has no dbPath. Guessing one would report some other
        # process's disk as this one's.
        out.append(Finding(
            "INFO", "System probes",
            "This is a mongos log: it has no dbPath, so only memory and load "
            "are checked."))
        return out + _host_probes()
    running = find_mongods()
    if explicit:
        dbpath, how = explicit, "--dbpath"
    else:
        dbpath, how = discover_dbpath(dbpath_from_log)
    found = pick_mongod(running, log_pids, explicit or dbpath)

    if len(running) > 1:
        if found:
            out.append(Finding(
                "INFO", "Multiple mongod processes",
                "%d mongod processes are running here; this log belongs to "
                "pid %d (matched by %s), so only that one is reported." % (
                    len(running), found[0],
                    "the pid the log records" if found[0] in (log_pids or [])
                    else "its dbPath")))
        else:
            rows = []
            for pid, argv in running[:6]:
                rows.append("pid %d%s%s" % (
                    pid,
                    "  port %s" % port_from_argv(argv) if port_from_argv(argv) else "",
                    "  dbPath %s" % dbpath_from_argv(argv)
                    if dbpath_from_argv(argv) else ""))
            if len(running) > 6:
                rows.append("... and %d more" % (len(running) - 6))
            out.append(Finding(
                "INFO", "Multiple mongod processes",
                "%d mongod processes are running here, so mdbkit did not guess "
                "which one this log belongs to." % len(running), rows,
                "Pass --dbpath /path/to/that/instance to include disk and "
                "metrics checks."))

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
    return out + _host_probes()


def _host_probes() -> List[Finding]:
    out: List[Finding] = []
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
    except (OSError, AttributeError):          # no load average on Windows
        out.append(Finding("INFO", "Load probe unavailable", ""))
    return out


# ------------------------------------------------------------------ run ----

def ftdc_findings(path: str, ts_from=None, ts_to=None, tz=None) -> List[Finding]:
    """Turn decoded FTDC metrics into triage findings.

    FTDC is MongoDB's own flight recorder: it already holds CPU, memory,
    cache and connection history for every node, with no monitoring stack
    installed. This reads it offline. Times are shown in `tz` (the log's
    UTC offset) so they line up with the log findings; FTDC itself is UTC.
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

    def hm(t, secs=False, label=True):
        if t is None:
            return "?"
        if tz is not None and t.tzinfo is not None:
            return t.astimezone(tz).strftime("%H:%M:%S" if secs else "%H:%M")
        return t.strftime("%H:%M:%S" if secs else "%H:%M") + (" UTC" if label else "")

    def dur(seconds):
        seconds = int(round(seconds))
        return ("%ds" % seconds if seconds < 120 else
                "%dm" % round(seconds / 60.0) if seconds < 7200 else
                "%.1fh" % (seconds / 3600.0))

    step = reader.sample_secs or 1
    span = ""
    if reader.first_ts and reader.last_ts:
        span = " (%s -> %s)" % (hm(reader.first_ts, label=False), hm(reader.last_ts))
    out.append(Finding(
        "INFO", "FTDC metrics",
        "Decoded %d chunk(s), %d sample(s) from %d file(s)%s." % (
            reader.chunks, reader.samples, len(files), span)))

    pct = reader.cache_pct()
    if pct is not None:
        hot = reader.cache_hot_samples * step
        dirty = reader.dirty_pct_peak
        detail = "Peak %.0f%% full%s." % (
            pct, ", %.0f%% dirty" % dirty if dirty is not None else "")
        if hot:
            detail += (" Past WiredTiger's default eviction triggers (95%% "
                       "full or 20%% dirty, where application threads start "
                       "helping to evict) for %s in total, between %s and %s." % (dur(hot), hm(reader.cache_hot_first, True, False),
                                                hm(reader.cache_hot_last, True)))
        sev = "WARN" if hot >= 60 else "INFO" if hot or pct >= 80 else "OK"
        out.append(Finding(
            sev, "WiredTiger cache", detail,
            next_step="" if sev != "WARN" else
            "Read the eviction finding below: if application threads spent "
            "real time evicting, operations were slowed by it. A larger "
            "cache, a smaller working set or fewer bulk writes at once help."))

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
    if cp and cp.vmax:
        worst = cp.vmax / 1000.0
        sev = "WARN" if worst >= 60 else "INFO" if worst >= 10 else "OK"
        out.append(Finding(
            sev, "Checkpoints (FTDC)",
            "Longest checkpoint in window %.1fs%s." % (
                worst, " (they normally run every 60s, so one this long "
                "starts the next late)" if sev == "WARN" else ""),
            next_step="" if sev != "WARN" else
            "Sustained long checkpoints usually mean disk saturation or a "
            "large dirty cache; correlate with disk metrics."))

    # Time application threads spent evicting (all versions); the page
    # count it replaced is gone from 8.0's statistics. Judged on the
    # busiest minute: a window average hides a short incident.
    ev = reader.series.get("evict.appThreadMicros")
    pages = reader.series.get("evict.appThreadPages")
    avg = reader.rate("evict.appThreadMicros")
    if ev is not None and (ev.peak_rate is not None or avg is not None):
        # under a minute of data has no busiest minute: use the average
        peak_ms = (ev.peak_rate if ev.peak_rate is not None else avg) / 1000.0
        if peak_ms < 1:
            out.append(Finding("OK", "Cache eviction (FTDC)",
                               "Application threads did (almost) no eviction "
                               "work; background eviction kept up."))
        else:
            sev = "WARN" if peak_ms >= 100 else "INFO"
            out.append(Finding(
                sev, "Cache eviction pressure (FTDC)",
                "Application threads had to evict pages themselves: %.0f ms "
                "per second in the busiest minute (from %s), %.1f ms/s on "
                "average. Time an operation spends evicting is time it is not "
                "doing its own work." % (peak_ms, hm(ev.peak_rate_ts),
                                         (avg or 0) / 1000.0)
                if ev.peak_rate is not None else
                "Application threads had to evict pages themselves: %.0f ms "
                "per second (less than a minute of metrics). Time an operation "
                "spends evicting is time it is not doing its own work." % peak_ms,
                next_step="" if sev == "INFO" else
                "Compare with the WiredTiger cache finding above; a larger "
                "cache, a smaller working set or spreading out bulk writes "
                "relieves it."))
    elif pages is not None and pages.peak_rate is not None and pages.peak_rate >= 1:
        sev = "WARN" if pages.peak_rate >= 100 else "INFO"
        out.append(Finding(
            sev, "Cache eviction pressure (FTDC)",
            "Application threads evicted up to %.0f pages/s (busiest minute, "
            "from %s). When user operations have to evict, the cache is not "
            "keeping up." % (pages.peak_rate, hm(pages.peak_rate_ts)),
            next_step="" if sev == "INFO" else
            "Compare cache used vs configured above; consider a larger "
            "WiredTiger cache or reducing the working set."))

    lagged = reader.series.get("flowControl.isLagged")
    fc_wait = reader.series.get("flowControl.waitMicros")
    if lagged and lagged.vmax:
        waited = fc_wait.increase / 1e6 if fc_wait and fc_wait.increase else None
        out.append(Finding(
            "WARN", "Flow control engaged (FTDC)",
            "The majority-commit point fell behind, so the primary throttled "
            "writes: lagged for %s between %s and %s%s." % (
                dur(lagged.nonzero * step), hm(lagged.first_nonzero_ts, True, False),
                hm(lagged.last_nonzero_ts, True),
                "; writers spent %s in total waiting for flow-control tickets"
                % dur(waited) if waited else ""),
            next_step="Find the secondary that fell behind: replication lag "
                      "and secondary health (disk, CPU, a long-running "
                      "operation, fsyncLock) at that time."))

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


def local_hostname() -> Optional[str]:
    """This machine's name, read from the kernel, never looked up over the network."""
    try:
        return os.uname()[1].split(".")[0]
    except AttributeError:
        pass
    for path in ("/etc/hostname",):
        val = _read(path, 256)
        if val:
            return val.strip().split(".")[0]
    return None


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


def find_router_ftdc(logpath: str) -> Optional[str]:
    """mongos keeps FTDC next to its log, named after it: mongos.log ->
    mongos.diagnostic.data. A rotated or compressed log (mongos.log.1.gz)
    belongs to the same directory."""
    folder, name = os.path.split(logpath)
    if name.endswith(".gz"):
        name = name[:-3]
    stems = []
    if ".log" in name:
        stems.append(name[:name.index(".log")])        # mongos.log.2026-10-01
    stems.append(os.path.splitext(name)[0])            # last extension only
    stems.append(name.split(".", 1)[0])                # every extension
    for stem in stems:
        if not stem:
            continue
        candidate = os.path.join(folder, stem + ".diagnostic.data")
        if os.path.isdir(candidate):
            try:
                if any(n.startswith("metrics.") for n in os.listdir(candidate)):
                    return candidate
            except OSError:
                return None
    return None


def _oom_attribution(engine: "TriageEngine", kills, detail: str):
    """Say whether an OOM kill hit the mongod that wrote this log.

    On a host running several mongods a kill of any of them used to be
    reported as "this explains the restart above". The kernel line names
    the pid; the log's startup lines say which pid this mongod had when.
    """
    ours, others, unknown = [], [], []
    for e in kills:
        killed = int(num(e.detail.get("pid"), 0))
        proc = (e.detail.get("process") or "").lower()
        if not killed:
            unknown.append(e)
        elif killed in engine.pids:
            ours.append(killed)
        else:
            running = engine.pid_at(e.ts)
            if running is not None and running != killed:
                others.append((killed, proc))
            else:
                unknown.append(e)
    if ours:
        return (detail + " pid %s was this log's mongod." % ", ".join(
                    str(p) for p in sorted(set(ours))),
                "This explains the unexplained restart in the mongod log above.")
    if others and not unknown:
        mongo = [p for p, proc in others if "mongo" in proc]
        what = ("another mongod on this host" if mongo
                else "another process on this host")
        return (detail + " Killed pid(s) %s: %s, not the mongod that wrote "
                "this log (pid %s at the time)." % (
                    ", ".join(str(p) for p, _ in others), what,
                    ", ".join(str(engine.pid_at(e.ts)) for e in kills[:1])),
                "The host is short of memory. Triage the killed instance's own "
                "log; on a host running several mongods, check that their "
                "WiredTiger cache sizes add up to well under the RAM.")
    return (detail,
            "If the killed pid was this log's mongod, this explains an "
            "unexplained restart above. The log does not show which pid it "
            "had at that moment, so mdbkit cannot confirm it.")


def run_triage(logfile: str, window_min: Optional[int] = None,
               dbpath: Optional[str] = None, no_sysprobe: bool = False,
               ftdc_path: Optional[str] = None, oslog=None,
               ftdc_cap_minutes: int = 240):
    """Analyze the last `window_min` minutes of log time (default 60).

    window_min=0 analyzes the whole file.
    """
    if window_min is None:
        window_min = DEFAULT_WINDOW_MIN

    paths = logfile if isinstance(logfile, list) else [logfile]
    cutoff = None
    if window_min and paths != ["-"]:
        pre = ParseStats()
        early: List[LogEntry] = []
        from .sharding import ID_SHARD_REGISTRY
        for e in iter_entries_multi(paths, pre):
            if e.msg_id in (ID_STARTUP, ID_PROCESS_DETAILS, ID_OPTIONS) or \
                    e.msg_id in ID_SHARD_REGISTRY:
                early.append(e)
        if pre.last_ts:
            cutoff = pre.last_ts - timedelta(minutes=window_min)

    stats = ParseStats()
    engine = TriageEngine()
    if cutoff is not None and pre.first_ts is not None:
        engine.at_file_start = pre.first_ts >= cutoff
    if window_min and paths != ["-"]:
        # The process that wrote the log may have started before the
        # window; its pid and dbPath still identify it on this host.
        for e in early:
            # What the process is (mongos, shard, config server) and which
            # shards exist are logged at startup, usually before the window.
            if e.msg_id == ID_OPTIONS or e.msg_id in ID_SHARD_REGISTRY:
                engine.sharding.consume(e)
            if e.msg_id in ID_SHARD_REGISTRY:
                continue
            if e.msg_id == ID_OPTIONS:
                engine.dbpath = engine.dbpath or dbpath_of_options(e)
                continue
            engine.note_pid(e)
            engine.dbpath = text(e.attr.get("dbPath")) or engine.dbpath
    for path in expand_paths(paths):
        engine.begin_file()
        for entry in iter_entries(path, stats):
            if cutoff and entry.ts and entry.ts < cutoff:
                engine.note_skipped(entry)
                continue
            engine.consume(entry)

    findings = engine.findings()
    resolved_dbpath = None
    router = engine.sharding.role == "mongos"
    if not no_sysprobe:
        if not router:
            resolved_dbpath = dbpath or discover_dbpath(engine.dbpath)[0]
        findings += sysprobe(engine.dbpath, explicit=dbpath,
                             log_pids=engine.pids, router=router)
    if not ftdc_path:
        # Only auto-discover local metrics when the log actually belongs to
        # this machine. Correlating one host's log with another host's
        # metrics produces confident, wrong answers.
        here = local_hostname()
        log_host = engine.log_host
        foreign = bool(here and log_host and here.lower() != log_host.lower())
        if foreign:
            findings.append(Finding(
                "INFO", "Metrics not collected",
                "This log is from '%s' but you are running on '%s', so local "
                "diagnostic.data was not used — it would describe a different "
                "server." % (log_host, here),
                next_step="If you copied the metrics too, pass "
                          "--ftdc /path/to/diagnostic.data"))
        else:
            auto, where = None, "next to the dbPath"
            if router:
                # mongos has no dbPath; it writes FTDC next to its log,
                # named after it: mongos.log -> mongos.diagnostic.data.
                # Looking anywhere else would find a mongod's metrics.
                files = [p for p in expand_paths(paths) if p != "-"]
                if files:
                    auto = find_router_ftdc(files[-1])
                    where = "next to the log"
            else:
                auto = find_diagnostic_data(resolved_dbpath or dbpath
                                            or engine.dbpath)
            if auto:
                ftdc_path = auto
                findings.append(Finding(
                    "INFO", "FTDC discovered",
                    "Using %s (found %s). Pass --ftdc to "
                    "override." % (auto, where)))
    if ftdc_path:
        try:
            # Bound the decode even when the log's window does not overlap
            # the metrics, so an unrelated diagnostic.data can never turn
            # into a multi-minute full-history decode.
            floor = None
            if ftdc_cap_minutes:
                from .ftdc import ftdc_files, iter_documents, chunk_timestamp
                newest = None
                for f in ftdc_files(ftdc_path):
                    for doc in iter_documents(f):
                        if doc.get("type") == 1:
                            t = chunk_timestamp(doc)
                            if t and (newest is None or t > newest):
                                newest = t
                if newest:
                    floor = newest - timedelta(minutes=ftdc_cap_minutes)
            effective = cutoff
            if floor and (effective is None or effective < floor):
                effective = floor
            # and not past the end of the log, so metrics and log describe
            # the same time
            findings += ftdc_findings(
                ftdc_path, ts_from=effective,
                ts_to=stats.last_ts + timedelta(minutes=1) if stats.last_ts else None,
                tz=stats.last_ts.tzinfo if stats.last_ts else None)
        except Exception as exc:  # never let metrics break log triage
            findings.append(Finding("INFO", "FTDC unavailable", str(exc)[:200]))
    # OS-level events. The mongod log cannot record its own OOM kill: the
    # process is gone before it can write anything. That answer lives here.
    from . import oslog as OS
    os_paths = list(oslog) if oslog else []
    if not os_paths and not no_sysprobe:
        os_paths = OS.discover()
        if os_paths:
            findings.append(Finding(
                "INFO", "System log", "Also scanned %s."
                % ", ".join(os_paths)))
    if os_paths:
        try:
            events = OS.scan(os_paths, ts_from=cutoff)
            for g in OS.summarize(events):
                when = (" (last at %s)" % g["last"].strftime("%H:%M:%S")
                        if g["last"] else "")
                detail = "%s %d occurrence(s)%s." % (
                    g["explanation"] or g["kind"], g["count"], when)
                if g["processes"]:
                    detail += " Processes: %s." % ", ".join(g["processes"])
                next_step = ""
                if g["kind"] == "oom-kill":
                    detail, next_step = _oom_attribution(
                        engine, [e for e in events if e.kind == "oom-kill"],
                        detail)
                findings.append(Finding(
                    g["severity"], "System: %s" % g["kind"], detail,
                    g["examples"][:2], next_step))
        except OSError:
            pass
    elif not no_sysprobe and OS.uses_journald():
        findings.append(Finding(
            "INFO", "System log not read",
            "This host uses journald, and mdbkit does not run commands for "
            "you.", [], OS.JOURNAL_HINT.replace("\n  ", " ")))

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
    parts.append("mdbkit is read-only: it never runs commands against your "
                 "cluster. Review every next step before acting.")
    return "\n".join(parts)
