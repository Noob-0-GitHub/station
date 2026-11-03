"""Wake schedule: cron parsing, next-wake selection, merging, hooks, time zone, and board limits."""

import datetime as dt
import os
import time

import pytest

from station import clock
from station.board import Board
from station.runner import Runner
from station.schedule import Cron, candidates, due, next_morning, next_wake

from test_loop import make, use


def ts(y, mo, d, h=0, mi=0):
    return clock.wall(dt.date(y, mo, d), h, mi)


def at(t):
    return clock.at(t).replace(tzinfo=None)


def sched(cron=(), morning_last=None):
    return {"cron": list(cron), "morning_last": morning_last or ts(2026, 10, 31, 9, 0), "next_id": 1}


def test_cron_daily_and_weekdays():
    assert at(Cron("30 22 * * *").next_after(ts(2026, 10, 31, 12))) == dt.datetime(2026, 10, 31, 22, 30)
    assert at(Cron("30 22 * * *").next_after(ts(2026, 10, 31, 22, 30))) == dt.datetime(2026, 11, 1, 22, 30)
    # 2026-10-31 is a Saturday; the next weekday is Monday 11-02.
    assert at(Cron("0 9 * * 1-5").next_after(ts(2026, 10, 31, 12))) == dt.datetime(2026, 11, 2, 9, 0)
    assert at(Cron("*/15 * * * *").next_after(ts(2026, 10, 31, 12, 7))) == dt.datetime(2026, 10, 31, 12, 15)
    assert at(Cron("0 0 * * 7").next_after(ts(2026, 10, 31, 12))) == dt.datetime(2026, 11, 1, 0, 0)


def test_cron_day_fields_are_or_when_both_set():
    # 13th of the month OR a Friday, like Vixie cron.
    assert at(Cron("0 12 13 * 5").next_after(ts(2026, 10, 31, 13))) == dt.datetime(2026, 11, 6, 12, 0)


@pytest.mark.parametrize("bad", ["", "* * * *", "60 * * * *", "* 24 * * *", "a * * * *", "*/0 * * * *"])
def test_cron_rejects_bad_expressions(bad):
    with pytest.raises(ValueError):
        Cron(bad)


def test_cron_that_never_fires_is_rejected():
    with pytest.raises(ValueError):
        Cron("0 0 31 2 *")


def test_cron_spacing_is_normalized():
    assert Cron("30  22 *  * *").expr == "30 22 * * *"


@pytest.fixture
def los_angeles():
    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def test_morning_across_dst(los_angeles):
    # DST ends 2026-11-01 02:00. 09:00 on 10-31 is 16:00 UTC; on 11-01 it is 17:00 UTC.
    before = dt.datetime.fromtimestamp(next_morning(ts(2026, 10, 30, 12), morning_at="09:00"), dt.timezone.utc)
    after = dt.datetime.fromtimestamp(next_morning(ts(2026, 10, 31, 12), morning_at="09:00"), dt.timezone.utc)
    assert (before.day, before.hour, after.day, after.hour) == (31, 16, 1, 17)


def utc(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).replace(tzinfo=None)


def test_cron_across_dst(los_angeles):
    # Spring forward 2026-03-08: 02:30 does not exist, so it fires at 03:00 PDT (10:00 UTC).
    assert utc(Cron("30 2 * * *").next_after(ts(2026, 3, 7, 12))) == dt.datetime(2026, 3, 8, 10, 0)
    # Fall back 2026-11-01: 01:30 happens twice and fires once, at the first (PDT, 08:30 UTC).
    first = Cron("30 1 * * *").next_after(ts(2026, 10, 31, 12))
    assert utc(first) == dt.datetime(2026, 11, 1, 8, 30)
    assert utc(Cron("30 1 * * *").next_after(first)) == dt.datetime(2026, 11, 2, 9, 30)
    # Wall-clock 22:30 stays 22:30 across the change.
    assert at(Cron("30 22 * * *").next_after(ts(2026, 11, 1, 12))) == dt.datetime(2026, 11, 1, 22, 30)


def test_next_wake_with_morning_enabled():
    now = ts(2026, 10, 31, 8)
    s = sched([{"id": 1, "expr": "30 8 * * *", "last": now}], morning_last=ts(2026, 10, 30, 9))
    assert next_wake(s, None, now, now, morning_at="09:00") == ts(2026, 10, 31, 8, 30)
    assert next_wake(sched(morning_last=ts(2026, 10, 30, 9)), None, now, now, morning_at="09:00") == ts(2026, 10, 31, 9)
    assert next_wake(sched(), {"minutes": 10}, now, now + 600, morning_at="09:00") == now + 1200
    assert next_wake(sched(), {"minutes": 10, "once": True}, now, now + 600, morning_at="09:00") == ts(2026, 11, 1, 9)


@pytest.mark.parametrize("morning_at", ["00:00", "07:45", "12:30", "23:59"])
def test_configured_morning_time(morning_at):
    hour, minute = map(int, morning_at.split(":"))
    last = ts(2026, 10, 30, hour, minute)
    expected = ts(2026, 10, 31, hour, minute)
    s = sched(morning_last=last)
    assert next_morning(last, morning_at) == expected
    assert next_morning(expected - 1, morning_at) == expected
    assert next_morning(expected, morning_at) == ts(2026, 11, 1, hour, minute)
    assert candidates(s, None, last, last, morning_at=morning_at) == [(expected, "morning", None)]
    assert next_wake(s, None, last, last, morning_at=morning_at) == expected
    assert due(s, None, last, last, expected - 1, morning_at=morning_at) == []
    assert due(s, None, last, last, expected, morning_at=morning_at) == [(expected, "morning", None)]


@pytest.mark.parametrize("bad", [
    "", "9:00", "09:0", "009:00", "24:00", "23:60", "-1:00", "09:00:00",
    " 09:00", "09:00 ", "09:00\n", "09\n00", "09.00", "０９:００", None, 900, False,
])
def test_morning_rejects_invalid_strict_formats(bad):
    with pytest.raises(ValueError, match="HH:MM"):
        next_morning(ts(2026, 10, 31), bad)


@pytest.mark.parametrize("compute", [candidates, next_wake, due])
def test_schedule_rejects_invalid_configured_morning(compute):
    now = ts(2026, 10, 31)
    args = (sched(), None, now, now)
    if compute is due:
        args += (now,)
    with pytest.raises(ValueError, match="HH:MM"):
        compute(*args, morning_at="9:00")


def test_empty_schedule_has_no_wake_by_default():
    now = ts(2026, 10, 31, 8)
    # Disabled morning does not even require a persisted morning_last.
    s = {"cron": []}
    assert candidates(s, None, now, now) == []
    assert next_wake(s, None, now, now) is None
    assert due(s, None, now, now, now + 86400) == []
    assert next_wake(s, {"minutes": 10, "once": True}, now, now + 600) is None


def test_null_morning_disables_only_morning():
    now = ts(2026, 10, 31, 8)
    s = sched([{"id": 1, "expr": "30 8 * * *", "last": now}], morning_last=ts(2026, 10, 30, 9))
    interval = {"minutes": 10}
    expected = [(now + 600, "interval", interval), (now + 1800, "cron", s["cron"][0])]
    assert candidates(s, interval, now, now, morning_at=None) == expected
    assert next_wake(s, interval, now, now, morning_at=None) == now + 600
    assert due(s, interval, now, now, ts(2026, 10, 31, 10), morning_at=None) == expected
    assert candidates(sched(), None, now, now, morning_at=None) == []
    assert next_wake(sched(), None, now, now, morning_at=None) is None
    assert due(sched(), None, now, now, ts(2026, 11, 2), morning_at=None) == []


def test_morning_spring_gap_not_due_before_actual_time(los_angeles):
    last = ts(2026, 3, 7, 2, 30)
    s = sched(morning_last=last)
    expected = ts(2026, 3, 8, 3, 30)
    # clock.wall maps the nonexistent 02:30 to 03:30, not cron's 03:00.
    assert utc(next_morning(last, "02:30")) == dt.datetime(2026, 3, 8, 10, 30)
    assert next_wake(s, None, last, last, morning_at="02:30") == expected
    assert due(s, None, last, last, ts(2026, 3, 8, 3), morning_at="02:30") == []
    assert due(s, None, last, last, expected - 1, morning_at="02:30") == []
    assert due(s, None, last, last, expected, morning_at="02:30") == [(expected, "morning", None)]
    # During the gap's normalized hour, today's actual wake is still ahead.
    assert next_morning(ts(2026, 3, 8, 3), "02:30") == expected


def test_morning_fall_repeat_fires_only_once(los_angeles):
    last = ts(2026, 10, 31, 1, 30)
    first = next_morning(last, "01:30")
    assert utc(first) == dt.datetime(2026, 11, 1, 8, 30)
    assert utc(next_morning(first, "01:30")) == dt.datetime(2026, 11, 2, 9, 30)
    s = sched(morning_last=first)
    assert due(s, None, first, first, first + 3600, morning_at="01:30") == []


def test_sources_due_together_are_one_wake():
    nine = ts(2026, 10, 31, 9)
    s = sched([{"id": 1, "expr": "0 9 * * *", "last": nine - 3600}], morning_last=nine - 86400)
    fired = due(s, {"minutes": 60}, nine - 3600, nine - 3600, nine, morning_at="09:00")
    assert sorted(kind for _, kind, _ in fired) == ["cron", "interval", "morning"]


def test_schedule_tool(tmp_path):
    life = make(tmp_path, [
        [use("register", "r", name="Lin")],
        [use("schedule", "s1", action="interval", minutes=45, once=True, note="check the kettle")],
        [use("schedule", "s2", action="cron", expr="30 22 * * *", note="read a bit")],
        [use("schedule", "s3", action="cron", expr="30  22 * * *", note="watch a film instead", once=True)],
        [use("schedule", "s4", action="cron", expr="nope")],
        [use("schedule", "s5", action="list")],
    ])
    life.cfg["schedule"]["morning_at"] = "09:00"
    life.run(max_calls=6)
    reqs = life.llm.requests

    def result(i):
        return reqs[i]["messages"][-1]["content"][0]

    assert result(3)["content"].startswith("Added #1")
    assert "#1 is already 30 22 * * *" in result(4)["content"]
    assert result(5)["is_error"]
    cron = life.state["schedule"]["cron"]
    assert len(cron) == 1 and cron[0]["note"] == "watch a film instead" and cron[0]["once"]
    board = reqs[5]["messages"][-1]["content"][-1]["text"]
    assert "every 45 min (once)" in board and "daily 09:00" in board and "wake at" in board


def test_schedule_list(tmp_path):
    life = make(tmp_path, [
        [use("register", "r", name="Lin")],
        [use("schedule", "s1", action="interval", minutes=45, note="check the kettle")],
        [use("schedule", "s2", action="cron", expr="30 22 * * *", once=True)],
        [use("schedule", "s3", action="list")],
        [use("bash", "b", command="true")],
    ])
    life.cfg["schedule"]["morning_at"] = "09:00"
    life.run(max_calls=5)
    listing = life.llm.requests[4]["messages"][-1]["content"][0]["content"]
    assert "note: check the kettle" in listing and "cron #1 30 22 * * * (once)" in listing
    assert "PDT" in listing or "PST" in listing


def test_hook_exit_zero_skips_one_wake(tmp_path):
    hook = "[ -f flag ] || { touch flag; exit 0; }; echo something new; exit 1"
    life = make(tmp_path, [
        [use("register", "r", name="Lin")],
        [use("schedule", "s", action="interval", minutes=0.002, hook=hook, note="look")],
        [use("schedule", "sleep", action="sleep")],
        [use("bash", "b", command="true")],
    ])
    life.cfg["schedule"]["morning_at"] = "09:00"
    life.run(max_calls=4)
    woke = life.llm.requests[3]["messages"][-1]["content"][0]["text"]
    assert "exit code 1: something new" in woke and ": look." in woke
    log = (tmp_path / "state" / "log.jsonl").read_text()
    assert log.count('"kind": "hook"') == 2


def test_board_total_limit_cuts_later_panels(tmp_path):
    board = Board(tmp_path / "board", Runner(tmp_path), max_chars=700)
    board.tool("write", "a", text="---\norder: 1\n---\n" + "甲" * 300)
    board.tool("write", "b", text="---\norder: 2\n---\n" + "乙" * 500)
    board.tool("write", "c", text="---\norder: 3\n---\n" + "丙" * 50)
    out = board.render({})
    assert "甲" * 300 in out and "乙" * 500 not in out and "丙" not in out
    assert "cut b" in out and "hid c" in out


def test_panel_default_limit(tmp_path):
    board = Board(tmp_path / "board", Runner(tmp_path), panel_max_chars=100)
    board.tool("write", "long", text="x" * 500)
    assert "omitted" in board.render({})
