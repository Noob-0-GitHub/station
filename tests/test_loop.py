"""Offline tests: a scripted fake model drives the real loop."""

import json
import threading
import time

import pytest

from station.board import Board, parse_panel
from station.inbox import Inbox
from station.loop import Life
from station.runner import Runner


class FakeLLM:
    def __init__(self, script, input_tokens=10):
        self.script = list(script)
        self.requests = []
        self.input_tokens = input_tokens

    def call(self, system, tools, messages, tool_choice=None):
        self.requests.append({"system": system, "tools": [t["name"] for t in tools], "messages": messages,
                              "tool_choice": tool_choice})
        step = self.script.pop(0)
        content = step(messages) if callable(step) else step
        return {"content": content, "stop_reason": "end_turn",
                "usage": {"input_tokens": self.input_tokens, "output_tokens": 5,
                          "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}


def use(tool, uid, **args):
    return {"type": "tool_use", "id": uid, "name": tool, "input": args}


def make(tmp_path, script):
    (tmp_path / "station.yaml").write_text(
        "schedule: {poll_seconds: 0.02, default_minutes: 0.002, min_minutes: 0}\n"
        "errors: {retry_minutes: 0}\nbrowser: {binary: /bin/true}\n"
        "credits: {allowances: {daily: 5, weekly: 17.5}}\n")
    life = Life(tmp_path, echo=lambda *_: None)
    life.llm = FakeLLM(script)
    return life


def main_history(tmp_path):
    return (tmp_path / "state" / "threads" / "main.jsonl").read_text()


def test_exit_on_refusal(tmp_path):
    life = make(tmp_path, [[use("exit", "t1", reason="no")]])
    assert life.run() == "exited"
    assert "declined the terms" in (tmp_path / "state" / "outbox.jsonl").read_text()
    # A second run does nothing.
    assert make(tmp_path, []).run() == "exited"


def test_two_texts_during_onboarding_exit(tmp_path):
    life = make(tmp_path, [[{"type": "text", "text": "hmm"}], [{"type": "text", "text": "hmm"}]])
    assert life.run() == "exited"


def test_onboarding_tools_only_before_register(tmp_path):
    life = make(tmp_path, [[use("bash", "t1", command="echo hi")], [use("exit", "t2")]])
    life.run()
    first = life.llm.requests[0]
    assert first["tools"] == ["register", "exit"]
    result = life.llm.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] and "only register and exit" in result["content"]


def test_register_board_and_cache_layout(tmp_path):
    life = make(tmp_path, [
        [use("register", "t1", name="Lin")],
        [use("board", "t2", action="write", name="plans", text="---\ntags: [me]\n---\ngroceries")],
        [use("bash", "t3", command="echo hello")],
        [use("schedule", "t4", action="list")],
    ])
    assert life.run(max_calls=4) == "max_calls"
    reqs = life.llm.requests
    assert reqs[1]["tools"] == ["bash", "schedule", "focus", "compress", "diary", "todo", "board", "skill", "web", "view", "memory"]
    last = reqs[3]["messages"][-1]["content"]
    board = last[-1]["text"]
    assert board.startswith("<board>") and "## plans [me]" in board and "groceries" in board
    assert "## clock" in board and "## system" in board
    # Breakpoint sits on the block right before the board, never on the board itself.
    assert "cache_control" in last[-2] and "cache_control" not in last[-1]
    # Persisted history has no board, so the next prefix matches the cached one.
    stored = main_history(tmp_path)
    assert "<board>" not in stored and "cache_control" not in stored
    assert "hello" in stored


def test_prefix_is_stable_across_calls(tmp_path):
    life = make(tmp_path, [[use("register", "t1", name="Lin")], [use("bash", "t2", command="true")],
                           [use("bash", "t3", command="true")]])
    life.run(max_calls=3)
    a, b = life.llm.requests[1]["messages"], life.llm.requests[2]["messages"]

    def strip(msgs):
        out = json.loads(json.dumps(msgs))
        for m in out:
            for blk in m["content"]:
                if isinstance(blk, dict):
                    blk.pop("cache_control", None)
            m["content"] = [blk for blk in m["content"] if not str(blk.get("text", "")).startswith("<board>")]
        return out

    assert strip(b)[: len(a)] == strip(a)


def test_message_does_not_wake(tmp_path):
    life = make(tmp_path, [[use("register", "t1", name="Lin")], [{"type": "text", "text": "sleeping"}], [use("bash", "t3", command="true")]])
    life.cfg["schedule"]["default_minutes"] = 0.02  # 1.2 s
    threading.Timer(0.2, lambda: Inbox(tmp_path / "inbox").put("message", "Anthony", "you there?")).start()
    t0 = time.time()
    life.run(max_calls=3)
    assert time.time() - t0 >= 1.0
    woke = life.llm.requests[2]["messages"][-1]["content"]
    texts = " ".join(b["text"] for b in woke)
    assert "-minute interval" in texts and "Anthony: you there?" in texts


def test_text_reply_ends_the_turn(tmp_path):
    life = make(tmp_path, [[use("register", "t1", name="Lin")], [{"type": "text", "text": "ok"}], [use("bash", "t2", command="true")]])
    life.run(max_calls=3)
    msg = life.llm.requests[2]["messages"][-1]["content"]
    assert "Woke after" in msg[0]["text"]
    assert '"kind": "text"' in (tmp_path / "state" / "log.jsonl").read_text()


def test_crash_recovery_resumes(tmp_path):
    life = make(tmp_path, [[use("register", "t1", name="Lin")], [use("bash", "t2", command="echo x")]])
    life.run(max_calls=2)
    # Simulate a crash after the assistant turn was stored but before results.
    hist = tmp_path / "state" / "threads" / "main.jsonl"
    lines = hist.read_text().splitlines()
    hist.write_text("\n".join(lines[:-1]) + "\n")
    again = make(tmp_path, [[use("bash", "t3", command="true")]])
    assert again.run(max_calls=1) == "max_calls"
    msgs = again.llm.requests[0]["messages"]
    assert msgs[-1]["content"][0]["tool_use_id"] == "t2"
    assert "restarted" in msgs[-1]["content"][0]["content"]


def test_crash_while_asleep_resumes_sleep(tmp_path):
    life = make(tmp_path, [[use("register", "t1", name="Lin")], [{"type": "text", "text": "sleeping"}]])
    life.run(max_calls=2)
    hist = tmp_path / "state" / "threads" / "main.jsonl"
    lines = hist.read_text().splitlines()
    hist.write_text("\n".join(lines[:-1]) + "\n")  # lose the wake-up message
    state = json.loads((tmp_path / "state" / "state.json").read_text())
    state["sleep"] = {"since": time.time(), "cursor": time.time(), "thread": "main"}
    (tmp_path / "state" / "state.json").write_text(json.dumps(state))
    again = make(tmp_path, [[use("bash", "t3", command="true")]])
    again.run(max_calls=1)
    last = again.llm.requests[0]["messages"][-1]
    assert last["role"] == "user" and "Woke after" in last["content"][0]["text"]


def test_panel_command_cooldown_and_stale(tmp_path):
    runner = Runner(tmp_path)
    board = Board(tmp_path / "board", runner)
    flag = tmp_path / "ok"
    flag.write_text("1")
    board.tool("write", "probe", text=f"---\ncommand: cat {flag} && date +%s%N\ncooldown: 0\n---\n")
    first = board.render({})
    assert "command failed" not in first
    flag.unlink()
    second = board.render({})
    assert "stale" in second


def test_panel_patch_requires_unique(tmp_path):
    board = Board(tmp_path / "board", Runner(tmp_path))
    board.tool("write", "a", text="x x")
    with pytest.raises(ValueError):
        board.tool("patch", "a", old="x", new="y")
    with pytest.raises(ValueError):
        board.tool("write", "../etc", text="x")


def test_parse_panel_without_front_matter():
    p = parse_panel("n", "just text")
    assert p.body == "just text" and p.command is None
