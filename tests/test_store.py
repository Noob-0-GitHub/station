"""Log rotation: log.jsonl by size (bounded). Usage lives in the ledger (test_credits)."""

import gzip
import json
import time

from station import store as store_mod
from station.store import Store

DAY = 86400


def test_log_rotates_by_size_and_keeps_a_bounded_number(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "LOG_MAX_BYTES", 500)
    monkeypatch.setattr(store_mod, "LOG_KEEP", 3)
    s = Store(tmp_path)
    for i in range(200):
        s.log("text", text=f"line {i} " + "x" * 40)
    archives = sorted((tmp_path / "logs").glob("log-*.jsonl.gz"))
    assert len(archives) == 3
    assert tmp_path.joinpath("log.jsonl").stat().st_size <= 500 + 200
    with gzip.open(archives[-1], "rt") as f:
        assert all(json.loads(line)["kind"] == "text" for line in f)


def test_interrupted_rotation_is_finished_without_losing_rows(tmp_path):
    s = Store(tmp_path)
    s.log("text", text="kept")
    (tmp_path / "log.jsonl").rename(tmp_path / "log.jsonl.rotating")  # crashed after the move
    s.finish_rotation()
    assert not (tmp_path / "log.jsonl.rotating").exists()
    (archive,) = (tmp_path / "logs").glob("*.jsonl.gz")
    with gzip.open(archive, "rt") as f:
        assert json.loads(f.readline())["text"] == "kept"


def test_disk_usage_by_entry(tmp_path):
    s = Store(tmp_path)
    s.log("x", text="y" * 1000)
    (tmp_path / "archive").mkdir()
    (tmp_path / "archive" / "a.jsonl").write_text("z" * 5000)
    sizes = s.disk_usage()
    assert sizes["archive"] == 5000 and sizes["log.jsonl"] > 1000


def test_history_keeps_everything_and_reads_from_the_last_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(store_mod, "SCAN_BLOCK", 37)  # markers and lines straddle block boundaries
    s = Store(tmp_path)
    msg = lambda i: {"role": "user", "content": [{"type": "text", "text": f"message {i} " + "x" * (i * 7)}]}
    s.append_history("main", *[msg(i) for i in range(10)])
    assert s.load_history("main") == [msg(i) for i in range(10)]
    s.compact_history("main", [msg(100), msg(9)], method="summary")
    assert s.load_history("main") == [msg(100), msg(9)]
    s.append_history("main", msg(11))
    s.compact_history("main", [msg(200)], method="summary")
    s.append_history("main", msg(12))
    assert s.load_history("main") == [msg(200), msg(12)]
    # Rewriting the live part keeps every byte before the last marker.
    path = tmp_path / "threads" / "main.jsonl"
    before = path.read_bytes()[: Store.live_start(path)]
    s.rewrite_history("main", [msg(300)])
    assert path.read_bytes().startswith(before) and s.load_history("main") == [msg(300)]
    lines = [json.loads(x) for x in path.read_text().splitlines()]
    assert [r for r in lines if "compaction" not in r][:10] == [msg(i) for i in range(10)]
    assert sum("compaction" in r for r in lines) == 2


def test_a_marker_cut_by_a_crash_does_not_count(tmp_path):
    s = Store(tmp_path)
    s.append_history("main", {"role": "user", "content": []})
    path = tmp_path / "threads" / "main.jsonl"
    with open(path, "a") as f:
        f.write('{"compaction": {"ts": 1')  # no newline: the write did not finish
    assert Store.live_start(path) == 0


def test_memory_reads_each_message_once(tmp_path):
    from station.memory import read_jsonl

    s = Store(tmp_path)
    m = lambda t: {"role": "user", "content": [{"type": "text", "text": t}]}
    s.append_history("main", m("a"), m("b"), m("c"))
    s.compact_history("main", [m("summary of a and b"), m("c")], method="summary")
    s.append_history("main", m("d"))
    assert [r["content"][0]["text"] for r in read_jsonl(tmp_path / "threads" / "main.jsonl")] == ["a", "b", "c", "d"]
