"""A stop signal interrupts a blocked model call; silent streams time out; slow calls are logged."""

import contextlib
import json
import os
import signal
import time

import pytest

from station import router as router_mod
from station.llm import CallInterrupted, Codex, LLMError, _with_retries
from test_loop import FakeLLM, make


def ok(text="ok"):
    return [{"type": "text", "text": text}]


def log_rows(tmp_path):
    path = tmp_path / "state" / "log.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


@contextlib.contextmanager
def restore_signals():
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield
    finally:
        for s, handler in saved.items():
            signal.signal(s, handler)


class Blocking(FakeLLM):
    """A model call that hangs (like the silent Codex stream) after Station receives SIGTERM."""

    def call(self, system, tools, messages, tool_choice=None):
        self.requests.append({"blocked": True})
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(30)  # the signal handler must interrupt this
        raise AssertionError("the stop signal did not interrupt the call")


def registered(tmp_path, main, backup=None):
    life = make(tmp_path, [])
    if backup is not None:
        life.cfg["fallback"] = [{"provider": "anthropic", "model": "claude-opus-5-5"}]
        life.router.clients[1] = backup
    life.llm = main
    life.state["status"] = "registered"
    life.store.append_history("main", {"role": "user", "content": [{"type": "text", "text": "awake"}]})
    return life


def test_sigterm_interrupts_a_blocked_model_call(tmp_path):
    backup = FakeLLM([ok("backup")])
    life = registered(tmp_path, Blocking([]), backup)
    started = time.monotonic()
    with restore_signals():
        assert life.run() == "stopped"
    assert time.monotonic() - started < 10
    assert backup.requests == []  # no fallback
    saved = life.store.load_state()
    assert saved["last_exit"]["reason"] == "stopped"
    assert saved.get("model_route", {}).get("active", 0) == 0
    assert "model_failed" not in [r["kind"] for r in log_rows(tmp_path)]
    assert life.in_model_call is False


def test_signal_outside_a_model_call_only_sets_the_flag(tmp_path):
    life = make(tmp_path, [])
    life._on_signal()
    assert life.stopping
    life.in_model_call = True
    life._on_signal()  # a second signal never raises into whatever runs next
    life.stopping = False
    life.in_model_call = True
    with pytest.raises(CallInterrupted):
        life._on_signal()


def test_with_retries_reports_each_retry(monkeypatch):
    monkeypatch.setattr("station.llm.time.sleep", lambda _: None)
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise TimeoutError("timed out")
        return "done"

    retries = []
    assert _with_retries(flaky, retries) == "done"
    assert [r["wait"] for r in retries] == [5.0, 15.0]
    assert all("TimeoutError" in r["error"] for r in retries)
    assert _with_retries(lambda: "plain") == "plain"  # the list stays optional


class FakeCred:
    def get(self, force=False):
        return {"headers": {}, "token": "t", "base_url": "https://codex.invalid"}


class FakeStream:
    def __init__(self, lines):
        self.lines = [x.encode() for x in lines]

    def __enter__(self):
        return iter(self.lines)

    def __exit__(self, *exc):
        return False


COMPLETED = ['data: {"type": "response.completed", "response": {"output": [{"type": "message", "role": '
             '"assistant", "content": [{"type": "output_text", "text": "hi"}]}], "usage": {"input_tokens": 3, '
             '"output_tokens": 2}}}']


def codex(idle_timeout=None):
    cfg = {"model": "gpt-test", "codex": {"token_file": "/nonexistent"}}
    if idle_timeout is not None:
        cfg["codex"]["idle_timeout"] = idle_timeout
    client = Codex(cfg)
    client.cred = FakeCred()
    return client


def test_codex_stream_uses_the_idle_timeout_and_retries_a_silent_stream(monkeypatch):
    monkeypatch.setattr("station.llm.time.sleep", lambda _: None)
    seen = []

    def post(url, data, headers, timeout=600):
        seen.append(timeout)
        if len(seen) == 1:
            raise TimeoutError("The read operation timed out")
        return FakeStream(COMPLETED)

    monkeypatch.setattr("station.llm._post", post)
    client = codex()
    assert client.idle_timeout == 120
    result = client.call([{"type": "text", "text": "sys"}], [], [{"role": "user", "content": [{"type": "text", "text": "x"}]}])
    assert result["content"][0]["text"] == "hi"
    assert seen == [120, 120]
    assert len(client.retries) == 1 and "TimeoutError" in client.retries[0]["error"]
    assert codex(idle_timeout=30).idle_timeout == 30
    # The next call starts with an empty retry list.
    client.call([{"type": "text", "text": "sys"}], [], [{"role": "user", "content": [{"type": "text", "text": "y"}]}])
    assert client.retries == []


class Retried(FakeLLM):
    def call(self, system, tools, messages, tool_choice=None):
        self.retries = [{"error": "HTTP 503", "wait": 5.0}]
        return super().call(system, tools, messages, tool_choice)


def test_slow_or_retried_calls_are_logged(tmp_path, monkeypatch):
    life = make(tmp_path, [])
    life.llm = Retried([ok(), ok()])
    life.call_model([], [], [{"role": "user", "content": []}])
    slow = [r for r in log_rows(tmp_path) if r["kind"] == "model_call_slow"]
    assert len(slow) == 1 and slow[0]["retries"] == [{"error": "HTTP 503", "wait": 5.0}]
    assert slow[0]["model"] == f"{life.cfg['provider']}/{life.cfg['model']}"

    life.llm = FakeLLM([ok()])
    life.call_model([], [], [{"role": "user", "content": []}])
    assert len([r for r in log_rows(tmp_path) if r["kind"] == "model_call_slow"]) == 1  # fast, no retries

    monkeypatch.setattr(router_mod, "SLOW_CALL_SECONDS", 0)
    life.llm = FakeLLM([ok()])
    life.call_model([], [], [{"role": "user", "content": []}])
    rows = [r for r in log_rows(tmp_path) if r["kind"] == "model_call_slow"]
    assert len(rows) == 2 and rows[-1]["retries"] == []


def test_failed_calls_also_reset_the_in_call_flag(tmp_path):
    life = make(tmp_path, [])

    class Down(FakeLLM):
        def call(self, *a, **k):
            raise LLMError("down", "transient")

    life.llm = Down([])
    with pytest.raises(LLMError):
        life.call_model([], [], [{"role": "user", "content": []}])
    assert life.in_model_call is False
