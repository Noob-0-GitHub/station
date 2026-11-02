"""Run shell commands and write files on behalf of the AI, optionally as another user."""

from __future__ import annotations

import os
import pwd
import signal
import subprocess
from pathlib import Path


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n…[{len(text) - limit} characters omitted]…\n{text[-half:]}"


def inside(path, base) -> bool:
    real = Path(os.path.realpath(path))
    root = Path(os.path.realpath(base))
    return real == root or root in real.parents


def safe_read(path, base) -> str:
    """Read a file the AI controls without following links out of `base`.

    The runtime may run as root while the AI owns its home; a symlink planted
    by the AI must not turn a root-side read into a read of a secret file.
    """
    if not inside(path, base):
        raise ValueError(f"{path} points outside {base}; refusing to read")
    return Path(os.path.realpath(path)).read_text()


class Runner:
    def __init__(self, cwd, user: str | None = None, max_output: int = 12000, env: dict | None = None):
        self.cwd = str(cwd)
        self.max_output = max_output
        self.env = dict(env or {})
        self.prefix: list[str] = []
        self.ids: tuple[int, int] | None = None
        if user and os.geteuid() == 0:
            pw = pwd.getpwnam(user)
            self.ids = (pw.pw_uid, pw.pw_gid)
            self.prefix = ["setpriv", f"--reuid={pw.pw_uid}", f"--regid={pw.pw_gid}", "--clear-groups", "--"]

    def run(self, command: str, timeout: float, env: dict | None = None) -> tuple[int | None, str]:
        """Return (exit code or None on timeout, combined output)."""
        full_env = {
            "PATH": f"{self.cwd}/bin:/usr/local/bin:/usr/bin:/bin",
            "HOME": self.cwd,
            "LANG": "C.UTF-8",
            "TERM": "dumb",
            **self.env,
        }
        full_env.update(env or {})
        proc = subprocess.Popen(
            self.prefix + ["bash", "-c", command],
            cwd=self.cwd,
            env=full_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            out, _ = proc.communicate(timeout=timeout)
            code: int | None = proc.returncode
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            try:
                out, _ = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:  # a detached child still holds the pipe
                proc.kill()
                out = b""
            code = None
        return code, clip(out.decode(errors="replace"), self.max_output)

    # ---- files in the AI's home ------------------------------------------
    # The runtime may be root while the AI owns these directories, so writes
    # refuse symlinked parents and never open an existing temp path.

    def chown(self, path) -> None:
        if self.ids:
            os.chown(path, *self.ids)

    def mkdir(self, path) -> Path:
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        if not inside(path, self.cwd):
            raise ValueError(f"{path} is outside the home directory; refusing to write")
        self.chown(path)
        return path

    def write(self, path, text: str, mode: int | None = None, own: bool = True) -> None:
        path = Path(path)
        self.mkdir(path.parent)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, mode if mode is not None else 0o644)
        if own:
            self.chown(tmp)
        os.replace(tmp, path)
