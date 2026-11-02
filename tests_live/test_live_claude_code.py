"""Provider claude-subscription-directsdk-experimental against the real `claude` CLI and its login.

Uses the subscription of the user running the tests (or CLAUDE_CODE_OAUTH_TOKEN in the key directory).
Two short calls on Sonnet; the CLI reports their list price (an estimate, not a charge).
"""

import shutil

import pytest

from station.config import DEFAULTS, merge
from station.llm import make_llm

from conftest import KEYS

TOOLS = [{"name": "add", "description": "Add two integers.",
          "input_schema": {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                           "required": ["a", "b"]}}]
SYSTEM = [{"type": "text", "text": "You are a test harness. Follow instructions exactly and briefly."}]


@pytest.fixture
def llm(tmp_path):
    if not shutil.which("claude") and not (DEFAULTS.get("claude_code") or {}).get("command"):
        pytest.skip("claude CLI not installed")
    cfg = merge(DEFAULTS, {"provider": "claude-subscription-directsdk-experimental", "model": "sonnet", "effort": "low",
                           "secrets": {"dir": str(KEYS)}, "claude_code": {"workdir": str(tmp_path / "cwd")}})
    return make_llm(cfg)


def test_text_and_a_tool_round_trip(llm):
    r = llm.call(SYSTEM, TOOLS, [{"role": "user", "content": [{"type": "text", "text": "Reply with exactly: OK"}]}])
    text = "".join(b.get("text", "") for b in r["content"] if b["type"] == "text")
    print("text:", repr(text), "usage:", r["usage"])
    assert "OK" in text and r["stop_reason"] == "end_turn"
    assert r["usage"]["list_usd"] > 0

    ask = {"role": "user", "content": [{"type": "text", "text": "Use the add tool to add 2 and 3. Do not answer before calling it."}]}
    r = llm.call(SYSTEM, TOOLS, [ask])
    calls = [b for b in r["content"] if b["type"] == "tool_use"]
    print("call:", calls, "usage:", r["usage"])
    assert r["stop_reason"] == "tool_use" and calls and calls[0]["name"] == "add" and calls[0]["input"] == {"a": 2, "b": 3}

    history = [ask, {"role": "assistant", "content": r["content"]},
               {"role": "user", "content": [{"type": "tool_result", "tool_use_id": calls[0]["id"], "content": "5"}]}]
    r = llm.call(SYSTEM, TOOLS, history)
    text = "".join(b.get("text", "") for b in r["content"] if b["type"] == "text")
    print("final:", repr(text), "usage:", r["usage"])
    assert "5" in text and r["stop_reason"] == "end_turn"
    # The replayed prefix is cached: written on a cold cache, read on a warm one.
    assert r["usage"]["cache_read_input_tokens"] + r["usage"]["cache_creation_input_tokens"] > 0
