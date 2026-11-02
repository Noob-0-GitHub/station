"""The one place Station reads wall-clock time.

Times follow the system time zone as an IANA zone from `tzlocal` ($TZ first,
then /etc/localtime), so DST gaps and repeats use the real zone rules. A change
to $TZ is picked up on the next call; a change to /etc/localtime needs a restart.
"""

from __future__ import annotations

import datetime as dt
import os
from zoneinfo import ZoneInfo

import tzlocal

_seen_tz: str | None = None


def zone() -> ZoneInfo:
    global _seen_tz
    env = os.environ.get("TZ")
    if env != _seen_tz:
        _seen_tz = env
        return tzlocal.reload_localzone()
    return tzlocal.get_localzone()


def now() -> dt.datetime:
    return dt.datetime.now(zone())


def at(ts: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts, zone())


def today() -> dt.date:
    return now().date()


def wall(day: dt.date, hour: int, minute: int) -> float:
    """Timestamp of a local wall-clock time. A time skipped by DST lands an hour later; a repeated one uses its first occurrence."""
    return dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone()).timestamp()
