import json

import pytest

from station.llm import LLMError, thinking_allowed
from test_loop import FakeLLM, make, use


def ok(text="ok"):
    return [{"type": "text", "text": text}]


class Flaky(FakeLLM):
    """Fails with `error` while `down` is true."""

    def __init__(self, script, error=None):
        super().__init__(script)
        self.down = error is not None
        self.error = error

    def call(self, system, tools, messages, tool_choice=None):
        if self.down:
            self.requests.append({"failed": True})
            raise self.error
        return super().call(system, tools, messages, tool_choice)


def routed(tmp_path, main, backup, per_hour=3):
    life = make(tmp_path, [])
    life.cfg["fallback"] = [{"provider": "anthropic", "model": "claude-opus-5-5"}]
    life.cfg["fallback_retry_per_hour"] = per_hour
    life.llm = main
    life.router.clients[1] = backup
    return life


def test_no_fallback_by_default(tmp_path):
    life = make(tmp_path, [])
    assert life.cfg["fallback"] == []
    life.llm = Flaky([], LLMError("down", "transient"))
    with pytest.raises(LLMError):
        life.call_model([], [], [{"role": "user", "content": []}])


def test_switch_notify_retry_three_times_an_hour_and_switch_back(tmp_path, monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr("station.router.time.time", lambda: now[0])
    main = Flaky([ok("main back")], LLMError("token expired", "auth", status=401))
    backup = FakeLLM([ok("b1"), ok("b2"), ok("b3"), ok("b4")])
    life = routed(tmp_path, main, backup)
    msgs = [{"role": "user", "content": []}]

    assert life.call_model([], [], msgs)["content"][0]["text"] == "b1"
    assert life.state["model_route"]["active"] == 1
    assert life.last_route["model"] == "claude-opus-5-5"
    # The switch is an error: in errors.jsonl, as an event for the AI, and as a notice.
    err = [json.loads(x) for x in (tmp_path / "state" / "errors.jsonl").read_text().splitlines()]
    assert err[-1]["kind"] == "auth" and "switched to anthropic/claude-opus-5-5" in err[-1]["message"]
    events = [b["text"] for b in life.drain_events()]
    assert any("failed" in e and "3 times an hour" in e for e in events)
    assert any(n["key"] == "model-fallback" for n in life.notices.list())

    # Within 20 minutes the main model is not tried again.
    now[0] += 600
    tried = len(main.requests)
    assert life.call_model([], [], msgs)["content"][0]["text"] == "b2"
    assert len(main.requests) == tried
    # After 20 minutes it is tried once, still down: stay on the fallback, no new notice.
    now[0] += 601
    assert life.call_model([], [], msgs)["content"][0]["text"] == "b3"
    assert len(main.requests) == tried + 1 and life.state["model_route"]["active"] == 1
    assert [b["text"] for b in life.drain_events()] == []
    # 20 minutes later it answers: back on the main model, the AI is told, the notice goes.
    now[0] += 1201
    main.down = False
    assert life.call_model([], [], msgs)["content"][0]["text"] == "main back"
    assert life.state["model_route"] == {"active": 0}
    assert any("answers again" in e for e in [b["text"] for b in life.drain_events()])
    assert not any(n["key"] == "model-fallback" for n in life.notices.list())
    assert life.last_route["model"] == life.cfg["model"]


def test_all_models_failing_raise_the_main_error(tmp_path):
    life = routed(tmp_path, Flaky([], LLMError("main down", "transient")),
                  Flaky([], LLMError("backup down", "quota")))
    with pytest.raises(LLMError) as e:
        life.call_model([], [], [{"role": "user", "content": []}])
    assert str(e.value) == "main down"
    assert life.state.get("model_route", {}).get("active", 0) == 0


class KilledByStop(FakeLLM):
    """A model call whose child process dies because Station itself is stopping (systemd cgroup SIGTERM)."""

    def __init__(self, life_ref):
        super().__init__([])
        self.life_ref = life_ref

    def call(self, system, tools, messages, tool_choice=None):
        self.requests.append({"failed": True})
        self.life_ref[0].stopping = True
        raise LLMError("Claude Code exited while replaying the history", "transient")


def test_failure_while_stopping_is_not_a_model_failure(tmp_path):
    from station.loop import Stop

    ref = [None]
    backup = FakeLLM([ok("backup")])
    life = routed(tmp_path, KilledByStop(ref), backup)
    ref[0] = life
    with pytest.raises(Stop):
        life.call_model([], [], [{"role": "user", "content": []}])
    assert backup.requests == []  # the fallback is not tried
    assert life.state.get("model_route", {}).get("active", 0) == 0
    log = (tmp_path / "state" / "log.jsonl")
    assert not log.exists() or '"model_failed"' not in log.read_text()
    errors = tmp_path / "state" / "errors.jsonl"
    assert not errors.exists() or errors.read_text() == ""
    assert life.drain_events() == []


def test_stop_during_a_model_call_ends_the_run_without_fallback(tmp_path):
    ref = [None]
    backup = FakeLLM([ok("backup")])
    life = routed(tmp_path, KilledByStop(ref), backup)
    ref[0] = life
    life.state["status"] = "registered"
    life.store.append_history("main", {"role": "user", "content": [{"type": "text", "text": "awake"}]})
    assert life.run() == "stopped"
    assert backup.requests == []
    saved = life.store.load_state()
    assert saved.get("model_route", {}).get("active", 0) == 0
    assert saved["last_exit"]["reason"] == "stopped"


def test_route_survives_a_restart(tmp_path, monkeypatch):
    life = routed(tmp_path, Flaky([], LLMError("x", "auth")), FakeLLM([ok()]))
    life.call_model([], [], [{"role": "user", "content": []}])
    life.store.save_state(life.state)
    again = routed(tmp_path, Flaky([], LLMError("x", "auth")), FakeLLM([ok("still backup")]))
    assert again.state["model_route"]["active"] == 1
    assert again.call_model([], [], [{"role": "user", "content": []}])["content"][0]["text"] == "still backup"


def test_usage_is_priced_for_the_model_that_answered(tmp_path):
    life = routed(tmp_path, Flaky([], LLMError("x", "transient")), FakeLLM([ok()]))
    life.cfg["prices"]["claude-opus-5-5"] = {"input": 5.0, "output": 25.0}
    life.call_model([], [], [{"role": "user", "content": []}])
    life.record_usage({"input_tokens": 1_000_000, "output_tokens": 0})
    (row,) = life.store.ledger.rows(service="model")
    assert row["model"] == "claude-opus-5-5" and row["provider"] == "anthropic"
    assert row["list_usd"] == pytest.approx(5.0)


def test_thinking_is_skipped_when_the_open_tool_turn_has_none():
    tool_turn = {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "bash", "input": {}}]}
    assert not thinking_allowed([tool_turn, {"role": "user", "content": []}])
    tool_turn["content"].insert(0, {"type": "thinking", "thinking": "", "signature": "s"})
    assert thinking_allowed([tool_turn, {"role": "user", "content": []}])
    assert thinking_allowed([{"role": "user", "content": []}])


def test_check_constructs_fallbacks(tmp_path):
    life = make(tmp_path, [])
    life.cfg["fallback"] = [{"provider": "nope", "model": "x"}]
    with pytest.raises(LLMError):
        life.router.check()


def test_memory_tool(tmp_path, monkeypatch):
    life = make(tmp_path, [[use("register", "r", name="Lin")]])
    life.run(max_calls=1)
    text, err = life.memory_tool({"action": "add", "text": "Anthony likes sqlite"})
    assert not err and text.startswith("saved as memory ")
    cid = int(text.rsplit(" ", 1)[1])
    text, err = life.memory_tool({"action": "search", "query": "sqlite"})  # no network: word matches only
    assert not err and "word matches only" in text and f"[{cid}]" in text
    assert "Anthony likes sqlite" in life.memory_tool({"action": "show", "id": cid})[0]
    assert life.memory_tool({"action": "forget", "id": cid}) == (f"forgot {cid}", False)
    assert life.memory_tool({"action": "search", "query": "x", "since": "soon"})[1]
    assert "memory" in [t["name"] for t in life.tools()]


def test_status_command(tmp_path, capsys):
    from station.cli import main

    life = routed(tmp_path, Flaky([], LLMError("token expired", "auth", status=401)), FakeLLM([ok()]))
    with open(tmp_path / "station.yaml", "a") as f:
        f.write("fallback: [{provider: anthropic, model: claude-opus-5-5}]\n")
    life.call_model([], [], [{"role": "user", "content": []}])
    life.store.save_state(life.state)
    assert main(["--root", str(tmp_path), "status"]) == 0
    out = capsys.readouterr().out
    assert "Station: never started" in out
    assert "FALLBACK" in out and "auth: token expired" in out
    assert "Credits: daily 0.00/5" in out and "Notices unread: 1" in out and "Errors in 24 h: 1" in out
    assert main(["--root", str(tmp_path), "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["model_route"]["active"] == 1
