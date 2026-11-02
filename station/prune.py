"""Mechanical clean-up of old tool results, tried before a summary (no model call).

Old tool outputs are replaced by a one-line stub that keeps what was run and
how it ended. Tool calls themselves are never changed, so every call still has
its result. Exact duplicates keep only the newest copy.
"""

from __future__ import annotations

import hashlib
import json

from .vision import IMAGE_CHARS, has_image, is_image, text_of

STUB = " (old output cleared)"
DUP = "(same as a later output; cleared)"
MIN_DUP_CHARS = 200
REASONING_BLOCKS = {"openai_reasoning", "thinking", "redacted_thinking"}


def _calls(messages: list[dict]) -> dict[str, tuple[str, dict]]:
    out = {}
    for m in messages:
        if m["role"] == "assistant":
            for b in m["content"]:
                if b.get("type") == "tool_use":
                    out[b["id"]] = (b["name"], b.get("input") or {})
    return out


def _short(text, n: int = 80) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


def stub(name: str, args: dict, content: str, error: bool) -> str:
    """One line saying what the call was and how it ended."""
    failed = "error, " if error else ""
    if name == "bash":
        status = content.split("\n", 1)[0]  # "[exit 0]" or "[timed out after … s]"
        lines = content.count("\n")
        return f"[bash] `{_short(args.get('command', ''))}` {status}, {lines} lines{STUB}"
    if name == "web":
        what = args.get("query") or ", ".join(map(str, args.get("urls") or []))
        return f"[web {args.get('action', '?')}] {_short(what)}, {failed}{len(content)} chars{STUB}"
    target = args.get("name") or args.get("date") or args.get("action") or ""
    target = f" {_short(target)}" if target else ""
    return f"[{name}]{target}, {failed}{len(content)} chars{STUB}"


def prune(messages: list[dict], end: int, min_chars: int) -> tuple[list[dict], int, int]:
    """Clean tool results in messages[:end]. Returns (copy, results changed, characters removed)."""
    calls = _calls(messages)
    out = [{**m, "content": [dict(b) for b in m["content"]]} for m in messages]
    seen: set[str] = set()
    count = removed = 0
    for i in range(len(out) - 1, -1, -1):  # newest first, so the newest copy of a duplicate stays
        if out[i]["role"] != "user":
            continue
        for b in out[i]["content"]:
            text = b.get("content")
            if b.get("type") == "tool_result" and has_image(text):
                if i < end:  # an old image: keep its one-line description only
                    name, args = calls.get(b["tool_use_id"], ("?", {}))
                    new = f"[{name}] {_short(text_of(text))} (old image cleared)"
                    removed += IMAGE_CHARS
                    count += 1
                    b["content"] = new
                continue
            if b.get("type") != "tool_result" or not isinstance(text, str) or len(text) < MIN_DUP_CHARS:
                continue
            digest = hashlib.sha1(text.encode()).hexdigest()
            duplicate = digest in seen
            seen.add(digest)
            if i >= end or text.endswith(STUB) or text == DUP:
                continue
            if duplicate:
                new = DUP
            elif len(text) > min_chars:
                name, args = calls.get(b["tool_use_id"], ("?", {}))
                new = stub(name, args, text, bool(b.get("is_error")))
            else:
                continue
            removed += len(text) - len(new)
            count += 1
            b["content"] = new
    return out, count, removed


def text_size(obj) -> int:
    """Characters of a request part, not counting opaque reasoning (its size says little about tokens)."""
    if isinstance(obj, list):
        return sum(text_size(x) for x in obj)
    if isinstance(obj, dict):
        if obj.get("type") in REASONING_BLOCKS:
            return 0
        if is_image(obj):
            return IMAGE_CHARS
        if "content" in obj and isinstance(obj["content"], list):
            return text_size(obj["content"]) + 20
    return len(json.dumps(obj, ensure_ascii=False))
