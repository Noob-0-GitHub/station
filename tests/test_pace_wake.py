"""Pause between rounds (pace.gap_seconds) and `station say --wake`."""

import threading
import time

from station import cli
from station.config import DEFAULTS
from station.inbox import Inbox

from test_loop import make, use


def test_default_gap_is_one_minute():
    import importlib

    from station import config

    fresh = importlib.reload(config)  # conftest zeroes the default for speed; check the shipped value
    assert fresh.DEFAULTS["pace"]["gap_seconds"] == 60


def test_message_during_a_tool_joins_its_result(tmp_path):
    life = make(tmp_path, [[use("register", "r", name="Lin")],
                           [use("bash", "b", command="sleep 0.3; echo done")],
                           [use("bash", "c", command="true")]])
    life.cfg["pace"]["gap_seconds"] = 0.6
    # Sent while the tool runs; with the pause it arrives before the next call is composed.
    threading.Timer(0.5, lambda: Inbox(tmp_path / "inbox").put("message", "Anthony", "one more thing")).start()
    life.run(max_calls=3)
    content = life.llm.requests[2]["messages"][-1]["content"]
    assert content[0]["type"] == "tool_result" and "done" in str(content[0]["content"])
    assert any("Anthony: one more thing" in b.get("text", "") for b in content)


def test_pause_also_follows_a_text_reply(tmp_path):
    life = make(tmp_path, [[use("register", "r", name="Lin")], [{"type": "text", "text": "hm"}],
                           [use("bash", "c", command="true")]])
    life.cfg["pace"]["gap_seconds"] = 0.3
    t0 = time.time()
    life.run(max_calls=3)
    assert time.time() - t0 >= 0.3


def test_no_pause_before_sleep_or_during_onboarding(tmp_path):
    reached = []
    life = make(tmp_path, [[use("register", "r", name="Lin")], [use("schedule", "s", action="sleep")],
                           lambda _: reached.append(time.time()) or [use("schedule", "s2", action="sleep")]])
    life.cfg["pace"]["gap_seconds"] = 5
    life.cfg["schedule"]["default_minutes"] = 0.002
    t0 = time.time()
    life.run(max_calls=3)  # register (onboarding), sleep: neither pauses
    assert reached and reached[0] - t0 < 4


def test_say_wake_wakes_a_sleeping_ai_and_skips_hooks(tmp_path):
    life = make(tmp_path, [[use("register", "r", name="Lin")], [use("schedule", "s", action="sleep")],
                           [use("bash", "c", command="true")]])
    life.cfg["schedule"]["default_minutes"] = 60  # the alarm alone would sleep an hour
    threading.Timer(0.3, lambda: cli.main(["--root", str(tmp_path), "say", "--wake", "are you up?"])).start()
    t0 = time.time()
    life.run(max_calls=3)
    assert time.time() - t0 < 10
    woke = " ".join(b["text"] for b in life.llm.requests[2]["messages"][-1]["content"])
    assert "A wake request" in woke and "station say --wake" in woke
    assert "message · unknown: are you up?" in woke  # the text stays an ordinary message
    assert not list((tmp_path / "inbox").glob("*.json"))


def test_say_wake_without_text_and_say_without_either(tmp_path, capsys):
    assert cli.main(["--root", str(tmp_path), "say", "--wake"]) == 0
    events = Inbox(tmp_path / "inbox").peek()
    assert [e.type for e in events] == ["wake"] and "Wake-up requested" in events[0].text
    assert cli.main(["--root", str(tmp_path), "say", ""]) == 2
    assert "nothing to say" in capsys.readouterr().err
    assert len(Inbox(tmp_path / "inbox").peek()) == 1


def test_plain_say_does_not_wake(tmp_path):
    cli.main(["--root", str(tmp_path), "say", "hi"])
    assert [e.type for e in Inbox(tmp_path / "inbox").peek()] == ["message"]


def test_wake_while_awake_is_just_an_event(tmp_path):
    life = make(tmp_path, [[use("register", "r", name="Lin")], [use("bash", "b", command="true")],
                           [use("bash", "c", command="true")]])
    life.run(max_calls=1)
    cli.main(["--root", str(tmp_path), "say", "--wake"])
    life.llm.script = [[use("bash", "b", command="true")], [use("bash", "c", command="true")]]
    life.run(max_calls=2)
    texts = " ".join(b.get("text", "") for b in life.llm.requests[-1]["messages"][-1]["content"])
    assert "Wake-up requested" in texts


def test_check_rejects_a_bad_gap(tmp_path, capsys):
    (tmp_path / "station.yaml").write_text("pace: {gap_seconds: -1}\nbrowser: {binary: /bin/true}\n"
                                           "credits: {allowances: {daily: 5, weekly: 17.5}}\n")
    assert cli.check(tmp_path) == 2
    assert "pace.gap_seconds" in capsys.readouterr().err
