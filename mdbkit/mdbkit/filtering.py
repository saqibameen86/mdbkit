"""`mdbkit filter` — the mlogfilter successor.

Streams matching raw logv2 lines to stdout so output stays valid JSON logs
and can be chained: `mdbkit filter f.log --slow 500 | mdbkit queries -`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from .parser import LogEntry


class Filter:
    def __init__(
        self,
        component: Optional[str] = None,
        severity: Optional[str] = None,
        namespace: Optional[str] = None,
        slow_ms: Optional[int] = None,
        ts_from: Optional[datetime] = None,
        ts_to: Optional[datetime] = None,
        msg_contains: Optional[str] = None,
    ):
        self.component = component.upper() if component else None
        self.severity = severity.upper() if severity else None
        self.namespace = namespace
        self.slow_ms = slow_ms
        self.ts_from = ts_from
        self.ts_to = ts_to
        self.msg_contains = msg_contains.lower() if msg_contains else None

    def matches(self, entry: LogEntry) -> bool:
        if self.component and entry.component.upper() != self.component:
            return False
        if self.severity and entry.severity.upper() != self.severity:
            return False
        if self.namespace and entry.attr.get("ns") != self.namespace:
            return False
        if self.slow_ms is not None:
            if int(entry.attr.get("durationMillis", -1) or -1) < self.slow_ms:
                return False
        if self.ts_from is not None:
            if entry.ts is None or _lt(entry.ts, self.ts_from):
                return False
        if self.ts_to is not None:
            if entry.ts is None or _lt(self.ts_to, entry.ts):
                return False
        if self.msg_contains and self.msg_contains not in entry.msg.lower():
            return False
        return True


def _lt(a: datetime, b: datetime) -> bool:
    """Compare two datetimes safely when one may be naive.

    Log timestamps carry an offset (e.g. +04:00); a user-supplied bound may
    not. Comparing them directly raises TypeError, so a naive value is
    interpreted in the other value's timezone (i.e. "local wall clock").
    """
    if (a.tzinfo is None) != (b.tzinfo is None):
        if a.tzinfo is None:
            a = a.replace(tzinfo=b.tzinfo)
        else:
            b = b.replace(tzinfo=a.tzinfo)
    return a < b


def parse_when(value: str) -> datetime:
    """Parse a --from/--to value.

    Accepts, with or without a timezone offset:
        2026-07-01T08:00:00+04:00   2026-07-01T08:00:00Z
        2026-07-01T08:00:00         2026-07-01 08:00:00
        2026-07-01T08:00            2026-07-01
    A value without an offset is treated as wall-clock time in the log's own
    timezone.
    """
    raw = value.strip().replace("Z", "+00:00").replace(" ", "T")
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    raise ValueError(
        "Could not parse timestamp %r. Examples: 2026-07-01T08:00:00+04:00, "
        "2026-07-01T08:00:00Z, 2026-07-01T08:00:00, 2026-07-01" % value)
