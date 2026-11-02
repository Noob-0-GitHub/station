"""The view tool, image blocks on every provider, and pruning of old images. No network."""

import base64
import io
import json

import pytest
from PIL import Image

from station import vision
from station.claude_code import frames
from station.llm import to_responses_input
from station.prune import prune, text_size


def png(path, size=(40, 30), mode="RGB"):
    Image.new(mode, size, (200, 30, 30) if mode == "RGB" else (200, 30, 30, 128)).save(path)
    return path


def test_view_reads_an_image_in_home(tmp_path):
    png(tmp_path / "a.png")
    content, desc = vision.load("a.png", tmp_path)
    assert content[0] == {"type": "text", "text": "a.png: PNG 40x30"}
    img = content[1]
    assert img["type"] == "image" and img["source"]["media_type"] == "image/png"
    assert base64.b64decode(img["source"]["data"]) == (tmp_path / "a.png").read_bytes()  # small: sent as is
    assert vision.load("~/a.png", tmp_path)[1] == "~/a.png: PNG 40x30"
    assert vision.load(str(tmp_path / "a.png"), tmp_path)[0][1] == img


def test_large_images_are_scaled(tmp_path):
    png(tmp_path / "big.png", size=(4000, 1000))
    content, desc = vision.load("big.png", tmp_path)
    assert "shown at 1568x392" in desc
    shown = Image.open(io.BytesIO(base64.b64decode(content[1]["source"]["data"])))
    assert shown.size == (1568, 392)
    png(tmp_path / "alpha.png", size=(3000, 3000), mode="RGBA")
    content, _ = vision.load("alpha.png", tmp_path)
    assert content[1]["source"]["media_type"] in ("image/png", "image/jpeg")


@pytest.mark.parametrize("bad", ["../outside.png", "/etc/hostname", "link.png", "nope.png", "text.png"])
def test_view_refuses(tmp_path, bad):
    home = tmp_path / "home"
    home.mkdir()
    png(tmp_path / "outside.png")
    (home / "link.png").symlink_to(tmp_path / "outside.png")
    (home / "text.png").write_text("not an image")
    with pytest.raises(ValueError):
        vision.load(bad, home)


def result(content, tid="t1"):
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": content}]}


def call(tid="t1", path="a.png"):
    return {"role": "assistant", "content": [{"type": "tool_use", "id": tid, "name": "view", "input": {"path": path}}]}


def test_codex_gets_input_image(tmp_path):
    png(tmp_path / "a.png")
    content, _ = vision.load("a.png", tmp_path)
    items = to_responses_input([{"role": "user", "content": [{"type": "text", "text": "hi"}]}, call(), result(content)])
    out = items[-1]
    assert out["type"] == "function_call_output" and out["call_id"] == "t1"
    assert out["output"][0] == {"type": "input_text", "text": "a.png: PNG 40x30"}
    assert out["output"][1]["type"] == "input_image" and out["output"][1]["image_url"].startswith("data:image/png;base64,")
    # Text-only results are unchanged.
    assert to_responses_input([result("plain")])[0]["output"] == "plain"


def test_claude_cli_gets_the_image_block(tmp_path):
    png(tmp_path / "a.png")
    content, _ = vision.load("a.png", tmp_path)
    out = frames([{"role": "user", "content": [{"type": "text", "text": "hi"}]}, call(), result(content)], {"view"})
    sent = out[-1]["message"]["content"][0]["content"]
    assert sent[1]["type"] == "image" and sent[1]["source"]["data"] == content[1]["source"]["data"]


def test_old_images_are_pruned_and_counted(tmp_path):
    png(tmp_path / "a.png")
    content, _ = vision.load("a.png", tmp_path)
    history = [{"role": "user", "content": [{"type": "text", "text": "hi"}]}, call(), result(content),
               {"role": "assistant", "content": [{"type": "text", "text": "a red box"}]},
               {"role": "user", "content": [{"type": "text", "text": "next"}]}]
    assert text_size(content[1]) == vision.IMAGE_CHARS  # not the base64 length
    new, count, removed = prune(history, end=3, min_chars=10_000)
    assert count == 1 and removed == vision.IMAGE_CHARS
    assert new[2]["content"][0]["content"] == "[view] a.png: PNG 40x30 (old image cleared)"
    assert history[2]["content"][0]["content"] is content  # the input is not changed
    new, count, _ = prune(history, end=2, min_chars=10_000)  # recent images stay
    assert count == 0


def test_loop_runs_view(tmp_path):
    from test_loop import make

    life = make(tmp_path, [])
    life.state["status"] = "registered"
    life.home.mkdir(parents=True, exist_ok=True)
    png(life.home / "shot.png")
    content, err = life.run_tool({"name": "view", "input": {"path": "~/shot.png"}})
    assert not err and content[1]["type"] == "image"
    json.dumps(content)  # storable in the thread's JSONL
