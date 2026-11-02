"""Inbox: a spool directory of events. Writers drop JSON files; the loop consumes them."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import clock


@dataclass
class Event:
    ts: float
    type: str
    source: str
    text: str
    path: Path | None = None

    def render(self) -> str:
        stamp = clock.at(self.ts).strftime("%m-%d %H:%M")
        return f"<env>{stamp} · {self.type} · {self.source}: {self.text}</env>"


class Inbox:
    def __init__(self, directory):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def put(self, type: str, source: str, text: str) -> Path:
        name = f"{time.time_ns()}-{uuid.uuid4().hex[:6]}"
        tmp = self.dir / f".{name}.tmp"
        tmp.write_text(json.dumps({"ts": time.time(), "type": type, "source": source, "text": text}, ensure_ascii=False))
        path = self.dir / f"{name}.json"
        os.replace(tmp, path)
        return path

    def peek(self) -> list[Event]:
        events = []
        for path in sorted(self.dir.glob("*.json")):
            try:
                d = json.loads(path.read_text())
                events.append(Event(d["ts"], d["type"], d["source"], d["text"], path))
            except (OSError, ValueError, KeyError):
                continue
        return events

    @staticmethod
    def ack(events: list[Event]) -> None:
        for event in events:
            if event.path:
                event.path.unlink(missing_ok=True)
