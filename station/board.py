"""Board: labelled panels rendered fresh into the tail of every request."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml

from . import clock
from .runner import Runner, clip, safe_read

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
FRONT_RE = re.compile(r"\A---\n(.*?)\n---[ \t]*(?:\n(.*))?\Z", re.S)


@dataclass
class Panel:
    name: str
    body: str = ""
    tags: list[str] = field(default_factory=list)
    command: str | None = None
    cooldown: float = 60.0
    timeout: float = 5.0
    max_chars: int = 1200
    order: int = 100


def parse_panel(name: str, text: str, max_chars: int = 1200) -> Panel:
    meta: dict = {}
    body = text
    match = FRONT_RE.match(text)
    if match:
        meta = yaml.safe_load(match.group(1)) or {}
        if not isinstance(meta, dict):
            raise ValueError("front matter must be a YAML mapping")
        body = match.group(2) or ""
    tags = meta.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    return Panel(
        name=name,
        body=body.strip(),
        tags=[str(t) for t in tags],
        command=meta.get("command") or None,
        cooldown=float(meta.get("cooldown", 60)),
        timeout=float(meta.get("timeout", 5)),
        max_chars=int(meta.get("max_chars", max_chars)),
        order=int(meta.get("order", 100)),
    )


class Board:
    def __init__(self, directory, runner: Runner, builtins: dict[str, Callable[[], str | None]] | None = None,
                 base=None, panel_max_chars: int = 1200, max_chars: int = 6000):
        self.dir = Path(directory)
        self.runner = runner
        self.builtins = builtins or {}
        self.base = base or self.dir
        self.panel_max_chars = panel_max_chars
        self.max_chars = max_chars
        self._cache: dict[str, dict] = {}

    # ---- storage ---------------------------------------------------------

    def path(self, name: str) -> Path:
        if not NAME_RE.match(name or ""):
            raise ValueError(f"bad panel name: {name!r} (lowercase letters, digits, - and _)")
        if name in self.builtins:
            raise ValueError(f"{name} is a built-in panel and cannot be changed")
        return self.dir / f"{name}.md"

    def panels(self) -> list[Panel]:
        out = []
        for path in self.dir.glob("*.md"):
            name = path.stem
            if not NAME_RE.match(name) or name in self.builtins:
                continue
            try:
                out.append(parse_panel(name, safe_read(path, self.base), self.panel_max_chars))
            except Exception as e:  # show the error instead of hiding the panel
                out.append(Panel(name=name, body=f"(panel could not be parsed: {e})"))
        return sorted(out, key=lambda p: (p.order, p.name))

    def _write(self, path: Path, text: str) -> None:
        self.runner.write(path, text)

    # ---- rendering -------------------------------------------------------

    def _command_output(self, panel: Panel, env: dict, now: float) -> str:
        cached = self._cache.get(panel.name)
        if cached and cached["cmd"] == panel.command and now - cached["ts"] < panel.cooldown:
            return cached["text"]
        code, out = self.runner.run(panel.command, timeout=panel.timeout, env={**env, "CTX_PANEL": panel.name})
        out = out.strip()
        good = cached["good"] if cached and cached["cmd"] == panel.command else None
        if code == 0:
            text = out
            good = (now, out)
        else:
            why = "timed out" if code is None else f"exit code {code}"
            if good:
                stamp = clock.at(good[0]).strftime("%H:%M")
                text = f"{good[1]}\n(stale: output from {stamp}; this run {why})"
            else:
                text = f"(command failed: {why})\n{out}".strip()
        self._cache[panel.name] = {"ts": now, "cmd": panel.command, "text": text, "good": good}
        return text

    def render(self, env: dict) -> str:
        now = time.time()
        parts: list[tuple[str, str]] = []
        for name, fn in self.builtins.items():
            try:
                body = fn()
            except Exception as e:
                body = f"(error: {e})"
            if body is None:  # a built-in with nothing to say is left out
                continue
            parts.append((name, f"## {name} · built-in\n{clip(body, self.panel_max_chars)}"))
        for panel in self.panels():
            body = self._command_output(panel, env, now) if panel.command else panel.body
            tags = f" [{', '.join(panel.tags)}]" if panel.tags else ""
            parts.append((panel.name, f"## {panel.name}{tags}\n{clip(body, panel.max_chars) or '(empty)'}"))
        return "<board>\n" + "\n\n".join(self.fit(parts)) + "\n</board>"

    def fit(self, parts: list[tuple[str, str]]) -> list[str]:
        """Keep panels in order within max_chars; cut the first one that overflows, drop the rest."""
        out: list[str] = []
        used = 0
        cut: str | None = None
        dropped: list[str] = []
        for name, text in parts:
            room = self.max_chars - used
            if cut or dropped:
                dropped.append(name)
            elif len(text) <= room:
                out.append(text)
                used += len(text) + 2
            elif room >= 300:
                out.append(text[: room - 20].rstrip() + "\n…(cut)")
                used = self.max_chars
                cut = name
            else:
                dropped.append(name)
        if cut or dropped:
            what = "; ".join(x for x in (f"cut {cut}" if cut else "",
                                          f"hid {', '.join(dropped)}" if dropped else "") if x)
            out.append(f"(the board is over {self.max_chars} characters: {what}. Lower a panel's max_chars or delete panels you do not use.)")
        return out

    # ---- tool ------------------------------------------------------------

    def tool(self, action: str, name: str | None = None, text: str | None = None,
             old: str | None = None, new: str | None = None) -> str:
        if action == "list":
            lines = [f"{n} · built-in" for n in self.builtins]
            for p in self.panels():
                kind = "command" if p.command else "static"
                lines.append(f"{p.name} · {kind} · tags={p.tags}")
            return "\n".join(lines)
        path = self.path(name or "")
        if action == "read":
            if not path.exists():
                raise ValueError(f"no panel {name}")
            return safe_read(path, self.base)
        if action == "write":
            if text is None:
                raise ValueError("write needs text")
            parse_panel(name, text, self.panel_max_chars)
            self._write(path, text)
            self._cache.pop(name, None)
            return f"wrote {name}"
        if action == "patch":
            if not path.exists():
                raise ValueError(f"no panel {name}")
            if not old or new is None:
                raise ValueError("patch needs old and new")
            current = safe_read(path, self.base)
            count = current.count(old)
            if count != 1:
                raise ValueError(f"old appears {count} times in {name}; it must appear exactly once")
            updated = current.replace(old, new, 1)
            parse_panel(name, updated, self.panel_max_chars)
            self._write(path, updated)
            self._cache.pop(name, None)
            return f"patched {name}"
        if action == "delete":
            if not path.exists():
                raise ValueError(f"no panel {name}")
            path.unlink()
            self._cache.pop(name, None)
            return f"deleted {name}"
        raise ValueError(f"unknown action: {action}")
