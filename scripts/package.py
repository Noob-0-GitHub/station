#!/usr/bin/env python3
"""Build a Station release tarball containing only what the AI needs.

Usage: scripts/package.py [--out DIR] [--ref REF]

The release is taken from a git commit (default HEAD), so uncommitted changes
are never shipped. Output: <out>/station-<version>.tar.gz with one top-level
directory and a RELEASE file (version, commit, build time).
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import re
import subprocess
import sys
import tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SHIP = ["station", "docs", "pyproject.toml", "requirements.lock", "install.sh", ".gitignore"]
NEVER = re.compile(r"(^|/)(tests|dev|research|probes|scripts)/|(^|/)DESIGN\.md$|\.env$|(^|/)__pycache__/")


def git(*args: str) -> bytes:
    return subprocess.run(["git", "-C", str(REPO), *args], check=True, capture_output=True).stdout


def version(ref: str) -> str:
    text = git("show", f"{ref}:pyproject.toml").decode()
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    if not match:
        raise SystemExit("no project version in pyproject.toml")
    base = match.group(1)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+\-]*", base):
        raise SystemExit("unsafe project version in pyproject.toml")
    return f"{base}+g{git('rev-parse', '--short', ref).decode().strip()}"


def build(out: Path, ref: str = "HEAD") -> Path:
    # Resolve once so a branch moving during the build cannot mix revisions.
    ref = git("rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
    tracked = set(git("ls-tree", "--name-only", ref).decode().split())
    missing = [p for p in SHIP if p not in tracked]
    if missing:
        raise SystemExit(f"not committed at {ref}: {', '.join(missing)}")
    ver = version(ref)
    commit = git("rev-parse", ref).decode().strip()
    prefix = f"station-{ver}/"
    raw = git("archive", "--format=tar", f"--prefix={prefix}", ref, "--", *SHIP)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    release = f"{ver}\ncommit {commit}\nbuilt {stamp}\n".encode()
    with tarfile.open(fileobj=io.BytesIO(raw)) as src:
        members = src.getmembers()
        for member in members:
            name = member.name.removeprefix(prefix)
            if member.name.rstrip("/") == prefix.rstrip("/"):
                continue
            allowed = (
                name in SHIP
                or (name.startswith("station/") and name.endswith(".py"))
                or (name.startswith("station/templates/") and name.endswith(".md"))
                or (name.startswith("docs/") and name.endswith(".md"))
                or (name.startswith("station/licenses/") and name.endswith(".txt"))
            )
            if NEVER.search(name) or not (member.isfile() or member.isdir()) or (member.isfile() and not allowed):
                raise SystemExit(f"refusing to ship {name}")
        out.mkdir(parents=True, exist_ok=True)
        target = out / f"station-{ver}.tar.gz"
        with tarfile.open(target, "w:gz") as dst:
            for member in members:
                dst.addfile(member, src.extractfile(member) if member.isfile() else None)
            info = tarfile.TarInfo(prefix + "RELEASE")
            info.size, info.mode, info.mtime = len(release), 0o644, int(dt.datetime.now().timestamp())
            dst.addfile(info, io.BytesIO(release))
    return target


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "Build a Station release").splitlines()[0])
    p.add_argument("--out", type=Path, default=REPO / "dist")
    p.add_argument("--ref", default="HEAD")
    args = p.parse_args(argv)
    if git("status", "--porcelain").strip() and args.ref == "HEAD":
        print("note: the working tree has uncommitted changes; they are not in the release", file=sys.stderr)
    print(build(args.out, args.ref))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
