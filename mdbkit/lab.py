"""`mdbkit lab` — a disposable local MongoDB for trying things out.

Evaluating mdbkit, reproducing a slow query, or rehearsing a demo all need a
MongoDB with something interesting in its log. This starts a throwaway
replica set on your own machine and hands you the log path.

SAFETY MODEL — read this before changing anything here:

* This is the ONLY part of mdbkit that starts external processes. Every
  analysis command remains offline and read-only; see SECURITY.md.
* It only ever runs `mongod` and `mongosh` from your PATH. It never
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
    data_dir = node.get("data") or ""
    if not data_dir:
        return 0
    cmd = _cmdline(pid)
    if not cmd:
        return 0
    if os.path.abspath(data_dir) in cmd or data_dir in cmd:
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
    and only this far: a local bind attempt, so a busy port produces a clear
    message instead of a half-started replica set.
    """
    import socket
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
    except OSError:
        return True
    finally:
        probe.close()
    return False


def start(directory: str = DEFAULT_DIR, nodes: int = 3,
          base_port: int = DEFAULT_BASE_PORT, slowms: int = 0,
          standalone: bool = False, cache_gb: float = 0.25,
          echo=print) -> dict:
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
        live = [n for n in existing.get("nodes", []) if lab_pid(n)]
        if live:
            raise LabError(
                "a lab is already running in %s (%d node(s)).\n"
                "  Use `mdbkit lab status`, or `mdbkit lab destroy` to remove it."
                % (directory, len(live)))
        echo("note: reusing existing lab directory %s" % directory)

    nodes = 1 if standalone else max(1, nodes)
    busy = [base_port + i for i in range(nodes) if port_in_use(base_port + i)]
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
        "replicaSet": None if standalone else RS_NAME,
        "slowms": slowms,
        "status": "starting",
        "nodes": [],
    }
    save_state(directory, state)          # marker first, always

    try:
        for i in range(nodes):
            _start_node(mongod, directory, state, i, base_port + i, slowms,
                        standalone, cache_gb, echo)
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
    if not standalone:
        _initiate(state, echo=echo)
    return state


def _start_node(mongod, directory, state, i, port, slowms, standalone,
                cache_gb, echo):
    node_dir = os.path.join(directory, "node%d" % i)
    data_dir = os.path.join(node_dir, "data")
    log_path = os.path.join(node_dir, "mongod.log")
    pid_file = os.path.join(node_dir, "mongod.pid")
    os.makedirs(data_dir, exist_ok=True)

    node = {"index": i, "port": port, "dir": node_dir, "data": data_dir,
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
    if not standalone:
        cmd += ["--replSet", RS_NAME]
    if os.name == "posix":
        cmd.append("--fork")

    # Readiness is judged only from what this start appends to the log, so a
    # "Waiting for connections" line from a previous run cannot fool it.
    try:
        offset = os.path.getsize(log_path)
    except OSError:
        offset = 0

    echo("starting node%d on 127.0.0.1:%d" % (i, port))
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
            raise LabError("mongod did not return while starting node%d "
                           "(see %s)" % (i, out_path))

    if not _wait_ready(log_path, offset=offset):
        raise LabError("node%d did not report '%s' within the timeout.\n"
                       "  Check %s" % (i, READY_MARKER, log_path))
    node["pid"] = _read_pid(pid_file)
    save_state(directory, state)


def _tail(path: str, limit: int = 600) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()[-limit:]
    except OSError:
        return "(no output)"


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
    # give the election a moment so the log has a PRIMARY transition in it
    time.sleep(3)


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
    for n in state.get("nodes", []):
        pid = lab_pid(n)
        if not pid:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            stopped += 1
            echo("stopping node%d (pid %d)" % (n["index"], pid))
        except OSError as exc:
            echo("could not stop node%d: %s" % (n["index"], exc))
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
    hosts = ",".join("127.0.0.1:%d" % n["port"] for n in state["nodes"])
    if state.get("replicaSet"):
        return "mongodb://%s/?replicaSet=%s" % (hosts, state["replicaSet"])
    return "mongodb://%s/" % hosts


def log_paths(state: dict) -> List[str]:
    return [n["log"] for n in state["nodes"]]


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


def seed_script(docs: int = 50000) -> str:
    return SEED_SCRIPT % {"docs": docs}


def seed(directory: str = DEFAULT_DIR, docs: int = 50000, echo=print) -> bool:
    """Populate the lab and run a workload. Returns True if it ran."""
    state = load_state(directory)
    if not state:
        raise LabError("no lab found in %s — run `mdbkit lab start` first"
                       % directory)
    live = [n for n in state["nodes"] if is_running(_read_pid(n["pidfile"]))]
    if not live:
        raise LabError("the lab in %s is not running — `mdbkit lab start`"
                       % directory)
    script = seed_script(docs)
    mongosh = find_binary("mongosh") or find_binary("mongo")
    if not mongosh:
        echo("mongosh was not found. Save the script below and run it yourself:")
        echo(script)
        return False
    port = live[0]["port"]
    echo("seeding via %s on port %d (this takes a moment) ..." % (mongosh, port))
    res = subprocess.run([mongosh, "--quiet", "--port", str(port),
                          "--eval", script],
                         capture_output=True, text=True, timeout=900)
    out = (res.stdout or "").strip()
    if out:
        echo(out[-1500:])
    if res.returncode != 0:
        echo("warning: seeding reported a problem:\n%s"
             % (res.stderr or "").strip()[:400])
        return False
    return True
