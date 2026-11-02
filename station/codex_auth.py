"""Codex (ChatGPT subscription) OAuth access tokens.

Two sources, never mixed with the API-key dotenv files (`secrets.py`):

- `HermesCodexCredential` (development): each lookup runs a short helper inside
  Hermes's own Python runtime. Hermes selects a credential from its pool,
  refreshing it under its own cross-process lock when it is about to expire,
  and prints the access token and request headers to a pipe.
- `FileCodexCredential` (production, `codex.token_file`): Station only reads a
  JSON file that a root-side job (`scripts/codex-token-sync.py`) writes. The
  refresh token never leaves root, so Station cannot refresh and never races
  Hermes on a rotating refresh token. The file is re-read whenever it changes.

Both return `{"token", "base_url", "headers", "exp"}` and refuse any base URL
that is not the official Codex endpoint.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

HELPER = r"""
import json, os, sys
os.environ.pop("PYTHONHOME", None); os.environ.pop("PYTHONPATH", None); os.environ.pop("VIRTUAL_ENV", None)
sys.path.insert(0, sys.argv[1])
os.environ.setdefault("HERMES_HOME", sys.argv[2])
import hermes_bootstrap  # noqa: F401
from agent.auxiliary_client import _resolve_codex_credential_and_base
from agent.codex_headers import codex_cloudflare_headers, is_official_codex_base_url
token, base = _resolve_codex_credential_and_base(sys.argv[3])
if not token:
    sys.exit("no usable openai-codex credential in the Hermes pool")
if not is_official_codex_base_url(base):
    sys.exit("refusing: credential does not route to the official Codex endpoint")
json.dump({"token": token, "base_url": base, "headers": codex_cloudflare_headers(token, base_url=base)}, sys.stdout)
"""

# Seconds before expiry at which a token is treated as unusable.
EXPIRY_MARGIN = 60
FILE_VERSION = 1


class CodexAuthError(RuntimeError):
    pass


def jwt_exp(token: str) -> float:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return float(json.loads(base64.urlsafe_b64decode(payload)).get("exp") or 0)
    except Exception:
        return 0.0


def is_official_codex_base_url(base_url: str) -> bool:
    """Same rule as Hermes: https://chatgpt.com/backend-api/codex only."""
    try:
        parsed = urlparse(base_url)
        path = parsed.path.rstrip("/")
        return (parsed.scheme == "https" and parsed.hostname == "chatgpt.com" and parsed.port in (None, 443)
                and (path == "/backend-api/codex" or path.startswith("/backend-api/codex/")))
    except (TypeError, ValueError):
        return False


def make_credential(cfg: dict, model: str):
    codex = cfg.get("codex") or {}
    if codex.get("token_file"):
        return FileCodexCredential(codex["token_file"])
    return HermesCodexCredential(codex, model)


class HermesCodexCredential:
    def __init__(self, cfg: dict, model: str):
        self.cfg = cfg
        self.model = model
        self._cached: dict | None = None

    def _python(self) -> str:
        if self.cfg.get("python"):
            return self.cfg["python"]
        out = subprocess.run(["hermes", "--print-runtime-command"], capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            raise CodexAuthError(f"hermes --print-runtime-command failed: {out.stderr.strip()[:200]}")
        return json.loads(out.stdout)[0]

    def get(self, force: bool = False) -> dict:
        now = time.time()
        if not force and self._cached and self._cached["exp"] - now > 300:
            return self._cached
        proc = subprocess.run(
            [self._python(), "-I", "-c", HELPER, self.cfg["hermes_root"], self.cfg["hermes_home"], self.model],
            capture_output=True, text=True, timeout=90,
        )
        if proc.returncode != 0:
            # stderr may contain tracebacks but never the token (it is only printed on success).
            raise CodexAuthError(proc.stderr.strip().splitlines()[-1][:300] if proc.stderr.strip() else "helper failed")
        data = json.loads(proc.stdout)
        data["exp"] = jwt_exp(data["token"]) or now + 600
        self._cached = data
        return data


def parse_token_file(text: str, now: float | None = None) -> dict:
    """Validate the synced file. Error messages never include token text."""
    try:
        raw = json.loads(text)
    except ValueError:
        raise CodexAuthError("codex token file is not valid JSON") from None
    if not isinstance(raw, dict) or raw.get("version") != FILE_VERSION:
        raise CodexAuthError(f"codex token file: expected version {FILE_VERSION}")
    token, base, headers = raw.get("access_token"), raw.get("base_url"), raw.get("headers")
    if not isinstance(token, str) or not token.strip():
        raise CodexAuthError("codex token file has no access_token")
    if not isinstance(base, str) or not is_official_codex_base_url(base):
        raise CodexAuthError("codex token file: base_url is not the official Codex endpoint")
    if not isinstance(headers, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
        raise CodexAuthError("codex token file: headers must map strings to strings")
    if any(k.lower() == "authorization" for k in headers):
        raise CodexAuthError("codex token file: headers must not contain Authorization")
    exp = raw.get("expires_at")
    exp = float(exp) if isinstance(exp, (int, float)) and not isinstance(exp, bool) else jwt_exp(token)
    if not exp:
        raise CodexAuthError("codex token file has no expiry")
    now = time.time() if now is None else now
    if exp - now <= EXPIRY_MARGIN:
        raise CodexAuthError("codex access token has expired; waiting for the root-side sync")
    return {"token": token.strip(), "base_url": base, "headers": dict(headers), "exp": exp}


class FileCodexCredential:
    """Reads the root-synced token file; reloads when its identity or mtime changes."""

    def __init__(self, path: str | os.PathLike):
        self.path = Path(path).expanduser()
        self._stamp: tuple | None = None
        self._cached: dict | None = None

    def _read(self) -> tuple[tuple, str]:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            raise CodexAuthError(f"codex token file {self.path} does not exist yet; waiting for the root-side sync") from None
        except OSError as e:
            raise CodexAuthError(f"cannot open codex token file {self.path}: {e.strerror}") from None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise CodexAuthError("codex token file is not a regular file")
            if st.st_mode & 0o077:
                raise CodexAuthError("codex token file is readable by group or others (chmod 600)")
            if st.st_uid != os.geteuid():
                raise CodexAuthError("codex token file is not owned by the Station user")
            with os.fdopen(fd, "r", closefd=False) as f:
                text = f.read(1 << 20)
            return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size), text
        finally:
            os.close(fd)

    def get(self, force: bool = False) -> dict:
        # force cannot refresh here; it only rereads the file the sync may have replaced.
        stamp, text = self._read()
        if force or stamp != self._stamp or self._cached is None:
            self._cached, self._stamp = parse_token_file(text), stamp
        elif self._cached["exp"] - time.time() <= EXPIRY_MARGIN:
            raise CodexAuthError("codex access token has expired; waiting for the root-side sync")
        return self._cached
