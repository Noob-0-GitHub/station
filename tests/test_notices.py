"""Notices (store, CLI, board panel) and the restart wake. Offline: fake models only."""

import json
import os
import subprocess
import sys
import time

import pytest

from station.cli import main
from station.notices import Notices, parse_duration

from test_loop import FakeLLM, make, use


def test_add_update_by_key_read_clear(tmp_path):
    n = Notices(tmp_path)
    a = n.add("3 new DMs", source="bilibili", key="dm")
    b = n.add("hello", source="telegram")
    assert (a["id"], b["id"]) == (1, 2)
    n.mark_read([1])
    again = n.add("4 new DMs", source="bilibili", key="dm")  # same source+key: in place, unread again
    assert again["id"] == 1 and again["read"] is None
    assert [x["text"] for x in n.list()] == ["4 new DMs", "hello"]
    n.add("other source, same key", source="telegram", key="dm")
    assert len(n.list()) == 3
    assert n.clear("dm", source="bilibili") == 1
    assert n.mark_read() == 2 and n.list(unread=True) == []
    with pytest.raises(ValueError, match="no notice #99"):
        n.mark_read([99])


def test_expiry_priority_and_limits(tmp_path):
    n = Notices(tmp_path)
    n.add("gone soon", ttl=0.01)
    n.add("urgent", priority="high")
    n.add("meh", priority="low")
    time.sleep(0.05)
    assert [x["text"] for x in n.list()] == ["urgent", "meh"]
    assert [x["text"] for x in n.list(min_priority="normal")] == ["urgent"]
    assert len(n.add("x" * 5000)["text"]) == 1000
    with pytest.raises(ValueError):
        n.add("  ")
    with pytest.raises(ValueError):
        n.add("x", priority="urgent")
    assert parse_duration("90m") == 5400 and parse_duration("1d") == 86400 and parse_duration("never") is None
    with pytest.raises(ValueError):
        parse_duration("soon")


def test_read_notices_are_dropped_after_a_week(tmp_path, monkeypatch):
    n = Notices(tmp_path)
    n.add("old")
    n.mark_read()
    real = time.time
    monkeypatch.setattr("station.notices.time.time", lambda: real() + 8 * 86400)
    n.add("new")
    assert [x["text"] for x in n.list()] == ["new"]


def test_concurrent_writers_lose_nothing(tmp_path):
    code = ("import sys; sys.path.insert(0, sys.argv[1]); from station.notices import Notices\n"
            "n = Notices(sys.argv[2])\nfor i in range(25): n.add(f'{sys.argv[3]}-{i}', source=sys.argv[3])\n")
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    procs = [subprocess.Popen([sys.executable, "-c", code, repo, str(tmp_path), f"w{k}"]) for k in range(4)]
    assert all(p.wait(timeout=60) == 0 for p in procs)
    items = Notices(tmp_path).list()
    assert len(items) == 100 and len({x["id"] for x in items}) == 100


def test_board_panel_shows_unread_only(tmp_path):
    life = make(tmp_path, [[use("register", "r", name="Lin")], [use("bash", "b", command="true")]])
    life.notices.add("read me", source="telegram", priority="high")
    life.notices.add("seen")
    life.notices.mark_read([2])
    life.run(max_calls=2)
    board = life.llm.requests[1]["messages"][-1]["content"][-1]["text"]
    assert "## notices · built-in" in board and "1 unread" in board and "#1 [high] telegram" in board
    assert "seen" not in board
    # Not stored in history: the board is stripped before saving.
    assert "read me" not in (tmp_path / "state" / "threads" / "main.jsonl").read_text()


def test_cli(tmp_path, capsys, monkeypatch):
    r = ["--root", str(tmp_path)]
    assert main([*r, "notify", "--source", "bilibili", "--key", "dm:1", "--ttl", "1d", "new DM"]) == 0
    assert capsys.readouterr().out.strip() == "#1"
    main([*r, "notify", "--priority", "high", "quota until 18:00"])
    assert main([*r, "notices", "--quiet"]) == 1  # unread exist: a hook would wake the AI
    assert main([*r, "notices", "--quiet", "--min-priority", "high"]) == 1
    main([*r, "notices"])
    out = capsys.readouterr().out
    assert "* #1 bilibili" in out and "(until" in out and "* #2 [high] manual" in out
    main([*r, "notices", "read", "2"])
    assert main([*r, "notices", "--quiet", "--min-priority", "high"]) == 0
    main([*r, "notify", "--source", "bilibili", "--clear", "dm:1"])
    assert "cleared 1" in capsys.readouterr().out
    assert main([*r, "notices", "--quiet"]) == 0
    with pytest.raises(SystemExit):
        main([*r, "notices", "read"])
    # $STATION_ROOT stands in for --root, as in the AI's shell.
    monkeypatch.setenv("STATION_ROOT", str(tmp_path))
    assert main(["notify", "from env"]) == 0 and "#3" in capsys.readouterr().out


def test_seeded_station_command_from_ai_shell(tmp_path):
    life = make(tmp_path, [[use("register", "r", name="Lin")],
                           [use("bash", "b", command="station notify --source me 'from bash' && station notices")]])
    life.run(max_calls=2)
    wrapper = tmp_path / "home" / "bin" / "station"
    assert os.access(wrapper, os.X_OK) and str(tmp_path.resolve()) in wrapper.read_text()
    assert Notices(tmp_path / "state").list()[0]["text"] == "from bash"


# ---- restart wake --------------------------------------------------------

def asleep_after_registering(tmp_path, minutes=600):
    life = make(tmp_path, [[use("register", "r", name="Lin")], [{"type": "text", "text": "night"}]])
    life.cfg["schedule"]["default_minutes"] = minutes
    life.stopping_after_sleep = True
    orig = life.wait

    def wait(restart=None):
        if restart is None and life.state.get("sleep"):
            life.stopping = True  # fall asleep, then the operator stops Station
        return orig(restart)

    life.wait = wait
    assert life.run() == "stopped"
    return life


def restarted(tmp_path, script, minutes=600):
    again = make(tmp_path, script)
    again.cfg["schedule"]["default_minutes"] = minutes
    return again


def test_operator_restart_wakes_with_a_line_and_a_notice(tmp_path):
    asleep_after_registering(tmp_path)
    again = restarted(tmp_path, [[use("bash", "b", command="true")]])
    assert again.run(max_calls=1) == "max_calls"
    woke = again.llm.requests[0]["messages"][-1]["content"][0]["text"]
    assert "Woke after" in woke and "Station restarted at" in woke and "previous run: stopped" in woke
    assert Notices(tmp_path / "state").list()[0]["key"] == "restart"


def test_hard_kill_is_detected_from_the_heartbeat(tmp_path):
    asleep_after_registering(tmp_path)
    state = json.loads((tmp_path / "state" / "state.json").read_text())
    state.pop("last_exit")
    state["running"] = {"pid": 1, "since": time.time() - 3600}  # never recorded an exit
    (tmp_path / "state" / "state.json").write_text(json.dumps(state))
    beat = tmp_path / "state" / "heartbeat"
    os.utime(beat, (time.time() - 1800, time.time() - 1800))
    again = restarted(tmp_path, [[use("bash", "b", command="true")]])
    again.run(max_calls=1)
    woke = again.llm.requests[0]["messages"][-1]["content"][0]["text"]
    assert "down about 30 minutes" in woke and "killed without a clean exit" in woke


def test_hot_reload_restart_does_not_wake(tmp_path):
    asleep_after_registering(tmp_path)
    state = json.loads((tmp_path / "state" / "state.json").read_text())
    state["last_exit"]["reason"] = "reload"
    (tmp_path / "state" / "state.json").write_text(json.dumps(state))
    again = restarted(tmp_path, [[use("bash", "b", command="true")]])
    again.wait_calls = []
    orig = again.wait

    def wait(restart=None):
        again.wait_calls.append(restart)
        again.stopping = True
        return orig(restart)

    again.wait = wait
    assert again.run() == "stopped" and again.wait_calls == [None] and again.llm.requests == []
    assert Notices(tmp_path / "state").list() == []


def test_restart_wakes_are_rate_limited(tmp_path):
    asleep_after_registering(tmp_path)
    state = json.loads((tmp_path / "state" / "state.json").read_text())
    state["restart_wakes"] = [time.time() - 60]  # woke for a restart a minute ago
    (tmp_path / "state" / "state.json").write_text(json.dumps(state))
    again = restarted(tmp_path, [[use("bash", "b", command="true")]])
    orig = again.wait
    seen = []

    def wait(restart=None):
        seen.append(restart)
        again.stopping = True
        return orig(restart)

    again.wait = wait
    assert again.run() == "stopped" and seen == [None]  # resumed the sleep instead of waking
    assert "rate-limited" in Notices(tmp_path / "state").list()[0]["text"]


def test_crash_mid_turn_mentions_the_restart_in_the_lost_result(tmp_path):
    life = make(tmp_path, [[use("register", "r", name="Lin")], [use("bash", "b", command="true")]])
    life.run(max_calls=2)
    hist = tmp_path / "state" / "threads" / "main.jsonl"
    hist.write_text("\n".join(hist.read_text().splitlines()[:-1]) + "\n")
    state = json.loads((tmp_path / "state" / "state.json").read_text())
    state["last_exit"] = {"ts": time.time() - 120, "reason": "crashed (RuntimeError)"}
    (tmp_path / "state" / "state.json").write_text(json.dumps(state))
    again = restarted(tmp_path, [[use("bash", "b2", command="true")]])
    again.run(max_calls=1)
    content = again.llm.requests[0]["messages"][-1]["content"]
    assert "result of this call was lost" in content[0]["content"]
    assert any("previous run: crashed (RuntimeError)" in b.get("text", "") for b in content[1:])


def test_bounded_dev_run_is_not_a_restart(tmp_path):
    life = make(tmp_path, [[use("register", "r", name="Lin")], [use("bash", "b", command="true")]])
    life.run(max_calls=2)
    again = make(tmp_path, [[use("bash", "b2", command="true")]])
    again.run(max_calls=1)
    assert "Station restarted" not in json.dumps(again.llm.requests[0]["messages"][-1])
    assert Notices(tmp_path / "state").list() == []


def test_first_completed_turn_writes_confirmed(tmp_path):
    life = make(tmp_path, [[use("register", "r", name="Lin")], [use("bash", "b", command="true")]])
    life.run(max_calls=2)
    confirmed = json.loads((tmp_path / "state" / "confirmed.json").read_text())
    assert confirmed["pid"] == os.getpid() and confirmed["ts"] <= time.time()


def test_launcher_previous_exit_wakes_with_its_reason(tmp_path, monkeypatch):
    asleep_after_registering(tmp_path)
    state = json.loads((tmp_path / "state" / "state.json").read_text())
    state["last_exit"]["reason"] = "reload"  # the crashed run left no clean exit of its own
    (tmp_path / "state" / "state.json").write_text(json.dumps(state))
    monkeypatch.setenv("STATION_PREVIOUS_EXIT", "crashed (RuntimeError), exit code 1; the launcher rolled the code back")
    monkeypatch.setenv("STATION_DOWN_SINCE", str(time.time() - 125))
    again = restarted(tmp_path, [[use("bash", "b", command="true")]])
    again.run(max_calls=1)
    woke = again.llm.requests[0]["messages"][-1]["content"][0]["text"]
    assert "down about 2 minutes" in woke and "rolled the code back" in woke


def test_reload_under_the_launcher_exits_3(tmp_path, monkeypatch):
    import station.cli as cli

    monkeypatch.setattr(cli.Life, "run", lambda self, max_calls=None: "restart")
    monkeypatch.setattr(cli.reload_mod, "restart", lambda root: pytest.fail("must not exec under the launcher"))
    monkeypatch.setenv("STATION_LAUNCHER", "1")
    assert cli.main(["--root", str(tmp_path), "run"]) == cli.EXIT_RELOAD == 3
