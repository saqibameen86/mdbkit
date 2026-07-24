"""Rebuild a runnable mongosh command from a slow-query log line.

`mdbkit explain` needs an explain document, but getting one meant reading a
raw log line and hand-writing the query. This turns the log line back into
the command that produced it, ready to paste into mongosh.

Values in the log are the real values that ran, so treat the output as
sensitive: it is a query, not a redacted shape.
"""

from __future__ import annotations

import json
from typing import Optional

from .parser import LogEntry

# BSON/Extended-JSON wrappers we can render as mongosh constructors.
_EJSON = {
    "$oid": lambda v: 'ObjectId("%s")' % v,
    "$date": lambda v: 'ISODate("%s")' % v if isinstance(v, str)
    else "new Date(%s)" % v,
    "$numberLong": lambda v: "NumberLong(%s)" % v,
    "$numberInt": lambda v: str(v),
    "$numberDouble": lambda v: str(v),
    "$numberDecimal": lambda v: 'NumberDecimal("%s")' % v,
    "$uuid": lambda v: 'UUID("%s")' % v,
}


def _js(value, depth: int = 0) -> str:
    """Render a JSON value as mongosh-compatible JavaScript."""
    if depth > 30:
        return "{}"
    if isinstance(value, dict):
        if len(value) == 1:
            key = next(iter(value))
            if key in _EJSON:
                inner = value[key]
                if key == "$date" and isinstance(inner, dict):
                    inner = inner.get("$numberLong", 0)
                return _EJSON[key](inner)
            if key == "$binary":
                return '"<binary>"'
            if key == "$regularExpression" and isinstance(value[key], dict):
                pat = value[key].get("pattern", "")
                opts = value[key].get("options", "")
                return "/%s/%s" % (pat, opts)
            if key == "$timestamp":
                return "Timestamp(0, 0)"
        parts = ["%s: %s" % (json.dumps(k), _js(v, depth + 1))
                 for k, v in value.items()]
        return "{ " + ", ".join(parts) + " }"
    if isinstance(value, list):
        return "[ " + ", ".join(_js(v, depth + 1) for v in value) + " ]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value)


def to_mongosh(entry: LogEntry, explain: bool = True,
               verbosity: str = "executionStats") -> Optional[str]:
    """Rebuild the mongosh command for a slow-query log entry.

    Returns None if the entry is not a reconstructible operation.
    """
    attr = entry.attr or {}
    command = attr.get("command")
    if not isinstance(command, dict):
        return None

    ns = str(attr.get("ns") or "")
    db = command.get("$db") or (ns.split(".", 1)[0] if "." in ns else "")
    coll = None
    for key in ("find", "aggregate", "count", "distinct", "findAndModify",
                "update", "delete"):
        if isinstance(command.get(key), str):
            coll = command[key]
            break
    if coll is None and "." in ns:
        coll = ns.split(".", 1)[1]
    if not coll or not db:
        return None

    base = 'db.getSiblingDB("%s").getCollection("%s")' % (db, coll)
    suffix = '.explain("%s")' % verbosity if explain else ""

    if "find" in command:
        parts = [_js(command.get("filter") or {})]
        projection = command.get("projection")
        if projection:
            parts.append(_js(projection))
        call = "%s.find(%s)" % (base, ", ".join(parts))
        if command.get("sort"):
            call += ".sort(%s)" % _js(command["sort"])
        if command.get("skip"):
            call += ".skip(%s)" % command["skip"]
        if command.get("limit"):
            call += ".limit(%s)" % command["limit"]
        if command.get("hint"):
            call += ".hint(%s)" % _js(command["hint"])
        return call + suffix

    if "aggregate" in command:
        pipeline = command.get("pipeline") or []
        opts = {}
        if command.get("allowDiskUse") is not None:
            opts["allowDiskUse"] = command["allowDiskUse"]
        args = _js(pipeline)
        if opts:
            args += ", " + _js(opts)
        return "%s.aggregate(%s)%s" % (base, args, suffix)

    if "count" in command:
        return "%s.count(%s)%s" % (base, _js(command.get("query") or {}), suffix)

    if "distinct" in command:
        return '%s.distinct("%s", %s)%s' % (
            base, command.get("key", ""), _js(command.get("query") or {}), suffix)

    if "findAndModify" in command:
        spec = {k: command[k] for k in ("query", "sort", "update", "remove",
                                        "new", "upsert") if k in command}
        return "%s.findAndModify(%s)%s" % (base, _js(spec), suffix)

    if "update" in command:
        updates = command.get("updates") or []
        first = updates[0] if updates and isinstance(updates[0], dict) else {}
        if "q" not in first and "q" not in command:
            # Batched write logged at COMMAND level: the per-op predicate
            # lives in the paired WRITE entry, so there is nothing to rebuild.
            return None
        q = first.get("q", command.get("q") or {})
        u = first.get("u", command.get("u") or {})
        if explain:
            # explain() on a cursor is the reliable way to see the plan for
            # the update's query predicate.
            return ('%s.find(%s)%s   // update predicate; the update itself: '
                    '%s.updateMany(%s, %s)' % (
                        base, _js(q), suffix, base, _js(q), _js(u)))
        return "%s.updateMany(%s, %s)" % (base, _js(q), _js(u))

    if "delete" in command:
        deletes = command.get("deletes") or []
        first = deletes[0] if deletes and isinstance(deletes[0], dict) else {}
        if "q" not in first and "q" not in command:
            return None
        q = first.get("q", command.get("q") or {})
        if explain:
            return "%s.find(%s)%s   // delete predicate" % (base, _js(q), suffix)
        return "%s.deleteMany(%s)" % (base, _js(q))

    return None


HEADER = """// Rebuilt by mdbkit from a slow-query log line.
// Run in mongosh and redirect to a file, then analyze:
//   mongosh --quiet --host HOST --username USER --password PASS \\
//           --authenticationDatabase admin --eval "$(cat this_file.js)" > explain.json
//   mdbkit explain explain.json
// NOTE: contains the real query values from your log — treat as sensitive.
"""


def wrap_for_export(command: str) -> str:
    """Wrap a rebuilt command so its output is valid JSON for `mdbkit explain`."""
    return "%sEJSON.stringify(%s)\n" % (HEADER, command)
