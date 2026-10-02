#!/usr/bin/env python3
"""Stand-in for mongod used by the lab tests: honours the flags mdbkit lab
passes, binds the port, forks and detaches like the real server, writes the
readiness line to the log, and exits on SIGTERM. FAKE_FAIL_PORT makes it
fail on one port, to test recovery from a half-started lab."""
import sys, os, time, json, socket
a = sys.argv[1:]
def opt(n, d=None): return a[a.index(n) + 1] if n in a else d
port = int(opt("--port", "27017")); log = opt("--logpath"); pidf = opt("--pidfilepath")
if str(port) == os.environ.get("FAKE_FAIL_PORT"):
    print("ERROR: simulated crash during startup on port %d" % port); sys.exit(14)
s = socket.socket()
try: s.bind(("127.0.0.1", port))
except OSError: print("ERROR: Address already in use"); sys.exit(48)
s.listen(5)
if "--fork" in a:
    if os.fork(): print("child process started successfully, parent exiting"); sys.exit(0)
    os.setsid()
    if os.fork(): os._exit(0)
    dn = os.open(os.devnull, os.O_RDWR)          # real mongod detaches stdio
    for fd in (0, 1, 2): os.dup2(dn, fd)
open(pidf, "w").write(str(os.getpid()))
with open(log, "a") as f:
    f.write(json.dumps({"t": {"$date": "2026-10-02T10:00:00.000+00:00"}, "s": "I", "c": "NETWORK", "id": 23016, "ctx": "listener", "msg": "Waiting for connections", "attr": {"port": port}}) + "\n")
import signal
signal.signal(signal.SIGTERM, lambda *x: sys.exit(0))
while True: time.sleep(0.2)
