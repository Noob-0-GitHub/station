"""Memory: chunking, indexing, search by meaning and by words, notes, the sleep hook. Offline:
the embeddings API is faked at urlopen with a bag-of-words vector, so related texts are near."""

import hashlib
import io
import json
import math
import re
import time

import pytest

from station import api
from station.cli import main
from station.config import load
from station import memory as memory_mod
from station.memory import Memory, chat_chunks, diary_chunks, split_long
from station.store import Store

from test_loop import make, use


def bow(text, dims):
    v = [0.0] * dims
    for w in re.findall(r"[a-z]{3,}|[\u4e00-\u9fff]", text.lower()):
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % dims] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class FakeResponse(io.BytesIO):
    status = 200
    headers = {"content-type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeEmbeddings:
    def __init__(self, monkeypatch, fail=False, mangle=None, during=None):
        self.calls, self.fail, self.mangle, self.during = [], fail, mangle, during
        monkeypatch.setattr(api.urllib.request, "urlopen", self)

    def __call__(self, req, timeout=None):
        if self.fail:
            raise api.urllib.error.URLError("no network")
        body = json.loads(req.data)
        assert req.full_url == "https://api.openai.com/v1/embeddings"
        self.calls.append(body)
        if self.during:
            self.during()
        data = [{"index": i, "embedding": bow(t, body["dimensions"])} for i, t in enumerate(body["input"])]
        if self.mangle:
            data = self.mangle(data)
        tokens = sum(len(t) // 4 + 1 for t in body["input"])
        return FakeResponse(json.dumps({"model": body["model"], "data": data,
                                        "usage": {"prompt_tokens": tokens, "total_tokens": tokens}}).encode())


@pytest.fixture
def root(tmp_path, monkeypatch):
    keys = tmp_path / "keys"
    keys.mkdir()
    (keys / "openai.env").write_text("OPENAI_API_KEY=sk-test\n")
    (keys / "openai.env").chmod(0o600)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    (tmp_path / "station.yaml").write_text(f"secrets: {{dir: {keys}}}\ncredits: {{allowances: {{daily: 5, weekly: 17.5}}}}\n")
    (tmp_path / "home" / "diary").mkdir(parents=True)
    return tmp_path


def memory(root):
    return Memory(root, load(root), Store(root / "state").ledger)


def diary(root, day, text):
    (root / "home" / "diary" / f"{day}.md").write_text(text)


def thread(root, tid, messages):
    d = root / "state" / "threads"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{tid}.jsonl").write_text("".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages))


def say(role, text):
    return {"role": role, "content": [{"type": "text", "text": text}]}


# ---- chunking ------------------------------------------------------------------

def test_diary_entries_become_dated_chunks():
    chunks = diary_chunks("2026-10-31", "# 2026-10-31\n\n## 19:05\n\nArrived and chose Mira.\n\n## 22:40\n\nRead a poem.\n")
    assert [c.title for c in chunks] == ["diary 2026-10-31 19:05", "diary 2026-10-31 22:40"]
    assert chunks[0].text == "Arrived and chose Mira." and chunks[1].ts - chunks[0].ts == 3 * 3600 + 35 * 60


def test_long_text_is_split_at_paragraphs():
    parts = split_long("\n\n".join(["x" * 900] * 4), 2000)
    assert len(parts) == 2 and all(len(p) <= 2000 for p in parts)
    assert split_long("y" * 5000, 2000) == ["y" * 2000, "y" * 2000, "y" * 1000]


def test_conversation_chunks_leave_the_live_end_and_tool_noise_out():
    msgs = [say("user", "<env>2026-11-01 Sun 09:00 PST Woke after 30 minutes.</env>"),
            {"role": "assistant", "content": [{"type": "openai_reasoning", "encrypted": "zzz"},
                                              {"type": "tool_use", "id": "1", "name": "bash", "input": {"command": "ls"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "1", "content": "a" * 5000}]}]
    msgs += [say("assistant", f"thought number {i} " + "w" * 300) for i in range(8)]
    live = chat_chunks("main", msgs, final=False)
    done = chat_chunks("main", msgs, final=True)
    assert len(done) == len(live) + 1
    first = live[0].text
    assert first.startswith("station: 2026-11-01 Sun 09:00 PST Woke") and "<env>" not in first
    assert "me: [bash]" in first and "zzz" not in first and "a" * 301 not in first
    assert live[0].ts is not None


# ---- indexing and search ---------------------------------------------------------

def test_sync_then_search_by_meaning_and_words(root, monkeypatch):
    fake = FakeEmbeddings(monkeypatch)
    diary(root, "2026-10-30", "# 2026-10-30\n\n## 10:00\n\nWalked along the beach and watched the seagulls.\n\n"
                              "## 21:00\n\nDebugged a cron expression for my schedule.\n")
    diary(root, "2026-10-31", "# 2026-10-31\n\n## 08:00\n\n今天去海边散步，看到很多海鸥。\n")
    mem = memory(root)
    r = mem.sync()
    assert r["changed"] == 2 and r["embedded"] == 3 and r["pending"] == 0 and r["error"] is None
    results, note = mem.search("seagulls beach")
    assert note is None and results[0]["title"] == "diary 2026-10-30 10:00"
    assert set(results[0]["how"]) == {"meaning", "words"}
    results, _ = mem.search("海鸥")  # two Chinese characters: matched as words
    assert results[0]["title"] == "diary 2026-10-31 08:00" and "words" in results[0]["how"]
    # Unchanged files are not read or embedded again.
    calls = len(fake.calls)
    assert mem.sync()["changed"] == 0 and len(fake.calls) == calls
    # The embedding calls went through `station api` and are in the ledger.
    rows = Store(root / "state").ledger.rows(service="openai")
    assert rows and all(r["model"] == "text-embedding-3-small" and r["list_usd"] > 0 for r in rows)


def test_edits_reindex_only_changed_entries_and_deletions_are_removed(root, monkeypatch):
    fake = FakeEmbeddings(monkeypatch)
    diary(root, "2026-10-30", "# 2026-10-30\n\n## 10:00\n\nfirst entry about tea\n\n## 11:00\n\nsecond entry about rain\n")
    mem = memory(root)
    mem.sync()
    before = sum(len(c["input"]) for c in fake.calls)
    time.sleep(0.01)
    diary(root, "2026-10-30", "# 2026-10-30\n\n## 10:00\n\nfirst entry about tea\n\n## 11:00\n\nsecond entry about snow\n")
    mem.sync()
    assert sum(len(c["input"]) for c in fake.calls) == before + 1  # only the edited entry
    assert not any("rain" in r["text"] for r in mem.search("rain")[0])
    assert any("snow" in r["text"] for r in mem.search("snow")[0])
    (root / "home" / "diary" / "2026-10-30.md").unlink()
    mem.sync()
    assert mem.status()["kinds"] == {}


def test_compression_archive_does_not_embed_twice(root, monkeypatch):
    fake = FakeEmbeddings(monkeypatch)
    msgs = [say("assistant" if i % 2 else "user", f"message {i} about the garden " + "g" * 400) for i in range(12)]
    thread(root, "main", msgs)
    mem = memory(root)
    mem.sync()
    embedded = sum(len(c["input"]) for c in fake.calls)
    # Compression: the raw history is archived, the live thread becomes a short handoff.
    (root / "state" / "archive").mkdir()
    (root / "state" / "threads" / "main.jsonl").rename(root / "state" / "archive" / "main-20261101-090000-000001.jsonl")
    thread(root, "main", [say("user", "handoff: we talked about the garden")])
    mem.sync()
    # Every chunk already embedded from the live thread is reused; only the archive's last chunk is new.
    assert sum(len(c["input"]) for c in fake.calls) - embedded <= 1
    assert mem.search("garden")[0]


def test_notes_add_show_forget(root, monkeypatch, capsys):
    FakeEmbeddings(monkeypatch)
    args = ["--root", str(root), "memory"]
    assert main(args + ["add", "Anthony prefers Chinese in chat and English in files."]) == 0
    cid = int(capsys.readouterr().out.split()[-1])
    assert main(args + ["search", "which language does Anthony prefer", "--kind", "note"]) == 0
    out = capsys.readouterr().out
    assert f"[{cid}] note" in out and "English in files" in out
    assert main(args + ["show", str(cid)]) == 0 and "from note:" in capsys.readouterr().out
    assert main(args + ["forget", str(cid)]) == 0
    assert main(args + ["show", str(cid)]) == 2


def test_only_notes_can_be_forgotten(root, monkeypatch):
    FakeEmbeddings(monkeypatch)
    diary(root, "2026-10-30", "# 2026-10-30\n\n## 10:00\n\nsomething\n")
    mem = memory(root)
    mem.sync()
    (hit,), _ = mem.search("something", limit=1)
    with pytest.raises(ValueError, match="only notes"):
        mem.forget(hit["id"])


def test_search_without_network_falls_back_to_words(root, monkeypatch, capsys):
    FakeEmbeddings(monkeypatch)
    diary(root, "2026-10-30", "# 2026-10-30\n\n## 10:00\n\nThe cronsim library parses schedules.\n")
    memory(root).sync()
    FakeEmbeddings(monkeypatch, fail=True)
    assert main(["--root", str(root), "memory", "search", "cronsim"]) == 0
    out = capsys.readouterr().out
    assert "word matches only" in out and "cronsim library" in out


def test_since_and_kind_filters(root, monkeypatch):
    FakeEmbeddings(monkeypatch)
    diary(root, "2020-01-01", "# 2020-01-01\n\n## 10:00\n\nold tea memory\n")
    diary(root, "2026-10-31", "# 2026-10-31\n\n## 10:00\n\nnew tea memory\n")
    mem = memory(root)
    mem.sync()
    from station import clock
    import datetime as dt

    results, _ = mem.search("tea memory", since=clock.wall(dt.date(2026, 1, 1), 0, 0))
    assert [r["title"] for r in results] == ["diary 2026-10-31 10:00"]


def test_changing_the_model_reembeds_everything(root, monkeypatch):
    fake = FakeEmbeddings(monkeypatch)
    diary(root, "2026-10-30", "# 2026-10-30\n\n## 10:00\n\na\n\n## 11:00\n\nb\n")
    memory(root).sync()
    (root / "station.yaml").write_text((root / "station.yaml").read_text() + "memory: {dimensions: 256}\n")
    r = memory(root).sync()
    assert r["embedded"] == 2 and fake.calls[-1]["dimensions"] == 256


def test_sync_stops_at_the_credit_limit(root, monkeypatch):
    FakeEmbeddings(monkeypatch)
    Store(root / "state").ledger.add("model", credits=100.0)
    diary(root, "2026-10-30", "# 2026-10-30\n\n## 10:00\n\nanything\n")
    r = memory(root).sync()
    assert r["embedded"] == 0 and r["pending"] == 1 and "credit limit" in r["error"]


@pytest.mark.parametrize("mangle", [
    lambda d: [{"index": x["index"]} for x in d],  # no embedding
    lambda d: [{**x, "embedding": None} for x in d],
    lambda d: [{**x, "index": 0} for x in d],  # repeated index
    lambda d: [{**x, "embedding": [float("nan")] + x["embedding"][1:]} for x in d],
    lambda d: [{**x, "embedding": [0.0] * len(x["embedding"])} for x in d],
    lambda d: d[:-1],  # one missing
    lambda d: "not a list",
], ids=["missing", "null", "repeated", "nan", "zero", "short", "shape"])
def test_bad_embeddings_are_never_stored(root, monkeypatch, mangle):
    FakeEmbeddings(monkeypatch, mangle=mangle)
    diary(root, "2026-10-30", "# 2026-10-30\n\n## 10:00\n\nfirst tea\n\n## 11:00\n\nsecond tea\n")
    mem = memory(root)
    r = mem.sync()
    assert r["embedded"] == 0 and r["pending"] == 2 and "embeddings API" in r["error"]
    results, note = mem.search("tea")  # never an exception; at worst word matches only
    assert len(results) == 2 and all("words" in r["how"] for r in results)
    assert note is None or "word matches only" in note


def test_notes_survive_switching_them_off(root, monkeypatch):
    FakeEmbeddings(monkeypatch)
    cid = memory(root).add_note("the spare key is under the blue pot")
    (root / "station.yaml").write_text((root / "station.yaml").read_text() + "memory: {sources: [diary, chat]}\n")
    off = memory(root)
    off.sync()
    assert "note" not in str(off.search("spare key blue pot")[0])
    with pytest.raises(ValueError, match="not in memory.sources"):
        off.search("key", kind="note")
    (root / "station.yaml").write_text((root / "station.yaml").read_text().replace("sources: [diary, chat]", "sources: [diary, chat, note]"))
    assert memory(root).show(cid)["text"] == "the spare key is under the blue pot"


def test_a_config_change_during_a_sync_never_mixes_models(root, monkeypatch):
    other = {}

    def change():  # another process starts with a new model while this sync waits for the API
        if not other:
            (root / "station.yaml").write_text((root / "station.yaml").read_text() + "memory: {model: text-embedding-3-large}\n")
            other["mem"] = memory(root)

    FakeEmbeddings(monkeypatch, during=change)
    diary(root, "2026-10-30", "# 2026-10-30\n\n## 10:00\n\nseagulls\n")
    old = memory(root)
    assert old.sync()["embedded"] == 1
    new = other["mem"]
    assert new.status()["kinds"]["diary"]["pending"] == 1  # the old model's vector is not the new one's
    assert new.sync()["embedded"] == 1


def test_ids_are_never_reused(root, monkeypatch):
    FakeEmbeddings(monkeypatch)
    mem = memory(root)
    first = mem.add_note("one")
    mem.forget(first)
    assert mem.add_note("two") != first


def test_sync_keeps_to_its_time_budget(root, monkeypatch):
    fake = FakeEmbeddings(monkeypatch)
    diary(root, "2026-10-30", "# 2026-10-30\n\n## 10:00\n\nsomething\n")
    mem = memory(root)
    r = mem.sync(budget=1e-9)  # already over: nothing is read or sent
    assert r["unread"] == 1 and r["changed"] == 0 and not fake.calls
    monkeypatch.setattr(memory_mod, "MIN_REQUEST_SECONDS", 1000)
    r = mem.sync(budget=60)  # files read, but too little time left for a request
    assert r["changed"] == 1 and r["embedded"] == 0 and r["error"] is None and not fake.calls
    assert mem.sync()["embedded"] == 1


def test_a_new_thread_starts_with_related_memories(root, monkeypatch):
    fake = FakeEmbeddings(monkeypatch)
    diary(root, "2026-10-28", "# 2026-10-28\n\n## 21:00\n\nTried to learn Rust ownership, the borrow checker beat me.\n\n"
                              "## 22:00\n\nCooked noodles with too much chili.\n")
    life = make(root, [[use("register", "r", name="Lin")],
                       [use("focus", "f", action="new", title="Learn Rust", goal="understand ownership",
                            context="Anthony recommended the Rust book.")],
                       [use("bash", "b", command="true")]])
    life.cfg["secrets"]["dir"] = str(root / "keys")
    Memory(root, life.cfg, life.store.ledger, home=life.home).sync()
    life.run(max_calls=3)
    first = life.llm.requests[2]["messages"][0]["content"][0]["text"]
    assert "Possibly related, from your memory" in first
    assert "borrow checker beat me" in first.split("Possibly related")[1].split("\n- ")[1]  # the best match first
    assert "Context from main: Anthony recommended the Rust book." in first
    assert fake.calls[-1]["input"][0].startswith("Learn Rust understand ownership")
    assert any(r["what"] == "memory search" for r in life.store.ledger.rows(service="openai"))


def test_recall_costs_nothing_with_an_empty_memory_and_can_be_off(root, monkeypatch):
    fake = FakeEmbeddings(monkeypatch)
    life = make(root, [[use("register", "r", name="Lin")], [use("focus", "f", action="new", title="A")],
                       [use("bash", "b", command="true")]])
    life.cfg["secrets"]["dir"] = str(root / "keys")
    life.run(max_calls=3)
    assert not fake.calls and "from your memory" not in json.dumps(life.llm.requests[2]["messages"])
    diary(root, "2026-10-28", "# 2026-10-28\n\n## 21:00\n\nsomething about A\n")
    Memory(root, life.cfg, life.store.ledger, home=life.home).sync()
    life.cfg["memory"]["recall_on_new"] = 0
    calls = len(fake.calls)
    life.focus_tool({"action": "new", "title": "A again"})
    assert "from your memory" not in life.apply_switch()[0]["content"][0]["text"] and len(fake.calls) == calls


def test_recall_never_breaks_a_new_thread(root, monkeypatch):
    FakeEmbeddings(monkeypatch, fail=True)
    diary(root, "2026-10-28", "# 2026-10-28\n\n## 21:00\n\nRust notes\n")
    life = make(root, [[use("register", "r", name="Lin")], [use("bash", "b", command="true")]])
    life.cfg["secrets"]["dir"] = str(root / "keys")
    life.run(max_calls=1)
    Memory(root, life.cfg, life.store.ledger, home=life.home).sync()
    life.focus_tool({"action": "new", "title": "Rust"})
    text = life.apply_switch()[0]["content"][0]["text"]  # no network: word matches still work
    assert "Rust notes" in text
    monkeypatch.setattr(Memory, "search", lambda *a, **k: 1 / 0)
    life.focus_tool({"action": "new", "title": "Rust again"})
    text = life.apply_switch()[0]["content"][0]["text"]
    assert "the new thread \"Rust again\"" in text and "from your memory" not in text
    assert "memory recall: ZeroDivisionError" in (root / "state" / "errors.jsonl").read_text()


def test_tests_cannot_reach_the_network():
    import urllib.request

    with pytest.raises(OSError, match="never use the network"):
        urllib.request.urlopen("https://api.openai.com/v1/models", timeout=1)


# ---- the loop --------------------------------------------------------------------------

def test_falling_asleep_indexes_memory(root, monkeypatch):
    FakeEmbeddings(monkeypatch)
    life = make(root, [[use("register", "r", name="Lin")], [use("diary", "d", action="write", text="I like the sea.")],
                       [{"type": "text", "text": "good night"}]])
    life.cfg["secrets"]["dir"] = str(root / "keys")
    life.run(max_calls=3)  # the third reply ends the turn: Station sleeps and indexes before waiting
    mem = Memory(root, life.cfg, life.store.ledger, home=life.home)
    hits, _ = mem.search("sea", kind="diary")
    assert hits and "I like the sea." in hits[0]["text"]


def test_memory_problems_never_stop_the_loop(root, monkeypatch):
    FakeEmbeddings(monkeypatch, fail=True)
    life = make(root, [[use("register", "r", name="Lin")], [use("diary", "d", action="write", text="x")],
                       [{"type": "text", "text": "night"}]])
    life.cfg["secrets"]["dir"] = str(root / "keys")
    life.run(max_calls=3)
    errors = [json.loads(line) for line in (root / "state" / "errors.jsonl").read_text().splitlines()]
    assert [e["where"] for e in errors] == ["memory"]
    assert life.state["memory_error"]
