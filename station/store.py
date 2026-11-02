"""Persistent state: threads, state, log, outbox (plain JSON files) and the spending ledger (SQLite)."""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import time
from pathlib import Path

from . import clock
from .ledger import Ledger

THREAD_ID = re.compile(r"^[a-z0-9]{1,16}$")
# A thread's file keeps its whole history. Compression appends one marker line and the new,
# shorter conversation after it; Station reads only what follows the last marker.
MARKER = b'{"compaction"'
SCAN_BLOCK = 1 << 16
LOG_MAX_BYTES = 5_000_000  # log.jsonl is gzipped into logs/ past this size ...
LOG_KEEP = 20  # ... and this many archives are kept (about 100 MB of log before compression)


def default_state() -> dict:
    return {
        "status": "unregistered",
        "name": None,
        "reminders": 0,
        "last_wake": None,
        "last_context": 0,
        "sleep": None,
        "current": "main",
        "next_thread": 1,
        "threads": {"main": {"title": "everyday", "goal": "", "parent": None, "status": "active"}},
    }


def copy_prefix(src, out, length: int) -> None:
    """Copy the first `length` bytes. copy_file_range lets XFS share the blocks instead of copying."""
    done = 0
    try:
        while done < length:
            n = os.copy_file_range(src.fileno(), out.fileno(), length - done, done, done)
            if n == 0:
                break
            done += n
        out.seek(done)
    except (AttributeError, OSError):
        src.seek(done)
        out.seek(done)
        while done < length:
            buf = src.read(min(1 << 20, length - done))
            if not buf:
                break
            out.write(buf)
            done += len(buf)
    if done != length:
        raise OSError(f"short copy: {done} of {length} bytes")


class Store:
    def __init__(self, directory):
        self.dir = Path(directory)
        self.threads_dir = self.dir / "threads"
        self.threads_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "state.json"
        self.log_path = self.dir / "log.jsonl"
        self.outbox_path = self.dir / "outbox.jsonl"
        legacy = self.dir / "history.jsonl"  # before threads existed
        if legacy.exists() and not self.thread_path("main").exists():
            os.replace(legacy, self.thread_path("main"))
        self.ledger = Ledger(self.dir / "ledger.db")
        self._import_usage()

    def _import_usage(self) -> None:
        """Move usage.jsonl (and its gzipped days) into the ledger once, then out of the way."""
        old = [self.dir / "usage.jsonl", self.dir / "usage.jsonl.rotating", *sorted((self.dir / "usage").glob("*.jsonl.gz"))]
        old = [p for p in old if p.exists()]
        if not old:
            return
        import fcntl

        with open(self.dir / "usage.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            old = [p for p in old if p.exists()]  # another process may have done it meanwhile
            rows = []
            for path in old:
                opener = gzip.open if path.suffix == ".gz" else open
                with opener(path, "rt") as f:
                    rows += [json.loads(line) for line in f if line.strip()]
            self.ledger.import_legacy(sorted(rows, key=lambda r: r["ts"]))
            dest = self.dir / "usage-legacy"
            dest.mkdir(exist_ok=True)
            for path in old:
                os.replace(path, dest / path.name)
            if (self.dir / "usage").is_dir() and not any((self.dir / "usage").iterdir()):
                (self.dir / "usage").rmdir()

    # ---- thread histories ------------------------------------------------

    def thread_path(self, tid: str) -> Path:
        if not THREAD_ID.match(tid):
            raise ValueError(f"bad thread id: {tid!r}")
        return self.threads_dir / f"{tid}.jsonl"

    @staticmethod
    def live_start(path: Path) -> int:
        """Byte offset just after the last complete compaction marker line (0 if none).

        Read backwards in blocks, so a long history costs only its live end. A marker line
        without its newline (a crash mid-write) does not count: the conversation before it
        stays live instead of an empty one."""
        with open(path, "rb") as f:
            pos = f.seek(0, os.SEEK_END)
            carry = b""  # the start of the block after this one, up to and including its first newline
            while pos > 0:
                step = min(SCAN_BLOCK, pos)
                pos -= step
                f.seek(pos)
                buf = f.read(step) + carry
                end = buf.rfind(b"\n")  # every newline closes a complete line
                while end != -1:
                    start = buf.rfind(b"\n", 0, end)
                    if start == -1 and pos > 0:
                        break  # this line may begin in the previous block
                    if buf.startswith(MARKER, start + 1):
                        return pos + end + 1
                    end = start
                first = buf.find(b"\n")
                carry = buf[: first + 1] if first != -1 else buf
        return 0

    def load_history(self, tid: str = "main") -> list[dict]:
        """The live conversation: what follows the last compaction marker."""
        path = self.thread_path(tid)
        if not path.exists():
            return []
        with open(path, "rb") as f:
            f.seek(self.live_start(path))
            data = f.read().decode()
        return [json.loads(line) for line in data.splitlines() if line.strip()]

    def append_history(self, tid: str, *messages: dict) -> None:
        self._append(self.thread_path(tid), messages)

    def rewrite_history(self, tid: str, messages: list[dict]) -> None:
        """Replace the live conversation; everything up to the last marker is kept byte for byte."""
        path = self.thread_path(tid)
        start = self.live_start(path) if path.exists() else 0
        tmp = path.with_suffix(".tmp")
        with open(tmp, "wb") as out:
            if start:
                with open(path, "rb") as src:
                    copy_prefix(src, out, start)
            out.write("".join(json.dumps(m, ensure_ascii=False) + "\n" for m in messages).encode())
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)

    def compact_history(self, tid: str, messages: list[dict], **info) -> None:
        """Start a new live conversation: append a marker line and `messages` in one write.
        Nothing before the marker is changed or removed. `carried` tells readers (memory)
        that these messages repeat or summarize what came before."""
        marker = {"compaction": {"ts": time.time(), "carried": len(messages), **info}}
        self._append(self.thread_path(tid), [marker, *messages])

    # ---- state -----------------------------------------------------------

    def load_state(self) -> dict:
        state = default_state()
        if self.state_path.exists():
            state.update(json.loads(self.state_path.read_text()))
        return state

    def save_state(self, state: dict) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1))
        os.replace(tmp, self.state_path)

    # ---- append-only logs ------------------------------------------------

    def log(self, kind: str, **data) -> None:
        self._rotate_log()
        self._append(self.log_path, [{"ts": time.time(), "kind": kind, **data}])

    # ---- rotation --------------------------------------------------------

    def _rotate_log(self) -> None:
        try:
            if self.log_path.stat().st_size <= LOG_MAX_BYTES:
                return
        except FileNotFoundError:
            return
        self._archive(self.log_path, self.dir / "logs", "log-" + clock.at(time.time()).strftime("%Y%m%d-%H%M%S"))
        old = sorted((self.dir / "logs").glob("log-*.jsonl.gz"))
        for path in old[:-LOG_KEEP]:
            path.unlink()

    @staticmethod
    def _archive(path: Path, directory: Path, stem: str) -> Path:
        """Move `path` aside first, then compress it, so a crash midway never duplicates rows."""
        directory.mkdir(exist_ok=True)
        pending = path.with_name(path.name + ".rotating")
        if not pending.exists():
            os.replace(path, pending)
        dest = directory / f"{stem}.jsonl.gz"
        n = 1
        while dest.exists():
            n += 1
            dest = directory / f"{stem}-{n}.jsonl.gz"
        tmp = dest.with_name(dest.name + ".tmp")
        with open(pending, "rb") as src, gzip.open(tmp, "wb") as out:
            shutil.copyfileobj(src, out)
        os.replace(tmp, dest)
        pending.unlink()
        return dest

    def finish_rotation(self) -> None:
        """Complete a rotation that a crash interrupted (called at start)."""
        pending = self.log_path.with_name(self.log_path.name + ".rotating")
        if pending.exists():
            self._archive(self.log_path, self.dir / "logs", "recovered-" + time.strftime("%Y%m%d-%H%M%S"))

    def disk_usage(self) -> dict[str, int]:
        """Bytes used under state/, by top-level entry."""
        out: dict[str, int] = {}
        for entry in self.dir.iterdir():
            if entry.is_dir():
                out[entry.name] = sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
            elif entry.is_file():
                out[entry.name] = entry.stat().st_size
        return out

    def outbox(self, text: str) -> None:
        """Append a local runtime notice; this is not a delivery queue."""
        self._append(self.outbox_path, [{"ts": time.time(), "text": text}])

    @staticmethod
    def _append(path: Path, rows) -> None:
        data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
        with open(path, "a") as f:  # one write per call, so paired lines land together
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
