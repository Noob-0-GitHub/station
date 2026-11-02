import json

from station import disk as disk_mod
from station.disk import Disk, measure
from test_loop import make, use


def test_measure_counts_each_file_once(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "f").write_bytes(b"x" * 50_000)
    once = measure([tmp_path / "a"])
    assert once >= 50_000
    assert measure([tmp_path, tmp_path / "a"]) == measure([tmp_path])  # nested paths are not counted twice
    assert measure([tmp_path / "missing"]) == 0


def test_panel_from_90_percent(tmp_path):
    life = make(tmp_path, [[use("register", "r", name="Lin")]])
    life.run(max_calls=1)
    life.disk.cfg = {**life.disk.cfg, "allowance_gb": 1}
    life.disk.used, life.disk.checked = int(0.5e9), 1e18
    assert life._panel_disk() is None
    life.disk.used = int(0.95e9)
    assert "0.95 GB of 1 GB (95%)" in life._panel_disk()


def test_gate_waits_until_space_is_freed(tmp_path, monkeypatch):
    life = make(tmp_path, [[use("register", "r", name="Lin")]])
    life.run(max_calls=1)
    life.disk.cfg = {**life.disk.cfg, "allowance_gb": 1, "check_minutes": 0}
    readings = iter([int(1.2e9), int(1.1e9), int(0.4e9)])
    monkeypatch.setattr(disk_mod, "measure", lambda paths, timeout=120: next(readings))
    pauses = []
    monkeypatch.setattr(life, "_pause", lambda s: pauses.append(s))
    history = life.store.load_history("main")
    life.disk_gate(history)
    assert len(pauses) == 2  # 1.2 GB, 1.1 GB: wait; 0.4 GB: go on
    assert "under again (0.40 GB of 1 GB (40%))" in history[-1]["content"][-1]["text"]
    assert not [n for n in life.notices.list() if n.get("key") == "disk"]  # cleared once under
    outbox = [json.loads(x) for x in (tmp_path / "state" / "outbox.jsonl").read_text().splitlines()]
    assert "1.20 GB of 1 GB" in outbox[-1]["text"]
    log = (tmp_path / "state" / "log.jsonl").read_text()
    assert '"disk_limit"' in log


def test_default_allowance_is_5_gb(tmp_path):
    life = make(tmp_path, [])
    assert life.cfg["disk"]["allowance_gb"] == 5 and life.disk.allowance == 5_000_000_000
    assert life.disk.paths == [life.home, life.root]
