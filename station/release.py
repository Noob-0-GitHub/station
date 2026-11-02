"""Initialize the editable release repository (stdlib-only bootstrap)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def init_repository(code_dir: Path) -> bool:
    """Create upstream/main for an extracted release; never reinitialize a repo."""
    code_dir = code_dir.expanduser().resolve()
    if (code_dir / ".git").exists():
        return False
    release = code_dir / "RELEASE"
    if not release.is_file() or not (code_dir / "pyproject.toml").is_file():
        raise ValueError(f"not an extracted Station release: {code_dir}")
    version = release.read_text().splitlines()[0]

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(code_dir), *args], check=True)

    git("init", "-q", "-b", "upstream")
    git("config", "--local", "user.name", "Station")
    git("config", "--local", "user.email", "station@localhost")
    git("add", "-A")
    git("commit", "-q", "-m", f"Station {version}")
    git("checkout", "-q", "-b", "main")
    return True


if __name__ == "__main__":
    init_repository(Path(sys.argv[1]))
