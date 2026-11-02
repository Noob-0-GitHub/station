"""Diary and todo: two small personal stores in the AI's home."""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

from . import clock
from .runner import Runner, safe_read

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def today() -> str:
    return clock.today().isoformat()


class Diary:
    """One Markdown file per day: diary/YYYY-MM-DD.md, entries headed by time."""

    def __init__(self, directory, runner: Runner, home):
        self.dir = Path(directory)
        self.runner = runner
        self.home = home

    def _path(self, date: str) -> Path:
        if not DATE_RE.match(date):
            raise ValueError(f"date must be YYYY-MM-DD: {date}")
        return self.dir / f"{date}.md"

    def write(self, text: str) -> str:
        text = text.strip()
        if not text:
            raise ValueError("diary entry is empty")
        path = self._path(today())
        old = safe_read(path, self.home) if path.exists() else f"# {today()}\n"
        stamp = clock.now().strftime("%H:%M")
        self.runner.mkdir(self.dir)
        self.runner.write(path, f"{old.rstrip()}\n\n## {stamp}\n\n{text}\n")
        return f"written to {path.name}"

    def read(self, date: str | None = None) -> str:
        path = self._path(date or today())
        if not path.exists():
            return f"no diary for {path.stem}"
        return safe_read(path, self.home)

    def dates(self) -> list[str]:
        return sorted(p.stem for p in self.dir.glob("*.md") if DATE_RE.match(p.stem))

    def count_today(self) -> int:
        path = self._path(today())
        if not path.exists():
            return 0
        return sum(1 for line in safe_read(path, self.home).splitlines() if line.startswith("## "))

    def tool(self, action: str, text: str | None = None, date: str | None = None) -> str:
        if action == "write":
            return self.write(text or "")
        if action == "read":
            return self.read(date)
        if action == "list":
            return "\n".join(self.dates()) or "no diary yet"
        raise ValueError(f"unknown action: {action}")


class Todo:
    """A small JSON list. Open items show on the board."""

    def __init__(self, path, runner: Runner, home):
        self.path = Path(path)
        self.runner = runner
        self.home = home

    def load(self) -> dict:
        if not self.path.exists():
            return {"next": 1, "items": []}
        return json.loads(safe_read(self.path, self.home))

    def save(self, data: dict) -> None:
        self.runner.write(self.path, json.dumps(data, ensure_ascii=False, indent=1))

    def _find(self, data: dict, item_id) -> dict:
        for item in data["items"]:
            if item["id"] == int(item_id) and item["status"] == "open":
                return item
        raise ValueError(f"no open todo #{item_id}")

    def tool(self, action: str, text: str | None = None, id: int | None = None) -> str:
        data = self.load()
        now = clock.now().isoformat(timespec="minutes")
        if action == "add":
            text = (text or "").strip()
            if not text:
                raise ValueError("todo text is empty")
            item = {"id": data["next"], "text": text, "status": "open", "created": now}
            data["next"] += 1
            data["items"].append(item)
            self.save(data)
            return f"added #{item['id']}"
        if action in ("done", "drop"):
            if id is None:
                raise ValueError(f"{action} needs id")
            item = self._find(data, id)
            item["status"] = action
            item["closed"] = now
            self.save(data)
            return f"#{item['id']} {'done' if action == 'done' else 'dropped'}"
        if action == "list":
            return self.render(all_items=True)
        raise ValueError(f"unknown action: {action}")

    def render(self, all_items: bool = False) -> str:
        items = self.load()["items"]
        open_items = [i for i in items if i["status"] == "open"]
        lines = [f"- [ ] #{i['id']} {i['text']}" for i in open_items]
        closed = [i for i in items if i["status"] != "open"]
        recent = closed if all_items else [i for i in closed if i.get("closed", "").startswith(today())]
        for i in recent[-5:]:
            mark = "x" if i["status"] == "done" else "-"
            lines.append(f"- [{mark}] #{i['id']} {i['text']}")
        return "\n".join(lines) or "(no todos)"
