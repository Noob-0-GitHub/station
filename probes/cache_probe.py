"""M0 cache probe: verify prompt caching through the real provider path.

Reads the Anthropic relay endpoint and key from Hermes config (never printed),
sends a few small requests, and prints only the `usage` fields.
"""

import json
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

import yaml

MODEL = "claude-opus-5-5"
PROVIDER_NAME = "PackyAPIAnthropics"


def load_endpoint():
    cfg = yaml.safe_load(Path("/root/.hermes/config.yaml").read_text())
    for p in cfg.get("custom_providers") or []:
        if p.get("name") == PROVIDER_NAME:
            return p["base_url"].rstrip("/"), p["api_key"]
    sys.exit(f"provider {PROVIDER_NAME} not found")


BASE, KEY = load_endpoint()
RUN = uuid.uuid4().hex[:8]  # unique per run, so earlier runs cannot pre-warm the cache


def filler(tag, n):
    # Deterministic text, roughly 25 tokens per line.
    return "\n".join(
        f"[{tag} {i:04d}] The quick brown fox jumps over the lazy dog near river bend number {i}."
        for i in range(n)
    )


SYSTEM_TEXT = f"run {RUN}. You are a probe. Always reply with the single word OK.\n" + filler("sys", 120)
HISTORY_TEXT = filler("hist", 160)


def cc(ttl):
    return {"type": "ephemeral", "ttl": "1h"} if ttl == "1h" else {"type": "ephemeral"}


def call(label, messages, ttl=None, system_text=SYSTEM_TEXT, extra_headers=None):
    body = {
        "model": MODEL,
        "max_tokens": 256,
        "system": [{"type": "text", "text": system_text, "cache_control": cc(ttl)}],
        "messages": messages,
    }
    headers = {
        "content-type": "application/json",
        "x-api-key": KEY,
        "anthropic-version": "2023-06-01",
        **(extra_headers or {}),
    }
    req = urllib.request.Request(f"{BASE}/messages", data=json.dumps(body).encode(), headers=headers)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"{label}: HTTP {e.code} {e.read()[:300].decode(errors='replace')}")
        return None
    u = data.get("usage", {})
    print(
        f"{label}: {time.time() - t0:5.1f}s "
        f"in={u.get('input_tokens')} "
        f"cache_write={u.get('cache_creation_input_tokens')} "
        f"cache_read={u.get('cache_read_input_tokens')} "
        f"out={u.get('output_tokens')} "
        f"detail={u.get('cache_creation')}"
    )
    return data


def user_turn(history_ttl, board, extra_blocks=()):
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": HISTORY_TEXT, "cache_control": cc(history_ttl)},
            *extra_blocks,
            {"type": "text", "text": f"<board>clock {time.time()} | {board}</board>"},
        ],
    }


print(f"run={RUN} model={MODEL} endpoint=<{PROVIDER_NAME}>")

# 1-2: 5m TTL, board after breakpoint changes between calls.
call("A write (5m)", [user_turn(None, "v1")])
call("B board changed", [user_turn(None, "v2")])

# 3: incremental history. Previous turn persisted WITHOUT the board, new turn appended.
hist_msg = {"role": "user", "content": [{"type": "text", "text": HISTORY_TEXT}]}
call(
    "C next turn",
    [
        hist_msg,
        {"role": "assistant", "content": [{"type": "text", "text": "OK"}]},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "tool_result: done " + filler("turn", 10), "cache_control": cc(None)},
                {"type": "text", "text": f"<board>clock {time.time()} | v3</board>"},
            ],
        },
    ],
)

# 4-5: 1h TTL on a fresh prefix.
sys_1h = SYSTEM_TEXT.replace(f"run {RUN}", f"run {RUN}-1h")
r = call("D write (1h)", [user_turn("1h", "v1")], ttl="1h", system_text=sys_1h)
if r is None:
    call(
        "D write (1h, beta header)",
        [user_turn("1h", "v1")],
        ttl="1h",
        system_text=sys_1h,
        extra_headers={"anthropic-beta": "extended-cache-ttl-2025-04-11"},
    )
call("E read (1h)", [user_turn("1h", "v2")], ttl="1h", system_text=sys_1h)
