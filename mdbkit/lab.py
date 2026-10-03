"""`mdbkit lab` — a disposable local MongoDB for trying things out.

Evaluating mdbkit, reproducing a slow query, or rehearsing a demo all need a
MongoDB with something interesting in its log. This starts a throwaway
replica set, standalone or sharded cluster on your own machine and hands you
the log paths.

SAFETY MODEL — read this before changing anything here:

* This is the ONLY part of mdbkit that starts external processes. Every
  analysis command remains offline and read-only; see SECURITY.md.
* It only ever runs `mongod`, `mongos` and `mongosh` from your PATH. It never
  contacts a network service and never touches a deployment it did not
  create.
* It refuses to use a directory it did not create. Every lab directory
  carries a `.mdbkit-lab.json` marker, and `destroy` will not delete a
  directory without one.
* It binds to 127.0.0.1 only, and defaults to an unusual port range so it
  can never be confused with a real deployment on 27017.
* It is for laptops and scratch VMs. It is not a deployment tool.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from typing import Dict, List, Optional

MARKER = ".mdbkit-lab.json"
DEFAULT_DIR = os.path.join(os.path.expanduser("~"), ".mdbkit-lab")
# Deliberately far from 27017-27019 and the legacy 28017 web port.
DEFAULT_BASE_PORT = 28110
RS_NAME = "mdbkitlab"
CONFIG_RS = "mdbkitcfg"
READY_MARKER = "Waiting for connections"


class LabError(RuntimeError):
    pass


# --------------------------------------------------------------- helpers ---

def find_binary(name: str) -> Optional[str]:
    return shutil.which(name)


def require_mongod() -> str:
    path = find_binary("mongod")
    if not path:
        raise LabError(
            "mongod was not found on your PATH.\n"
            "  mdbkit lab starts a real MongoDB locally, so the server binary "
            "must be installed.\n"
            "  Install the MongoDB Community Server for your platform, or use "
            "`mdbkit demo`\n"
            "  instead — it generates a realistic log with no MongoDB at all.")
    return path


def state_path(directory: str) -> str:
    return os.path.join(directory, MARKER)


def load_state(directory: str) -> Optional[dict]:
    try:
        with open(state_path(directory), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def save_state(directory: str, state: dict) -> None:
    with open(state_path(directory), "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)


def is_running(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _cmdline(pid: int) -> str:
    """The command line of `pid`, or "" if it cannot be read."""
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as fh:
            return fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        pass
    try:                                   # macOS and other non-procfs systems
        res = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=10)
        return res.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def lab_pid(node: dict) -> int:
    """The pid of this node's mongod, only if it really is that mongod.

    A pid file outlives its process. After a reboot the same number can
    belong to anything — signalling it blindly could stop an unrelated
    service. So a pid counts only while its command line names this node's
    own data directory.
    """
    pid = _read_pid(node.get("pidfile", "")) or node.get("pid", 0)
    if not is_running(pid):
        return 0
    # mongod is identified by its data directory, mongos (which has none)
    # by its log path.
    ident = node.get("data") or node.get("log") or ""
    if not ident:
        return 0
    cmd = _cmdline(pid)
    if not cmd:
        return 0
    if os.path.abspath(ident) in cmd or ident in cmd:
        return pid
    return 0


def _read_pid(pidfile: str) -> int:
    try:
        with open(pidfile, "r", encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return 0


def _wait_ready(logpath: str, timeout: float = 40.0, offset: int = 0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with open(logpath, "r", encoding="utf-8", errors="replace") as fh:
                if offset and os.path.getsize(logpath) >= offset:
                    fh.seek(offset)
                if READY_MARKER in fh.read():
                    return True
        except OSError:
            pass
        time.sleep(0.3)
    return False


# ----------------------------------------------------------------- start ---

def port_in_use(port: int) -> bool:
    """Whether something already listens on 127.0.0.1:port.

    The lab is the one part of mdbkit allowed to touch the network stack,
    and only this far: a connection attempt and a bind attempt on
    127.0.0.1, so a busy port produces a clear message instead of a
    half-started replica set.
    """
    import socket
    # 1. Anything accepting connections there, including a mongod bound to
    #    all addresses (on macOS a 127.0.0.1 bind can still succeed then).
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            return True
    except OSError:
        pass
    finally:
        probe.close()
    # 2. Bound but not listening. SO_REUSEADDR as mongod itself binds:
    #    connections left in TIME_WAIT by a killed node must not count.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError:
        return True
    finally:
        probe.close()
    return False


def plan_nodes(nodes: int, standalone: bool, shards: int) -> List[dict]:
    """The processes a lab consists of, in start order."""
    if shards:
        plan = [{"name": "config0", "role": "config", "set": CONFIG_RS,
                 "process": "mongod"}]
        for k in range(1, shards + 1):
            for i in range(nodes):
                plan.append({"name": "shard%d-%d" % (k, i), "role": "shard",
                             "set": "shard%d" % k, "process": "mongod"})
        plan.append({"name": "mongos", "role": "mongos", "set": None,
                     "process": "mongos"})
        return plan
    if standalone:
        return [{"name": "node0", "role": "standalone", "set": None,
                 "process": "mongod"}]
    return [{"name": "node%d" % i, "role": "replica", "set": RS_NAME,
             "process": "mongod"} for i in range(nodes)]


_OPTION = {"shards": "--shards", "nodes": "--nodes", "standalone": "--standalone",
           "port": "--port", "slowms": "--slowms"}


def _show(key: str, value) -> str:
    if key == "standalone":
        return "a standalone" if value else "no standalone"
    if key == "shards":
        return "%d shard(s)" % value if value else "no shards"
    return str(value)


def topology(state: Dict) -> Dict:
    """What an existing lab was made with, read back from its state file."""
    nodes = state.get("nodes") or []
    shard_sets = {n.get("set") for n in nodes if n.get("role") == "shard"}
    data = [n for n in nodes if n.get("role") in ("shard", "replica", "standalone")]
    per_set = (len([n for n in data if n.get("role") == "shard"]) // len(shard_sets)
               if shard_sets else len(data))
    standalone = (not state.get("replicaSet") and not state.get("sharded")
                  and len(nodes) == 1)
    return {"shards": len(shard_sets), "nodes": per_set if not standalone else 1,
            "standalone": standalone,
            "port": min(n["port"] for n in nodes) if nodes else None,
            "slowms": state.get("slowms", 0)}


def start(directory: str = DEFAULT_DIR, nodes: int = 3,
          base_port: int = DEFAULT_BASE_PORT, slowms: int = 0,
          standalone: bool = False, cache_gb: float = 0.25,
          shards: int = 0, echo=print, requested: Optional[Dict] = None) -> dict:
    """Create and start a lab deployment. Returns the state dict.

    The marker file is written before anything is started and updated as
    each node comes up, so a start that fails half way always leaves a lab
    that `stop` and `destroy` can clean up. Earlier versions wrote the
    marker last: a failed start left a directory mdbkit then refused to
    delete, and a mongod still running inside it.
    """
    mongod = require_mongod()
    if os.path.isdir(directory) and os.listdir(directory):
        existing = load_state(directory)
        if existing is None:
            raise LabError(
                "%s already exists and was not created by mdbkit lab.\n"
                "  Refusing to touch it. Choose another path with --dir."
                % directory)
        if existing.get("status") == "starting":
            raise LabError(
                "a previous start of the lab in %s did not finish (interrupted?).\n"
                "  Remove it with `mdbkit lab destroy --yes` and start again."
                % directory)
        if existing.get("nodes") and existing.get("status") != "failed":
            # An existing lab: bring back whatever is down, with the ports
            # and data it already has. Options that would make a different
            # lab are refused rather than silently ignored.
            have = topology(existing)
            differ = ["%s (it has %s)" % (_OPTION[k], _show(k, have[k]))
                      for k, v in sorted((requested or {}).items())
                      if k in have and v != have[k]]
            if differ:
                raise LabError(
                    "the lab in %s already exists and was made differently: %s.\n"
                    "  `mdbkit lab start` with no options starts it as it is; to "
                    "make a different one, `mdbkit lab destroy --yes` first or "
                    "use another --dir." % (directory, ", ".join(differ)))
            return _restart(directory, existing, mongod, echo)
        echo("note: reusing existing lab directory %s" % directory)

    nodes = 1 if standalone else max(1, nodes)
    plan = plan_nodes(nodes, standalone, shards)
    mongos = None
    if shards:
        mongos = find_binary("mongos")
        if not mongos:
            raise LabError("mongos was not found on your PATH. A sharded lab "
                           "needs it; it ships with the MongoDB server.")
        if not (find_binary("mongosh") or find_binary("mongo")):
            raise LabError("mongosh was not found on your PATH. A sharded lab "
                           "needs it to initiate the replica sets and add "
                           "the shards.")
    busy = [base_port + i for i in range(len(plan))
            if port_in_use(base_port + i)]
    if busy:
        raise LabError(
            "port(s) %s on 127.0.0.1 are already in use — probably another "
            "lab or a local mongod.\n"
            "  Pick another base port, e.g. --port %d, or check "
            "`mdbkit lab status --dir <other lab>`."
            % (", ".join(str(p) for p in busy), base_port + 100))

    os.makedirs(directory, exist_ok=True)
    state: Dict = {
        "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "dir": os.path.abspath(directory),
        "replicaSet": None if (standalone or shards) else RS_NAME,
        "sharded": bool(shards),
        "slowms": slowms,
        "cacheGb": cache_gb,
        "status": "starting",
        "nodes": [],
    }
    save_state(directory, state)          # marker first, always

    try:
        for i, spec in enumerate(plan):
            if spec["process"] == "mongos":
                _initiate_sharded(state, echo=echo)
                _start_mongos(mongos, directory, state, i, base_port + i,
                              slowms, spec, echo)
                _add_shards(state, echo=echo)
            else:
                _start_node(mongod, directory, state, i, base_port + i, slowms,
                            standalone, cache_gb, echo, spec=spec)
    except LabError as exc:
        try:
            stop(directory, echo=lambda *a: None)
        except LabError:
            pass
        state["status"] = "failed"
        save_state(directory, state)
        raise LabError(
            "%s\n  Anything already started was stopped. Remove the partial "
            "lab with:\n    mdbkit lab destroy --dir %s --yes"
            % (exc, directory))

    state["status"] = "running"
    save_state(directory, state)
    if not standalone and not shards:
        _initiate(state, echo=echo)
    return state


def _spec_of(node: dict, state: dict) -> dict:
    """A node's role as recorded (labs made before 0.8 lack it)."""
    role = node.get("role") or ("replica" if state.get("replicaSet") else "standalone")
    return {"name": node.get("name") or "node%d" % node["index"], "role": role,
            "set": node.get("set", state.get("replicaSet")),
            "process": node.get("process") or "mongod"}


def _restart(directory: str, state: dict, mongod: str, echo=print) -> dict:
    """Start the nodes of an existing lab that are not running."""
    order = {"config": 0, "shard": 1, "replica": 1, "standalone": 1, "mongos": 2}
    nodes = sorted(state["nodes"], key=lambda n: (order.get(_spec_of(n, state)["role"], 1),
                                                  n["index"]))
    down = [n for n in nodes if not lab_pid(n)]
    if not down:
        raise LabError(
            "the lab in %s is already running (%d node(s)).\n"
            "  Use `mdbkit lab status`, or `mdbkit lab destroy` to remove it."
            % (directory, len(nodes)))
    # A node that was just killed can hold its port for a moment.
    deadline = time.time() + 10
    busy = [n["port"] for n in down if port_in_use(n["port"])]
    while busy and time.time() < deadline:
        time.sleep(0.25)
        busy = [n["port"] for n in down if port_in_use(n["port"])]
    if busy:
        raise LabError("port(s) %s are taken by something else, so the stopped "
                       "lab node(s) cannot come back on them."
                       % ", ".join(str(p) for p in busy))
    slowms = state.get("slowms", 0)
    cache_gb = state.get("cacheGb", 0.25)
    up = len(nodes) - len(down)
    if up:
        echo("%d node(s) already running; starting the %d that are down"
             % (up, len(down)))
    for n in down:
        spec = _spec_of(n, state)
        if spec["process"] == "mongos":
            mongos = find_binary("mongos")
            if not mongos:
                raise LabError("mongos was not found on your PATH.")
            _start_mongos(mongos, directory, state, n["index"], n["port"],
                          slowms, spec, echo)
        else:
            _start_node(mongod, directory, state, n["index"], n["port"], slowms,
                        spec["role"] == "standalone", cache_gb, echo, spec=spec)
    state["status"] = "running"
    save_state(directory, state)
    return state


def _start_node(mongod, directory, state, i, port, slowms, standalone,
                cache_gb, echo, spec=None):
    spec = spec or {"name": "node%d" % i, "role": "standalone" if standalone
                    else "replica", "set": None if standalone else RS_NAME,
                    "process": "mongod"}
    node_dir = os.path.join(directory, spec["name"])
    data_dir = os.path.join(node_dir, "data")
    log_path = os.path.join(node_dir, "mongod.log")
    pid_file = os.path.join(node_dir, "mongod.pid")
    os.makedirs(data_dir, exist_ok=True)

    node = {"index": i, "name": spec["name"], "role": spec["role"],
            "set": spec["set"], "process": "mongod", "port": port,
            "dir": node_dir, "data": data_dir,
            "log": log_path, "pidfile": pid_file, "pid": 0}
    state["nodes"] = [n for n in state["nodes"] if n["index"] != i] + [node]
    save_state(directory, state)          # recorded before it can fail

    cmd = [mongod,
           "--port", str(port),
           "--dbpath", data_dir,
           "--logpath", log_path,
           "--logappend",
           "--bind_ip", "127.0.0.1",
           "--pidfilepath", pid_file,
           "--slowms", str(slowms),
           "--wiredTigerCacheSizeGB", str(cache_gb)]
    if spec["set"]:
        cmd += ["--replSet", spec["set"]]
    if spec["role"] == "config":
        cmd.append("--configsvr")
    elif spec["role"] == "shard":
        cmd.append("--shardsvr")
    if os.name == "posix":
        cmd.append("--fork")

    # Readiness is judged only from what this start appends to the log, so a
    # "Waiting for connections" line from a previous run cannot fool it.
    try:
        offset = os.path.getsize(log_path)
    except OSError:
        offset = 0

    echo("starting %s on 127.0.0.1:%d" % (spec["name"], port))
    # Output goes to a file, not a pipe: a forked daemon that inherits a
    # pipe keeps it open, and waiting on it would hang.
    out_path = os.path.join(node_dir, "startup.out")
    with open(out_path, "w") as out:
        try:
            if os.name == "posix":
                res = subprocess.run(cmd, stdout=out, stderr=subprocess.STDOUT,
                                     timeout=120)
                if res.returncode != 0:
                    out.flush()
                    raise LabError("mongod failed to start on port %d:\n%s"
                                   % (port, _tail(out_path)))
            else:
                subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT)
        except subprocess.TimeoutExpired:
            raise LabError("mongod did not return while starting %s "
                           "(see %s)" % (spec["name"], out_path))

    if not _wait_ready(log_path, offset=offset):
        raise LabError("%s did not report '%s' within the timeout.\n"
                       "  Check %s" % (spec["name"], READY_MARKER, log_path))
    node["pid"] = _read_pid(pid_file)
    save_state(directory, state)


def _tail(path: str, limit: int = 600) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()[-limit:]
    except OSError:
        return "(no output)"


def _mongosh() -> str:
    sh = find_binary("mongosh") or find_binary("mongo")
    if not sh:
        raise LabError("mongosh was not found on your PATH.")
    return sh


def _run_js(port: int, script: str, timeout: int = 120) -> str:
    res = subprocess.run([_mongosh(), "--quiet", "--port", str(port),
                          "--eval", script],
                         capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise LabError("mongosh on port %d failed:\n%s" % (
            port, ((res.stderr or "") + (res.stdout or "")).strip()[-600:]))
    return res.stdout or ""


def _initiate_sharded(state: dict, echo=print) -> None:
    """rs.initiate the config server and every shard replica set."""
    sets: Dict[str, List[dict]] = {}
    for n in state["nodes"]:
        if n.get("set"):
            sets.setdefault(n["set"], []).append(n)
    for name, members in sets.items():
        config = ", ".join('{_id: %d, host: "127.0.0.1:%d"%s}'
                           % (i, m["port"], ", priority: 2" if i == 0 else "")
                           for i, m in enumerate(members))
        extra = ", configsvr: true" if members[0]["role"] == "config" else ""
        echo("initiating replica set %s" % name)
        _run_js(members[0]["port"], 'rs.initiate({_id: "%s"%s, members: [%s]})'
                % (name, extra, config))
    for name, members in sets.items():
        if not _wait_primary(_mongosh(), members[0]["port"]):
            raise LabError("replica set %s elected no primary within 60s" % name)


def _start_mongos(mongos, directory, state, i, port, slowms, spec, echo):
    node_dir = os.path.join(directory, spec["name"])
    os.makedirs(node_dir, exist_ok=True)
    log_path = os.path.join(node_dir, "mongos.log")
    pid_file = os.path.join(node_dir, "mongos.pid")
    node = {"index": i, "name": spec["name"], "role": "mongos", "set": None,
            "process": "mongos", "port": port, "dir": node_dir, "data": None,
            "log": log_path, "pidfile": pid_file, "pid": 0}
    state["nodes"] = [n for n in state["nodes"] if n["index"] != i] + [node]
    save_state(directory, state)
    cfg = [n for n in state["nodes"] if n.get("role") == "config"]
    configdb = "%s/%s" % (CONFIG_RS, ",".join("127.0.0.1:%d" % n["port"]
                                               for n in cfg))
    cmd = [mongos, "--port", str(port), "--configdb", configdb,
           "--logpath", log_path, "--logappend", "--bind_ip", "127.0.0.1",
           "--pidfilepath", pid_file, "--slowms", str(slowms)]
    if os.name == "posix":
        cmd.append("--fork")
    try:
        offset = os.path.getsize(log_path)
    except OSError:
        offset = 0
    echo("starting mongos on 127.0.0.1:%d" % port)
    out_path = os.path.join(node_dir, "startup.out")
    with open(out_path, "w") as out:
        try:
            if os.name == "posix":
                res = subprocess.run(cmd, stdout=out, stderr=subprocess.STDOUT,
                                     timeout=120)
                if res.returncode != 0:
                    out.flush()
                    raise LabError("mongos failed to start on port %d:\n%s"
                                   % (port, _tail(out_path)))
            else:
                subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT)
        except subprocess.TimeoutExpired:
            raise LabError("mongos did not return while starting (see %s)"
                           % out_path)
    if not _wait_ready(log_path, timeout=90, offset=offset):
        raise LabError("mongos did not report '%s' within the timeout.\n"
                       "  Check %s" % (READY_MARKER, log_path))
    node["pid"] = _read_pid(pid_file)
    save_state(directory, state)


def _add_shards(state: dict, echo=print) -> None:
    mongos = [n for n in state["nodes"] if n.get("role") == "mongos"][0]
    sets: Dict[str, List[int]] = {}
    for n in state["nodes"]:
        if n.get("role") == "shard":
            sets.setdefault(n["set"], []).append(n["port"])
    for name, ports in sets.items():
        echo("adding shard %s" % name)
        _run_js(mongos["port"], 'sh.addShard("%s/%s")' % (
            name, ",".join("127.0.0.1:%d" % p for p in ports)), timeout=180)


def _initiate(state: dict, echo=print) -> None:
    """rs.initiate() the lab replica set, if mongosh is available."""
    ports = [n["port"] for n in state["nodes"]]
    members = ", ".join(
        '{_id: %d, host: "127.0.0.1:%d"%s}'
        % (i, p, ", priority: 2" if i == 0 else "")
        for i, p in enumerate(ports))
    script = 'rs.initiate({_id: "%s", members: [%s]})' % (RS_NAME, members)

    mongosh = find_binary("mongosh") or find_binary("mongo")
    if not mongosh:
        state["initiated"] = False
        state["initiateCommand"] = script
        save_state(state["dir"], state)
        echo("\nmongosh was not found, so the replica set was not initiated.")
        echo("Run this yourself to finish setting it up:")
        echo('  mongosh --port %d --eval \'%s\'' % (ports[0], script))
        return

    echo("initiating replica set %s" % RS_NAME)
    res = subprocess.run([mongosh, "--quiet", "--port", str(ports[0]),
                          "--eval", script],
                         capture_output=True, text=True, timeout=90)
    ok = res.returncode == 0
    state["initiated"] = ok
    save_state(state["dir"], state)
    if not ok:
        echo("warning: rs.initiate reported a problem:\n%s"
             % (res.stdout or res.stderr or "").strip()[:400])
        return
    # rs.initiate returns before the election finishes. Wait for a primary,
    # so `lab seed` straight after `lab start` does not hit "not primary".
    if not _wait_primary(mongosh, ports[0]):
        echo("warning: no primary was elected within 60s; `mdbkit lab seed` "
             "may fail until one is.")


WAIT_PRIMARY_JS = (
    "for (let i = 0; i < 120; i++) {"
    " const h = db.hello ? db.hello() : db.isMaster();"
    " if (h.isWritablePrimary || h.ismaster) { print('PRIMARY'); quit(0); }"
    " sleep(500); }"
    " print('NO_PRIMARY');")


def _wait_primary(mongosh: str, port: int) -> bool:
    """Poll the node until it is a writable primary (up to 60 s)."""
    try:
        res = subprocess.run([mongosh, "--quiet", "--port", str(port),
                              "--eval", WAIT_PRIMARY_JS],
                             capture_output=True, text=True, timeout=90)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return "PRIMARY" in (res.stdout or "") and "NO_PRIMARY" not in res.stdout


# ------------------------------------------------------------ lifecycle ---

def status(directory: str = DEFAULT_DIR) -> Optional[dict]:
    state = load_state(directory)
    if not state:
        return None
    for n in state.get("nodes", []):
        pid = lab_pid(n)
        n["pid"] = pid
        n["running"] = bool(pid)
    return state


def stop(directory: str = DEFAULT_DIR, echo=print) -> int:
    state = load_state(directory)
    if not state:
        raise LabError("no lab found in %s" % directory)
    stopped = 0
    # mongos first and the config server last, the order a cluster is
    # taken down in.
    order = {"mongos": 0, "shard": 1, "replica": 1, "standalone": 1, "config": 2}
    for n in sorted(state.get("nodes", []),
                    key=lambda n: order.get(n.get("role", "replica"), 1)):
        pid = lab_pid(n)
        if not pid:
            continue
        name = n.get("name") or "node%d" % n["index"]
        try:
            os.kill(pid, signal.SIGTERM)
            stopped += 1
            echo("stopping %s (pid %d)" % (name, pid))
        except OSError as exc:
            echo("could not stop %s: %s" % (name, exc))
    # A clean WiredTiger shutdown takes a checkpoint and can take a while.
    deadline = time.time() + 90
    while time.time() < deadline:
        if not any(lab_pid(n) for n in state.get("nodes", [])):
            break
        time.sleep(0.25)
    return stopped


def still_running(directory: str) -> List[int]:
    state = load_state(directory) or {}
    return [p for p in (lab_pid(n) for n in state.get("nodes", [])) if p]


def destroy(directory: str = DEFAULT_DIR, echo=print) -> None:
    """Stop and delete a lab. Refuses any directory it did not create."""
    if not os.path.isdir(directory):
        raise LabError("no such directory: %s" % directory)
    if not os.path.exists(state_path(directory)):
        raise LabError(
            "%s has no %s marker, so mdbkit did not create it.\n"
            "  Refusing to delete anything." % (directory, MARKER))
    try:
        stop(directory, echo=echo)
    except LabError:
        pass
    alive = still_running(directory)
    if alive:
        raise LabError(
            "mongod (pid %s) is still shutting down; not deleting its data "
            "underneath it. Run destroy again in a minute."
            % ", ".join(str(p) for p in alive))
    shutil.rmtree(directory)
    echo("removed %s" % directory)


def connection_string(state: dict) -> str:
    routers = [n for n in state["nodes"] if n.get("role") == "mongos"]
    if routers:
        return "mongodb://%s/" % ",".join("127.0.0.1:%d" % n["port"]
                                          for n in routers)
    hosts = ",".join("127.0.0.1:%d" % n["port"] for n in state["nodes"])
    if state.get("replicaSet"):
        return "mongodb://%s/?replicaSet=%s" % (hosts, state["replicaSet"])
    return "mongodb://%s/" % hosts


def log_paths(state: dict) -> List[str]:
    """Log files, mongos first in a sharded lab (it sees every query)."""
    order = {"mongos": 0, "shard": 1, "config": 2}
    nodes = sorted(state["nodes"], key=lambda n: (order.get(n.get("role"), 1),
                                                   n["index"]))
    return [n["log"] for n in nodes]


# ------------------------------------------------------------------ seed ---

SEED_SCRIPT = r"""// mdbkit lab seed -- creates sample data and runs a workload whose
// slow queries are deliberately interesting to analyse.
const db = db.getSiblingDB("shop");
const N = %(docs)d;

print("seeding shop.orders ...");
db.orders.drop();
const statuses = ["pending", "paid", "shipped", "cancelled"];
let batch = [];
for (let i = 0; i < N; i++) {
  batch.push({
    status: statuses[i %% statuses.length],
    createdAt: new Date(Date.now() - Math.floor(Math.random() * 7776000000)),
    customerId: "cust-" + (i %% 5000),
    total: Math.round(Math.random() * 40000) / 100,
    items: [{sku: "SKU-" + (i %% 900), qty: 1 + (i %% 4)}]
  });
  if (batch.length === 1000) { db.orders.insertMany(batch); batch = []; }
}
if (batch.length) db.orders.insertMany(batch);

print("seeding shop.users ...");
db.users.drop();
batch = [];
for (let i = 0; i < Math.min(N, 20000); i++) {
  batch.push({email: "user" + i + "@example.com", name: "User " + i,
              active: i %% 3 !== 0});
  if (batch.length === 1000) { db.users.insertMany(batch); batch = []; }
}
if (batch.length) db.users.insertMany(batch);
db.users.createIndex({email: 1}, {unique: true});

print("running workload ...");
// healthy: indexed point lookups
for (let i = 0; i < 40; i++) {
  db.users.find({email: "user" + (i * 7) + "@example.com"}).toArray();
}
// unhealthy: equality + range + sort with no supporting index
for (let i = 0; i < 25; i++) {
  db.orders.find({status: "pending", createdAt: {$gt: new Date(Date.now() - 2592000000)}})
           .sort({createdAt: -1}).limit(50).toArray();
}
// unhealthy: aggregation scanning the collection
for (let i = 0; i < 10; i++) {
  db.orders.aggregate([
    {$match: {status: "shipped"}},
    {$sort: {createdAt: -1}},
    {$group: {_id: "$customerId", n: {$sum: 1}}},
    {$limit: 20}
  ]).toArray();
}
// unhealthy: update with no index on the predicate
for (let i = 0; i < 15; i++) {
  db.orders.updateMany({customerId: "cust-" + i}, {$set: {touched: new Date()}});
}
print("done. orders=" + db.orders.countDocuments() +
      " users=" + db.users.countDocuments());
"""


SHARDED_SEED_SCRIPT = r"""// mdbkit lab seed (sharded) -- shards a collection, moves a range between
// shards (a real chunk migration), and runs queries mongos can route to one
// shard and queries it has to send to every shard.
const db = db.getSiblingDB("shop");
const N = %(docs)d;
const shards = db.getSiblingDB("config").shards.find().toArray().map(s => s._id);

print("sharding shop.orders on { customerId: 1 } ...");
db.orders.drop();
db.adminCommand({enableSharding: "shop"});
db.orders.createIndex({customerId: 1});
db.adminCommand({shardCollection: "shop.orders", key: {customerId: 1}});
const statuses = ["pending", "paid", "shipped", "cancelled"];
let batch = [];
for (let i = 0; i < N; i++) {
  batch.push({
    customerId: "cust-" + String(i %% 5000).padStart(5, "0"),
    status: statuses[i %% statuses.length],
    createdAt: new Date(Date.now() - Math.floor(Math.random() * 7776000000)),
    total: Math.round(Math.random() * 40000) / 100
  });
  if (batch.length === 1000) { db.orders.insertMany(batch); batch = []; }
}
if (batch.length) db.orders.insertMany(batch);

// split the key range and give each shard a part: real migrations
for (let k = 1; k < shards.length; k++) {
  const at = "cust-" + String(Math.floor(5000 * k / shards.length)).padStart(5, "0");
  db.adminCommand({split: "shop.orders", middle: {customerId: at}});
  const res = db.adminCommand({moveRange: "shop.orders", min: {customerId: at},
                               toShard: shards[k]});
  print("moved range from " + at + " to " + shards[k] + ": " + (res.ok ? "ok" : res.errmsg));
}

print("running workload ...");
// targeted: the shard key in the filter, so mongos asks one shard
for (let i = 0; i < 40; i++) {
  db.orders.find({customerId: "cust-" + String(i * 37).padStart(5, "0")}).toArray();
}
// scatter-gather: no shard key, so every shard is asked
for (let i = 0; i < 25; i++) {
  db.orders.find({status: "pending", createdAt: {$gt: new Date(Date.now() - 2592000000)}})
           .sort({createdAt: -1}).limit(50).toArray();
}
for (let i = 0; i < 10; i++) {
  db.orders.aggregate([{$match: {status: "shipped"}},
                       {$group: {_id: "$customerId", n: {$sum: 1}}}, {$limit: 20}]).toArray();
}
// a multi-document update without the shard key goes to every shard
for (let i = 0; i < 10; i++) {
  db.orders.updateMany({status: "paid", total: {$lt: i}}, {$set: {touched: new Date()}});
}
print("done. orders=" + db.orders.countDocuments() + " shards=" + shards.length);
"""


WORKLOAD_MARK = 'print("running workload ...");'


def seed_script(docs: int = 50000, sharded: bool = False,
                workload_only: bool = False) -> str:
    script = (SHARDED_SEED_SCRIPT if sharded else SEED_SCRIPT) % {"docs": docs}
    if workload_only:
        # keep the data (and any index you added): only the queries again
        head = script[:script.index("\nconst N = ")]
        script = (head + "\nif (!db.orders.estimatedDocumentCount()) {\n"
                  "  print(\"no data yet: run `mdbkit lab seed` first\"); quit(1);\n}\n"
                  + script[script.index(WORKLOAD_MARK):])
        script = script.replace('" shards=" + shards.length', '""')
    return script


def seed(directory: str = DEFAULT_DIR, docs: int = 50000, echo=print,
         workload_only: bool = False) -> bool:
    """Populate the lab and run a workload. Returns True if it ran."""
    state = load_state(directory)
    if not state:
        raise LabError("no lab found in %s — run `mdbkit lab start` first"
                       % directory)
    live = [n for n in state["nodes"] if is_running(_read_pid(n["pidfile"]))]
    if not live:
        raise LabError("the lab in %s is not running — `mdbkit lab start`"
                       % directory)
    sharded = bool(state.get("sharded"))
    script = seed_script(docs, sharded=sharded, workload_only=workload_only)
    mongosh = find_binary("mongosh") or find_binary("mongo")
    if not mongosh:
        echo("mongosh was not found. Save the script below and run it yourself:")
        echo(script)
        return False
    port = live[0]["port"]
    routers = [n for n in live if n.get("role") == "mongos"]
    if sharded:
        if not routers:
            raise LabError("the lab's mongos is not running — `mdbkit lab start`")
        port = routers[0]["port"]
        target = ["--port", str(port)]
    elif state.get("replicaSet"):
        # Address the whole set so writes go to whichever node is primary,
        # and wait for one if an election is still in progress.
        hosts = ",".join("127.0.0.1:%d" % n["port"] for n in live)
        target = ["mongodb://%s/?replicaSet=%s&serverSelectionTimeoutMS=60000"
                  % (hosts, state["replicaSet"])]
    else:
        target = ["--port", str(port)]
    echo("seeding via %s on port %d (this takes a moment) ..." % (mongosh, port))
    res = subprocess.run([mongosh, "--quiet"] + target + ["--eval", script],
                         capture_output=True, text=True, timeout=900)
    out = (res.stdout or "").strip()
    if out:
        echo(out[-1500:])
    if res.returncode != 0:
        echo("warning: seeding reported a problem:\n%s"
             % (res.stderr or "").strip()[:400])
        return False
    return True
