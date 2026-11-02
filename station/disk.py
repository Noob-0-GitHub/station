"""Soft disk quota: what the AI's home and Station's runtime use, against a fixed allowance.

No filesystem quota (that needs a reboot here): Station measures with `du` at most every
`disk.check_minutes`, and acts like the credits do:

- from `warn_ratio` (90%) a `disk` panel is on the board;
- at the allowance (100%) a high notice, and no model calls until usage is back under it.
  Station checks again every `check_minutes` and goes on by itself once space is freed.

Hard writes past the allowance are not prevented in between checks.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

GB = 1_000_000_000


def measure(paths: list[Path], timeout: float = 120) -> int | None:
    """Bytes used by `paths` together (each file once, also when one path is inside another)."""
    existing = [str(p) for p in paths if p.exists()]
    if not existing:
        return 0
    try:
        r = subprocess.run(["nice", "-n", "19", "du", "-sxbc", "--", *existing], capture_output=True,
                           text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = [x for x in r.stdout.splitlines() if x.endswith("\ttotal")]
    try:
        return int(lines[-1].split("\t")[0])
    except (IndexError, ValueError):
        return None


class Disk:
    def __init__(self, cfg: dict, paths: list[Path]):
        self.cfg, self.paths = cfg, paths
        self.used: int | None = None
        self.checked = 0.0

    @property
    def allowance(self) -> int:
        return int(float(self.cfg["allowance_gb"]) * GB)

    def check(self, force: bool = False) -> int | None:
        now = time.time()
        if force or now - self.checked >= self.cfg["check_minutes"] * 60:
            self.checked = now
            used = measure(self.paths)
            if used is not None:
                self.used = used
        return self.used

    def ratio(self) -> float:
        return (self.used or 0) / self.allowance if self.allowance else 0.0

    def over(self) -> bool:
        return self.cfg["enabled"] and self.used is not None and self.ratio() >= 1.0

    def text(self) -> str:
        return f"{(self.used or 0) / GB:.2f} GB of {self.allowance / GB:g} GB ({self.ratio():.0%})"
