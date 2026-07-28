"""`mdbkit demo` — generate a realistic MongoDB structured log.

Evaluating a log analyser normally requires a cluster with interesting
problems in it, which is a lot to ask of someone who just wants to see what
the tool does. This writes a synthetic but realistic logv2 stream so every
mdbkit command can be tried in seconds, with no MongoDB installed at all.

The output is deterministic for a given seed, so a demo behaves identically
every time you run it — including on a conference projector.

Pure stdlib, no network, writes only where you tell it to.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

SCENARIOS = ("healthy", "incident", "mixed")

APPS = [
    ("OrderService", "mongo-java-driver|sync", "4.11.0"),
    ("ReportWorker", "PyMongo", "4.8.0"),
    ("checkout-api", "nodejs", "6.5.0"),
]

CLIENT_IPS = ["10.20.4.11", "10.20.4.12", "10.20.5.30", "10.20.7.8"]

# (namespace, operation, filter shape, sort, plan, docsExamined, nreturned, ms)
_SLOW_TEMPLATES = [
    # the headline offender: equality + range + sort, no index
    ("shop.orders", "find",
     {"status": "pending", "createdAt": {"$gt": {"$date": "2026-06-30T00:00:00Z"}}},
     {"createdAt": -1}, "COLLSCAN", 125000, 42, (900, 2400), True),
    # aggregation scanning a large collection
    ("shop.events", "aggregate",
     {"tenantId": "t-8841", "ts": {"$gte": {"$date": "2026-06-01T00:00:00Z"}}},
     {"ts": -1}, "COLLSCAN", 890000, 9, (4000, 9000), True),
    # index used but weakly selective
    ("shop.sessions", "find",
     {"active": True, "lastSeen": {"$gt": {"$date": "2026-07-01T00:00:00Z"}}},
     {"lastSeen": -1}, "IXSCAN { active: 1 }", 48000, 310, (300, 900), True),
    # healthy: point lookup on an index
    ("shop.users", "find", {"email": "a.user@example.com"}, None,
     "IXSCAN { email: 1 }", 1, 1, (101, 140), False),
    # update with no supporting index
    ("shop.products", "update", {"sku": "SKU-40199"}, None,
     "COLLSCAN", 54000, 1, (400, 800), False),
]


def _ts(dt: datetime) -> dict:
    return {"$date": dt.strftime("%Y-%m-%dT%H:%M:%S.") + "%03d" % (dt.microsecond // 1000)
            + dt.strftime("%z")[:3] + ":" + dt.strftime("%z")[3:]}


def _line(dt: datetime, sev: str, comp: str, mid: int, ctx: str, msg: str,
          attr: Optional[dict] = None) -> str:
    doc = {"t": _ts(dt), "s": sev, "c": comp, "id": mid, "ctx": ctx, "msg": msg}
    if attr is not None:
        doc["attr"] = attr
    return json.dumps(doc, separators=(",", ":"))


class DemoLog:
    """Builds a synthetic mongod log for a given scenario."""

    def __init__(self, scenario: str = "mixed", minutes: int = 90,
                 seed: int = 7, host: str = "demo-rs0",
                 start: Optional[datetime] = None, port: int = 27017):
        if scenario not in SCENARIOS:
            raise ValueError("scenario must be one of %s" % ", ".join(SCENARIOS))
        self.scenario = scenario
        self.minutes = max(1, minutes)
        self.rng = random.Random(seed)
        self.host = host
        self.port = port
        self.start = start or datetime(
            2026, 7, 1, 8, 0, 0, tzinfo=timezone(timedelta(hours=4)))
        self.lines: List[str] = []
        self._conn = 10

    # -- helpers ----------------------------------------------------------
    def at(self, minute: float) -> datetime:
        return self.start + timedelta(seconds=minute * 60.0)

    def add(self, *args, **kwargs):
        self.lines.append(_line(*args, **kwargs))

    # -- sections ---------------------------------------------------------
    def _startup(self):
        t = self.at(0)
        self.add(t, "I", "CONTROL", 4615611, "initandlisten", "MongoDB starting",
                 {"pid": 4242, "port": self.port, "dbPath": "/var/lib/mongodb",
                  "architecture": "64-bit", "host": self.host})
        self.add(t + timedelta(milliseconds=6), "I", "CONTROL", 23403,
                 "initandlisten", "Build Info",
                 {"buildInfo": {"version": "7.0.14", "gitVersion": "d7fbd0e",
                                "modules": [], "allocator": "tcmalloc",
                                "environment": {"distmod": "ubuntu2204",
                                                "distarch": "x86_64"}}})
        self.add(t + timedelta(milliseconds=40), "I", "STORAGE", 22297,
                 "initandlisten", "Storage engine start",
                 {"storageEngine": "wiredTiger"})
        self.add(t + timedelta(milliseconds=900), "I", "NETWORK", 23016,
                 "listener", "Waiting for connections",
                 {"port": self.port, "ssl": "off"})

    def _connection(self, minute: float, ip: Optional[str] = None,
                    app: Optional[tuple] = None):
        t = self.at(minute)
        ip = ip or self.rng.choice(CLIENT_IPS)
        app = app or self.rng.choice(APPS)
        self._conn += 1
        cid = self._conn
        port = self.rng.randint(40000, 60000)
        self.add(t, "I", "NETWORK", 22943, "listener", "Connection accepted",
                 {"remote": "%s:%d" % (ip, port), "uuid": "c%d" % cid,
                  "connectionId": cid, "connectionCount": self._conn})
        self.add(t + timedelta(milliseconds=8), "I", "NETWORK", 51800,
                 "conn%d" % cid, "client metadata",
                 {"remote": "%s:%d" % (ip, port), "client": "conn%d" % cid,
                  "doc": {"application": {"name": app[0]},
                          "driver": {"name": app[1], "version": app[2]},
                          "os": {"type": "Linux"}}})
        return cid

    def _slow(self, minute: float, template, conn: int):
        ns, op, filt, sort, plan, docs, nret, ms_range, heavy = template
        t = self.at(minute)
        ms = self.rng.randint(*ms_range)
        db, coll = ns.split(".", 1)
        app = self.rng.choice(APPS)[0]

        if op == "find":
            command: Dict = {"find": coll, "filter": filt, "$db": db}
            if sort:
                command["sort"] = sort
                command["limit"] = 50
            comp, otype = "COMMAND", "command"
        elif op == "aggregate":
            pipeline = [{"$match": filt}]
            if sort:
                pipeline.append({"$sort": sort})
            pipeline.append({"$group": {"_id": "$kind", "n": {"$sum": 1}}})
            command = {"aggregate": coll, "pipeline": pipeline,
                       "cursor": {}, "$db": db}
            comp, otype = "COMMAND", "command"
        else:  # update
            command = {"q": filt, "u": {"$set": {"stock": self.rng.randint(1, 40)}},
                       "multi": False, "upsert": False}
            comp, otype = "WRITE", "update"

        attr = {"type": otype, "ns": ns, "appName": app, "command": command,
                "planSummary": plan, "keysExamined": 0 if "COLLSCAN" in plan else nret,
                "docsExamined": docs, "numYields": max(0, docs // 128),
                "queryHash": "%08X" % (abs(hash(ns + op)) % (16 ** 8)),
                "reslen": 200 + nret * 180, "protocol": "op_msg",
                "durationMillis": ms}
        if otype == "update":
            attr["nMatched"] = nret
            attr["nModified"] = nret
        else:
            attr["nreturned"] = nret
        if sort and "COLLSCAN" in plan:
            attr["hasSortStage"] = True
        self.add(t, "I", comp, 51803, "conn%d" % conn, "Slow query", attr)

    # -- incidents --------------------------------------------------------
    def _connection_storm(self, minute: float):
        for i in range(220):
            self._connection(minute + (i % 60) / 3600.0, ip="10.20.9.77",
                             app=("checkout-api", "nodejs", "6.5.0"))

    def _election(self, minute: float):
        t = self.at(minute)
        self.add(t, "I", "REPL", 4784900, "ReplCoord",
                 "Stopping replication producer")
        self.add(t + timedelta(seconds=2), "I", "ELECTION", 4615601, "ReplCoord",
                 "Starting an election, since we've seen no PRIMARY in election "
                 "timeout period", {"electionTimeoutPeriodMillis": 10000})
        self.add(t + timedelta(seconds=3), "I", "ELECTION", 21444, "ReplCoord",
                 "Dry election run succeeded, running for election", {"term": 73})
        self.add(t + timedelta(seconds=4), "I", "ELECTION", 21450, "ReplCoord",
                 "Election succeeded, assuming primary role", {"term": 74})
        self.add(t + timedelta(seconds=4, milliseconds=200), "I", "REPL", 21358,
                 "ReplCoord", "Replica set state transition",
                 {"newState": "PRIMARY", "oldState": "SECONDARY"})
        self.add(t + timedelta(seconds=5), "I", "REPL", 21215, "ReplCoord",
                 "Member is in new state",
                 {"hostAndPort": "%s-2:%d" % (self.host, self.port),
                  "newState": "SECONDARY"})

    def _index_build(self, minute: float):
        t = self.at(minute)
        self.add(t, "I", "INDEX", 20438, "conn12", "Index build: registering",
                 {"namespace": "shop.orders",
                  "buildUUID": {"uuid": {"$uuid": "6f1c2b70-1f1a-4a3b-9f2e-0a1b2c3d4e5f"}},
                  "properties": {"v": 2, "key": {"status": 1, "createdAt": -1},
                                 "name": "status_1_createdAt_-1"}})
        self.add(t + timedelta(seconds=95), "I", "INDEX", 20440, "conn12",
                 "Index build: done building", {"namespace": "shop.orders"})

    def _slow_checkpoint(self, minute: float):
        t = self.at(minute)
        self.add(t, "W", "STORAGE", 22430, "Checkpointer", "WiredTiger message",
                 {"message": "checkpoint took 71 seconds to complete",
                  "durationMillis": 71000})

    def _errors(self, minute: float, n: int = 3):
        for i in range(n):
            t = self.at(minute + i * 0.4)
            self.add(t, "E", "QUERY", 23017, "conn%d" % self._conn,
                     "Plan executor error during find command",
                     {"error": {"code": 50, "codeName": "MaxTimeMSExpired",
                                "errmsg": "operation exceeded time limit"}})

    # -- build ------------------------------------------------------------
    def build(self) -> List[str]:
        self._startup()
        for i in range(6):
            self._connection(0.2 + i * 0.05)

        healthy = [t for t in _SLOW_TEMPLATES if not t[8]]
        heavy = [t for t in _SLOW_TEMPLATES if t[8]]

        # steady background traffic across the whole window
        per_minute = 3
        for m in range(self.minutes):
            for _ in range(per_minute):
                minute = m + self.rng.random()
                conn = 11 + self.rng.randint(0, 5)
                if self.scenario == "healthy":
                    tpl = self.rng.choice(healthy)
                elif self.scenario == "incident":
                    tpl = self.rng.choice(heavy if self.rng.random() < 0.75
                                          else healthy)
                else:
                    tpl = self.rng.choice(_SLOW_TEMPLATES)
                self._slow(minute, tpl, conn)

        if self.scenario in ("incident", "mixed"):
            mid = self.minutes * 0.55
            self._index_build(mid - 6)
            self._connection_storm(mid)
            self._election(mid + 1)
            self._errors(mid + 1.5)
            self._slow_checkpoint(mid + 2)
            # a burst of the worst shape right after the election
            for i in range(28):
                self._slow(mid + 2 + i / 90.0, _SLOW_TEMPLATES[0], 14)

        for i in range(4):
            t = self.at(self.minutes - 0.5 + i * 0.05)
            self.add(t, "I", "NETWORK", 22944, "conn%d" % (11 + i),
                     "Connection ended",
                     {"remote": "%s:%d" % (CLIENT_IPS[i % len(CLIENT_IPS)],
                                           40000 + i),
                      "connectionId": 11 + i,
                      "connectionCount": max(0, self._conn - i - 1)})

        self.lines.sort(key=lambda ln: json.loads(ln)["t"]["$date"])
        return self.lines


# ---------------------------------------------------------------- extras ---

DEMO_INDEXES = {
    "db": "shop",
    "generatedAt": "2026-07-01T09:30:00.000Z",
    "collections": {
        "orders": [
            {"v": 2, "key": {"_id": 1}, "name": "_id_"},
            {"v": 2, "key": {"createdAt": -1}, "name": "createdAt_-1"},
        ],
        "events": [
            {"v": 2, "key": {"_id": 1}, "name": "_id_"},
        ],
        "sessions": [
            {"v": 2, "key": {"_id": 1}, "name": "_id_"},
            {"v": 2, "key": {"active": 1}, "name": "active_1"},
        ],
        "users": [
            {"v": 2, "key": {"_id": 1}, "name": "_id_"},
            {"v": 2, "key": {"email": 1}, "name": "email_1", "unique": True},
        ],
        "products": [
            {"v": 2, "key": {"_id": 1}, "name": "_id_"},
        ],
    },
}

DEMO_SCHEMA = {
    "db": "shop",
    "generatedAt": "2026-07-01T09:30:00.000Z",
    "sampleSize": 100,
    "collections": {
        "orders": {"sampleSize": 100, "fields": {
            "_id": {"types": ["objectId"], "presence": 1.0},
            "status": {"types": ["string"], "presence": 1.0},
            "createdAt": {"types": ["date"], "presence": 1.0},
            "items": {"types": ["array"], "presence": 0.98},
            "total": {"types": ["double"], "presence": 1.0}}},
        "events": {"sampleSize": 100, "fields": {
            "tenantId": {"types": ["string"], "presence": 1.0},
            "ts": {"types": ["date"], "presence": 1.0},
            "kind": {"types": ["string"], "presence": 1.0}}},
        "sessions": {"sampleSize": 100, "fields": {
            "active": {"types": ["bool"], "presence": 1.0},
            "lastSeen": {"types": ["date"], "presence": 1.0}}},
        "users": {"sampleSize": 100, "fields": {
            "email": {"types": ["string"], "presence": 1.0}}},
        "products": {"sampleSize": 100, "fields": {
            "sku": {"types": ["string"], "presence": 1.0},
            "stock": {"types": ["int"], "presence": 1.0}}},
    },
}

DEMO_EXPLAIN = {
    "explainVersion": "1",
    "queryPlanner": {
        "namespace": "shop.orders",
        "parsedQuery": {"$and": [{"status": {"$eq": "pending"}},
                                 {"createdAt": {"$gt": "2026-06-30T00:00:00Z"}}]},
        "winningPlan": {
            "stage": "SORT",
            "sortPattern": {"createdAt": -1},
            "memLimit": 33554432,
            "type": "simple",
            "inputStage": {
                "stage": "COLLSCAN",
                "filter": {"$and": [{"status": {"$eq": "pending"}},
                                    {"createdAt": {"$gt": "2026-06-30T00:00:00Z"}}]},
                "direction": "forward"},
        },
        "rejectedPlans": [],
    },
    "executionStats": {
        "executionSuccess": True,
        "nReturned": 42,
        "executionTimeMillis": 1834,
        "totalKeysExamined": 0,
        "totalDocsExamined": 125000,
    },
    "command": {
        "find": "orders",
        "filter": {"status": "pending",
                   "createdAt": {"$gt": {"$date": "2026-06-30T00:00:00Z"}}},
        "sort": {"createdAt": -1},
        "limit": 50,
        "$db": "shop",
    },
    "ok": 1,
}


def write_extras(directory: str) -> List[str]:
    """Write indexes.json, schema.json and explain.json next to a demo log."""
    import os
    written = []
    for name, payload in (("indexes.json", DEMO_INDEXES),
                          ("schema.json", DEMO_SCHEMA),
                          ("explain.json", DEMO_EXPLAIN)):
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        written.append(path)
    return written
