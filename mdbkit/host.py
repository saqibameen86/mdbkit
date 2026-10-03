"""`mdbkit host` — one host running many mongod instances.

Hosts that run dozens of mongods side by side (one member of each of many
replica sets) fail in ways no single log shows: every instance left on the
default WiredTiger cache size, which assumes the machine is its alone; the
OOM killer picking off whichever instance is largest; a kernel setting that
every instance warns about at startup. This reads every instance's log,
gives one line per instance, and checks the host as a whole.

Offline and read-only like every other analysis command. On the host
itself it also reads /proc (RAM, running mongods); nothing leaves the
machine.
"""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Dict, List, Optional, Tuple

from .audit import audit_entries
from .parser import (ID_BUILD_INFO, ID_STARTUP, LogEntry, ParseStats,
                     iter_entries, iter_entries_multi, num, text)
from .sharding import ID_SHARD_REGISTRY
from .triage import (ID_OPTIONS, ID_PROCESS_DETAILS, SEV_ORDER, Finding,
                     TriageEngine, dbpath_from_argv, dbpath_of_options,
                     find_mongods, local_hostname, port_from_argv)

ID_OPEN_WT = 22315          # "Opening WiredTiger", config has cache_size=
HEADER_LINES = 400          # identity lines sit at the top of every file
ROTATED = re.compile(r"(\.gz)?$")
ROTATION_SUFFIX = re.compile(r"\.(\d+|\d{4}-\d\d-\d\dT[\d-]+)$")
LOG_NAME = re.compile(r"\.log(\.|$)", re.I)

# MongoDB's default WiredTiger cache: 50% of (RAM - 1 GB), at least 256 MB.
def default_cache_mb(ram_mb: float) -> float:
    return max(256.0, 0.5 * (ram_mb - 1024.0))


@dataclass
class Instance:
    key: str
    files: List[str] = field(default_factory=list)
    host: str = ""
    port: Optional[int] = None
    repl_set: str = ""
    version: str = ""
    dbpath: str = ""
    cache_mb: Optional[float] = None       # from "Opening WiredTiger"
    cache_option_gb: Optional[float] = None
    cache_source: str = ""
    engine: Optional[TriageEngine] = None
    findings: List[Finding] = field(default_factory=list)
    stats: Optional[ParseStats] = None
    running_pid: Optional[int] = None

    # -- derived -------------------------------------------------------
    @property
    def name(self) -> str:
        return self.key

    @property
    def kind(self) -> str:
        """mongos, shard, config, replica or standalone."""
        if self.engine and self.engine.sharding.role:
            return self.engine.sharding.role
        return "replica" if self.repl_set else "standalone"

    @property
    def role(self) -> str:
        if self.kind == "mongos":
            return "mongos"
        if self.engine and self.engine.self_state:
            return self.engine.self_state
        return "replica set" if self.repl_set else "standalone"

    @property
    def set_label(self) -> str:
        if self.kind in ("shard", "config") and self.repl_set:
            return "%s (%s)" % (self.repl_set, self.kind)
        return self.repl_set or "-"

    @property
    def crashes(self) -> int:
        e = self.engine
        if not e:
            return 0
        starts = list(zip(e.start_clean, e.start_reported))
        first_is_log_start = e.at_file_start and e.history_before_start == 0
        n = 0
        for i, (clean, reported) in enumerate(starts):
            if i == 0 and first_is_log_start and not reported:
                continue        # where the log begins; nothing known before
            if not clean:
                n += 1
        return n

    @property
    def worst(self) -> str:
        sevs = [f.severity for f in self.own_findings()]
        return min(sevs, key=lambda s: SEV_ORDER.get(s, 9)) if sevs else "OK"

    def own_findings(self) -> List[Finding]:
        """Findings about this instance (host-wide ones are shown once)."""
        skip = {"Startup configuration", "Log begins at a startup",
                "Replica set created"}
        return [f for f in self.findings
                if f.severity in ("CRIT", "WARN") and f.title not in skip]

    def slow(self) -> Tuple[int, float, Optional[float]]:
        if not self.engine:
            return 0, 0.0, None
        shapes = self.engine.qagg.results()
        count = sum(s.count for s in shapes)
        total = sum(s.total_ms for s in shapes)
        timed = sum(s.timed_total_ms for s in shapes)
        waiting = sum(s.waiting_ms for s in shapes)
        return count, total, (100.0 * waiting / timed) if timed else None

    def to_dict(self) -> dict:
        count, total, waiting = self.slow()
        return {
            "instance": self.key, "files": self.files, "host": self.host,
            "port": self.port, "replicaSet": self.repl_set or None,
            "kind": self.kind,
            "version": self.version or None, "role": self.role,
            "dbPath": self.dbpath or None,
            "cacheMB": self.cache_mb, "cacheSource": self.cache_source or None,
            "starts": len(self.engine.startups) if self.engine else 0,
            "crashes": self.crashes,
            "pids": self.engine.pids if self.engine else [],
            "runningPid": self.running_pid,
            "slowOps": count, "slowMs": total,
            "waitingPct": round(waiting, 1) if waiting is not None else None,
            "worst": self.worst,
            "findings": [f.to_dict() for f in self.own_findings()],
        }


@dataclass
class HostReport:
    instances: List[Instance]
    findings: List[Finding]
    ram_mb: Optional[float]
    ram_source: str
    hosts: List[str]
    window_min: int

    def to_dict(self) -> dict:
        return {"hosts": self.hosts, "ramMB": self.ram_mb,
                "ramSource": self.ram_source or None,
                "windowMinutes": self.window_min,
                "findings": [f.to_dict() for f in self.findings],
                "instances": [i.to_dict() for i in self.instances]}


# ------------------------------------------------------------- inputs ---

def expand_inputs(args: List[str]) -> List[str]:
    """Files, globs and directories (searched three levels deep for logs)."""
    out: List[str] = []
    for arg in args:
        if any(c in arg for c in "*?["):
            out.extend(sorted(p for p in glob.glob(arg) if os.path.isfile(p)))
        elif os.path.isdir(arg):
            base = arg.rstrip(os.sep).count(os.sep)
            for root, dirs, files in os.walk(arg):
                if root.count(os.sep) - base >= 3:
                    dirs[:] = []
                # never walk into a data directory
                if os.path.exists(os.path.join(root, "WiredTiger")):
                    dirs[:] = []
                    continue
                dirs[:] = [d for d in dirs if d not in ("diagnostic.data", "journal")]
                for f in sorted(files):
                    if LOG_NAME.search(f):
                        out.append(os.path.join(root, f))
        else:
            out.append(arg)
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def _base_name(path: str) -> str:
    """mongod.log.2026-10-02T17-54-00.gz -> mongod.log (same directory)."""
    p = ROTATED.sub("", path)
    return ROTATION_SUFFIX.sub("", p)


def _header(path: str) -> dict:
    """Identity from the first lines of one file: who wrote it."""
    info: dict = {"first_ts": None}
    stats = ParseStats()
    for n, e in enumerate(iter_entries(path, stats)):
        if info["first_ts"] is None and e.ts is not None:
            info["first_ts"] = e.ts
        if e.msg_id in (ID_STARTUP, ID_PROCESS_DETAILS):
            host = text(e.attr.get("host"))
            port = int(num(e.attr.get("port"), 0)) or None
            if host and port:
                info.setdefault("host", host.split(":")[0])
                info.setdefault("port", port)
        elif e.msg_id == ID_OPTIONS and "options_port" not in info:
            # mongos never logs "MongoDB starting"; its options give the port
            opts = e.attr.get("options") if isinstance(e.attr.get("options"), dict) else {}
            net = opts.get("net") if isinstance(opts.get("net"), dict) else {}
            port = int(num(net.get("port"), 0))
            if port:
                info["options_port"] = port
        if n >= HEADER_LINES and ("port" in info or "options_port" in info):
            break
        if n >= HEADER_LINES * 5:
            break
    return info


def group_instances(paths: List[str]) -> List[Instance]:
    """One Instance per mongod: files are matched by the host and port the
    log records, rotated files without such a line by their base name."""
    headers = {p: _header(p) for p in paths}
    by_key: Dict[str, Instance] = {}
    base_key: Dict[str, str] = {}
    def key_of(h):
        if "port" in h:
            return "%s:%d" % (h["host"], h["port"])
        if "options_port" in h:
            return "port %d" % h["options_port"]
        return None

    for p in paths:
        key = key_of(headers[p])
        if key:
            base_key.setdefault(_base_name(p), key)
    for p in paths:
        h = headers[p]
        if key_of(h):
            key = key_of(h)
        else:
            key = base_key.get(_base_name(p)) or _base_name(p)
        inst = by_key.setdefault(key, Instance(key=key))
        inst.files.append(p)
    for inst in by_key.values():
        inst.files.sort(key=lambda f: headers[f]["first_ts"].timestamp()
                        if headers[f]["first_ts"] else float("inf"))
    return list(by_key.values())


def _last_ts(path: str):
    """Timestamp of the last line, read from the end of a plain file."""
    from .parser import parse_line
    if path.endswith(".gz"):
        return None
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 65536))
            tail = fh.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    for line in reversed(tail):
        e = parse_line(line)
        if e is not None and e.ts is not None:
            return e.ts
    return None


# -------------------------------------------------------------- analysis ---

def _cache_from_config(cfg: str) -> Optional[float]:
    m = re.search(r"cache_size=(\d+)([MG])", cfg or "")
    if not m:
        return None
    return float(m.group(1)) * (1024 if m.group(2) == "G" else 1)


def analyse_instance(inst: Instance, window_min: int) -> None:
    cutoff = None
    last = _last_ts(inst.files[-1]) if window_min else None
    if last is not None:
        cutoff = last - timedelta(minutes=window_min)
    engine = TriageEngine()
    stats = ParseStats()
    first = True
    for e in iter_entries_multi(inst.files, stats):
        # identity and configuration, whatever the window
        if e.msg_id in (ID_STARTUP, ID_PROCESS_DETAILS):
            inst.host = text(e.attr.get("host")).split(":")[0] or inst.host
            inst.port = int(num(e.attr.get("port"), 0)) or inst.port
            inst.dbpath = text(e.attr.get("dbPath")) or inst.dbpath
        elif e.msg_id == ID_BUILD_INFO:
            bi = e.attr.get("buildInfo")
            inst.version = text(bi.get("version")) if isinstance(bi, dict) else inst.version
        elif e.msg_id == ID_OPTIONS:
            opts = e.attr.get("options") if isinstance(e.attr.get("options"), dict) else {}
            repl = opts.get("replication") if isinstance(opts.get("replication"), dict) else {}
            inst.repl_set = text(repl.get("replSet") or repl.get("replSetName")) or ""
            inst.dbpath = dbpath_of_options(e) or inst.dbpath
            storage = opts.get("storage") if isinstance(opts.get("storage"), dict) else {}
            wt = storage.get("wiredTiger") if isinstance(storage.get("wiredTiger"), dict) else {}
            ec = wt.get("engineConfig") if isinstance(wt.get("engineConfig"), dict) else {}
            gb = ec.get("cacheSizeGB")
            inst.cache_option_gb = float(num(gb)) if gb is not None else None
            if gb is None:
                inst.cache_mb = None          # a restart may have dropped it
        elif e.msg_id == ID_OPEN_WT:
            inst.cache_mb = _cache_from_config(text(e.attr.get("config")))
        if cutoff is not None and e.ts is not None and e.ts < cutoff:
            if e.msg_id in (ID_STARTUP, ID_PROCESS_DETAILS):
                engine.note_pid(e)
            elif e.msg_id == ID_OPTIONS or e.msg_id in ID_SHARD_REGISTRY:
                # mongos / shard / config server, and the shard list
                engine.sharding.consume(e)
            first = False
            continue
        if first:
            engine.at_file_start = True
            first = False
        elif cutoff is not None and engine.seen == 0:
            engine.at_file_start = False
        engine.consume(e)
    inst.engine = engine
    inst.stats = stats
    inst.findings = engine.findings()
    if not inst.dbpath and engine.dbpath:
        inst.dbpath = engine.dbpath


def _ram_mb(ram_arg: Optional[str], hosts: List[str]) -> Tuple[Optional[float], str]:
    if ram_arg:
        m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([GgMmTt])?[Bb]?\s*", ram_arg)
        if not m:
            raise ValueError("--ram takes a size like 64G or 65536M")
        mult = {"t": 1024 * 1024, "g": 1024, "m": 1, None: 1024}[
            (m.group(2) or "").lower() or None]
        return float(m.group(1)) * mult, "--ram"
    here = (local_hostname() or "").lower()
    if hosts and here and any(h.lower() != here for h in hosts):
        return None, ""
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / 1024.0, "this machine"
    except (OSError, ValueError, IndexError):
        pass
    return None, ""


def _capped(rows: List[str], n: int = 8) -> List[str]:
    return rows if len(rows) <= n else rows[:n] + ["... and %d more" % (len(rows) - n)]


def _gb(mb: float) -> str:
    return "%.1f GiB" % (mb / 1024.0) if mb >= 1024 else "%d MiB" % mb


def host_findings(instances: List[Instance], ram_mb: Optional[float],
                  ram_source: str, oslog_paths: Optional[List[str]] = None,
                  on_host: bool = False) -> List[Finding]:
    out: List[Finding] = []

    # 1. WiredTiger caches against RAM
    known, defaults, unknown = 0.0, [], []
    for inst in instances:
        if inst.kind == "mongos":
            continue                      # a router has no storage engine
        if inst.cache_mb is not None:
            inst.cache_source = "logged"
        elif inst.cache_option_gb is not None:
            inst.cache_mb = inst.cache_option_gb * 1024
            inst.cache_source = "option"
        elif ram_mb is not None:
            inst.cache_mb = default_cache_mb(ram_mb)
            inst.cache_source = "default (estimated)"
        if inst.cache_mb is None:
            unknown.append(inst)
            continue
        known += inst.cache_mb
        if inst.cache_option_gb is None:
            defaults.append(inst)
    storage = [i for i in instances if i.kind != "mongos"]
    if len(storage) > 1 and defaults:
        out.append(Finding(
            "WARN", "Default cache size on a shared host",
            "%d of %d mongod instances run with the default WiredTiger cache "
            "(cacheSizeGB not set). The default is half of (RAM - 1 GB) and "
            "assumes the mongod has the machine to itself." % (
                len(defaults), len(storage)),
            _capped(["%s: %s" % (i.key, _gb(i.cache_mb))
                     for i in sorted(defaults, key=lambda i: i.key)]),
            "Set storage.wiredTiger.engineConfig.cacheSizeGB on every "
            "instance so that together they fit (see below)."))
    if known and ram_mb:
        pct = 100.0 * known / ram_mb
        sev = "CRIT" if pct > 85 else "WARN" if pct > 60 else "OK"
        detail = ("WiredTiger caches add up to %s, %.0f%% of %s RAM (%s)." % (
            _gb(known), pct, _gb(ram_mb), ram_source))
        if unknown:
            detail += " %d instance(s) not counted: their cache size is not in the log." % len(unknown)
        out.append(Finding(
            sev, "Cache sizes vs RAM", detail,
            ["MongoDB's default gives a single mongod about half the RAM; "
             "the rest is for the OS file cache and each process's "
             "connections, sorts and aggregations."],
            "" if sev == "OK" else
            "Lower cacheSizeGB so the total stays well under the RAM "
            "(around half, as the single-instance default does). Above "
            "100%, the OOM killer decides which instance dies."))
    elif known:
        out.append(Finding(
            "INFO", "Cache sizes vs RAM",
            "WiredTiger caches add up to %s; RAM is unknown (the logs are "
            "from another machine)." % _gb(known),
            next_step="Pass --ram 64G (the host's memory) to check the total."))

    # 2. crashes
    crashed = [i for i in instances if i.crashes]
    if crashed:
        out.append(Finding(
            "CRIT", "Instances that crashed",
            "%d instance(s) started after an unclean stop (crash, kill -9 or "
            "OOM kill)." % len(crashed),
            _capped(["%s: %d unclean start(s)" % (i.key, i.crashes)
                     for i in sorted(crashed, key=lambda i: i.key)]),
            "With --oslog, OOM kills are matched to instances below; then "
            "run mdbkit triage on that instance's log."))

    # 3. OOM kills, matched to instances by pid
    if oslog_paths:
        from . import oslog as OS
        try:
            events = [e for e in OS.scan(oslog_paths) if e.kind == "oom-kill"]
        except OSError as exc:
            events = []
            out.append(Finding("INFO", "System log not read", str(exc)))
        if events:
            rows = []
            for ev in events[:20]:
                pid = int(num(ev.detail.get("pid"), 0))
                owner = None
                for inst in instances:
                    if inst.engine and (pid in inst.engine.pids
                                        or inst.engine.pid_at(ev.ts) == pid):
                        owner = inst
                        break
                when = ev.ts.strftime("%m-%d %H:%M:%S") if ev.ts else "?"
                what = owner.key if owner else (
                    "not one of these instances (%s)" % (ev.detail.get("process") or "?"))
                rows.append("%s  pid %s  -> %s" % (when, pid or "?", what))
            out.append(Finding(
                "CRIT", "OOM kills", "%d OOM kill(s) in the system log." % len(events),
                rows, "The host ran out of memory; check cache sizes vs RAM above."))

    # 4. startup warnings: host settings repeat in every instance
    per_key: Dict[str, List[str]] = {}
    titles: Dict[str, Tuple[str, str]] = {}
    for inst in instances:
        if not inst.engine:
            continue
        res = audit_entries(inst.engine.audit_entries)
        for item in res.items:
            if item.key.startswith("other:"):
                continue          # instance-specific, e.g. crash recovery
            per_key.setdefault(item.key, []).append(inst.key)
            titles[item.key] = (item.severity, item.title)
    if per_key:
        rows = []
        worst = "INFO"
        for key, insts in sorted(per_key.items(),
                                 key=lambda kv: (SEV_ORDER.get(titles[kv[0]][0], 9), -len(kv[1]))):
            sev, title = titles[key]
            if SEV_ORDER.get(sev, 9) < SEV_ORDER.get(worst, 9):
                worst = sev
            rows.append("(%s) %s: %d of %d instances" % (
                sev.lower(), title, len(insts), len(instances)))
        out.append(Finding(
            worst, "Startup configuration",
            "Warnings mongod logged at startup, counted across instances. "
            "Kernel and limit settings belong to the host, so one fix "
            "covers every instance.", rows[:12],
            "Details and fixes: mdbkit audit <one instance's log>"))

    # 5. on the host: what is running, compared with the logs
    if on_host:
        running = find_mongods()
        claimed = set()
        for inst in instances:
            pids = inst.engine.pids if inst.engine else []
            match = None
            for pid, argv in running:
                port = port_from_argv(argv)
                if pid in pids or (inst.dbpath and dbpath_from_argv(argv) == inst.dbpath) \
                        or (inst.port and port and str(inst.port) == str(port)):
                    match = (pid, argv)
                    break
            if match:
                inst.running_pid = match[0]
                claimed.add(match[0])
        down = [i for i in instances if i.running_pid is None]
        extra = [(pid, argv) for pid, argv in running if pid not in claimed]
        if down:
            out.append(Finding(
                "WARN", "Instances not running",
                "%d instance(s) have a log here but no running mongod." % len(down),
                _capped(sorted(i.key for i in down)),
                "If they should be up, start them and check why they stopped "
                "(mdbkit triage on that log)."))
        if extra:
            out.append(Finding(
                "INFO", "Running instances not analysed",
                "%d mongod process(es) are running whose logs were not given." % len(extra),
                ["pid %d%s%s" % (pid,
                                 "  port %s" % port_from_argv(argv) if port_from_argv(argv) else "",
                                 "  dbPath %s" % dbpath_from_argv(argv) if dbpath_from_argv(argv) else "")
                 for pid, argv in extra[:10]],
                "Add their log directories to the command."))

    out.sort(key=lambda f: SEV_ORDER.get(f.severity, 9))
    return out


def run_host(args: List[str], window_min: int = 1440, ram: Optional[str] = None,
             oslog: Optional[List[str]] = None, progress=None) -> HostReport:
    paths = expand_inputs(args)
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        raise FileNotFoundError(missing[0])
    if not paths:
        raise FileNotFoundError(" ".join(args))
    instances = group_instances(paths)
    for n, inst in enumerate(instances, 1):
        analyse_instance(inst, window_min)
        if progress:
            progress(n, len(instances))
    hosts = sorted({i.host for i in instances if i.host})
    ram_mb, ram_source = _ram_mb(ram, hosts)
    here = (local_hostname() or "").lower()
    on_host = bool(hosts) and all(h.lower() == here for h in hosts) and not ram
    findings = host_findings(instances, ram_mb, ram_source, oslog, on_host)
    instances.sort(key=lambda i: (SEV_ORDER.get(i.worst, 9), -i.slow()[1], i.key))
    return HostReport(instances, findings, ram_mb, ram_source, hosts, window_min)


# ---------------------------------------------------------------- render ---

def render_host(rep: HostReport, limit: int = 0) -> str:
    from .render import _ms, _table
    n = len(rep.instances)
    where = ", ".join(rep.hosts) if rep.hosts else "unknown host"
    parts = ["== mdbkit host: %d instance(s) on %s ==" % (n, where)]
    window = ("last %d minutes of each log" % rep.window_min
              if rep.window_min else "whole logs")
    ram = ("%s RAM (%s)" % (_gb(rep.ram_mb), rep.ram_source)
           if rep.ram_mb else "RAM unknown (pass --ram)")
    parts.append("%s | %s" % (window, ram))
    parts.append("")
    for f in rep.findings:
        parts.append("[%s] %s: %s" % (f.severity, f.title, f.detail))
        for line in f.evidence:
            parts.append("        - %s" % line)
        if f.next_step:
            parts.append("        next: %s" % f.next_step)
    if rep.findings:
        parts.append("")
    rows = []
    shown = rep.instances[:limit] if limit else rep.instances
    for i in shown:
        count, total, waiting = i.slow()
        own = i.own_findings()
        e = i.engine
        rows.append((
            i.key, i.set_label, i.version or "?", i.role,
            ("%s%s" % (_gb(i.cache_mb), "*" if i.cache_source.startswith("default") else ""))
            if i.cache_mb else ("-" if i.kind == "mongos" else "?"),
            str(len(e.startups) if e else 0), str(i.crashes),
            format(count, ","), _ms(total) if total else "-",
            ("%.0f%%" % waiting) if waiting is not None else "-",
            i.worst, own[0].title if own else "",
        ))
    parts.append(_table(["instance", "set", "version", "role", "cache", "starts",
                         "crashes", "slow ops", "slow time", "waiting",
                         "worst", "first issue"], rows))
    if limit and n > limit:
        parts.append("... %d more (--limit 0 for all)" % (n - limit))
    parts.append("")
    parts.append("cache* = MongoDB's default, estimated from the RAM; "
                 "waiting = share of slow-op time spent waiting (8.0+).")
    parts.append("next: mdbkit triage <that instance's log> for any row that "
                 "is not OK.")
    return "\n".join(parts)


def worst_severity(rep: HostReport) -> str:
    sevs = [f.severity for f in rep.findings] + [i.worst for i in rep.instances]
    return min(sevs, key=lambda s: SEV_ORDER.get(s, 9)) if sevs else "OK"
