"""Wake-up schedule: a per-thread interval, cron entries, and an optional morning.

Times are wall-clock times in the system time zone (see `clock`). Nothing here
sleeps; `due` and `next_wake` only compute times from saved state.
Cron expressions are parsed and stepped by `cronsim`.
"""

from __future__ import annotations

import datetime as dt
import re

from cronsim import CronSim, CronSimError

from . import clock

USAGE = "cron needs 5 fields: minute hour day month weekday, e.g. 30 22 * * *"


class Cron:
    """Five-field cron: minute hour day-of-month month day-of-week (0 or 7 = Sunday).

    When both day fields are restricted, either one matching is enough (Vixie cron).
    DST (cronsim's rules): a time skipped by the clock change fires at the first
    minute after the gap; a repeated time with a fixed hour fires once, at its first
    occurrence, while a wildcard hour (`0 * * * *`) fires in both repeated hours.
    """

    def __init__(self, expr: str):
        self.expr = " ".join(expr.split())
        if len(self.expr.split()) != 5:
            raise ValueError(USAGE)
        try:
            CronSim(self.expr, clock.now())
        except CronSimError as e:
            # cronsim also rejects dates that can never happen, such as Feb 31.
            raise ValueError(f"bad cron expression {self.expr!r}: {e}. {USAGE}") from None

    def next_after(self, ts: float) -> float | None:
        """First matching minute strictly after `ts`, or None if it never matches."""
        try:
            return next(CronSim(self.expr, clock.at(ts))).timestamp()
        except (StopIteration, CronSimError):
            return None


def next_morning(ts: float, morning_at: str) -> float:
    """Next configured HH:MM strictly after `ts`, using local wall-clock rules."""
    if not isinstance(morning_at, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", morning_at):
        raise ValueError("morning_at must be a time in strict HH:MM format (00:00 to 23:59)")
    hour, minute = map(int, morning_at.split(":"))
    day = clock.at(ts).date()
    t = clock.wall(day, hour, minute)
    return t if t > ts else clock.wall(day + dt.timedelta(days=1), hour, minute)


def next_interval(interval: dict, since: float, cursor: float) -> float | None:
    """Next interval wake after `cursor`, counted from `since` (when the turn ended)."""
    step = float(interval["minutes"]) * 60
    first = since + step
    if first > cursor:
        return first
    if interval.get("once"):
        return None
    k = int((cursor - since) // step) + 1
    return since + k * step


def candidates(schedule: dict, interval: dict | None, since: float, cursor: float, morning_at: str | None = None) -> list[tuple[float, str, dict | None]]:
    """Every source's next wake as (time, kind, entry). Times may be in the past (overdue).

    Cron and morning count from their own `last` (the last wake that handled
    them), so a time that passed while the AI was awake is still owed. The
    interval counts from when this sleep started. `morning_at=None` disables
    the morning source.
    """
    out = []
    if morning_at is not None:
        out.append((next_morning(schedule["morning_last"], morning_at), "morning", None))
    if interval:
        t = next_interval(interval, since, cursor)
        if t is not None:
            out.append((t, "interval", interval))
    for entry in schedule["cron"]:
        t = Cron(entry["expr"]).next_after(entry["last"])
        if t is not None:
            out.append((t, "cron", entry))
    return out


def next_wake(schedule: dict, interval: dict | None, since: float, cursor: float, morning_at: str | None = None) -> float | None:
    """Earliest source's wake, or None when no source has another wake."""
    return min((t for t, _, _ in candidates(schedule, interval, since, cursor, morning_at)), default=None)


def due(schedule: dict, interval: dict | None, since: float, cursor: float, now: float, morning_at: str | None = None) -> list[tuple[float, str, dict | None]]:
    """Sources due by `now`. Several sources due at once make a single wake."""
    return [c for c in candidates(schedule, interval, since, cursor, morning_at) if c[0] <= now]
