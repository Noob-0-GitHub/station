"""Skills: Markdown how-tos in the AI's home, one directory per skill.

    skills/<name>/SKILL.md     front matter: name, description
    skills/<name>/<other files>
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from .runner import safe_read

FRONT_RE = re.compile(r"\A---\n(.*?)\n---", re.S)
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class Skills:
    def __init__(self, directory, home):
        self.dir = Path(directory)
        self.home = home

    def index(self) -> list[tuple[str, str]]:
        out = []
        for skill_md in sorted(self.dir.glob("*/SKILL.md")):
            name = skill_md.parent.name
            if not NAME_RE.match(name):
                continue
            desc = ""
            try:
                match = FRONT_RE.match(safe_read(skill_md, self.home))
                meta = yaml.safe_load(match.group(1)) if match else {}
                desc = str((meta or {}).get("description") or "")
            except Exception as e:
                desc = f"(could not read: {e})"
            out.append((name, desc))
        return out

    def render(self) -> str:
        rows = [f"- {name}: {desc}" for name, desc in self.index()]
        return "\n".join(rows) or "(no skills yet)"

    def read(self, name: str, file: str | None = None) -> str:
        if not NAME_RE.match(name or ""):
            raise ValueError(f"bad skill name: {name!r}")
        base = self.dir / name
        if not base.is_dir():
            raise ValueError(f"no skill {name}")
        target = base / (file or "SKILL.md")
        text = safe_read(target, base)
        if not file:
            others = sorted(str(p.relative_to(base)) for p in base.rglob("*") if p.is_file() and p.name != "SKILL.md")
            if others:
                text += "\n\n---\nOther files in this skill: " + ", ".join(others)
        return text
