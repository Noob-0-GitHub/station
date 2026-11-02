"""Error kinds, redaction, and the error log (`state/errors.jsonl`).

Kinds decide what the loop does with a failed model call:

- `transient`, `rate_limit`: retry after `errors.retry_minutes` (or the provider's
  Retry-After); stop after `errors.max_consecutive` failures in a row.
- `auth`, `quota`: the operator or time fixes these, not a retry storm. Wait
  `errors.wait_minutes`, doubling up to `errors.max_wait_minutes` (or until the
  provider's reset time), and never stop.
- `request`, `config`, `bug`: retrying sends the same thing again. Stop at once.

Notices about errors go to the AI as inbox events. The log is for the AI and
the operator to read; every message is redacted before it is written.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

RETRY = {"transient", "rate_limit"}
WAIT = {"auth", "quota"}
FATAL = {"request", "config", "bug"}
KINDS = RETRY | WAIT | FATAL | {"tool", "compress", "hook", "reload", "crash"}

QUOTA_WORDS = ("usage_limit", "usage limit", "quota", "extra usage", "billing", "credit balance",
               "insufficient_credits", "payment_required", "out of credits")

_SECRETS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"tvly-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-~+/=]{8,}"),
    re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password|authorization|cookie|sessdata)"
               r"[\"']?\s*[:=]\s*[\"']?)[^\s\"',;}]{4,}"),
]


def redact(text) -> str:
    text = str(text)
    for pattern in _SECRETS[:-1]:
        text = pattern.sub("[REDACTED]", text)
    return _SECRETS[-1].sub(lambda m: m.group(1) + "[REDACTED]", text)


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 20] + f"… [{len(text) - limit + 20} more]"


def classify_http(status: int, body: str = "") -> str:
    text = (body or "").lower()
    if status in (401, 403):
        return "auth"
    if status == 402 or (status in (400, 429) and any(w in text for w in QUOTA_WORDS)):
        return "quota"
    if status == 429:
        return "rate_limit"
    if status in (408, 409, 425) or status >= 500:
        return "transient"
    return "request"


def classify_text(text: str) -> str:
    """For errors reported inside a stream rather than as an HTTP status."""
    return "quota" if any(w in (text or "").lower() for w in QUOTA_WORDS) else "transient"


def retry_after(headers, body: str = "", now: float | None = None) -> float | None:
    """Seconds to wait, from a Retry-After header or a reset field in a JSON body."""
    now = time.time() if now is None else now
    try:
        value = headers.get("retry-after") if headers is not None else None
        if value is not None and float(value) >= 0:
            return float(value)
    except (TypeError, ValueError):
        pass
    try:
        data = json.loads(body or "")
    except ValueError:
        return None
    for d in (data, data.get("error") if isinstance(data, dict) else None):
        if not isinstance(d, dict):
            continue
        seconds = d.get("resets_in_seconds")
        if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and seconds >= 0:
            return float(seconds)
        at = d.get("resets_at")
        if isinstance(at, (int, float)) and not isinstance(at, bool) and at > now:
            return float(at - now)
    return None


class ErrorLog:
    """Append-only JSON lines; rotated to errors.1.jsonl past `max_bytes`."""

    def __init__(self, directory, max_bytes: int = 1_000_000):
        self.path = Path(directory) / "errors.jsonl"
        self.max_bytes = max_bytes

    def record(self, where: str, kind: str, message, *, fatal: bool = False, detail: str | None = None, **extra) -> dict:
        row = {"ts": time.time(), "where": where, "kind": kind, "message": clip(redact(message), 2000), "fatal": fatal}
        row.update({k: v for k, v in extra.items() if v is not None})
        if detail:
            row["detail"] = clip(redact(detail), 6000)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if self.path.stat().st_size > self.max_bytes:
                os.replace(self.path, self.path.with_name("errors.1.jsonl"))
        except FileNotFoundError:
            pass
        with open(self.path, "a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return row

    def recent(self, limit: int = 20) -> list[dict]:
        if not self.path.exists():
            return []
        rows = []
        for line in self.path.read_text().splitlines()[-limit:]:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows
