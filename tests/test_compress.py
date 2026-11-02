"""Compression: automatic threshold, the compress tool, failure handling, and protocol integrity."""

import json

from station.prune import DUP, STUB, prune

from test_loop import make, use

HANDOFF = [{"type": "text", "text": "Handoff: I am reading a book, now at chapter three."}]


def bash(i):
    return [{"type": "openai_reasoning", "encrypted_content": f"enc{i}"}, use("bash", f"b{i}", command=f"echo {i}")]


def stored(tmp_path, tid="main"):
    """The live conversation, as Station reads it."""
    from station.store import Store

    return Store(tmp_path / "state").load_history(tid)


def raw(tmp_path, tid="main"):
    """Every line of the thread's file, markers included."""
    path = tmp_path / "state" / "threads" / f"{tid}.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def configure(life, threshold, keep=2):
    life.cfg["compress"].update(threshold_tokens=threshold, keep_cycles=keep)


def test_auto_compress_at_threshold(tmp_path):
    # Fake usage reports 300 input tokens on every call, so the thread is over the threshold
    # from the start; compression waits until there are more than keep_cycles assistant turns.
    life = make(tmp_path, [[use("register", "r", name="Lin")], bash(1), bash(2), HANDOFF, bash(3)])
    life.llm.input_tokens = 300
    configure(life, threshold=250, keep=2)
    life.run(max_calls=4)
    reqs = life.llm.requests
    summary_req = reqs[3]
    assert summary_req["tool_choice"] == "none"
    assert [r["tool_choice"] for r in reqs] == [None, None, None, "none", None]
    last = summary_req["messages"][-1]["content"]
    assert "just write a handoff" in last[-1]["text"] and not any("<board>" in str(b) for b in last)
    hist = stored(tmp_path)
    assert hist[0]["role"] == "user" and "Compression 1 of this thread" in hist[0]["content"][0]["text"]
    assert hist[0]["content"][1]["text"] == HANDOFF[0]["text"]
    assert "Welcome to Station" not in json.dumps(hist, ensure_ascii=False)
    # The tail is the last 2 assistant turns at compression time; every tool_use keeps its result.
    uses = {b["id"] for m in hist if m["role"] == "assistant" for b in m["content"] if b["type"] == "tool_use"}
    results = {b["tool_use_id"] for m in hist if m["role"] == "user" for b in m["content"] if b["type"] == "tool_result"}
    assert uses == results == {"b1", "b2", "b3"}
    # Reasoning in the kept tail is replayed, every turn of it.
    reasoning = [b["encrypted_content"] for m in hist for b in m["content"] if b["type"] == "openai_reasoning"]
    assert reasoning == ["enc1", "enc2", "enc3"]
    assert [b["encrypted_content"] for m in reqs[4]["messages"] for b in m["content"]
            if b.get("type") == "openai_reasoning"] == ["enc1", "enc2"]
    # The whole history stays in the same file, before one marker; nothing is archived elsewhere.
    lines = raw(tmp_path)
    marks = [i for i, row in enumerate(lines) if "compaction" in row]
    assert len(marks) == 1 and lines[marks[0]]["compaction"]["method"] == "summary"
    assert "Welcome to Station" in json.dumps(lines[: marks[0]], ensure_ascii=False)
    assert lines[marks[0] + 1:] == hist
    assert not (tmp_path / "state" / "archive").exists()
    assert reqs[4]["messages"][0]["content"][1]["text"] == HANDOFF[0]["text"]


def test_below_threshold_does_nothing(tmp_path):
    life = make(tmp_path, [[use("register", "r", name="Lin")], bash(1), bash(2), bash(3), bash(4)])
    configure(life, threshold=250_000, keep=2)
    life.run(max_calls=5)
    assert all(r["tool_choice"] is None for r in life.llm.requests)
    assert not any("compaction" in row for row in raw(tmp_path))


def test_failed_summary_keeps_history_and_backs_off(tmp_path):
    life = make(tmp_path, [[use("register", "r", name="Lin")], bash(1), bash(2),
                           [use("bash", "x", command="touch should-not-exist")], bash(3), bash(4)])
    life.llm.input_tokens = 300
    configure(life, threshold=250, keep=2)
    life.run(max_calls=5)
    reqs = life.llm.requests
    assert reqs[3]["tool_choice"] == "none"
    # The tool call inside the summary was not executed, and history is unchanged.
    assert not (tmp_path / "home" / "should-not-exist").exists()
    assert "Welcome to Station" in json.dumps(stored(tmp_path), ensure_ascii=False)
    assert life.state["threads"]["main"]["compress_retry_at"] == 330
    # Context stays at 300 < 330, so no new attempt on the following calls.
    assert [r["tool_choice"] for r in reqs[4:]] == [None, None]
    assert '"kind": "compress_failed"' in (tmp_path / "state" / "log.jsonl").read_text()


def test_compress_tool(tmp_path):
    life = make(tmp_path, [[use("register", "r", name="Lin")], bash(1), bash(2), bash(3),
                           [use("compress", "c", focus="thoughts on chapter three")], HANDOFF, bash(4)])
    configure(life, threshold=250_000, keep=2)
    life.run(max_calls=6)
    reqs = life.llm.requests
    prompt = reqs[5]["messages"][-1]["content"][-1]["text"]
    assert "Be sure to keep: thoughts on chapter three" in prompt
    hist = stored(tmp_path)
    assert hist[0]["content"][1]["text"] == HANDOFF[0]["text"]
    # The compress call itself is in the kept tail, with its result.
    assert any(b.get("tool_use_id") == "c" for m in hist for b in m["content"])


def test_compress_tool_with_too_little_history(tmp_path):
    life = make(tmp_path, [[use("register", "r", name="Lin")], [use("compress", "c")], bash(1)])
    life.run(max_calls=3)
    note = life.llm.requests[2]["messages"][-1]["content"][-1]
    assert "<board>" in note["text"]
    texts = " ".join(str(b.get("text", "")) for b in life.llm.requests[2]["messages"][-1]["content"])
    assert "Too little to compress" in texts


def test_threads_compress_independently(tmp_path):
    life = make(tmp_path, [[use("register", "r", name="Lin")], bash(1), bash(2), [use("focus", "f", action="new", title="A")]])
    life.run(max_calls=4)
    assert life.state["current"] == "t1"
    configure(life, threshold=250, keep=1)
    # main is over the threshold, but the current thread t1 has no usage yet: nothing happens.
    life.state["threads"]["main"]["context"] = 300
    t1 = life.store.load_history("t1")
    assert life.maybe_compress(t1) == t1
    assert not any("compaction" in row for row in raw(tmp_path, "main"))
    # Back in main, the next call compresses main only.
    life.llm.script = [HANDOFF]
    life.focus_tool({"action": "switch", "id": "main"})
    history = life.recover(life.apply_switch())
    history = life.maybe_compress(history)
    assert history[0]["content"][1]["text"] == HANDOFF[0]["text"]
    assert sum("compaction" in row for row in raw(tmp_path, "main")) == 1
    assert not any("compaction" in row for row in raw(tmp_path, "t1"))


def bash_big(i, chars=5000):
    return [use("bash", f"g{i}", command=f"head -c {chars} /dev/zero | tr '\\\\0' x; echo; echo {i}")]


def test_prune_stubs_and_dedupes():
    big = "[exit 0]\n" + "x" * 3000 + "\n"
    msgs = [
        {"role": "assistant", "content": [use("bash", "a", command="cat big.log")]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": big}]},
        {"role": "assistant", "content": [use("web", "w", action="extract", urls=["https://x.test"])]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "w", "content": "y" * 500}]},
        {"role": "assistant", "content": [use("bash", "b", command="cat big.log")]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "b", "content": big}]},
    ]
    out, count, removed = prune(msgs, end=4, min_chars=2000)
    first, small, last = (out[i]["content"][0]["content"] for i in (1, 3, 5))
    assert first == DUP  # same as the newer copy, which is kept in full
    assert small == "y" * 500  # under min_chars
    assert last == big  # inside the protected tail
    assert count == 1 and removed == len(big) - len(DUP)
    out, _, _ = prune(msgs[:2], end=2, min_chars=2000)
    assert out[1]["content"][0]["content"] == f"[bash] `cat big.log` [exit 0], 2 lines{STUB}"
    assert msgs[1]["content"][0]["content"] == big  # input not mutated


def test_auto_prunes_before_summarizing(tmp_path):
    # Five big outputs, keep 2 cycles: three old ones become stubs and that is enough.
    life = make(tmp_path, [[use("register", "r", name="Lin")], *[bash_big(i) for i in range(5)], bash(9)])
    configure(life, threshold=250_000, keep=2)
    life.run(max_calls=6)
    # The stubs remove about 10% of the request's characters, so 300 tokens becomes about 271.
    life.cfg["compress"].update(threshold_tokens=290, prune_target=1.0)
    life.state["threads"]["main"]["context"] = 300
    history = life.maybe_compress(life.store.load_history("main"))
    assert all(r["tool_choice"] is None for r in life.llm.requests)  # no summary call
    results = [b["content"] for m in history if m["role"] == "user" for b in m["content"]
               if b.get("type") == "tool_result"]
    assert sum(r.endswith(STUB) for r in results) == 3
    assert "Welcome to Station" in json.dumps(history, ensure_ascii=False)  # nothing else changed
    assert "old tool outputs were replaced by one-line stubs" in history[-1]["content"][-1]["text"]
    assert life.store.load_history("main") == history
    lines = raw(tmp_path)
    (mark,) = [i for i, row in enumerate(lines) if "compaction" in row]
    assert lines[mark]["compaction"]["method"] == "prune"
    assert not any(json.dumps(m).count(STUB) for m in lines[:mark])  # the originals are kept in full
    assert '"method": "prune"' in (tmp_path / "state" / "log.jsonl").read_text()


def test_prune_not_enough_falls_back_to_summary(tmp_path):
    life = make(tmp_path, [[use("register", "r", name="Lin")], *[bash_big(i, 2500) for i in range(3)]])
    configure(life, threshold=250_000, keep=2)
    life.run(max_calls=4)
    # Pretend most of the context is something prune cannot touch.
    life.cfg["compress"]["threshold_tokens"] = 250
    life.state["threads"]["main"]["context"] = 100_000
    life.llm.script = [HANDOFF]
    history = life.maybe_compress(life.store.load_history("main"))
    assert life.llm.requests[-1]["tool_choice"] == "none"
    assert history[0]["content"][1]["text"] == HANDOFF[0]["text"]
    assert '"kind": "prune_skipped"' in (tmp_path / "state" / "log.jsonl").read_text()


def test_compress_hint_once_per_cycle(tmp_path):
    life = make(tmp_path, [[use("register", "r", name="Lin")], bash(1), bash(2), bash(3)])
    life.llm.input_tokens = 210
    configure(life, threshold=250, keep=2)
    life.cfg["compress"]["enabled"] = True
    life.run(max_calls=4)
    texts = [b.get("text", "") for m in stored(tmp_path) if m["role"] == "user" for b in m["content"]]
    hints = [t for t in texts if "compressed automatically" in t]
    assert len(hints) == 1 and "Use compress" in hints[0]
    # The board itself no longer carries the threshold.
    assert "compressed automatically" not in life.llm.requests[-1]["messages"][-1]["content"][-1]["text"]


def test_rejected_old_reasoning_is_retried_without_it(tmp_path):
    from station.llm import LLMError

    life = make(tmp_path, [[use("register", "r", name="Lin")], bash(1), bash(2), bash(3)])
    life.run(max_calls=3)
    seen = []

    def call(system, tools, messages, tool_choice=None):
        seen.append(messages)
        if len(seen) == 1:
            raise LLMError("HTTP 400: invalid encrypted content", "request", status=400)
        return {"content": [{"type": "text", "text": "ok"}], "usage": {}}

    life.llm.call = call
    history = life.store.load_history("main")
    assert life.call_model([], [], history)["content"][0]["text"] == "ok"
    found = lambda msgs: [b["encrypted_content"] for m in msgs for b in m["content"]
                          if b.get("type") == "openai_reasoning"]
    # The retry keeps only the last assistant turn's reasoning (its tool loop may still need it).
    assert found(seen[0]) == ["enc1", "enc2"] and found(seen[1]) == ["enc2"]
    assert "rejected with old reasoning" in (tmp_path / "state" / "errors.jsonl").read_text()
    assert found(life.store.load_history("main")) == ["enc1", "enc2"]  # the history on disk keeps it
