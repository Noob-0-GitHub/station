"""The view tool against real models: each provider must read a word drawn in a test image.

One call each on Claude (CLI subscription) and Codex (ChatGPT subscription, Hermes pool in dev).
"""

import shutil

import pytest
from PIL import Image, ImageDraw, ImageFont

from station import vision
from station.config import DEFAULTS, merge
from station.llm import LLMError, make_llm

from conftest import KEYS

SYSTEM = [{"type": "text", "text": "You are a test harness. Answer briefly."}]
TOOLS = [t for t in __import__("station.tools", fromlist=["LIFE_TOOLS"]).LIFE_TOOLS if t["name"] == "view"]


def picture(home):
    img = Image.new("RGB", (900, 300), "white")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", 120)
    except OSError:
        font = ImageFont.load_default(size=120)
    d.text((60, 80), "PELICAN", fill="black", font=font)
    d.ellipse((760, 100, 860, 200), fill="red")
    img.save(home / "card.png")


def history(home):
    content, _ = vision.load("card.png", home)
    return [{"role": "user", "content": [{"type": "text", "text": "Look at ~/card.png with the view tool."}]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_v1", "name": "view", "input": {"path": "~/card.png"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_v1", "content": content},
                                         {"type": "text", "text": "What word is written, and what colour is the circle? One line."}]}]


@pytest.mark.parametrize("provider,model", [("claude-subscription-directsdk-experimental", "sonnet"), ("codex", "gpt-6.1-sol")])
def test_model_sees_the_image(tmp_path, monkeypatch, provider, model):
    # Hermes hides its credential pool from anything running under pytest; this test is an
    # explicit opt-in to the real subscription, so the Codex helper must not look like a test.
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    if provider.startswith("claude") and not shutil.which("claude"):
        pytest.skip("claude CLI not installed")
    picture(tmp_path)
    cfg = merge(DEFAULTS, {"provider": provider, "model": model, "effort": "low", "secrets": {"dir": str(KEYS)},
                           "claude_code": {"workdir": str(tmp_path / "cwd")}})
    try:
        r = make_llm(cfg).call(SYSTEM, TOOLS, history(tmp_path))
    except LLMError as e:
        if e.kind in ("auth", "config"):
            pytest.skip(f"{provider}: {e}")
        raise
    text = "".join(b.get("text", "") for b in r["content"] if b["type"] == "text")
    print(provider, repr(text), r["usage"])
    assert "PELICAN" in text.upper() and "RED" in text.upper()
