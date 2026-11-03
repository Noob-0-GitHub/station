"""Model clients. History uses Anthropic-style content blocks; each client converts."""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import yaml

from .errors import classify_http, classify_text, redact, retry_after

RETRY_STATUS = {408, 429, 500, 502, 503, 504, 529}


class LLMError(RuntimeError):
    """A failed model call. `kind` is one of station.errors.KINDS and decides what the loop does."""

    def __init__(self, message: str, kind: str = "transient", *, status: int | None = None,
                 retry_after: float | None = None):
        super().__init__(redact(message))
        self.kind = kind
        self.status = status
        self.retry_after = retry_after


class CallInterrupted(LLMError):
    """Raised into a model call by the stop signal handler (Life._on_signal); the router turns it into Stop."""


def make_llm(cfg: dict):
    provider = cfg.get("provider", "anthropic")
    if provider == "anthropic":
        return Anthropic(cfg)
    if provider == "codex":
        return Codex(cfg)
    if provider == "claude-subscription-directsdk-experimental":
        from .claude_code import ClaudeCode

        return ClaudeCode(cfg)
    raise LLMError(f"unknown provider: {provider}", "config")


def _post(url: str, data: bytes, headers: dict, timeout: float = 600):
    return urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=timeout)


def _with_retries(fn, retries: list | None = None):
    """Call `fn`, retrying transient HTTP and connection errors; each retry is appended to `retries`."""
    delay = 5.0
    for attempt in range(4):
        try:
            return fn()
        except urllib.error.HTTPError as e:
            detail = e.read()[:500].decode(errors="replace")
            kind = classify_http(e.code, detail)
            if e.code in RETRY_STATUS and kind in ("transient", "rate_limit") and attempt < 3:
                if retries is not None:
                    retries.append({"error": f"HTTP {e.code}", "wait": delay})
                time.sleep(delay)
                delay *= 3
                continue
            raise LLMError(f"HTTP {e.code}: {detail}", kind, status=e.code,
                           retry_after=retry_after(e.headers, detail)) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < 3:
                if retries is not None:
                    retries.append({"error": redact(f"{type(e).__name__}: {e}")[:200], "wait": delay})
                time.sleep(delay)
                delay *= 3
                continue
            raise LLMError(f"connection failed: {e}", "transient") from None
    raise LLMError("unreachable")


# ---- Anthropic Messages API ---------------------------------------------------

ANTHROPIC_BLOCKS = {"text", "tool_use", "tool_result", "thinking", "redacted_thinking", "image"}


def thinking_allowed(messages: list) -> bool:
    """Anthropic rejects thinking when the last assistant turn called tools without a thinking
    block, which happens when that turn came from another provider (a fallback switch). Such a
    request goes without thinking; the next one has it again."""
    last = next((m for m in reversed(messages) if m["role"] == "assistant"), None)
    if last is None or not any(b.get("type") == "tool_use" for b in last["content"]):
        return True
    return any(b.get("type") in ("thinking", "redacted_thinking") for b in last["content"])


def resolve_api(api: dict) -> tuple[str, str]:
    base = api.get("base_url")
    key = os.environ.get(api.get("api_key_env") or "", "")
    provider = api.get("hermes_provider")
    if provider and (not base or not key):
        cfg = yaml.safe_load(Path(api["hermes_config"]).read_text())
        for entry in cfg.get("custom_providers") or []:
            if entry.get("name") == provider:
                base = base or entry.get("base_url")
                key = key or entry.get("api_key")
                break
    if not base or not key:
        raise LLMError("API base_url or key is not configured", "config")
    return base.rstrip("/"), key


class Anthropic:
    def __init__(self, cfg: dict):
        self.base, self.key = resolve_api(cfg["api"])
        self.model = cfg["model"]
        self.effort = cfg.get("effort")
        self.max_tokens = cfg["max_tokens"]
        self.retries: list = []

    def call(self, system: list, tools: list, messages: list, tool_choice: str | None = None) -> dict:
        self.retries = []
        clean = [{**m, "content": [b for b in m["content"] if b.get("type") in ANTHROPIC_BLOCKS]} for m in messages]
        body = {"model": self.model, "max_tokens": self.max_tokens, "system": system, "tools": tools, "messages": clean}
        if tool_choice:
            body["tool_choice"] = {"type": tool_choice}
        if self.effort and thinking_allowed(clean):
            body["thinking"] = {"type": "adaptive", "display": "summarized"}
            body["output_config"] = {"effort": self.effort}
        data = json.dumps(body, ensure_ascii=False).encode()
        headers = {"content-type": "application/json", "x-api-key": self.key, "anthropic-version": "2023-06-01"}

        def once():
            with _post(f"{self.base}/messages", data, headers) as response:
                return json.loads(response.read())

        return _with_retries(once, self.retries)


# ---- OpenAI Responses API via the Codex subscription -------------------------


def _image_url(block: dict) -> str | None:
    src = block.get("source") or {}
    if src.get("type") == "base64" and src.get("data"):
        return f"data:{src.get('media_type', 'image/png')};base64,{src['data']}"
    if src.get("type") == "url" and src.get("url"):
        return src["url"]
    return None


def _input_image(block: dict) -> dict:
    return {"type": "input_image", "image_url": _image_url(block), "detail": "auto"}


def to_responses_input(messages: list) -> list:
    items: list = []
    for m in messages:
        role = m["role"]
        for b in m["content"]:
            t = b.get("type")
            if role == "user" and t == "text":
                items.append({"type": "message", "role": "user", "content": [{"type": "input_text", "text": b["text"]}]})
            elif role == "user" and t == "image" and _image_url(b):
                items.append({"type": "message", "role": "user", "content": [_input_image(b)]})
            elif role == "user" and t == "tool_result":
                out = b.get("content")
                if isinstance(out, list) and any(x.get("type") == "image" for x in out if isinstance(x, dict)):
                    parts = [{"type": "input_text", "text": x.get("text", "")} if x.get("type") == "text" else _input_image(x)
                             for x in out if isinstance(x, dict) and (x.get("type") == "text" or _image_url(x))]
                    items.append({"type": "function_call_output", "call_id": b["tool_use_id"], "output": parts})
                else:
                    items.append({"type": "function_call_output", "call_id": b["tool_use_id"],
                                  "output": out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)})
            elif role == "assistant" and t == "text" and b.get("text"):
                items.append({"type": "message", "role": "assistant",
                              "content": [{"type": "output_text", "text": b["text"]}]})
            elif role == "assistant" and t == "tool_use":
                items.append({"type": "function_call", "call_id": b["id"], "name": b["name"],
                              "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False)})
            elif role == "assistant" and t == "openai_reasoning" and b.get("encrypted_content"):
                items.append({"type": "reasoning", "encrypted_content": b["encrypted_content"],
                              "summary": b.get("summary") or []})
    return items


def from_responses_output(output: list) -> list:
    content: list = []
    for item in output:
        t = item.get("type")
        if t == "message":
            text = "".join(c.get("text", "") for c in item.get("content") or [] if c.get("type") == "output_text")
            if text:
                content.append({"type": "text", "text": text})
        elif t == "function_call":
            try:
                args = json.loads(item.get("arguments") or "{}")
            except ValueError:
                args = {"_raw": item.get("arguments")}
            content.append({"type": "tool_use", "id": item["call_id"], "name": item["name"], "input": args})
        elif t == "reasoning" and item.get("encrypted_content"):
            content.append({"type": "openai_reasoning", "encrypted_content": item["encrypted_content"],
                            "summary": item.get("summary") or []})
    return content


class Codex:
    def __init__(self, cfg: dict):
        from .codex_auth import make_credential

        self.model = cfg["model"]
        self.effort = cfg.get("effort")
        self.cred = make_credential(cfg, self.model)
        self.session = str(uuid.uuid4())
        # Seconds a streamed response may stay silent (connect or between chunks) before it counts as
        # stuck and is retried. Normal calls stream continuously; a silent stream once hung a stop.
        self.idle_timeout = float((cfg.get("codex") or {}).get("idle_timeout") or 120)
        self.retries: list = []

    def build(self, system: list, tools: list, messages: list, tool_choice: str | None = None) -> dict:
        instructions = "\n\n".join(b["text"] for b in system)
        rtools = [{"type": "function", "name": t["name"], "description": t.get("description", ""),
                   "parameters": t["input_schema"]} for t in tools]
        key = hashlib.sha256(json.dumps([instructions, rtools], ensure_ascii=False).encode()).hexdigest()[:32]
        body = {
            "model": self.model,
            "instructions": instructions,
            "input": to_responses_input(messages),
            "tools": rtools,
            "tool_choice": tool_choice or "auto",
            "parallel_tool_calls": True,
            "store": False,
            "stream": True,
            "include": ["reasoning.encrypted_content"],
            "prompt_cache_key": f"station-{key}",
        }
        if self.effort:
            body["reasoning"] = {"effort": self.effort, "summary": "auto"}
        return body

    def _stream(self, body: dict, force_auth: bool) -> dict:
        from .codex_auth import CodexAuthError

        try:
            auth = self.cred.get(force=force_auth)
        except CodexAuthError as e:
            raise LLMError(f"codex auth: {e}", "auth") from None
        headers = {
            **auth["headers"],
            "Authorization": f"Bearer {auth['token']}",
            "content-type": "application/json",
            "accept": "text/event-stream",
            "session_id": self.session,
        }
        data = json.dumps(body, ensure_ascii=False).encode()
        output: list = []
        final: dict | None = None
        with _post(f"{auth['base_url'].rstrip('/')}/responses", data, headers, timeout=self.idle_timeout) as response:
            for raw in response:
                line = raw.decode(errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                event = json.loads(payload)
                kind = event.get("type")
                if kind == "response.output_item.done":
                    output.append(event["item"])
                elif kind == "response.completed":
                    final = event["response"]
                elif kind in ("response.failed", "error", "response.incomplete"):
                    text = json.dumps(event, ensure_ascii=False)[:500]
                    raise LLMError(f"{kind}: {text}", classify_text(text))
        if final is None:
            raise LLMError("stream ended without response.completed", "transient")
        return {"output": output or final.get("output") or [], "usage": final.get("usage") or {}}

    def call(self, system: list, tools: list, messages: list, tool_choice: str | None = None) -> dict:
        body = self.build(system, tools, messages, tool_choice)
        self.retries = []
        try:
            result = _with_retries(lambda: self._stream(body, force_auth=False), self.retries)
        except LLMError as e:
            if e.status != 401:
                raise
            result = _with_retries(lambda: self._stream(body, force_auth=True), self.retries)
        u = result["usage"]
        cached = (u.get("input_tokens_details") or {}).get("cached_tokens") or 0
        usage = {
            "input_tokens": max((u.get("input_tokens") or 0) - cached, 0),
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": cached,
            "output_tokens": u.get("output_tokens") or 0,
            "reasoning_tokens": (u.get("output_tokens_details") or {}).get("reasoning_tokens"),
        }
        content = from_responses_output(result["output"])
        uses = any(b["type"] == "tool_use" for b in content)
        return {"content": content, "usage": usage, "stop_reason": "tool_use" if uses else "end_turn"}
