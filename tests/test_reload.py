"""Reload: `station check` and restarting into changed code at a safe point. Offline."""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml

from station import reload as reload_mod
from station.cli import main
from station.loop import Life, Restart

from test_loop import make, use

REPO = Path(__file__).resolve().parent.parent


def bump(path: Path, text: str) -> None:
    path.write_text(text)
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))


def configure_budget(root):
    path = root / "station.yaml"
    cfg = yaml.safe_load(path.read_text())
    cfg["credits"]["allowances"] = {"daily": 3, "weekly": 20}
    path.write_text(yaml.safe_dump(cfg))


def test_check_passes_on_a_fresh_runtime(tmp_path, capsys):
    main(["--root", str(tmp_path), "init"])
    assert main(["--root", str(tmp_path), "check"]) == 2  # no implicit personal budget
    configure_budget(tmp_path)
    assert main(["--root", str(tmp_path), "check"]) == 0
    assert capsys.readouterr().out.strip().endswith("ok")


def test_check_runs_the_code_on_disk(tmp_path):
    """A syntax error in a copy of the package fails the subprocess check; the original still passes."""
    copy = tmp_path / "src"
    shutil.copytree(REPO / "station", copy / "station", ignore=shutil.ignore_patterns("__pycache__"))
    root = tmp_path / "rt"
    main(["--root", str(root), "init"])
    configure_budget(root)
    env = {**os.environ, "PYTHONPATH": str(copy)}

    def check():
        return subprocess.run([sys.executable, "-m", "station.cli", "--root", str(root), "check"],
                              capture_output=True, text=True, env=env, cwd=tmp_path)

    assert check().returncode == 0
    (copy / "station" / "notes.py").write_text("def broken(:\n")
    bad = check()
    assert bad.returncode != 0 and "SyntaxError" in bad.stderr


def test_check_rejects_bad_config(tmp_path):
    (tmp_path / "station.yaml").write_text("- not\n- a mapping\n")
    with pytest.raises(SystemExit, match="mapping"):
        main(["--root", str(tmp_path), "check"])


def test_watcher_sees_code_and_config(tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "a.py").write_text("x = 1\n")
    (pkg / "__pycache__").mkdir()
    cfg = tmp_path / "station.yaml"
    w = reload_mod.Watcher([pkg], [cfg])
    assert w.poll() is None
    (pkg / "__pycache__" / "a.cpython.pyc").write_text("ignored")
    assert w.poll() is None
    cfg.write_text("model: x\n")
    _, files = w.poll()
    assert files == [str(cfg)]
    bump(pkg / "a.py", "x = 2\n")
    (pkg / "b.py").write_text("")
    snap, files = w.poll()
    assert set(files) == {str(cfg), str(pkg / "a.py"), str(pkg / "b.py")}
    w.rejected = snap
    assert w.poll() is None  # the same failing state is not retried
    bump(pkg / "b.py", "y = 1\n")
    assert w.poll() is not None


def asleep_life(tmp_path, monkeypatch, ok, out=""):
    pkg = tmp_path / "watched"
    pkg.mkdir()
    (pkg / "mod.py").write_text("x = 1\n")
    life = make(tmp_path, [[use("register", "t1", name="Lin")], [use("schedule", "sleep", action="sleep")]])
    life.cfg["schedule"]["default_minutes"] = 60
    life.cfg["reload"]["poll_seconds"] = 0
    life.watch_dirs = [pkg]
    calls = []

    def fake_check(root, timeout=120):
        calls.append(root)
        return ok, out

    monkeypatch.setattr(reload_mod, "run_check", fake_check)
    return life, pkg, calls


def edit_soon(path: Path, text: str, delay: float = 0.15):
    def go():
        time.sleep(delay)
        bump(path, text)
    import threading
    threading.Thread(target=go, daemon=True).start()


def test_good_change_while_asleep_restarts(tmp_path, monkeypatch):
    life, pkg, calls = asleep_life(tmp_path, monkeypatch, ok=True)
    edit_soon(pkg / "mod.py", "x = 2\n")
    assert life.run() == "restart"
    assert calls == [life.root]
    state = life.store.load_state()
    assert state["sleep"] is not None  # the next process resumes the same sleep
    events = [e.text for e in life.inbox.peek()]
    assert any("restarted to load your changes" in t and "mod.py" in t for t in events)
    # The next process (same files, no change) resumes the sleep and does not restart again.
    nxt = Life(tmp_path, echo=lambda *_: None)
    nxt.watch_dirs = [pkg]
    nxt.cfg["reload"]["poll_seconds"] = 0
    nxt.watcher = reload_mod.Watcher(nxt.watch_dirs, [nxt.root / "station.yaml"])
    nxt.check_reload(force=True)  # no Restart raised


def test_bad_change_keeps_running_and_tells_the_ai(tmp_path, monkeypatch):
    life, pkg, calls = asleep_life(tmp_path, monkeypatch, ok=False, out="SyntaxError: invalid syntax")
    life.cfg["schedule"]["default_minutes"] = 0.01  # wake after ~0.6 s
    life.llm.script.append([use("bash", "t3", command="true")])
    edit_soon(pkg / "mod.py", "def broken(:\n", delay=0.1)
    assert life.run(max_calls=3) == "max_calls"
    assert len(calls) == 1  # the same broken state is checked once, not on every poll
    woke = life.llm.requests[2]["messages"][-1]["content"]
    text = " ".join(b.get("text", "") for b in woke)
    assert "were not loaded" in text and "SyntaxError" in text and "mod.py" in text


def test_no_restart_during_a_turn(tmp_path, monkeypatch):
    """Edits made by tools inside a turn are only considered once the AI is asleep."""
    pkg = tmp_path / "watched"
    pkg.mkdir()
    (pkg / "mod.py").write_text("x = 1\n")
    target = pkg / "mod.py"
    life = make(tmp_path, [
        [use("register", "t1", name="Lin")],
        [use("bash", "t2", command=f"echo 'x = 3' > {target}")],
        [use("bash", "t3", command="true")],
    ])
    life.cfg["reload"]["poll_seconds"] = 0
    life.watch_dirs = [pkg]
    monkeypatch.setattr(reload_mod, "run_check", lambda root, timeout=120: (True, ""))
    assert life.run(max_calls=3) == "max_calls"  # three calls, no restart in between


def test_disabled(tmp_path, monkeypatch):
    life, pkg, calls = asleep_life(tmp_path, monkeypatch, ok=True)
    life.cfg["reload"]["enabled"] = False
    life.cfg["schedule"]["default_minutes"] = 0.01
    life.llm.script.append([use("bash", "t3", command="true")])
    edit_soon(pkg / "mod.py", "x = 2\n", delay=0.1)
    assert life.run(max_calls=3) == "max_calls" and calls == []


def test_restart_command_uses_this_interpreter(tmp_path):
    argv = reload_mod.restart_argv(tmp_path)
    assert argv[0] == sys.executable and argv[-3:] == ["--root", str(tmp_path), "run"]
    # The working directory must not decide which `station` package is imported.
    assert argv[1] == "-P" and reload_mod.check_command(tmp_path)[1] == "-P"


def test_check_ignores_a_station_package_in_the_working_directory(tmp_path):
    """A broken `station/` in the cwd must not be the code that `station check` runs."""
    decoy = tmp_path / "station"
    decoy.mkdir()
    (decoy / "__init__.py").write_text("raise SystemExit('decoy imported')\n")
    (decoy / "cli.py").write_text("raise SystemExit('decoy imported')\n")
    root = tmp_path / "rt"
    main(["--root", str(root), "init"])
    configure_budget(root)
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    proc = subprocess.run(reload_mod.check_command(root), capture_output=True, text=True, cwd=tmp_path, env=env)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "decoy" not in proc.stdout + proc.stderr
