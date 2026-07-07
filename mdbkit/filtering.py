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
        if self.ts_from and (entry.ts is None or entry.ts < self.ts_from):
            return False
        if self.ts_to and (entry.ts is None or entry.ts > self.ts_to):
            return False
        if self.msg_contains and self.msg_contains not in entry.msg.lower():
            return False
        return True


def parse_when(value: str) -> datetime:
    """Parse a --from/--to value (ISO 8601, 'Z' allowed)."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
