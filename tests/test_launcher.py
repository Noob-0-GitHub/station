"""The launcher: checkpoints, rollback after a crash, restart backoff.

Each test runs the real launcher.py against a throwaway Git repository holding a
fake `station.cli` (a few lines driven by files in the repo), so no Station loop
and no model ever starts.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parent.parent / "station" / "launcher.py"

# The fake run: behavior comes from mode.txt, a tracked file, so a "code change" is a file edit.
FAKE_CLI = r'''
import json, os, sys, time
from pathlib import Path
code = Path(__file__).resolve().parent.parent
root = Path(sys.argv[sys.argv.index("--root") + 1])
mode = (code / "mode.txt").read_text().strip()
runs = root / "runs.jsonl"
with open(runs, "a") as f:
    f.write(json.dumps({"mode": mode, "launcher": os.environ.get("STATION_LAUNCHER"),
                        "previous": os.environ.get("STATION_PREVIOUS_EXIT")}) + "\n")
def confirm():
    (root / "state").mkdir(exist_ok=True)
    (root / "state" / "confirmed.json").write_text(json.dumps({"ts": time.time()}))
def last_exit(reason):
    (root / "state").mkdir(exist_ok=True)
    p = root / "state" / "state.json"
    st = json.loads(p.read_text()) if p.exists() else {}
    st["last_exit"] = {"ts": time.time(), "reason": reason}
    p.write_text(json.dumps(st))
if mode == "good":            # completes a turn, then stops
    confirm(); time.sleep(0.6); last_exit("stopped"); sys.exit(0)
if mode == "good-then-reload":
    confirm(); time.sleep(0.6); last_exit("reload"); sys.exit(3)
if mode == "crash":           # dies before completing a turn
    raise RuntimeError("broken code")
if mode == "crash-after-turn":
    confirm(); time.sleep(0.6); raise RuntimeError("broken later")
if mode == "network":
    last_exit("stopped after a failed model call (transient)"); sys.exit(1)
if mode == "killed":
    os.kill(os.getpid(), 9)
'''


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=T", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    code = tmp_path / "code"
    (code / "station").mkdir(parents=True)
    (code / "station" / "__init__.py").write_text("")
    (code / "station" / "cli.py").write_text(FAKE_CLI)
    (code / ".gitignore").write_text("/launcher.py\n/launcher.py.prev\nignored/\n__pycache__/\n")
    (code / "mode.txt").write_text("good\n")
    git(code, "init", "-q", "-b", "main")
    git(code, "add", "-A")
    git(code, "commit", "-qm", "initial")
    (tmp_path / "root").mkdir()
    return code


def launch(code, root, *extra, timeout=60):
    env = {k: v for k, v in os.environ.items() if not k.startswith("STATION_")}
    env["PYTHONPATH"] = str(code)
    r = subprocess.run([sys.executable, str(LAUNCHER), "--root", str(root), "--code", str(code),
                        "--python", sys.executable, "--poll", "0.1", "--backoff", "0.05", "--max-backoff", "0.2",
                        *extra], capture_output=True, text=True, timeout=timeout, env=env)
    return r


def runs(root):
    return [json.loads(line) for line in (root / "runs.jsonl").read_text().splitlines()]


def refs(code, prefix):
    return git(code, "for-each-ref", "--format=%(refname)", prefix).splitlines()


def test_good_run_becomes_the_checkpoint_without_touching_head_or_index(repo, tmp_path):
    root = tmp_path / "root"
    (repo / "notes.txt").write_text("uncommitted, untracked\n")
    head = git(repo, "rev-parse", "HEAD")
    r = launch(repo, root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert runs(root) == [{"mode": "good", "launcher": "1", "previous": None}]
    good = git(repo, "rev-parse", "refs/station/good")
    assert "notes.txt" in git(repo, "ls-tree", "--name-only", good)  # untracked files are in the checkpoint
    assert git(repo, "rev-parse", "HEAD") == head and git(repo, "status", "--porcelain") == "?? notes.txt"
    assert len(refs(repo, "refs/station/checkpoints/")) == 1


def test_crash_before_a_turn_rolls_back_and_keeps_the_broken_version(repo, tmp_path):
    root = tmp_path / "root"
    launch(repo, root)  # a good checkpoint first
    good = git(repo, "rev-parse", "refs/station/good")
    # The AI breaks its code: an uncommitted edit plus a new untracked file, and leaves an ignored file.
    (repo / "mode.txt").write_text("crash\n")
    (repo / "station" / "new_module.py").write_text("x = 1\n")
    (repo / "ignored").mkdir()
    (repo / "ignored" / "keep.txt").write_text("not mine to touch\n")
    r = launch(repo, root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert [x["mode"] for x in runs(root)] == ["good", "crash", "good"]  # rolled back, started again, worked
    assert (repo / "mode.txt").read_text() == "good\n"
    assert not (repo / "station" / "new_module.py").exists()
    assert (repo / "ignored" / "keep.txt").read_text() == "not mine to touch\n"
    failed = refs(repo, "refs/heads/station-failed/")
    assert len(failed) == 1
    assert git(repo, "show", f"{failed[0]}:mode.txt") == "crash"
    assert git(repo, "show", f"{failed[0]}:station/new_module.py") == "x = 1"
    assert git(repo, "rev-parse", "refs/station/good") == good
    # The next run was told about the crash, and the AI got an event.
    assert "exit code 1" in runs(root)[2]["previous"] and "rolled the code back" in runs(root)[2]["previous"]
    events = [json.loads(p.read_text()) for p in (root / "inbox").glob("*.json")]
    assert len(events) == 1 and "station-failed/" in events[0]["text"] and events[0]["type"] == "rollback"
    kinds = [json.loads(line)["kind"] for line in (root / "state" / "errors.jsonl").read_text().splitlines()]
    assert kinds == ["rollback", "crash"]


def test_no_rollback_without_a_good_checkpoint(repo, tmp_path):
    root = tmp_path / "root"
    (repo / "mode.txt").write_text("crash\n")
    r = launch(repo, root, "--max-restarts", "2")
    assert r.returncode == 1 and [x["mode"] for x in runs(root)] == ["crash"] * 3
    assert (repo / "mode.txt").read_text() == "crash\n" and refs(repo, "refs/heads/station-failed/") == []


def test_crash_after_a_completed_turn_is_not_rolled_back(repo, tmp_path):
    root = tmp_path / "root"
    launch(repo, root)
    (repo / "mode.txt").write_text("crash-after-turn\n")
    r = launch(repo, root, "--max-restarts", "0")
    assert r.returncode == 1 and (repo / "mode.txt").read_text() == "crash-after-turn\n"
    assert refs(repo, "refs/heads/station-failed/") == []
    # That version completed a turn, so it became the good checkpoint.
    assert git(repo, "show", "refs/station/good:mode.txt") == "crash-after-turn"


@pytest.mark.parametrize("mode", ["network", "killed"])
def test_network_stops_and_kills_are_not_blamed_on_the_code(repo, tmp_path, mode):
    root = tmp_path / "root"
    launch(repo, root)
    (repo / "mode.txt").write_text(f"{mode}\n")
    r = launch(repo, root, "--max-restarts", "1")
    assert r.returncode != 0 and (repo / "mode.txt").read_text() == f"{mode}\n"
    assert refs(repo, "refs/heads/station-failed/") == []


def test_reload_exit_starts_again_without_backoff(repo, tmp_path):
    root = tmp_path / "root"
    (repo / "mode.txt").write_text("good-then-reload\n")
    proc = subprocess.Popen([sys.executable, str(LAUNCHER), "--root", str(root), "--code", str(repo),
                             "--python", sys.executable, "--poll", "0.1"],
                            env={**{k: v for k, v in os.environ.items() if not k.startswith("STATION_")},
                                 "PYTHONPATH": str(repo)}, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 30
    while time.time() < deadline and (not (root / "runs.jsonl").exists() or len(runs(root)) < 2):
        time.sleep(0.1)
    (repo / "mode.txt").write_text("good\n")  # the reloaded code stops normally
    assert proc.wait(timeout=30) == 0
    assert len(runs(root)) >= 2 and runs(root)[-1]["mode"] == "good"


def test_big_untracked_files_are_not_checkpointed(repo, tmp_path):
    root = tmp_path / "root"
    (repo / "big.bin").write_bytes(b"\0" * (1 << 20 | 1))
    (repo / "small.txt").write_text("ok\n")
    launch(repo, root)
    names = git(repo, "ls-tree", "--name-only", "refs/station/good").splitlines()
    assert "small.txt" in names and "big.bin" not in names
    loose = git(repo, "count-objects", "-v")
    assert int(dict(line.split(": ") for line in loose.splitlines())["size"]) < 64  # KiB: big.bin never hashed


def test_checkpoints_are_pruned(repo, tmp_path):
    root = tmp_path / "root"
    for i in range(4):
        (repo / "version.txt").write_text(f"{i}\n")  # a different tree each time, same behavior
        launch(repo, root, "--keep", "2")
    assert len(refs(repo, "refs/station/checkpoints/")) == 2


def test_install_copy_is_ignored_and_survives_rollback(repo, tmp_path):
    sys.path.insert(0, str(LAUNCHER.parent))
    try:
        import launcher
    finally:
        sys.path.pop(0)
    target = launcher.install(repo)
    assert target.read_bytes() == LAUNCHER.read_bytes() and os.access(target, os.X_OK)
    assert git(repo, "status", "--porcelain") == ""  # ignored
    target.write_text("# edited by the AI\n")
    launcher.install(repo)
    assert (repo / "launcher.py.prev").read_text() == "# edited by the AI\n"
