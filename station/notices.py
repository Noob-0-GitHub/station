"""Notices: pending things for the AI to see, with read state and expiry.

Events (the inbox) are things that happened; each is delivered once into the
conversation. Notices are things that are still true until handled: unread DMs,
a usage limit until 18:00, "Station restarted". They are shown on the board
(never stored in history, so they do not break the prompt cache), never wake
the AI, and are managed with `station notify` / `station notices`.

Storage: one JSON file, rewritten atomically under an flock, so the loop, the
AI's commands and outside writers (a Telegram forwarder, `bili poll`) can share it.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path

from . import clock

PRIORITIES = {"low": 0, "normal": 1, "high": 2}
MAX_TEXT = 1000
MAX_ITEMS = 200
KEEP_READ = 7 * 86400  # read notices are dropped this long after being read
DURATION = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhdw]?)$")
UNITS = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(text) -> float | None:
    """'30m', '2h', '1d', '3600' -> seconds. Empty, '0' or 'none' -> no expiry."""
    if text is None:
        return None
    s = str(text).strip().lower()
    if s in ("", "0", "none", "never"):
        return None
    m = DURATION.match(s)
    if not m:
        raise ValueError(f"bad duration {text!r}: use a number of seconds or 30m, 2h, 1d, 1w")
    return float(m.group(1)) * UNITS[m.group(2)]


class Notices:
    def __init__(self, directory):
        self.path = Path(directory) / "notices.json"
        self.lock_path = Path(directory) / "notices.lock"

    # ---- storage ---------------------------------------------------------

    @contextmanager
    def _locked(self, write: bool):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX if write else fcntl.LOCK_SH)
            data = self._load()
            yield data
            if write:
                self._save(data)

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                return data
        except (FileNotFoundError, ValueError):
            pass
        return {"next_id": 1, "items": []}

    def _save(self, data: dict) -> None:
        now = time.time()
        items = [n for n in data["items"] if not self._gone(n, now)]
        if len(items) > MAX_ITEMS:  # drop read ones first, then the oldest
            items.sort(key=lambda n: (n.get("read") is None, n["updated"]))
            items = sorted(items[-MAX_ITEMS:], key=lambda n: n["id"])
        data["items"] = items
        tmp = self.path.with_name(f".notices.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1))
        os.replace(tmp, self.path)
        owner = self.path.parent.stat()
        if os.geteuid() == 0 and owner.st_uid != 0:  # a root-side writer must not take the file away
            for p in (self.path, self.lock_path):
                os.chown(p, owner.st_uid, owner.st_gid)

    @staticmethod
    def _gone(n: dict, now: float) -> bool:
        if n.get("expires") and n["expires"] <= now:
            return True
        return bool(n.get("read")) and now - n["read"] > KEEP_READ

    # ---- operations ------------------------------------------------------

    def add(self, text: str, source: str = "manual", key: str | None = None, priority: str = "normal",
            ttl: float | None = None) -> dict:
        """Add a notice; the same source and key update it in place and mark it unread again."""
        text = str(text).strip()
        if not text:
            raise ValueError("notice text is empty")
        if priority not in PRIORITIES:
            raise ValueError(f"priority must be one of {', '.join(PRIORITIES)}")
        now = time.time()
        fields = {"text": text[:MAX_TEXT], "priority": priority, "updated": now, "read": None,
                  "expires": now + ttl if ttl else None}
        with self._locked(write=True) as data:
            for n in data["items"]:
                if key and n.get("key") == key and n["source"] == source and not self._gone(n, now):
                    n.update(fields)
                    return dict(n)
            n = {"id": data["next_id"], "ts": now, "source": str(source)[:40], "key": key, **fields}
            data["next_id"] += 1
            data["items"].append(n)
            return dict(n)

    def mark_read(self, ids: list[int] | None = None) -> int:
        """Mark the given ids, or all unread when ids is None. Returns how many changed."""
        now, count = time.time(), 0
        with self._locked(write=True) as data:
            known = {n["id"] for n in data["items"]}
            missing = [i for i in ids or [] if i not in known]
            if missing:
                raise ValueError(f"no notice #{', #'.join(map(str, missing))}")
            for n in data["items"]:
                if n.get("read") is None and (ids is None or n["id"] in ids):
                    n["read"] = now
                    count += 1
        return count

    def clear(self, key: str, source: str | None = None) -> int:
        """Remove notices with this key (from this source, if given): the thing was handled."""
        with self._locked(write=True) as data:
            keep = [n for n in data["items"] if not (n.get("key") == key and (source is None or n["source"] == source))]
            removed = len(data["items"]) - len(keep)
            data["items"] = keep
        return removed

    def list(self, unread: bool = False, min_priority: str = "low") -> list[dict]:
        now, floor = time.time(), PRIORITIES[min_priority]
        with self._locked(write=False) as data:
            items = [n for n in data["items"] if not self._gone(n, now)]
        return [n for n in items if PRIORITIES.get(n["priority"], 1) >= floor and (not unread or n.get("read") is None)]

    # ---- display ---------------------------------------------------------

    @staticmethod
    def line(n: dict, width: int = 300) -> str:
        stamp = clock.at(n["updated"]).strftime("%m-%d %H:%M")
        flag = "" if n["priority"] == "normal" else f" [{n['priority']}]"
        until = f" (until {clock.at(n['expires']).strftime('%m-%d %H:%M')})" if n.get("expires") else ""
        text = n["text"] if len(n["text"]) <= width else n["text"][: width - 1] + "…"
        return f"#{n['id']}{flag} {n['source']} · {stamp}{until}: {text}"

    def render(self, limit: int = 5) -> str:
        unread = self.list(unread=True)
        if not unread:
            return "no unread notices"
        unread.sort(key=lambda n: (-PRIORITIES.get(n["priority"], 1), -n["updated"]))
        lines = [f"{len(unread)} unread (`station notices read ID` or `--all`)"]
        lines += [self.line(n) for n in unread[:limit]]
        if len(unread) > limit:
            lines.append(f"… {len(unread) - limit} more: `station notices --unread`")
        return "\n".join(lines)
