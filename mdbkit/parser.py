"""Parser for MongoDB structured JSON logs (logv2, MongoDB 4.4+).

Every log line since MongoDB 4.4 is a single JSON document:

    {"t":{"$date":"..."},"s":"I","c":"COMMAND","id":51803,"ctx":"conn42",
     "msg":"Slow query","attr":{...}}

This module parses those lines defensively: real-world logs contain
truncated lines, interleaved plain-text output, and rotated .gz files.
Everything here is offline and dependency-free by design.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import sys
from dataclasses import dataclass, field
import math
import zlib
from datetime import datetime, timezone
from typing import Iterator, Optional

# Well-known logv2 message ids we care about.
ID_SLOW_QUERY = 51803
ID_CONN_ACCEPTED = 22943
ID_CONN_ENDED = 22944
ID_CLIENT_METADATA = 51800
ID_STARTUP = 4615611
ID_BUILD_INFO = 23403
# Authentication (ACCESS component). MongoDB has used several ids for these
# across versions, so the aggregator matches on id OR message text.
ID_AUTH_OK = 20250
ID_AUTH_OK_ALT = 5286306
ID_AUTH_FAIL = 20249
ID_LISTENING = 23016
ID_SHUTDOWN = 23138
# MongoDB 8.3+: an operation still running past slowOpInProgressThreshold.
# Logged under component SLOWPROG; it is NOT a completed slow query and must
# never be counted as one.
ID_SLOW_IN_PROGRESS = 1794200


# ------------------------------------------------------------- coercion ---
# Log attributes are written by mongod, but a log file is still untrusted
# input: lines get truncated, hand-edited, or produced by tools that bend the
# format. One odd value must never abort an analysis of a million lines, so
# every numeric read goes through these helpers.

def num(value, default=0):
    """Best-effort number from a log/BSON-ish value; `default` if unusable.

    Accepts ints, floats, numeric strings and Extended JSON wrappers such as
    {"$numberLong": "12"}. NaN, infinities, booleans-as-dicts and anything
    else non-numeric yield `default`.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else default
    if isinstance(value, dict):
        for key in ("$numberLong", "$numberInt", "$numberDouble",
                    "$numberDecimal"):
            if key in value:
                return num(value[key], default)
        return default
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text)
        except ValueError:
            try:
                f = float(text)
            except ValueError:
                return default
            return f if math.isfinite(f) else default
    return default


def text(value, default: str = "") -> str:
    """A string field that might not be a string."""
    if value is None:
        return default
    return value if isinstance(value, str) else str(value)


@dataclass
class LogEntry:
    """One parsed logv2 line."""

    ts: Optional[datetime]
    severity: str
    component: str
    msg_id: int
    ctx: str
    msg: str
    attr: dict = field(default_factory=dict)
    raw: str = ""
    tags: tuple = ()

    @property
    def is_slow_query(self) -> bool:
        return self.msg_id == ID_SLOW_QUERY or self.msg == "Slow query"


@dataclass
class ParseStats:
    """Bookkeeping for how much of the file we understood."""

    total_lines: int = 0
    parsed: int = 0
    unparsed: int = 0
    first_ts: Optional[datetime] = None
    last_ts: Optional[datetime] = None
    damaged: list = field(default_factory=list)

    @property
    def unparsed_ratio(self) -> float:
        return self.unparsed / self.total_lines if self.total_lines else 0.0


def _parse_ts(value) -> Optional[datetime]:
    """Parse a logv2 timestamp: {"$date": "ISO"} or {"$date": {"$numberLong": ms}}."""
    if isinstance(value, dict):
        value = value.get("$date", value)
    if isinstance(value, dict):  # {"$numberLong": "..."} (epoch millis)
        millis = value.get("$numberLong")
        if millis is not None:
            try:
                return datetime.fromtimestamp(int(millis) / 1000.0,
                                              tz=timezone.utc)
            except (ValueError, OSError, OverflowError, TypeError):
                return None
    if isinstance(value, str):
        try:
            ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        # logv2 always writes an offset. A timestamp without one comes from a
        # hand-edited or third-party line; treat it as UTC rather than mixing
        # naive and aware datetimes, which cannot be compared.
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    return None


def parse_line(line: str) -> Optional[LogEntry]:
    """Parse one line. Returns None for anything that isn't a logv2 JSON doc."""
    # A UTF-8 byte-order mark (Windows editors, PowerShell redirection) is
    # not whitespace to str.strip(), and it would silently cost us line 1 —
    # usually the startup line carrying version, host and dbPath.
    line = line.strip().lstrip("\ufeff")
    if not line or not line.startswith("{"):
        return None
    try:
        doc = json.loads(line)
    except (json.JSONDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(doc, dict) or "s" not in doc or "c" not in doc:
        return None
    attr = doc.get("attr")
    msg_id = num(doc.get("id"), 0)
    return LogEntry(
        ts=_parse_ts(doc.get("t")),
        severity=text(doc.get("s")),
        component=text(doc.get("c")),
        msg_id=int(msg_id) if isinstance(msg_id, (int, float)) else 0,
        ctx=text(doc.get("ctx")),
        msg=text(doc.get("msg")),
        attr=attr if isinstance(attr, dict) else {},
        raw=line,
        tags=tuple(text(t) for t in doc["tags"]) if isinstance(doc.get("tags"), list) else (),
    )


def open_log(path: str) -> io.TextIOBase:
    """Open a log file, stdin ('-'), or a rotated .gz transparently."""
    if path == "-":
        return sys.stdin
    if os.path.isdir(path):
        raise IsADirectoryError("%s is a directory, not a log file" % path)
    # Decide by content, not by name: logrotate and hand-copying both produce
    # ".gz" files that are not compressed, and compressed files without ".gz".
    with open(path, "rb") as probe:
        magic = probe.read(2)
    if magic == b"\x1f\x8b":
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8", errors="replace")
    return open(path, "r", encoding="utf-8", errors="replace")


def iter_entries(path: str, stats: Optional[ParseStats] = None) -> Iterator[LogEntry]:
    """Stream LogEntry objects from a file, tracking parse stats if given."""
    handle = open_log(path)
    try:
        for line in _lines_until_damage(handle, path, stats):
            if stats is not None:
                stats.total_lines += 1
            entry = parse_line(line)
            if entry is None:
                if stats is not None and line.strip():
                    stats.unparsed += 1
                continue
            if stats is not None:
                stats.parsed += 1
                if entry.ts is not None:
                    if stats.first_ts is None:
                        stats.first_ts = entry.ts
                    stats.last_ts = entry.ts
            yield entry
    finally:
        if handle is not sys.stdin:
            handle.close()


def _lines_until_damage(handle, path: str, stats: Optional["ParseStats"]):
    """Yield lines, stopping cleanly if a compressed file is truncated.

    A rotated log copied while it was still being compressed ends without a
    gzip trailer. Everything before the damage is valid and worth analysing;
    crashing on the last few kilobytes would throw all of it away.
    """
    try:
        for line in handle:
            yield line
    except (EOFError, gzip.BadGzipFile, zlib.error, OSError) as exc:
        if stats is not None:
            stats.damaged.append(path)
        sys.stderr.write(
            "warning: %s is truncated or corrupt (%s); analysed everything "
            "before the damage\n" % (path, str(exc) or type(exc).__name__))


PRE_44_HINT = (
    "Most lines in this file are not structured JSON. This looks like a "
    "pre-4.4 MongoDB log (plain text format). mdbkit targets MongoDB 4.4+ "
    "structured logs; for older logs, the original mtools still works."
)


def expand_paths(patterns) -> list:
    """Resolve a list of paths/globs into an ordered file list.

    Rotated logs are the normal case on a real server (mongod.log,
    mongod.log.1.gz, ...), so every command accepts several files or a
    glob and reads them as one stream. Files are ordered by name, which
    matches MongoDB's own rotation naming.
    """
    import glob as _glob
    import os
    if isinstance(patterns, str):
        patterns = [patterns]
    out = []
    for pattern in patterns:
        if pattern == "-":
            out.append(pattern)
            continue
        if any(ch in pattern for ch in "*?["):
            matches = sorted(_glob.glob(pattern))
            if not matches:
                raise FileNotFoundError("no files matched %r" % pattern)
            out.extend(matches)
        else:
            out.append(pattern)
    # De-duplicate while preserving order.
    seen = set()
    ordered = []
    for p in out:
        real = os.path.abspath(p) if p != "-" else p
        if real in seen:
            continue
        seen.add(real)
        ordered.append(p)
    return ordered


def iter_entries_multi(paths, stats: "ParseStats" = None):
    """Iterate entries across several log files as a single stream."""
    for path in expand_paths(paths):
        for entry in iter_entries(path, stats):
            yield entry
