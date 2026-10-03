"""Sharded clusters: what mongos, shard and config server logs say.

A sharded cluster spreads one incident across several logs. The router
(mongos) sees every query but no plans; the shards see plans but not how a
query was routed; the config server runs the balancer. This module reads
whichever of them it is given and reports what that component can tell:

* mongos: queries sent to every shard (scatter-gather) instead of one,
  time spent waiting on the shards, routing-table refreshes, and shards or
  members it could not reach;
* shards: chunk migrations (moved, failed, and why), and migrations held up
  waiting for the range deleter;
* config server: balancer errors.

Every message id here was checked against real 6.0.29, 7.0.43, 8.0.32 and
9.0.2 clusters (see tests/fixtures/real/sharded).
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .parser import LogEntry, num, text

ID_OPTIONS = 21951
ID_SHARD_REGISTRY = (471691, 471693)     # "Updating the shard registry ..."
ID_MIGRATION_START = 22016               # donor: "Starting chunk migration donation"
ID_MIGRATION_FINISHED = 7627801          # donor: "Migration finished" (7.1+)
ID_MOVECHUNK_ERROR = 23777               # donor: "Error while doing moveChunk"
ID_RECIPIENT_ERROR = 21998               # recipient: "Error during migration"
ID_WAIT_ORPHANS = 21919                  # "Waiting for deletion of orphans"
ID_CHANGELOG = 22080                     # "About to log metadata event"
ID_BALANCER_ERROR = 21865                # config: "Error while doing balance"
ID_HOST_FAILED = 4712102                 # "Host failed in replica set"
ID_NO_HOST = 4333208                     # "RSM host selection timeout"

# Data operations whose failure an application saw.
DATA_OPS = frozenset({"find", "aggregate", "getMore", "count", "distinct",
                      "insert", "update", "delete", "findAndModify"})
# Errors that mean the database could not serve the operation, as opposed
# to a client mistake (DuplicateKey, a bad query) or a client hanging up.
SERIOUS_ERRORS = frozenset({
    "FailedToSatisfyReadPreference", "HostUnreachable", "NetworkTimeout",
    "MaxTimeMSExpired", "ExceededTimeLimit", "NotWritablePrimary",
    "NotPrimaryNoSecondaryOk", "NotPrimaryOrSecondary", "PrimarySteppedDown",
    "InterruptedDueToReplStateChange", "ShutdownInProgress",
    "InterruptedAtShutdown", "WriteConcernFailed", "WriteConcernTimeout",
    "ExceededMemoryLimit", "QueryExceededMemoryLimitNoDiskUseAllowed",
    "StaleConfig", "ShardNotFound", "LockTimeout", "TransactionExceededLifetimeLimitSeconds",
    "TemporarilyUnavailable", "ConnectionPoolExpired", "SocketException",
})


def role_of(entry: LogEntry) -> Tuple[Optional[str], Optional[str]]:
    """(role, replica set) from an "Options set by command line" entry.
    role is "mongos", "shard", "config", "replica" or "standalone"."""
    opts = entry.attr.get("options")
    if not isinstance(opts, dict):
        return None, None
    sharding = opts.get("sharding") if isinstance(opts.get("sharding"), dict) else {}
    repl = opts.get("replication") if isinstance(opts.get("replication"), dict) else {}
    rs = text(repl.get("replSet") or repl.get("replSetName")) or None
    if sharding.get("configDB"):
        return "mongos", None
    role = text(sharding.get("clusterRole"))
    if role == "shardsvr":
        return "shard", rs
    if role == "configsvr":
        return "config", rs
    return ("replica" if rs else "standalone"), rs


def config_set_of(entry: LogEntry) -> Optional[str]:
    opts = entry.attr.get("options")
    sharding = opts.get("sharding") if isinstance(opts, dict) else None
    db = text(sharding.get("configDB")) if isinstance(sharding, dict) else ""
    return db.split("/", 1)[0] if "/" in db else None


ROLE_LABEL = {"mongos": "mongos (router)", "shard": "shard server",
              "config": "config server", "replica": "replica set member",
              "standalone": "standalone"}


@dataclass
class Migration:
    ts: object
    ns: str = ""
    to_shard: str = ""
    from_shard: str = ""


@dataclass
class ShardingState:
    role: Optional[str] = None
    repl_set: Optional[str] = None
    config_set: Optional[str] = None
    shard_sets: set = field(default_factory=set)
    # donor side
    started: List[Migration] = field(default_factory=list)
    finished: List[dict] = field(default_factory=list)
    failed: List[Tuple[object, str]] = field(default_factory=list)
    recipient_failed: List[Tuple[object, str]] = field(default_factory=list)
    changelog: Counter = field(default_factory=Counter)
    changelog_errors: List[Tuple[object, str]] = field(default_factory=list)
    orphan_waits: List[Tuple[object, str]] = field(default_factory=list)
    balancer_errors: Counter = field(default_factory=Counter)
    balancer_error_last: object = None
    # reachability, from the replica set monitor
    host_failures: Dict[Tuple[str, str], List] = field(default_factory=lambda: defaultdict(list))
    no_primary: Dict[str, List] = field(default_factory=lambda: defaultdict(list))
    no_host_modes: Dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    # failed data operations on user collections
    failed_ops: Counter = field(default_factory=Counter)
    failed_examples: Dict[str, str] = field(default_factory=dict)

    def consume(self, entry: LogEntry) -> None:
        mid = entry.msg_id
        a = entry.attr
        if mid == ID_OPTIONS:
            role, rs = role_of(entry)
            if role:
                self.role, self.repl_set = role, rs
            self.config_set = config_set_of(entry) or self.config_set
        elif mid in ID_SHARD_REGISTRY:
            cs = text(a.get("connectionString"))
            if "/" in cs:
                self.shard_sets.add(cs.split("/", 1)[0])
        elif mid == ID_MIGRATION_START:
            req = a.get("requestParameters") if isinstance(a.get("requestParameters"), dict) else {}
            ns = text(req.get("_shardsvrMoveRange") or req.get("moveChunk")
                      or req.get("ns"))
            self.started.append(Migration(entry.ts, ns, text(req.get("toShard")),
                                          text(req.get("fromShard"))))
        elif mid == ID_MIGRATION_FINISHED:
            self.finished.append({
                "ts": entry.ts,
                "ms": int(max(0, num(a.get("totalTimeMillis")))),
                "docs": int(max(0, num(a.get("docsCloned")))),
                "bytes": int(max(0, num(a.get("bytesCloned"))))})
        elif mid == ID_MOVECHUNK_ERROR:
            self.failed.append((entry.ts, _error_text(a.get("error"))))
        elif mid == ID_RECIPIENT_ERROR:
            self.recipient_failed.append((entry.ts, _error_text(a.get("error"))))
        elif mid == ID_WAIT_ORPHANS:
            self.orphan_waits.append((entry.ts, text(a.get("namespace"))))
        elif mid == ID_CHANGELOG:
            ev = a.get("event") if isinstance(a.get("event"), dict) else {}
            what = text(ev.get("what"))
            if what.startswith("moveChunk.") or what.startswith("moveRange."):
                step = what.split(".", 1)[1]
                self.changelog[step] += 1
                if step == "error":
                    det = ev.get("details") if isinstance(ev.get("details"), dict) else {}
                    self.changelog_errors.append((entry.ts, text(det.get("errmsg"))))
        elif mid == ID_BALANCER_ERROR:
            err = a.get("error")
            name = text(err.get("codeName")) if isinstance(err, dict) else _error_text(err)
            self.balancer_errors[name or "unknown"] += 1
            self.balancer_error_last = entry.ts
        elif mid == ID_HOST_FAILED:
            err = a.get("error")
            code = text(err.get("codeName")) if isinstance(err, dict) else ""
            key = (text(a.get("replicaSet")), text(a.get("host")))
            self.host_failures[key].append((entry.ts, code))
        elif mid == ID_NO_HOST:
            # "Could not find host matching read preference { mode: "primary" }"
            err = a.get("error")
            msg = text(err.get("errmsg")) if isinstance(err, dict) else text(err)
            m = re.search(r'mode:\s*"?(\w+)', msg)
            self.no_primary[text(a.get("replicaSet"))].append(entry.ts)
            self.no_host_modes[text(a.get("replicaSet"))][m.group(1) if m else "?"] += 1
        elif entry.is_slow_query and a.get("errName"):
            self._failed_op(entry)

    def _failed_op(self, entry: LogEntry) -> None:
        a = entry.attr
        ns = text(a.get("ns"))
        db = ns.split(".", 1)[0]
        if not ns or ns.endswith(".$cmd") or db in ("admin", "config", "local"):
            return
        cmd = a.get("command") if isinstance(a.get("command"), dict) else {}
        op = next(iter(cmd), "") if cmd else text(a.get("type"))
        if op not in DATA_OPS and text(a.get("type")) not in ("update", "remove"):
            return
        name = text(a.get("errName"))
        self.failed_ops[name] += 1
        self.failed_examples.setdefault(name, "%s on %s: %s" % (
            op or "op", ns, text(a.get("errMsg"))[:160]))

    # ------------------------------------------------------------------
    @property
    def total_shards(self) -> int:
        sets = set(self.shard_sets)
        if self.config_set:
            sets.discard(self.config_set)
        return len(sets)

    def migration_summary(self) -> dict:
        moved = len(self.finished) or self.changelog.get("commit", 0)
        failed = len(self.failed) or self.changelog.get("error", 0)
        return {"started": len(self.started) or self.changelog.get("start", 0),
                "moved": moved, "failed": failed,
                "receiveFailed": len(self.recipient_failed),
                "totalMs": sum(f["ms"] for f in self.finished),
                "docs": sum(f["docs"] for f in self.finished),
                "bytes": sum(f["bytes"] for f in self.finished)}


def _error_text(err) -> str:
    if isinstance(err, dict):
        return text(err.get("codeName") or err.get("errmsg"))
    return text(err)


def _human_bytes(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%d B" % n


def _hm(ts) -> str:
    return ts.strftime("%H:%M:%S") if ts is not None else "?"


def _short_reason(reason: str) -> str:
    low = reason.lower()
    if "orphans cleanup" in low or "range deleter" in low or "orphan" in low:
        return "range from an earlier migration not yet cleaned up (range deleter)"
    if "jumbo" in low or "chunk too big" in low or "exceeds" in low and "size" in low:
        return "chunk too big to move (jumbo)"
    if "readpreference" in low or "unreachable" in low or "connect" in low:
        return "a shard could not be reached"
    if "conflicting" in low or "already in progress" in low:
        return "another migration or DDL operation was running"
    return reason[:120]


def findings(st: ShardingState, routed_shapes=None) -> list:
    """Sharding findings for one log. `routed_shapes` are the query shapes
    from a mongos log (ShapeStats with nShards)."""
    from .triage import Finding
    out = []

    # --- mongos: scatter-gather --------------------------------------
    shapes = [s for s in (routed_shapes or []) if s.routed]
    if shapes:
        total = max([st.total_shards] + [s.max_shards for s in shapes])
        if total >= 2:
            all_ms = 0.0
            routed_ms = sum(s.total_ms for s in shapes)
            rows = []
            for s in sorted(shapes, key=lambda s: -s.total_ms):
                runs = s.all_shard_runs(total)
                if not runs:
                    continue
                share = runs / float(s.count)
                all_ms += s.total_ms * share
                if len(rows) < 5:
                    rows.append("%s | %s | %d of %d runs to all %d shards | %s" % (
                        s.shape.ns, s.shape.operation, runs, s.count, total,
                        s.shape.pretty()))
            if rows:
                pct = 100.0 * all_ms / routed_ms if routed_ms else 0
                sev = "WARN" if pct >= 50 and all_ms >= 1000 else "INFO"
                out.append(Finding(
                    sev, "Scatter-gather queries",
                    "%.0f%% of slow-query time on this router came from queries "
                    "sent to all %d shards. That is usually a filter without the "
                    "shard key, or a range that spans every shard." % (pct, total), rows,
                    "Where the filter can include the shard key, add it so mongos "
                    "targets one shard. Plans are in the shard logs: mdbkit "
                    "queries <shard log>"))
        wait = sum(s.remote_wait_ms for s in shapes)
        dur = sum(s.total_ms for s in shapes)
        if dur >= 1000 and wait:
            out.append(Finding(
                "INFO", "Time waiting on shards",
                "%.0f%% of slow-query time on this router was spent waiting for "
                "the shards to answer (remoteOpWaitMillis)." % (100.0 * wait / dur),
                next_step="Slow shards show up in their own logs: mdbkit triage <shard log>"))
        routing = sum(s.routing_ms for s in shapes)
        worst = max((s for s in shapes if s.routing_ms), key=lambda s: s.routing_ms,
                    default=None)
        if routing >= 1000:
            out.append(Finding(
                "WARN", "Routing table refreshes",
                "Slow queries spent %s refreshing this router's routing table "
                "from the config servers (catalog cache lookups)." % _ms(routing),
                ["most on %s (%s)" % (worst.shape.ns, _ms(worst.routing_ms))] if worst else [],
                "Frequent refreshes follow chunk migrations; check migration "
                "volume on the shards and config server load."))

    # --- reachability ----------------------------------------------------
    if st.no_primary:
        rows = []
        for rs, ts in sorted(st.no_primary.items()):
            modes = st.no_host_modes.get(rs) or Counter()
            what = ", ".join(("no primary %dx" if mode == "primary" else
                              "no member for read preference " + mode + " %dx") % n
                             for mode, n in modes.most_common()) or \
                "no member found %dx" % len(ts)
            rows.append("%s: %s (%s to %s)" % (
                rs, what.replace("read preference ? ", "the read preference "),
                _hm(min((t for t in ts if t), default=None)),
                _hm(max((t for t in ts if t), default=None))))
        out.append(Finding(
            "CRIT", "Replica set unreachable",
            "Requests to %s could not find a member to serve them, so "
            "operations that needed one failed or waited."
            % ", ".join(sorted(st.no_primary)), rows,
            "Check those members are up and reachable: mdbkit triage <their logs>"))
    if st.host_failures:
        rows = []
        for (rs, host), events in sorted(st.host_failures.items(),
                                         key=lambda kv: -len(kv[1]))[:6]:
            codes = Counter(c for _, c in events if c)
            stamps = [t for t, _ in events if t is not None]
            rows.append("%s %s: %d failure(s) %s to %s%s" % (
                rs, host, len(events), _hm(min(stamps, default=None)),
                _hm(max(stamps, default=None)),
                " (%s)" % ", ".join(c for c, _ in codes.most_common(2)) if codes else ""))
        out.append(Finding(
            "WARN" if not st.no_primary else "CRIT",
            "Members unreachable",
            "The replica set monitor could not reach %d member(s)." % len(st.host_failures),
            rows, "Were they restarted, overloaded or partitioned at those times?"))

    # --- failed operations ---------------------------------------------
    if st.failed_ops:
        serious = {k: v for k, v in st.failed_ops.items() if k in SERIOUS_ERRORS}
        rows = ["%dx %s — %s" % (n, name, st.failed_examples.get(name, ""))
                for name, n in st.failed_ops.most_common(5)]
        out.append(Finding(
            "WARN" if serious else "INFO", "Failed operations",
            "%d failed operation(s) on your collections were logged%s. "
            "Only failures slow enough to be logged appear here." % (
                sum(st.failed_ops.values()),
                "; %d with errors that mean the database could not serve them "
                "(a member unreachable or failing over, a time or memory limit)"
                % sum(serious.values()) if serious else ""),
            rows, "Read them: mdbkit filter <log> --failed"))

    # --- migrations (shard logs) ---------------------------------------
    m = st.migration_summary()
    if m["started"] or m["moved"] or m["failed"] or st.recipient_failed:
        # Reasons: the donor's own error line where it logs one (8.0), else
        # the changelog's moveChunk.error, which every version writes.
        failures = (st.failed or st.changelog_errors) + st.recipient_failed
        reasons = Counter(_short_reason(r) for _, r in failures)
        detail = "%d chunk migration(s) started from this shard, %d moved, %d failed." % (
            m["started"], m["moved"], m["failed"])
        if st.recipient_failed:
            detail += " %d incoming migration(s) failed here." % len(st.recipient_failed)
        rows = []
        if m["moved"] and st.finished:
            rows.append("moved %s docs, %s, %s in total (slowest %s)" % (
                format(m["docs"], ","), _human_bytes(m["bytes"]), _ms(m["totalMs"]),
                _ms(max(f["ms"] for f in st.finished))))
        for reason, n in reasons.most_common(4):
            rows.append("%dx failed: %s" % (n, reason))
        sev = "WARN" if failures else "INFO"
        out.append(Finding(
            sev, "Chunk migrations", detail, rows,
            "Migrations compete with the workload for I/O and take a short "
            "critical section on the collection. If they cluster around a "
            "slowdown, consider a balancer window (sh.setBalancerState / "
            "balancer activeWindow)." if not failures else
            "Read the full reasons: mdbkit filter <log> --component MIGRATE --severity W"))
    if st.orphan_waits:
        ns = sorted({n for _, n in st.orphan_waits if n})
        out.append(Finding(
            "INFO", "Migrations waiting for the range deleter",
            "%d migration(s) waited for orphaned documents from an earlier "
            "migration to be deleted (%s)." % (len(st.orphan_waits), ", ".join(ns[:3])),
            next_step="The range deleter waits orphanCleanupDelaySecs (15 min by "
                      "default) before deleting; moving the same range back "
                      "soon after it left fails until then."))

    # --- config server: balancer ---------------------------------------
    if st.balancer_errors:
        out.append(Finding(
            "WARN", "Balancer errors",
            "The balancer failed %d time(s), last at %s." % (
                sum(st.balancer_errors.values()), _hm(st.balancer_error_last)),
            ["%dx %s" % (n, name) for name, n in st.balancer_errors.most_common(4)],
            "The balancer retries on its own; persistent errors usually mean a "
            "shard is unreachable (see above) or a chunk cannot move."))
    return out


def _ms(v: float) -> str:
    if v >= 60000:
        return "%.1fm" % (v / 60000.0)
    if v >= 1000:
        return "%.1fs" % (v / 1000.0)
    return "%dms" % v
