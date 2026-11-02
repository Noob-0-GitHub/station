"""API keys for third-party services, one dotenv file per service.

`secrets.dir` holds files like `tavily.env` (TAVILY_API_KEY) and `openai.env`
(OPENAI_API_KEY, an OpenAI API platform key). Lookup order for a name: the
process environment, then the `*.env` files in that directory, re-read on every
lookup so edits take effect without a restart.

Not handled here: the Codex / ChatGPT subscription login. That is an OAuth
access token with an expiry (see `codex_auth`), not an API key, and must never
be stored in or read from these files.

A file is refused if it is a symlink, not a regular file, or readable by group
or others. A name defined in two files is an error rather than a guess.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path


class SecretError(RuntimeError):
    pass


class SecretMissing(SecretError):
    pass


def parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip("'\"")
        if key and value:
            out[key] = value
    return out


def _read_checked(path: Path) -> str:
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise SecretError(f"{path.name}: not a regular file")
    if st.st_mode & 0o077:
        raise SecretError(f"{path.name}: readable by group or others (chmod 600)")
    return path.read_text()


def load(directory: str | os.PathLike | None) -> dict[str, str]:
    """All names from `<directory>/*.env`. Raises on unsafe files or duplicate names."""
    if not directory:
        return {}
    d = Path(directory).expanduser()
    if not d.is_dir():
        return {}
    found: dict[str, str] = {}
    origin: dict[str, str] = {}
    for path in sorted(d.glob("*.env")):
        for key, value in parse(_read_checked(path)).items():
            if key in found:
                raise SecretError(f"{key} is defined in both {origin[key]} and {path.name}")
            found[key], origin[key] = value, path.name
    return found


def get(cfg: dict, name: str) -> str:
    value = os.environ.get(name, "") or load(cfg.get("secrets", {}).get("dir")).get(name, "")
    if not value:
        raise SecretMissing(f"{name} is not set (environment or secrets.dir)")
    return value
