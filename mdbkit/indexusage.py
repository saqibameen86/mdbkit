"""`mdbkit indexes` — indexes that may not be earning their keep.

Every index costs write time, memory and disk. This reads what
`mdbkit export-script indexes` produced (index definitions and, where
available, $indexStats usage counters) and reports:

* unused indexes: no recorded use since the counters started;
* redundant indexes: a prefix of another index that can serve the same
  queries;
* indexes it deliberately does not judge: _id, unique, TTL, hidden, the
  shard key.

It never recommends dropping anything. Usage counters are kept per member
and reset on restart, so "unused" only means "unused here, since then". The
reversible next step is hideIndex: the planner stops using the index, the
index is still maintained, and unhideIndex brings it back instantly.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from .parser import num, text

SPECIAL = ("text", "2d", "2dsphere", "hashed", "geoHaystack")


@dataclass
class IndexInfo:
    ns: str
    name: str
    key: List[Tuple[str, object]]
    spec: dict
    ops: Optional[int] = None            # summed over hosts, None = no data
    since: Optional[datetime] = None     # latest counter start among hosts
    hosts: List[str] = field(default_factory=list)

    @property
    def unique(self) -> bool:
        return bool(self.spec.get("unique"))

    @property
    def ttl(self) -> bool:
        return "expireAfterSeconds" in self.spec

    @property
    def hidden(self) -> bool:
        return bool(self.spec.get("hidden"))

    @property
    def partial(self) -> bool:
        return "partialFilterExpression" in self.spec

    @property
    def sparse(self) -> bool:
        return bool(self.spec.get("sparse"))

    @property
    def special(self) -> bool:
        return any(isinstance(v, str) for _, v in self.key) or \
            any(f.endswith("$**") or f == "$**" for f, _ in self.key)

    def key_str(self) -> str:
        return "{ %s }" % ", ".join("%s: %s" % (f, _fmt(v)) for f, v in self.key)


@dataclass
class Finding:
    kind: str           # unused, redundant, not-judged, duplicate
    index: IndexInfo
    reason: str
    covered_by: Optional[IndexInfo] = None

    def to_dict(self) -> dict:
        i = self.index
        return {"kind": self.kind, "ns": i.ns, "index": i.name, "key": i.key_str(),
                "ops": i.ops, "since": i.since.isoformat() if i.since else None,
                "reason": self.reason,
                "coveredBy": self.covered_by.name if self.covered_by else None,
                "hideCommand": hide_command(i) if self.kind in ("unused", "redundant") else None}


@dataclass
class Report:
    findings: List[Finding]
    indexes: int
    collections: int
    has_usage: bool
    window_days: Optional[float]
    sources: List[dict]
    generated: Optional[datetime]

    def to_dict(self) -> dict:
        return {"indexes": self.indexes, "collections": self.collections,
                "usageAvailable": self.has_usage,
                "counterWindowDays": (round(self.window_days, 1)
                                      if self.window_days is not None else None),
                "sources": self.sources,
                "findings": [f.to_dict() for f in self.findings]}


def _fmt(v) -> str:
    if isinstance(v, str):
        return '"%s"' % v
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _date(v) -> Optional[datetime]:
    if isinstance(v, dict):
        v = v.get("$date", v.get("$numberLong"))
        if isinstance(v, dict):
            v = v.get("$numberLong")
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        try:
            return datetime.fromtimestamp(v / 1000.0, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(v, str) and v:
        try:
            d = datetime.fromisoformat(v.replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def hide_command(i: IndexInfo) -> str:
    db, coll = i.ns.split(".", 1) if "." in i.ns else ("<db>", i.ns)
    return 'db.getSiblingDB("%s").getCollection("%s").hideIndex("%s")' % (db, coll, i.name)


def load(paths: List[str]) -> Tuple[Dict[str, List[IndexInfo]], List[dict],
                                    Optional[datetime], Dict[str, list], bool]:
    """Read one or more index exports (e.g. one per replica set member) and
    merge their usage counters."""
    from .shelljson import loads_lenient, read_text_file
    by_ns: Dict[str, Dict[str, IndexInfo]] = defaultdict(dict)
    usage_seen: Dict[Tuple[str, str], Dict[str, Tuple[int, Optional[datetime]]]] = \
        defaultdict(dict)
    sources, shard_keys = [], {}
    generated = None
    has_usage = False
    for path in paths:
        data = loads_lenient(read_text_file(path))
        if not isinstance(data, dict) or not isinstance(data.get("collections"), dict):
            raise ValueError("%s is not an `mdbkit export-script indexes` file" % path)
        db = text(data.get("db"))
        gen = _date(data.get("generatedAt"))
        if gen and (generated is None or gen > generated):
            generated = gen
        if isinstance(data.get("source"), dict):
            sources.append(data["source"])
        for ns, idx_list in data["collections"].items():
            full = "%s.%s" % (db, ns) if db else ns
            if not isinstance(idx_list, list):
                continue
            for idx in idx_list:
                if not isinstance(idx, dict) or not idx.get("name"):
                    continue
                key = idx.get("key") if isinstance(idx.get("key"), dict) else {}
                by_ns[full].setdefault(text(idx["name"]), IndexInfo(
                    full, text(idx["name"]), list(key.items()), idx))
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        for ns, entries in usage.items():
            full = "%s.%s" % (db, ns) if db else ns
            if not isinstance(entries, list):
                continue
            has_usage = True
            for u in entries:
                if not isinstance(u, dict):
                    continue
                host = text(u.get("host")) or text(u.get("shard")) or path
                usage_seen[(full, text(u.get("name")))][host] = (
                    int(max(0, num(u.get("ops")))), _date(u.get("since")))
        keys = data.get("shardKeys") if isinstance(data.get("shardKeys"), dict) else {}
        for ns, key in keys.items():
            if isinstance(key, dict):
                shard_keys[ns] = list(key.items())
    for (ns, name), per_host in usage_seen.items():
        info = by_ns.get(ns, {}).get(name)
        if info is None:
            continue
        info.ops = sum(ops for ops, _ in per_host.values())
        starts = [s for _, s in per_host.values() if s]
        info.since = max(starts) if starts else None
        info.hosts = sorted(per_host)
    return ({ns: list(v.values()) for ns, v in by_ns.items()}, sources, generated,
            shard_keys, has_usage)


def _is_prefix(short: List[Tuple[str, object]], long: List[Tuple[str, object]]) -> bool:
    if len(short) >= len(long):
        return False
    return all(a == b for a, b in zip(short, long))


def analyse(paths: List[str], ns_filter: Optional[str] = None) -> Report:
    by_ns, sources, generated, shard_keys, has_usage = load(paths)
    findings: List[Finding] = []
    total = 0
    windows = []
    for ns in sorted(by_ns):
        if ns_filter and ns != ns_filter:
            continue
        idxs = by_ns[ns]
        total += len(idxs)
        skey = shard_keys.get(ns)
        # The shard key needs an index that starts with it. When one is
        # exactly the shard key, that is the one to keep; otherwise every
        # index starting with the shard key may be the one doing the job.
        exact = bool(skey) and any(i.key == skey and not i.hidden for i in idxs)
        for i in idxs:
            if i.since and generated:
                windows.append((generated - i.since).total_seconds() / 86400.0)
        for i in idxs:
            protected = None
            if i.name == "_id_":
                continue
            if skey and (i.key == skey or (not exact and i.key[:len(skey)] == skey)):
                protected = "supports the shard key"
            elif i.unique:
                protected = "enforces a unique constraint, whether or not queries use it"
            elif i.ttl:
                protected = "is a TTL index: the TTL monitor uses it, which the counters do not show"
            elif i.hidden:
                protected = "is already hidden (an unhide/drop decision is in progress)"
            if protected:
                if i.ops == 0:
                    findings.append(Finding("not-judged", i, "unused for queries, but it " + protected))
                continue
            # redundant: a strict prefix of another index that can do its job
            cover = None
            if not (i.special or i.partial or i.sparse or i.spec.get("collation")):
                for other in idxs:
                    if other is i or other.partial or other.sparse or other.hidden \
                            or other.special:
                        continue
                    if other.ops == 0:
                        # an unused index is itself a candidate: advising to
                        # hide both would leave the queries with neither
                        continue
                    if other.spec.get("collation") != i.spec.get("collation"):
                        continue
                    if _is_prefix(i.key, other.key):
                        cover = other
                        break
            if cover is not None:
                findings.append(Finding(
                    "redundant", i,
                    "its key is a prefix of %s %s, which can serve the same queries"
                    % (cover.name, cover.key_str()), cover))
            elif i.ops == 0:
                findings.append(Finding("unused", i, "no recorded use since the counters started"))
    window = min(windows) if windows else None
    order = {"unused": 0, "redundant": 1, "not-judged": 2}
    findings.sort(key=lambda f: (order.get(f.kind, 9), f.index.ns, f.index.name))
    return Report(findings, total, len([n for n in by_ns if not ns_filter or n == ns_filter]),
                  has_usage, window, sources, generated)


def render(rep: Report, min_days: int = 7) -> str:
    out = ["== mdbkit indexes ==",
           "%d index(es) on %d collection(s)." % (rep.indexes, rep.collections)]
    if rep.sources:
        where = []
        for s in rep.sources:
            role = ("mongos" if s.get("isMongos") else
                    "primary" if s.get("isPrimary") else "secondary")
            me = text(s.get("me"))
            if not me and s.get("isMongos"):
                where.append("a mongos (usage from each shard's primary)")
                continue
            where.append("%s (%s%s)" % (me or "?", role,
                                        ", " + text(s["setName"]) if s.get("setName") else ""))
        out.append("exported from: %s" % "; ".join(where))
    if not rep.has_usage:
        out.append("usage: not in this file (re-export with `mdbkit export-script "
                   "indexes` from mdbkit 0.8+, as a user allowed to run $indexStats).")
        out.append("       Only redundant indexes can be judged without it.")
    elif rep.window_days is not None:
        out.append("usage counters cover the last %.1f day(s) (since the most "
                   "recent restart of any member exported)." % rep.window_days)
        if rep.window_days < min_days:
            out.append("  ! that is under %d days: an index used weekly or monthly "
                       "can look unused. Re-export later before acting." % min_days)
    out.append("")
    groups = [("unused", "Unused since the counters started"),
              ("redundant", "Redundant: a prefix of another index"),
              ("not-judged", "Unused, but not a candidate")]
    any_found = False
    for kind, title in groups:
        items = [f for f in rep.findings if f.kind == kind]
        if not items:
            continue
        any_found = True
        out.append("%s (%d)" % (title, len(items)))
        for f in items:
            i = f.index
            use = ("%s use(s)" % format(i.ops, ",")) if i.ops is not None else "usage unknown"
            out.append("  %s  %s %s — %s" % (i.ns, i.name, i.key_str(), use))
            out.append("      %s" % f.reason)
            if kind in ("unused", "redundant"):
                out.append("      test safely: %s" % hide_command(i))
        out.append("")
    if not any_found:
        out.append("Nothing to report: every index was used or is not a candidate.")
        out.append("")
    out.append("These are candidates to examine, not to drop. Usage counters are "
               "per member and reset on restart, so check every member (secondaries "
               "serve reads too) and a long enough period. hideIndex makes the "
               "planner ignore an index while it is still maintained; if nothing "
               "slows down, it was not needed; db.collection.unhideIndex(name) "
               "undoes it instantly.")
    return "\n".join(out)
