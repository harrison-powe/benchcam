"""Time helpers.

Wall-clock times are stored as ISO 8601 strings (local time with offset) so the
session files stay human-readable. Elapsed time is computed from wall times so
that it still works across separate CLI invocations (``new`` and ``mark`` run as
different processes, so a process-local monotonic clock would not survive).
"""

from __future__ import annotations

from datetime import datetime


def now() -> datetime:
    """Return the current local time as a timezone-aware datetime."""
    return datetime.now().astimezone()


def to_iso(dt: datetime) -> str:
    """Serialize a datetime to an ISO 8601 string."""
    return dt.isoformat()


def from_iso(value: str) -> datetime:
    """Parse an ISO 8601 string back into a datetime."""
    return datetime.fromisoformat(value)


def folder_timestamp(dt: datetime) -> str:
    """Format a datetime for use in a session folder name (YYYY-MM-DD_HH-MM-SS)."""
    return dt.strftime("%Y-%m-%d_%H-%M-%S")
