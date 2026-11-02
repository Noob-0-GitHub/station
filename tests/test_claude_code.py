"""Provider claude-subscription-directsdk-experimental against a fake `claude` CLI. No network."""

import json
import os
import sys

import pytest

from station.claude_code import PREFIX, ClaudeCode, frames, native_model
from station.config import DEFAULTS, merge
from station.llm import LLMError, make_llm

FAKE = r'''
import json, os, pathlib, sys
log = pathlib.Path(os.environ["FAKE_LOG"])
rows = []
for line in sys.stdin:
    r = json.loads(line)
    rows.append(r)
    if r.get("shouldQuery") is False:
        print(json.dumps({"type": "result", "num_turns": 0, "is_error": False}), flush=True)
settings = json.loads(pathlib.Path(sys.argv[sys.argv.index("--settings") + 1]).read_text())
log.write_text(json.dumps({"argv": sys.argv[1:], "rows": rows, "body": json.loads(settings["env"]["CLAUDE_CODE_EXTRA_BODY"]),
                           "system": pathlib.Path(sys.argv[sys.argv.index("--system-prompt-file") + 1]).read_text(),
                           "env": {k: os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_MAX_RETRIES",
                                                                   "DISABLE_AUTO_COMPACT", "CLAUDE_CONFIG_DIR",
                                                                   "CLAUDE_CODE_OAUTH_TOKEN")},
                           "cwd": os.getcwd()}))
mode = os.environ.get("FAKE_MODE", "text")
if mode == "logout":
    print(json.dumps({"type": "assistant", "error": "authentication_failed", "message": {"role": "assistant",
          "content": [{"type": "text", "text": "Not logged in"}]}}), flush=True)
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True, "num_turns": 1}), flush=True)
    sys.exit(1)
if mode == "limit":
    print(json.dumps({"type": "assistant", "error": "rate_limit", "message": {"role": "assistant",
          "content": [{"type": "text", "text": "You've hit your session limit"}]}}), flush=True)
    print(json.dumps({"type": "result", "subtype": "success", "is_error": True, "num_turns": 1}), flush=True)
    sys.exit(1)
if mode == "tool":
    blocks = [{"type": "thinking", "thinking": "hm", "signature": "sig"},
              {"type": "tool_use", "id": "toolu_1", "name": "mcp__station__bash", "input": {"command": "ls"}}]
else:
    blocks = [{"type": "text", "text": "hello"}]
print(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": blocks, "id": "msg_1",
      "stop_reason": "tool_use" if mode == "tool" else "end_turn"}}), flush=True)
print(json.dumps({"type": "stream_event", "event": {"type": "message_stop"}}), flush=True)
usage = {"input_tokens": 3, "output_tokens": 5, "cache_read_input_tokens": 700, "cache_creation_input_tokens": 11,
         "cache_creation": {"ephemeral_1h_input_tokens": 11, "ephemeral_5m_input_tokens": 0}}
print(json.dumps({"type": "result", "num_turns": 2 if mode == "tool" else 1,
      "subtype": "error_max_turns" if mode == "tool" else "success", "is_error": mode == "tool",
      "usage": usage, "total_cost_usd": 0.0123, "modelUsage": {"claude-opus-5-5": {"costBasis": "list"}}}), flush=True)
sys.exit(1 if mode == "tool" else 0)
'''

TOOLS = [{"name": "bash", "description": "Run a command", "input_schema": {"type": "object", "properties": {"command": {"type": "string"}}}}]
SYSTEM = [{"type": "text", "text": "You are Lin.", "cache_control": {"type": "ephemeral"}}]


@pytest.fixture
def cli(tmp_path, monkeypatch):
    script = tmp_path / "claude"
    script.write_text(f"#!{sys.executable}\n" + FAKE)
    script.chmod(0o755)
    log = tmp_path / "log.json"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-not-reach-the-cli")
    keys = tmp_path / "keys"
    keys.mkdir(mode=0o700)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    cfg = merge(DEFAULTS, {"provider": "claude-subscription-directsdk-experimental", "model": "claude-opus-5-5",
                           "effort": "high", "secrets": {"dir": str(keys)},
                           "claude_code": {"command": str(script), "workdir": str(tmp_path / "cwd")}})
    return make_llm(cfg), (lambda: json.loads(log.read_text())), monkeypatch


def user(text):
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def test_text_reply_usage_and_list_price(cli):
    llm, log, _ = cli
    assert isinstance(llm, ClaudeCode)
    r = llm.call(SYSTEM, TOOLS, [user("hi")])
    assert r["content"] == [{"type": "text", "text": "hello"}] and r["stop_reason"] == "end_turn"
    assert r["usage"]["cache_read_input_tokens"] == 700 and r["usage"]["list_usd"] == pytest.approx(0.0123)
    seen = log()
    argv = seen["argv"]
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5[1m]"
    assert argv[argv.index("--tools") + 1] == "" and argv[argv.index("--max-turns") + 1] == "1"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk" and "--no-session-persistence" in argv
    assert argv[argv.index("--effort") + 1] == "high"
    assert seen["body"]["tools"][0]["name"] == PREFIX + "bash"
    assert seen["body"]["thinking"] == {"type": "adaptive"} and seen["body"]["output_config"] == {"effort": "high"}
    assert seen["system"] == "You are Lin."
    # No API key reaches the CLI; the base URL is Station's loopback relay; retries and compaction are off.
    assert seen["env"]["ANTHROPIC_API_KEY"] is None
    assert seen["env"]["ANTHROPIC_BASE_URL"].startswith("http://127.0.0.1:")
    assert seen["env"]["CLAUDE_CODE_MAX_RETRIES"] == "0" and seen["env"]["DISABLE_AUTO_COMPACT"] == "1"
    assert seen["cwd"].endswith("/cwd")


def test_history_replay_and_tool_calls(cli):
    llm, log, monkeypatch = cli
    monkeypatch.setenv("FAKE_MODE", "tool")
    history = [
        user("first"),
        {"role": "assistant", "content": [{"type": "thinking", "thinking": "t", "signature": "s"},
                                          {"type": "tool_use", "id": "toolu_0", "name": "bash", "input": {"command": "pwd"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_0", "content": "/home"},
                                     {"type": "text", "text": "<env>board</env>", "cache_control": {"type": "ephemeral"}}]},
    ]
    r = llm.call(SYSTEM, TOOLS, history)
    assert r["stop_reason"] == "tool_use"
    assert [b["type"] for b in r["content"]] == ["thinking", "tool_use"]
    assert r["content"][1]["name"] == "bash"  # the MCP prefix is removed again
    rows = log()["rows"]
    assert [x["type"] for x in rows] == ["user", "assistant", "user"]
    assert rows[0]["shouldQuery"] is False and "shouldQuery" not in rows[2]
    assert rows[1]["message"]["content"][1]["name"] == PREFIX + "bash"
    assert all("cache_control" not in b for b in rows[2]["message"]["content"])


def test_errors_map_to_station_kinds(cli):
    llm, _, monkeypatch = cli
    monkeypatch.setenv("FAKE_MODE", "logout")
    with pytest.raises(LLMError) as e:
        llm.call(SYSTEM, TOOLS, [user("hi")])
    assert e.value.kind == "auth" and "claude auth login" in str(e.value)
    monkeypatch.setenv("FAKE_MODE", "limit")
    with pytest.raises(LLMError) as e:
        llm.call(SYSTEM, TOOLS, [user("hi")])
    assert e.value.kind == "quota"


def test_missing_cli_is_a_config_error(tmp_path):
    cfg = merge(DEFAULTS, {"provider": "claude-subscription-directsdk-experimental", "model": "opus",
                           "claude_code": {"command": str(tmp_path / "nope")}})
    with pytest.raises(LLMError) as e:
        make_llm(cfg).call(SYSTEM, TOOLS, [user("hi")])
    assert e.value.kind == "config"


def test_bad_effort_is_rejected():
    with pytest.raises(LLMError):
        ClaudeCode(merge(DEFAULTS, {"model": "opus", "effort": "ultra"}))


def test_models_and_foreign_turns():
    assert native_model("opus") == "claude-opus-5-5[1m]"
    assert native_model("claude-haiku-4-5") == "claude-haiku-4-5-20251001"
    assert native_model("claude-new-model") == "claude-new-model"
    # Another provider's turn: its reasoning is dropped, its tool call is replayed as is.
    history = [user("go"),
               {"role": "assistant", "content": [{"type": "openai_reasoning", "encrypted_content": "x"},
                                                 {"type": "tool_use", "id": "call_1", "name": "bash", "input": {"command": "ls"}}]},
               {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "a.txt"}]}]
    out = frames(history, {"bash"})
    assert [f["type"] for f in out] == ["user", "assistant", "user"]
    assert out[1]["message"]["content"] == [{"type": "tool_use", "id": "call_1", "name": PREFIX + "bash", "input": {"command": "ls"}}]


def test_loop_records_the_cli_list_price(tmp_path):
    from test_loop import make

    life = make(tmp_path, [])
    life.last_route = {"provider": "claude-subscription-directsdk-experimental", "model": "claude-opus-5-5"}
    life.record_usage({"input_tokens": 3, "output_tokens": 5, "cache_read_input_tokens": 700, "list_usd": 0.0123})
    (row,) = life.store.ledger.rows(service="model")
    assert row["list_usd"] == pytest.approx(0.0123) and row["paid_usd"] == 0
    assert row["provider"] == "claude-subscription-directsdk-experimental"


def test_setup_token_from_secrets_dir(cli, tmp_path):
    llm, log, _ = cli
    llm.call(SYSTEM, TOOLS, [user("hi")])
    assert log()["env"]["CLAUDE_CODE_OAUTH_TOKEN"] is None  # none configured: the CLI's own login
    f = tmp_path / "keys" / "claude-code.env"
    f.write_text("CLAUDE_CODE_OAUTH_TOKEN=fake-setup-token\n")
    f.chmod(0o600)
    llm.call(SYSTEM, TOOLS, [user("hi")])
    assert log()["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "fake-setup-token"
    f.chmod(0o644)
    with pytest.raises(LLMError) as e:
        llm.call(SYSTEM, TOOLS, [user("hi")])
    assert e.value.kind == "config"
