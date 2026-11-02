"""Metered access to paid APIs: one place that holds the keys, checks the credit
limit before a call, reads the usage the service reports, prices it, and writes
the ledger.

Used in process by Station's own tools (`web`), and from anything else through
`station api SERVICE METHOD PATH ...` (a separate process, so a tool never
imports Station's code and Station never imports a tool's).

Pricing reads what the service reports, never what the caller claims:

- OpenAI: `usage` of Responses, Chat Completions, Embeddings and transcriptions
  (tokens, or seconds for duration-billed models), priced from `prices`
- Tavily: `usage.credits` (requested with `include_usage`), at `credits.tavily_usd`

A call whose cost cannot be determined is still recorded, with `list_usd` NULL,
and counted as unpriced in `station credits`.
"""

from __future__ import annotations

import json
import mimetypes
import re
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import credits as credits_mod
from . import secrets

PATH_RE = re.compile(r"^/[A-Za-z0-9._~/-]*$")


@dataclass(frozen=True)
class Service:
    base: str
    key: str  # secret name
    auth: str = "bearer"


SERVICES = {
    "openai": Service("https://api.openai.com", "OPENAI_API_KEY"),
    "tavily": Service("https://api.tavily.com", "TAVILY_API_KEY"),
}


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: bytes = b"", kind: str = "error"):
        super().__init__(message)
        self.status, self.body, self.kind = status, body, kind  # kind: error, limit, usage


@dataclass
class Result:
    status: int
    body: bytes
    content_type: str
    row: dict | None  # the ledger row, if the call was recorded

    def json(self):
        return json.loads(self.body)


# ---- the call ---------------------------------------------------------------

def check_limit(cfg: dict, ledger) -> None:
    c = cfg["credits"]
    try:
        credits_mod.validate(c)
    except ValueError as e:
        raise ApiError(str(e), kind="config") from None
    if not c["enabled"]:
        return
    over = [k for k, s in credits_mod.status(ledger, c, time.time()).items() if s["ratio"] >= c["limit_ratio"]]
    if over:
        raise ApiError(f"credit limit reached ({' and '.join(over)} at {c['limit_ratio']:.0%}); "
                       "paid API calls resume when the allowance resets (`station credits`)", kind="limit")


def request(cfg: dict, ledger, service: str, method: str, path: str, *, json_body=None,
            form: dict | None = None, files: dict | None = None, what: str = "", timeout: float = 600) -> Result:
    svc = SERVICES.get(service)
    if svc is None:
        raise ApiError(f"unknown service {service!r}; known: {', '.join(sorted(SERVICES))}", kind="usage")
    method = method.upper()
    if method not in {"GET", "POST", "DELETE"}:
        raise ApiError("method must be GET, POST or DELETE", kind="usage")
    if not PATH_RE.match(path or "") or ".." in path:
        raise ApiError("path must be an absolute path on the service, like /v1/embeddings", kind="usage")
    if isinstance(json_body, dict) and json_body.get("stream"):
        raise ApiError("streaming is not supported; send the request without stream", kind="usage")
    paid = method != "GET"
    if paid:
        check_limit(cfg, ledger)
    if service == "tavily" and isinstance(json_body, dict):
        json_body = {**json_body, "include_usage": True}
    try:
        key = secrets.get(cfg, svc.key)
    except secrets.SecretMissing:
        raise ApiError(f"{service} is not configured ({svc.key} is missing)", kind="usage") from None
    except secrets.SecretError as e:
        raise ApiError(f"{service} credentials are unusable: {e}", kind="usage") from None

    headers = {"Authorization": f"Bearer {key}"}
    data = None
    if files or form:
        data, ctype = multipart(form or {}, files or {})
        headers["Content-Type"] = ctype
    elif json_body is not None:
        data = json.dumps(json_body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(svc.base + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, body, ctype = r.status, r.read(), r.headers.get("content-type", "")
    except urllib.error.HTTPError as e:
        raise ApiError(f"{service} HTTP {e.code}", status=e.code, body=e.read()[:4000]) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise ApiError(f"{service}: {getattr(e, 'reason', e)}") from None

    row = None
    if paid:
        request_info = dict(json_body or {}) if isinstance(json_body, dict) else {}
        request_info.update(form or {})
        list_usd, units, model = price(cfg, service, path, request_info, body, ctype)
        c = cfg["credits"]
        row = ledger.add(service, model=model, kind=path, what=what[:200] or None, units=units,
                         list_usd=list_usd, paid_usd=credits_mod.paid(c, service, list_usd),
                         credits=(list_usd or 0.0) * credits_mod.factor(c, service), source="api")
    return Result(status, body, ctype, row)


def multipart(form: dict, files: dict) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in form.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    for name, path in files.items():
        path = Path(path)
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{path.name}"\r\n'
                     f"Content-Type: {ctype}\r\n\r\n".encode())
        parts += [path.read_bytes(), b"\r\n"]
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


# ---- pricing ----------------------------------------------------------------

def price(cfg: dict, service: str, path: str, req: dict, body: bytes, ctype: str) -> tuple[float | None, dict, str | None]:
    """(list price in USD or None, raw units, model) from the service's own response."""
    try:
        data = json.loads(body) if "json" in ctype or body[:1] in (b"{", b"[") else None
    except ValueError:
        data = None
    if service == "tavily":
        used = ((data or {}).get("usage") or {}).get("credits")
        if used is None:
            return None, {}, None
        return float(used) * cfg["credits"]["tavily_usd"], {"tavily_credits": used}, None
    if service == "openai":
        model = (data or {}).get("model") or req.get("model")
        usage = (data or {}).get("usage") if isinstance(data, dict) else None
        return openai_cost(cfg["prices"], model, usage), usage or {}, model
    return None, {}, None


def model_price(prices: dict, model: str | None) -> dict | None:
    """Price entry for a model; dated snapshots (`gpt-4o-transcribe-2025-03-20`) use their base name."""
    if not model:
        return None
    if model in prices:
        return prices[model]
    base = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", model)
    return prices.get(base)


def openai_cost(prices: dict, model: str | None, usage: dict | None) -> float | None:
    p = model_price(prices, model)
    if not p or not isinstance(usage, dict):
        return None
    if usage.get("type") == "duration":  # duration-billed transcription
        if "per_minute" not in p or usage.get("seconds") is None:
            return None
        return float(usage["seconds"]) / 60 * p["per_minute"]
    if "input_tokens" in usage or "prompt_tokens" in usage:
        if "input" not in p:
            return None
        total_in = usage.get("input_tokens", usage.get("prompt_tokens")) or 0
        details = usage.get("input_tokens_details") or usage.get("prompt_tokens_details") \
            or usage.get("input_token_details") or {}
        cached = details.get("cached_tokens") or 0
        audio = details.get("audio_tokens") or 0
        out = usage.get("output_tokens", usage.get("completion_tokens")) or 0
        if (out and "output" not in p) or (audio and "audio_input" not in p):
            return None
        if not audio:
            return credits_mod.call_cost({"output": 0.0, **p}, total_in - cached, 0, 0, cached, out)
        return ((total_in - cached - audio) * p["input"] + audio * p["audio_input"]
                + cached * p.get("cache_read", p["input"]) + out * p.get("output", 0.0)) / 1e6
    return None
