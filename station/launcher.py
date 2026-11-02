#!/usr/bin/env python3
"""Station launcher: keep `station run` running, and roll back code that breaks it.

Production runs this file, not the package: `station init --code-dir` copies it to
`<code>/launcher.py`, which `.gitignore` excludes. Checkpoints and rollbacks only
touch files Git tracks or would track, so a broken commit can never take the
launcher down with it. It uses only the standard library and imports nothing
from Station, because the code it supervises may be broken. The AI may edit the
copy like any other file in its home; a reinstall replaces it (keeping
`launcher.py.prev`).

    python launcher.py --root ~/.station [--code ~/station] [--python ~/station/.venv/bin/python]

Each start of `station run`:

1. Snapshot the code directory as a commit object: tracked files plus untracked
   files Git does not ignore, except untracked files over 1 MB. The AI's index,
   HEAD and branches are not touched.
2. Start `station run` with STATION_LAUNCHER=1. A hot reload then exits with code
   3 instead of exec'ing, and the launcher starts it again (back to step 1).
3. When the run reports its first completed turn (`state/confirmed.json`), its
   snapshot becomes the good checkpoint (`refs/station/good`, plus a dated ref
   under `refs/station/checkpoints/`; the newest 20 within 30 days are kept).
4. If the run exits with an error (a Python error or a crash signal, not SIGKILL
   or SIGTERM) before completing a turn, its code differs from the good
   checkpoint, and it did not stop over the network, rate limits or the config,
   the working tree is restored to the good checkpoint. The version that crashed
   is kept on branch `station-failed/<time>`, and the AI gets an event saying so.
5. After any crash, start again after a delay: 10 s, doubling up to 30 min, back
   to 10 s once a run has lasted 10 minutes. Exit code 0 (stopped, exited) ends
   the launcher; SIGTERM is passed on to `station run`.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

EXIT_RELOAD = 3  # `station run` asks to be started again (same value in station/cli.py)
GOOD = "refs/station/good"
CHECKPOINTS = "refs/station/checkpoints/"
FAILED = "refs/heads/station-failed/"
BIG_UNTRACKED = 1 << 20
# A run that stopped for these reasons failed because of the network or the provider, not its code.
NOT_THE_CODE = tuple(f"stopped after a failed model call ({k})" for k in ("transient", "rate_limit", "config"))
# Exit codes that point at the code: a Python error (1, 2, ...) or a crash signal. Not SIGKILL or
# SIGTERM: those come from the operator, a timeout or the OOM killer, and do not say the code is bad.
CODE_SIGNALS = {-signal.SIGSEGV, -signal.SIGABRT, -signal.SIGBUS, -signal.SIGILL, -signal.SIGFPE}


def log(text: str) -> None:
    print(f"[launcher {time.strftime('%Y-%m-%d %H:%M:%S')}] {text}", flush=True)


class Git:
    def __init__(self, code: Path):
        self.code = code

    def __call__(self, *args: str, env: dict | None = None, check: bool = True, input: str | None = None) -> str:
        r = subprocess.run(["git", "-C", str(self.code), "-c", "user.name=Station", "-c", "user.email=station@localhost",
                            *args], capture_output=True, text=True, input=input, env={**os.environ, **(env or {})})
        if check and r.returncode:
            raise RuntimeError(f"git {args[0]} failed: {r.stderr.strip()[:300]}")
        return r.stdout.rstrip("\n")

    def commit(self, ref: str) -> str | None:
        return self("rev-parse", "-q", "--verify", f"{ref}^{{commit}}", check=False) or None

    def tree(self, ref: str) -> str:
        return self("rev-parse", f"{ref}^{{tree}}")

    def scratch_index(self) -> Path:
        return Path(self("rev-parse", "--absolute-git-dir")) / f"station-index-{os.getpid()}-{uuid.uuid4().hex[:6]}"


def snapshot(git: Git, message: str) -> tuple[str, str]:
    """Commit the working tree into a scratch index; return (commit, tree). Nothing visible changes."""
    head = git.commit("HEAD")
    index = git.scratch_index()
    env = {"GIT_INDEX_FILE": str(index)}
    # Large untracked files are left out before `add`, so they are never even hashed into .git.
    big = [p for p in git("ls-files", "--others", "--exclude-standard", "-z").split("\0")
           if p and (git.code / p).is_file() and (git.code / p).stat().st_size > BIG_UNTRACKED]
    try:
        if head:
            git("read-tree", head, env=env)
        git("add", "-A", "--", ".", *(f":(exclude,literal){p}" for p in big), env=env)
        tree = git("write-tree", env=env)
    finally:
        index.unlink(missing_ok=True)
    commit = git("commit-tree", tree, *(["-p", head] if head else []), input=message + "\n")
    return commit, tree


def restore(git: Git, good: str, current: str) -> None:
    """Make the working tree match `good` for every path in either snapshot; leave HEAD and the index alone."""
    index = git.scratch_index()
    env = {"GIT_INDEX_FILE": str(index)}
    try:
        git("read-tree", good, env=env)
        git("checkout-index", "-a", "-f", env=env)
    finally:
        index.unlink(missing_ok=True)
    added = git("diff", "--name-only", "-z", "--diff-filter=A", good, current).split("\0")
    for rel in filter(None, added):
        path = (git.code / rel).resolve()
        if path.is_relative_to(git.code) and (path.is_file() or path.is_symlink()):
            path.unlink()


def prune(git: Git, prefix: str, keep: int, max_age_days: float | None) -> None:
    rows = [line.split() for line in git("for-each-ref", "--sort=-committerdate",
                                         "--format=%(refname) %(committerdate:unix)", prefix).splitlines() if line]
    now = time.time()
    for i, (ref, when) in enumerate(rows):
        if i >= keep or (max_age_days is not None and now - int(when) > max_age_days * 86400):
            git("update-ref", "-d", ref, check=False)


def promote(git: Git, commit: str, tree: str, keep: int, max_age_days: float) -> bool:
    good = git.commit(GOOD)
    if good and git.tree(good) == tree:
        return False
    git("update-ref", GOOD, commit)
    git("update-ref", f"{CHECKPOINTS}{time.strftime('%Y%m%d-%H%M%S')}-{commit[:7]}", commit)
    prune(git, CHECKPOINTS, keep, max_age_days)
    git("gc", "--auto", "--quiet", check=False)
    return True


def event(root: Path, text: str) -> None:
    """Drop an event into Station's inbox (same format as station/inbox.py)."""
    inbox = root / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    name = f"{time.time_ns()}-{uuid.uuid4().hex[:6]}"
    tmp = inbox / f".{name}.tmp"
    tmp.write_text(json.dumps({"ts": time.time(), "type": "rollback", "source": "launcher", "text": text}, ensure_ascii=False))
    os.replace(tmp, inbox / f"{name}.json")


def record(root: Path, kind: str, message: str, **extra) -> None:
    """Append to state/errors.jsonl (same format as station/errors.py)."""
    (root / "state").mkdir(parents=True, exist_ok=True)
    row = {"ts": time.time(), "where": "launcher", "kind": kind, "message": message[:2000], "fatal": False,
           **{k: v for k, v in extra.items() if v is not None}}
    with open(root / "state" / "errors.jsonl", "a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def confirmed_since(root: Path, started: float) -> bool:
    return float(read_json(root / "state" / "confirmed.json").get("ts") or 0) >= started


def exit_reason(root: Path, started: float) -> str:
    last = read_json(root / "state" / "state.json").get("last_exit") or {}
    if float(last.get("ts") or 0) >= started and last.get("reason"):
        return str(last["reason"])
    return "no clean exit recorded"


def install(code_dir: Path) -> Path:
    """Copy this file to <code_dir>/launcher.py; keep a differing old copy as launcher.py.prev."""
    target = Path(code_dir) / "launcher.py"
    source = Path(__file__).read_bytes()
    if target.exists() and target.read_bytes() != source:
        shutil.copy2(target, target.with_name("launcher.py.prev"))
    tmp = target.with_name(f".launcher.{os.getpid()}.tmp")
    tmp.write_bytes(source)
    tmp.chmod(0o755)
    os.replace(tmp, target)
    return target


class Launcher:
    def __init__(self, args):
        self.a = args
        self.root = Path(args.root).expanduser().resolve()
        self.code = Path(args.code).expanduser().resolve()
        self.git = Git(self.code)
        self.child: subprocess.Popen | None = None
        self.stopping = False
        self.previous: tuple[str, float] | None = None  # (how the last run ended, when) after a crash

    def on_signal(self, *_):
        self.stopping = True
        if self.child and self.child.poll() is None:
            self.child.send_signal(signal.SIGTERM)

    def sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end and not self.stopping:
            time.sleep(min(0.5, max(end - time.time(), 0)))

    def rollback(self, good: str, started: float) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        failed, _ = snapshot(self.git, f"Station: version that crashed, rolled back at {stamp}")
        self.git("update-ref", f"{FAILED}{stamp}", failed)
        restore(self.git, good, failed)
        prune(self.git, FAILED, self.a.keep, None)
        since = time.strftime("%Y-%m-%d %H:%M", time.localtime(started))
        text = (f"Station crashed before completing a turn with the code it started at {since}, so the launcher "
                f"rolled your code back to the last checkpoint that completed a turn ({good[:10]}). The version that "
                f"crashed is on branch station-failed/{stamp}. HEAD was not moved, so `git status` shows the rollback "
                f"as uncommitted changes: commit it, or fix forward. Ignored files, the runtime directory and your "
                f"home were not touched.")
        event(self.root, text)
        record(self.root, "rollback", f"rolled back to {good[:10]}; crashed version on station-failed/{stamp}",
               good=good, failed=failed)
        log(f"rolled back to {good[:10]}; crashed version kept on station-failed/{stamp}")

    def run_once(self) -> tuple[int, float, bool, str | None, str | None]:
        commit = tree = None
        try:
            commit, tree = snapshot(self.git, f"Station checkpoint {time.strftime('%Y-%m-%d %H:%M:%S')}")
        except Exception as e:  # no checkpoint this run, but keep Station up
            log(f"snapshot failed: {e}")
        started = time.time()
        argv = [self.a.python, "-P", "-m", "station.cli", "--root", str(self.root), "run"]
        env = {**os.environ, "STATION_LAUNCHER": "1"}
        env.pop("STATION_PREVIOUS_EXIT", None)
        env.pop("STATION_DOWN_SINCE", None)
        if self.previous:
            env["STATION_PREVIOUS_EXIT"], env["STATION_DOWN_SINCE"] = self.previous[0], str(self.previous[1])
            self.previous = None
        self.child = subprocess.Popen(argv, env=env, cwd=str(Path.home()))
        promoted = False
        while True:
            try:
                rc = self.child.wait(timeout=self.a.poll)
                break
            except subprocess.TimeoutExpired:
                if commit and tree and not promoted and confirmed_since(self.root, started):
                    promoted = True
                    if promote(self.git, commit, tree, self.a.keep, self.a.max_age_days):
                        log(f"checkpoint {commit[:10]} completed a turn; it is now the good checkpoint")
        if commit and tree and not promoted and confirmed_since(self.root, started):
            promoted = True
            promote(self.git, commit, tree, self.a.keep, self.a.max_age_days)
        return rc, started, promoted, commit, tree

    def main(self) -> int:
        signal.signal(signal.SIGTERM, self.on_signal)
        signal.signal(signal.SIGINT, self.on_signal)
        delay, crashes = self.a.backoff, 0
        while not self.stopping:
            rc, started, confirmed, commit, tree = self.run_once()
            if self.stopping:
                return 0
            if rc == EXIT_RELOAD:
                log("reload requested; starting again")
                continue
            if rc == 0:
                log("station run ended normally")
                return 0
            crashes += 1
            uptime = time.time() - started
            reason = exit_reason(self.root, started)
            log(f"station run exited {rc} after {uptime:.0f} s ({reason})")
            good = self.git.commit(GOOD)
            rolled = False
            if (not confirmed and tree and good and (rc > 0 or rc in CODE_SIGNALS)
                    and reason not in NOT_THE_CODE and self.git.tree(good) != tree):
                try:
                    self.rollback(good, started)
                    rolled = True
                except Exception as e:
                    log(f"rollback failed: {e}")
                    record(self.root, "rollback_failed", str(e))
            record(self.root, "crash", f"station run exited {rc} after {uptime:.0f} s ({reason})", rolled_back=rolled)
            self.previous = (f"{reason}, exit code {rc}" + ("; the launcher rolled the code back" if rolled else ""),
                             time.time())
            if self.a.max_restarts is not None and crashes > self.a.max_restarts:
                log("too many restarts; giving up")
                return rc
            if uptime >= self.a.reset_after:
                delay = self.a.backoff
            log(f"starting again in {delay:g} s")
            self.sleep(delay)
            delay = min(delay * 2, self.a.max_backoff)
        return 0


def parse(argv=None):
    here = Path(__file__).resolve().parent
    default_python = here / ".venv" / "bin" / "python"
    p = argparse.ArgumentParser(prog="launcher.py", description="Keep `station run` running; roll back code that breaks it.")
    p.add_argument("--root", required=True, help="Station's runtime directory (~/.station)")
    p.add_argument("--code", default=str(here), help="the code repository (default: this file's directory)")
    p.add_argument("--python", default=str(default_python) if default_python.exists() else sys.executable)
    p.add_argument("--backoff", type=float, default=10, help="first delay after a crash, seconds")
    p.add_argument("--max-backoff", type=float, default=1800)
    p.add_argument("--reset-after", type=float, default=600, help="a run this long resets the delay")
    p.add_argument("--poll", type=float, default=5, help="seconds between checks for a completed turn")
    p.add_argument("--keep", type=int, default=20, help="checkpoints and failed branches kept")
    p.add_argument("--max-age-days", type=float, default=30)
    p.add_argument("--max-restarts", type=int, default=None, help="give up after this many crashes (tests)")
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(Launcher(parse()).main())
