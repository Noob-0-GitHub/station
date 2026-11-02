"""Root-side helpers to publish a short-lived access token into the Station user's auth directory.

Used by scripts/codex-token-sync.py. The destination directory is
owned by the Station user, who could try to plant symlinks: every path component is opened with
O_NOFOLLOW from `/`, the file is created with O_EXCL in that directory handle and renamed into place
relative to the same handle. Writes happen only when the content changes. Nothing here prints a token.
"""

from __future__ import annotations

import os
import pwd
import stat
from pathlib import Path


def open_dir_nofollow(path: Path, owner: int) -> int:
    """Open an absolute directory path without following any symlink."""
    if not path.is_absolute():
        raise SystemExit("--dest must be an absolute path")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in path.parts[1:]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except OSError as e:
        os.close(fd)
        raise SystemExit(f"cannot open {path} without following links: {e.strerror}") from None
    st = os.fstat(fd)
    if st.st_uid != owner or stat.S_IMODE(st.st_mode) & 0o077:
        os.close(fd)
        raise SystemExit(f"{path} must be owned by the Station user with mode 700")
    return fd


def current_text(dir_fd: int, name: str) -> str | None:
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError:
        return None  # e.g. a planted symlink: replace it
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "r", closefd=False) as f:
            return f.read(1 << 20)
    finally:
        os.close(fd)


def write_atomic(dir_fd: int, name: str, text: str, uid: int, gid: int) -> None:
    tmp = f".{name}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=dir_fd)
    try:
        os.fchown(fd, uid, gid)
        os.fchmod(fd, 0o600)
        os.write(fd, text.encode())
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        os.unlink(tmp, dir_fd=dir_fd)
        raise
    os.close(fd)
    try:
        os.replace(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        os.unlink(tmp, dir_fd=dir_fd)
        raise
    os.fsync(dir_fd)


def publish(dest: Path, user: str, text: str, check: bool = False) -> str:
    """Write `text` to `dest` as `user` (mode 600) if it changed. Returns updated / unchanged / would update."""
    entry = pwd.getpwnam(user)
    dir_fd = open_dir_nofollow(dest.parent, entry.pw_uid)
    try:
        if current_text(dir_fd, dest.name) == text:
            return "unchanged"
        if check:
            return "would update"
        write_atomic(dir_fd, dest.name, text, entry.pw_uid, entry.pw_gid)
        return "updated"
    finally:
        os.close(dir_fd)
