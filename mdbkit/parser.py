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
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator, Optional

# Well-known logv2 message ids we care about.
ID_SLOW_QUERY = 51803
ID_CONN_ACCEPTED = 22943
ID_CONN_ENDED = 22944
ID_CLIENT_METADATA = 51800
ID_STARTUP = 4615611
ID_BUILD_INFO = 23403


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
                return datetime.fromtimestamp(int(millis) / 1000.0).astimezone()
            except (ValueError, OSError):
                return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def parse_line(line: str) -> Optional[LogEntry]:
    """Parse one line. Returns None for anything that isn't a logv2 JSON doc."""
    line = line.strip()
    if not line or not line.startswith("{"):
        return None
    try:
        doc = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(doc, dict) or "s" not in doc or "c" not in doc:
        return None
    return LogEntry(
        ts=_parse_ts(doc.get("t")),
        severity=str(doc.get("s", "")),
        component=str(doc.get("c", "")),
        msg_id=int(doc.get("id", 0) or 0),
        ctx=str(doc.get("ctx", "")),
        msg=str(doc.get("msg", "")),
        attr=doc.get("attr") or {},
        raw=line,
    )


def open_log(path: str) -> io.TextIOBase:
    """Open a log file, stdin ('-'), or a rotated .gz transparently."""
    if path == "-":
        return sys.stdin
    if path.endswith(".gz"):
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8", errors="replace")
    # Sniff gzip magic bytes even without the extension.
    with open(path, "rb") as probe:
        magic = probe.read(2)
    if magic == b"\x1f\x8b":
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8", errors="replace")
    return open(path, "r", encoding="utf-8", errors="replace")


def iter_entries(path: str, stats: Optional[ParseStats] = None) -> Iterator[LogEntry]:
    """Stream LogEntry objects from a file, tracking parse stats if given."""
    handle = open_log(path)
    try:
        for line in handle:
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


PRE_44_HINT = (
    "Most lines in this file are not structured JSON. This looks like a "
    "pre-4.4 MongoDB log (plain text format). mdbkit targets MongoDB 4.4+ "
    "structured logs; for older logs, the original mtools still works."
)
