"""Explicit sleep/continuation contracts, exercised offline with a scripted model."""

import copy
import json
import time

import pytest

from station.loop import Restart, Stop, template
from station.tools import LIFE_TOOLS
from test_loop import make, use


def build(tmp_path, script):
    life = make(tmp_path, script)
    life.cfg["memory"]["enabled"] = False
    life.cfg["disk"]["enabled"] = False
    life.cfg["reload"]["enabled"] = False
    return life


def registered_script(*steps):
    return [[use("register", "register", name="Lin")], *steps]


def events(life):
    return [json.loads(line) for line in life.store.log_path.read_text().splitlines()]


def fake_wake(life, monkeypatch, inspect=None):
    calls = []

    def wait(restart=None):
        if inspect:
            inspect()
        calls.append(restart)
        life.state["sleep"] = None
        life.state["last_wake"] = time.time()
        return [{"type": "text", "text": "<env>Woke after a test alarm.</env>"}]

    monkeypatch.setattr(life, "wait", wait)
    return calls


def test_sleep_schema_and_prompt_are_explicit():
    schedule = next(tool for tool in LIFE_TOOLS if tool["name"] == "schedule")
    assert "sleep" in schedule["input_schema"]["properties"]["action"]["enum"]
    assert "does not put you to sleep" in schedule["description"]
    assert 'action="sleep"' in template("system.md")
    assert "do not put you to sleep" in template("system.md")


def test_repeated_plain_text_continues_without_a_new_idle_cap(tmp_path, monkeypatch):
    life = build(tmp_path, registered_script(
        [{"type": "text", "text": "one"}],
        [{"type": "text", "text": "two"}],
        [{"type": "text", "text": "three"}],
    ))
    monkeypatch.setattr(life, "sleep", lambda: pytest.fail("text must not sleep"))
    assert life.run(max_calls=4) == "max_calls"
    assert len(life.llm.requests) == 4
    assert not life.state.get("sleep") and not life.pending_sleep
    assert [event["kind"] for event in events(life)].count("text") == 3
    assert not any(event["kind"] in ("rest", "wake") for event in events(life))
    for request in life.llm.requests[2:]:
        assert "You remain awake" in request["messages"][-1]["content"][0]["text"]
    assert not life.store.outbox_path.exists()


def test_plain_text_continuation_delivers_pending_events(tmp_path):
    life = build(tmp_path, [])
    life.state["status"] = "registered"
    life.inbox.put("message", "friend", "hello")
    result = life.handle_reply([{"type": "text", "text": "private"}])
    assert "You remain awake" in result["content"][0]["text"]
    assert any("friend: hello" in block.get("text", "") for block in result["content"])
    assert not life.state.get("sleep")


def test_alarm_management_does_not_sleep(tmp_path, monkeypatch):
    life = build(tmp_path, registered_script(
        [use("schedule", "interval", action="interval", minutes=5)],
        [use("schedule", "cron", action="cron", expr="0 12 * * *")],
        [use("schedule", "list", action="list")],
        [use("schedule", "remove", action="remove", id=1)],
    ))
    monkeypatch.setattr(life, "sleep", lambda: pytest.fail("alarm management must not sleep"))
    assert life.run(max_calls=5) == "max_calls"
    assert life.interval()["minutes"] == 5
    assert life.schedule_state()["cron"] == []
    assert not life.pending_sleep and not life.state.get("sleep_requested")


@pytest.mark.parametrize("extra", [{"minutes": 1}, {"once": True}, {"note": "later"}, {"sleep": True}])
def test_invalid_sleep_arguments_return_error_without_sleep(tmp_path, monkeypatch, extra):
    life = build(tmp_path, registered_script(
        [use("schedule", "sleep", action="sleep", **extra)],
        [{"type": "text", "text": "continue"}],
    ))
    monkeypatch.setattr(life, "sleep", lambda: pytest.fail("invalid request must not sleep"))
    assert life.run(max_calls=3) == "max_calls"
    result = life.llm.requests[2]["messages"][-1]["content"][0]
    assert result["is_error"] and "only action" in result["content"]
    assert not life.pending_sleep and not life.state.get("sleep_requested")


def test_sleep_finishes_and_saves_whole_batch_before_wait(tmp_path, monkeypatch):
    life = build(tmp_path, registered_script(
        [use("schedule", "sleep", action="sleep"),
         use("todo", "todo", action="add", text="After the sleep request")],
        [{"type": "text", "text": "awake again"}],
    ))

    def inspect():
        history = life.store.load_history("main")
        results = history[-1]["content"]
        assert [block["tool_use_id"] for block in results if block["type"] == "tool_result"] == ["sleep", "todo"]
        assert "After the sleep request" in life.todo.render()
        saved = life.store.load_state()
        assert saved["sleep"]["thread"] == "main"
        assert "sleep_requested" not in saved
        assert not life.pending_sleep

    calls = fake_wake(life, monkeypatch, inspect)
    assert life.run(max_calls=3) == "max_calls"
    assert len(calls) == 1
    assert [event["kind"] for event in events(life)].count("rest") == 1
    assert "sleep_requested" not in life.state and not life.state.get("sleep")
    assert "Woke after" in life.llm.requests[2]["messages"][-1]["content"][0]["text"]


def test_other_tool_error_does_not_erase_successful_sleep_request(tmp_path, monkeypatch):
    life = build(tmp_path, registered_script(
        [use("schedule", "sleep", action="sleep"), use("nonexistent", "bad")],
    ))

    def inspect():
        results = life.store.load_history()[-1]["content"]
        assert results[1]["is_error"] and results[1]["tool_use_id"] == "bad"

    calls = fake_wake(life, monkeypatch, inspect)
    life.run(max_calls=2)
    assert len(calls) == 1


def test_duplicate_sleep_calls_make_one_sleep_transition(tmp_path, monkeypatch):
    life = build(tmp_path, registered_script(
        [use("schedule", "sleep1", action="sleep"), use("schedule", "sleep2", action="sleep")],
    ))
    calls = fake_wake(life, monkeypatch)
    life.run(max_calls=2)
    assert len(calls) == 1


def test_sleep_uses_alarms_configured_later_in_same_batch(tmp_path, monkeypatch):
    life = build(tmp_path, registered_script(
        [use("schedule", "sleep", action="sleep"),
         use("schedule", "interval", action="interval", minutes=17)],
    ))
    original = copy.deepcopy(life.cfg["schedule"])
    def inspect():
        assert life.interval()["minutes"] == 17

    calls = fake_wake(life, monkeypatch, inspect)
    life.run(max_calls=2)
    assert len(calls) == 1
    assert life.cfg["schedule"] == original


def test_sleep_follows_focus_transition_before_wait(tmp_path, monkeypatch):
    life = build(tmp_path, registered_script(
        [use("schedule", "sleep", action="sleep"),
         use("focus", "focus", action="new", title="Reading")],
    ))

    def inspect():
        assert life.state["current"] == "t1"
        assert life.store.load_state()["sleep"]["thread"] == "t1"
        results = life.store.load_history("main")[-1]["content"]
        assert {block["tool_use_id"] for block in results if block["type"] == "tool_result"} == {"sleep", "focus"}

    calls = fake_wake(life, monkeypatch, inspect)
    life.run(max_calls=2)
    assert len(calls) == 1
    assert life.store.load_history("t1")[-1]["content"][0]["text"].startswith("<env>Woke after")


def test_sleep_waits_for_requested_compression(tmp_path, monkeypatch):
    life = build(tmp_path, registered_script(
        [use("schedule", "sleep", action="sleep"), use("compress", "compress")],
    ))
    ordering = []

    def compress(tid, history, focus, reason):
        assert life.store.load_history(tid)[-1]["content"][0]["tool_use_id"] == "sleep"
        ordering.append("compress")
        return None, "Too little to compress."

    monkeypatch.setattr(life, "compress", compress)
    calls = fake_wake(life, monkeypatch, lambda: ordering.append("wait"))
    life.run(max_calls=2)
    assert ordering == ["compress", "wait"] and len(calls) == 1


def test_real_alarm_wakes_once_and_text_afterwards_stays_awake(tmp_path):
    life = build(tmp_path, registered_script(
        [use("schedule", "sleep", action="sleep")],
        [{"type": "text", "text": "awake"}],
        [{"type": "text", "text": "still awake"}],
    ))
    life.run(max_calls=4)
    kinds = [event["kind"] for event in events(life)]
    assert kinds.count("rest") == 1 and kinds.count("wake") == 1
    assert "You remain awake" in life.llm.requests[3]["messages"][-1]["content"][0]["text"]
    history = life.store.load_history()
    assert "<board>" not in json.dumps(history)
    assert [row["role"] for row in history].count("assistant") == 4


@pytest.mark.parametrize("interrupt", [Stop, Restart])
def test_restart_with_saved_user_tool_results_resumes_sleep(tmp_path, monkeypatch, interrupt):
    life = build(tmp_path, registered_script([use("schedule", "sleep", action="sleep")]))
    monkeypatch.setattr(life, "wait", lambda restart=None: (_ for _ in ()).throw(interrupt()))
    assert life.run(max_calls=2) in ("stopped", "restart")
    saved_results = copy.deepcopy(life.store.load_history()[-1])
    assert saved_results["content"][0]["tool_use_id"] == "sleep"
    saved_since = life.store.load_state()["sleep"]["since"]
    again = build(tmp_path, [[{"type": "text", "text": "after restart"}]])
    again.cfg["restart"]["wake"] = False

    def inspect():
        assert again.state["sleep"]["since"] == saved_since
        assert again.store.load_history()[-1] == saved_results

    calls = fake_wake(again, monkeypatch, inspect)
    assert again.run(max_calls=1) == "max_calls"
    assert len(calls) == 1
    assert "result of this call was lost" not in json.dumps(again.store.load_history())
    assert [event["kind"] for event in events(again)].count("rest") == 1


def test_restart_between_saved_request_and_entering_sleep(tmp_path, monkeypatch):
    life = build(tmp_path, registered_script([use("schedule", "sleep", action="sleep")]))
    monkeypatch.setattr(life, "sleep", lambda: (_ for _ in ()).throw(Stop()))
    assert life.run(max_calls=2) == "stopped"
    assert life.store.load_state()["sleep_requested"] is True
    assert life.store.load_history()[-1]["content"][0]["tool_use_id"] == "sleep"
    again = build(tmp_path, [[{"type": "text", "text": "back"}]])
    calls = fake_wake(again, monkeypatch)
    assert again.run(max_calls=1) == "max_calls"
    assert len(calls) == 1
    assert "sleep_requested" not in again.store.load_state()


def test_results_write_failure_prevents_sleep(tmp_path, monkeypatch):
    life = build(tmp_path, registered_script([use("schedule", "sleep", action="sleep")]))
    append = life.store.append_history

    def fail_results(tid, *messages):
        if any(block.get("tool_use_id") == "sleep" for message in messages for block in message["content"]):
            raise OSError("injected result write failure")
        append(tid, *messages)

    monkeypatch.setattr(life.store, "append_history", fail_results)
    monkeypatch.setattr(life, "wait", lambda **kwargs: pytest.fail("must save tool results before sleeping"))
    with pytest.raises(OSError, match="result write failure"):
        life.run(max_calls=2)
    assert not life.store.load_state().get("sleep")
    assert not life.store.load_state().get("sleep_requested")


def test_saved_legacy_assistant_sleep_still_resumes(tmp_path, monkeypatch):
    life = build(tmp_path, registered_script([{"type": "text", "text": "old final text"}]))
    life.run(max_calls=2)
    history = life.store.load_history()[:-1]
    life.store.rewrite_history("main", history)
    life.state["sleep"] = {"since": time.time(), "cursor": time.time(), "thread": "main"}
    life.store.save_state(life.state)
    again = build(tmp_path, [[{"type": "text", "text": "new runtime"}]])
    calls = fake_wake(again, monkeypatch)
    again.run(max_calls=1)
    assert len(calls) == 1
    assert "Woke after" in again.llm.requests[0]["messages"][-1]["content"][0]["text"]


def test_sleep_is_unavailable_before_registration(tmp_path):
    life = build(tmp_path, [])
    text, error = life.run_tool(use("schedule", "sleep", action="sleep"))
    assert error and "only register and exit" in text
    assert not life.pending_sleep


def test_continuation_still_obeys_existing_credit_gate(tmp_path, monkeypatch):
    life = build(tmp_path, [])

    def consume_budget(messages):
        life.store.ledger.add("test", list_usd=10, credits=10, source="test")
        return [{"type": "text", "text": "continue, subject to the budget"}]

    life.llm.script = registered_script(consume_budget)
    monkeypatch.setattr(life, "_pause", lambda seconds: (_ for _ in ()).throw(Stop()))
    assert life.run() == "stopped"
    assert len(life.llm.requests) == 2
    assert any(event["kind"] == "credit_limit" for event in events(life))
    assert not life.state.get("sleep")


def test_saved_sleep_intent_repairs_orphan_calls_before_wait(tmp_path, monkeypatch):
    life = build(tmp_path, [])
    life.state.update(status="registered", sleep_requested=True)
    history = [{"role": "assistant", "content": [use("todo", "orphan", action="list")]}]
    life.store.append_history("main", *history)

    def inspect():
        tail = life.store.load_history()[-1]["content"][0]
        assert tail["type"] == "tool_result" and tail["tool_use_id"] == "orphan"
        assert "result of this call was lost" in tail["content"]

    calls = fake_wake(life, monkeypatch, inspect)
    recovered = life.recover(history)
    assert len(calls) == 1
    assert "Woke after" in recovered[-1]["content"][0]["text"]


@pytest.mark.parametrize("restart_wake", [True, False])
def test_user_tail_sleep_preserves_restart_wake_policy(tmp_path, monkeypatch, restart_wake):
    life = build(tmp_path, [])
    life.state.update(status="registered", sleep={"since": time.time(), "cursor": time.time(), "thread": "main"})
    life.restart = {"down": 120, "reason": "test crash"}
    life.cfg["restart"]["wake"] = restart_wake
    history = [{"role": "user", "content": [{"type": "text", "text": "saved tool results"}]}]
    life.store.append_history("main", *history)
    calls = fake_wake(life, monkeypatch)
    life.recover(history)
    assert len(calls) == 1
    if restart_wake:
        assert "test crash" in calls[0]
        assert len(life.state["restart_wakes"]) == 1
    else:
        assert calls[0] is None
        assert not life.state.get("restart_wakes")


@pytest.mark.parametrize("restart_wake", [True, False])
def test_legacy_assistant_tail_keeps_restart_notice_for_sleep_policy(tmp_path, monkeypatch, restart_wake):
    life = build(tmp_path, [])
    life.state.update(status="registered", sleep={"since": time.time(), "cursor": time.time(), "thread": "main"})
    life.restart = {"down": 120, "reason": "test legacy crash"}
    life.cfg["restart"]["wake"] = restart_wake
    history = [{"role": "assistant", "content": [{"type": "text", "text": "legacy sleep"}]}]
    life.store.append_history("main", *history)
    calls = fake_wake(life, monkeypatch)
    life.recover(history)
    assert len(calls) == 1
    if restart_wake:
        assert "test legacy crash" in calls[0]
        assert len(life.state["restart_wakes"]) == 1
    else:
        assert calls[0] is None
        assert not life.state.get("restart_wakes")
