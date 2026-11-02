"""Pick up changes to Station's own code and config without the operator.

Templates, docs, API keys and the Codex token file are already read on use, so
they need nothing here. Code and `station.yaml` are loaded once per process, so
a change to them needs a new process. Station restarts only at a safe point:
while the AI is asleep, after its turn ended, so a half-finished edit made
during a turn is never loaded. Before restarting it runs `station check` in a
separate process with the new files; if that fails, the old process keeps
running and the AI gets the error as an event.

Polling a few dozen `stat` calls every few seconds is cheaper and simpler than
an inotify dependency, and it also works when files change on a network or
bind mount.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent


def snapshot(dirs: list[Path], files: list[Path]) -> dict[str, tuple[int, int]]:
    """(mtime_ns, size) of every .py file under `dirs` and of each existing `files` entry."""
    out: dict[str, tuple[int, int]] = {}
    paths = [p for d in dirs for p in sorted(Path(d).rglob("*.py")) if "__pycache__" not in p.parts]
    for path in [*paths, *files]:
        try:
            st = path.stat()
        except OSError:
            continue
        out[str(path)] = (st.st_mtime_ns, st.st_size)
    return out


def changed(old: dict, new: dict) -> list[str]:
    return sorted(k for k in old.keys() | new.keys() if old.get(k) != new.get(k))


class Watcher:
    def __init__(self, dirs: list[Path], files: list[Path]):
        self.dirs, self.files = [Path(d) for d in dirs], [Path(f) for f in files]
        self.base = snapshot(self.dirs, self.files)
        self.rejected: dict | None = None  # snapshot whose check already failed

    def poll(self) -> tuple[dict, list[str]] | None:
        """The new snapshot and changed paths, or None if nothing new to try."""
        now = snapshot(self.dirs, self.files)
        if now == self.base or now == self.rejected:
            return None
        return now, changed(self.base, now)


# -P: keep the working directory off sys.path. Otherwise `python -m` run from a directory that
# contains another `station/` (the AI's home holds ~/station) can import that copy instead.
def check_command(root: Path) -> list[str]:
    return [sys.executable, "-P", "-m", "station.cli", "--root", str(root), "check"]


def run_check(root: Path, timeout: float = 120) -> tuple[bool, str]:
    """Run `station check` against the files on disk now, in a fresh interpreter.

    Same interpreter, flags and environment as the restart (`execv` keeps the environment),
    so the check imports exactly the code the restarted process would.
    """
    try:
        proc = subprocess.run(check_command(root), capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"station check timed out after {timeout:g} s"
    output = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, output[-1500:]


def restart_argv(root: Path) -> list[str]:
    return [sys.executable, "-P", "-m", "station.cli", "--root", str(root), "run"]


def restart(root: Path) -> None:  # pragma: no cover - replaces the process
    sys.stdout.flush()
    sys.stderr.flush()
    argv = restart_argv(root)
    os.execv(argv[0], argv)
