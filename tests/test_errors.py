"""Error kinds, the error log, and what the loop does with each kind. Offline: fake models only."""

import json
import urllib.error
from email.message import Message
from io import BytesIO

import pytest

from station import errors
from station.cli import main
from station.llm import LLMError, _with_retries

from test_loop import FakeLLM, make, use


class FailingLLM(FakeLLM):
    """Raise the queued errors first, then follow the script."""

    def __init__(self, failures, script):
        super().__init__(script)
        self.failures = list(failures)

    def call(self, system, tools, messages, tool_choice=None):
        if self.failures:
            self.requests.append({"messages": messages, "failed": True})
            raise self.failures.pop(0)
        return super().call(system, tools, messages, tool_choice)


def failing(tmp_path, failures, script, **cfg):
    life = make(tmp_path, [])
    life.llm = FailingLLM(failures, script)
    life.cfg["errors"].update(cfg)
    life._pause = lambda seconds: life.pauses.append(seconds)
    life.pauses = []
    return life


def rows(tmp_path):
    return errors.ErrorLog(tmp_path / "state").recent(100)


def inbox_texts(tmp_path):
    return [json.loads(p.read_text())["text"] for p in sorted((tmp_path / "inbox").glob("*.json"))]


def delivered(life):
    return json.dumps([r["messages"] for r in life.llm.requests if not r.get("failed")], ensure_ascii=False)


@pytest.mark.parametrize("status,body,kind", [
    (401, "", "auth"), (403, "forbidden", "auth"), (402, "", "quota"),
    (429, '{"error": {"type": "usage_limit_reached"}}', "quota"), (429, "slow down", "rate_limit"),
    (400, "You're out of extra usage", "quota"), (400, "bad tool schema", "request"),
    (404, "", "request"), (500, "", "transient"), (529, "", "transient"), (408, "", "transient"),
])
def test_classify_http(status, body, kind):
    assert errors.classify_http(status, body) == kind


def test_retry_after_sources():
    assert errors.retry_after({"retry-after": "30"}) == 30
    assert errors.retry_after({}, '{"error": {"resets_in_seconds": 90}}') == 90
    assert errors.retry_after({}, '{"resets_at": 1100}', now=1000) == 100
    assert errors.retry_after({}, "not json") is None and errors.retry_after(None) is None


def test_redaction():
    text = ("Authorization: Bearer abcdefghijklmnop sk-proj-ABCDEFGHIJKLMNOP tvly-abcdefghijk "
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcdefghij api_key=hunter2hunter2 "
            '"access_token": "zzzzzzzzzz"')
    out = errors.redact(text)
    for secret in ("abcdefghijklmnop", "ABCDEFGHIJKLMNOP", "tvly-abcdefghijk", "eyJhbGci", "hunter2", "zzzzzzzzzz"):
        assert secret not in out
    assert "api_key=[REDACTED]" in out
    assert str(LLMError("HTTP 500: sk-ant-oat01-ABCDEFGHIJ")).count("[REDACTED]") == 1


def test_log_rotates(tmp_path):
    log = errors.ErrorLog(tmp_path, max_bytes=200)
    for i in range(10):
        log.record("x", "bug", f"error {i} " + "y" * 50)
    assert (tmp_path / "errors.1.jsonl").exists() and log.recent()[-1]["message"].startswith("error 9")


def http_error(status, body="", headers=None):
    msg = Message()
    for k, v in (headers or {}).items():
        msg[k] = v
    return urllib.error.HTTPError("https://x.invalid", status, "x", msg, BytesIO(body.encode()))


def test_with_retries_does_not_retry_quota(monkeypatch):
    monkeypatch.setattr("station.llm.time.sleep", lambda _: None)
    calls = []

    def quota():
        calls.append(1)
        raise http_error(429, '{"error": {"type": "usage_limit_reached", "resets_in_seconds": 600}}')

    with pytest.raises(LLMError) as e:
        _with_retries(quota)
    assert e.value.kind == "quota" and e.value.retry_after == 600 and len(calls) == 1
    calls.clear()

    def overloaded():
        calls.append(1)
        raise http_error(529, "overloaded", {"Retry-After": "7"})

    with pytest.raises(LLMError) as e:
        _with_retries(overloaded)
    assert e.value.kind == "transient" and e.value.status == 529 and len(calls) == 4


def test_transient_failures_recover_and_tell_the_ai(tmp_path):
    life = failing(tmp_path, [LLMError("HTTP 503", "transient"), LLMError("HTTP 503", "transient")],
                   [[use("register", "r", name="Lin")], [use("bash", "b", command="true")]], retry_minutes=2)
    assert life.run(max_calls=2) == "max_calls"
    assert life.pauses == [120, 120]
    logged = rows(tmp_path)
    assert [r["kind"] for r in logged] == ["transient", "transient", "recovered"]
    assert not any(r["fatal"] for r in logged)
    # The AI hears about the outage in the next tool result, not as a separate wake.
    assert "Your model calls failed 2 times (transient)" in delivered(life)


def test_retryable_failures_stop_after_max_consecutive(tmp_path):
    life = failing(tmp_path, [LLMError("HTTP 503", "transient")] * 3, [], max_consecutive=3)
    with pytest.raises(LLMError):
        life.run()
    logged = rows(tmp_path)
    assert [r["fatal"] for r in logged] == [False, False, True]
    assert "3 failures in a row" in inbox_texts(tmp_path)[-1]


@pytest.mark.parametrize("kind", ["request", "config", "bug"])
def test_fatal_kinds_stop_at_once(tmp_path, kind):
    life = failing(tmp_path, [LLMError("broken", kind)], [])
    with pytest.raises(LLMError):
        life.run()
    assert life.pauses == [] and rows(tmp_path)[-1]["fatal"]
    assert "fail the same way every time" in inbox_texts(tmp_path)[-1]


def test_auth_and_quota_wait_with_backoff_and_never_stop(tmp_path):
    failures = [LLMError("codex auth: expired", "auth")] * 6 + [LLMError("quota", "quota", retry_after=7200)]
    life = failing(tmp_path, failures, [[use("register", "r", name="Lin")]],
                   max_consecutive=2, wait_minutes=1, max_wait_minutes=30)
    assert life.run(max_calls=1) == "max_calls"
    # 1, 2, 4, 8, 16, 30 (capped) minutes; then the provider's reset hint, capped too.
    assert life.pauses == [60, 120, 240, 480, 960, 1800, 1800]
    assert not any(r["fatal"] for r in rows(tmp_path))
    assert rows(tmp_path)[-1]["kind"] == "recovered"


def test_tool_bug_is_logged_but_bad_arguments_are_not(tmp_path):
    life = make(tmp_path, [[use("register", "r", name="Lin")],
                           [use("todo", "t", action="nope")],
                           [use("diary", "d", action="list")],
                           [use("bash", "b", command="true")]])
    life.diary.tool = lambda *a: 1 / 0
    life.run(max_calls=4)
    bad_args, bug = [r["messages"][-1]["content"][0] for r in life.llm.requests[2:4]]
    assert bad_args["is_error"] and "unknown action" in bad_args["content"]
    assert bug["is_error"] and "ZeroDivisionError" in bug["content"] and "bug in Station" in bug["content"]
    logged = rows(tmp_path)
    assert len(logged) == 1 and logged[0]["where"] == "tool" and logged[0]["kind"] == "bug"
    assert "Traceback" in logged[0]["detail"]


def test_bad_tool_arguments_are_not_logged(tmp_path):
    life = make(tmp_path, [[use("register", "r", name="Lin")], [use("todo", "t", action="nope")],
                           [use("bash", "b", command="true")]])
    life.run(max_calls=3)
    assert life.llm.requests[2]["messages"][-1]["content"][0]["is_error"] and rows(tmp_path) == []


def test_crash_is_recorded_redacted_and_noticed(tmp_path):
    life = make(tmp_path, [[use("register", "r", name="Lin")]])
    life.compose = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom sk-ant-oat01-ABCDEFGHIJKL"))
    with pytest.raises(RuntimeError):
        life.run()
    logged = rows(tmp_path)[-1]
    assert logged["kind"] == "crash" and logged["fatal"] and "ABCDEFGHIJKL" not in json.dumps(logged)
    assert "Station crashed (RuntimeError" in inbox_texts(tmp_path)[-1]


def test_auto_compress_failure_tells_the_ai(tmp_path):
    from test_compress import bash, configure

    life = make(tmp_path, [[use("register", "r", name="Lin")], bash(1), bash(2),
                           [use("bash", "x", command="true")], bash(3)])
    life.llm.input_tokens = 300
    configure(life, threshold=250, keep=2)
    life.run(max_calls=4)
    assert rows(tmp_path)[-1]["where"] == "compress"
    # Queued before the call; delivered with the next tool result, which is in the stored history.
    from test_loop import main_history
    assert "Automatic compression of this thread failed" in main_history(tmp_path)


def test_errors_command(tmp_path, capsys):
    log = errors.ErrorLog(tmp_path / "state")
    log.record("model", "auth", "codex auth: expired")
    log.record("loop", "crash", "boom", fatal=True)
    assert main(["--root", str(tmp_path), "errors", "-n", "5"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].endswith("model/auth: codex auth: expired") and "loop/crash FATAL: boom" in out[1]
    main(["--root", str(tmp_path), "errors", "--json"])
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["kind"] == "crash"
