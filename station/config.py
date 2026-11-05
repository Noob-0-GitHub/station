"""Configuration: defaults merged with `<root>/station.yaml`."""

from __future__ import annotations

import copy
from pathlib import Path

import yaml

DEFAULTS: dict = {
    "home": None,  # the AI's home; null = <root>/home (dev). Production: the station user's home
    "docs_dir": None,  # user docs listed on the registration screen; null = none
    # "codex" (ChatGPT subscription), "claude-subscription-directsdk-experimental" (Claude subscription
    # through the claude CLI; Hermes's provider name) or "anthropic" (API key)
    "provider": "codex",
    "model": "gpt-6.1-sol",
    "effort": "medium",  # reasoning effort; null sends no reasoning params
    # Models to use when the one above fails (any error), in order, e.g.
    # [{provider: claude-subscription-directsdk-experimental, model: claude-opus-5-5}]. Each entry may
    # also set effort. Default: none.
    "fallback": [],
    "fallback_retry_per_hour": 3,  # while on a fallback, how often the main model is tried again
    "max_tokens": 16000,
    "cache_ttl": "1h",  # Anthropic only: "5m" or "1h"
    "codex": {
        # Production: path of the access-token file written by root (scripts/codex-token-sync.py).
        # When set, Station only reads it and never touches Hermes. null = borrow from Hermes (dev).
        "token_file": None,
        "hermes_root": "/usr/local/lib/hermes-agent",
        "hermes_home": "/root/.hermes",
        "python": None,  # Hermes runtime python; null = ask `hermes --print-runtime-command`
        "idle_timeout": 120,  # seconds a streamed response may stay silent before the attempt is retried
    },
    "claude_code": {
        # provider claude-subscription-directsdk-experimental: the official `claude` CLI does the login.
        "command": None,  # null = `claude` on PATH or in ~/.local/bin
        "config_dir": None,  # CLAUDE_CONFIG_DIR for the CLI; null = its default (~/.claude)
        "workdir": None,  # the CLI's working directory; null = a private one under the temp directory
        "timeout": 600,  # seconds without output from the CLI before the call fails
        "telemetry": True,  # false = the CLI sends no telemetry (its model picker may then list fewer models)
    },
    "api": {
        "base_url": None,  # e.g. https://api.anthropic.com/v1
        "api_key_env": "STATION_API_KEY",
        "hermes_provider": None,  # dev only: read endpoint and key from a Hermes custom provider
        "hermes_config": "/root/.hermes/config.yaml",
    },
    "runner": {
        "user": None,  # run AI commands as this user (requires root); null = current user
        "timeout": 120,
        "max_timeout": 600,
        "max_output": 12000,
    },
    "schedule": {
        "default_minutes": 30,  # a thread's wake interval until the AI changes it (loop mode)
        "morning_at": None,  # optional local HH:MM daily wake; null disables it
        "min_minutes": 1,
        "max_minutes": 10080,  # one week
        "max_entries": 20,  # cron + at entries
        "hook_timeout": 60,  # seconds; a hook that times out wakes the AI
        "poll_seconds": 1.0,
    },
    "board": {
        "panel_max_chars": 1200,  # per panel, unless its front matter sets max_chars
        "max_chars": 6000,  # whole board; later panels are cut first
    },
    "compress": {
        "enabled": True,  # automatic compression; the compress tool works either way
        "threshold_tokens": 250000,  # compress a thread once one call's input reaches this
        "keep_cycles": 6,  # recent assistant turns (with their tool results) kept verbatim; at least 1
        "warn_ratio": 0.8,  # one context line once the thread reaches this share of threshold_tokens
        # First try replacing old tool outputs longer than this with a one-line stub (no model call) ...
        "prune_min_chars": 2000,
        # ... and keep that alone if the estimated context drops to this share of threshold_tokens.
        "prune_target": 0.6,
        "max_summary_chars": 12000,  # a longer handoff is rejected
        "retry_growth": 0.1,  # after a failed automatic compression, wait until the context grows by this much
    },
    "errors": {
        # Failed model calls, by kind (station/errors.py). transient/rate_limit: retry every
        # retry_minutes, stop after max_consecutive in a row. auth/quota: wait wait_minutes, doubling
        # up to max_wait_minutes, never stop. request/config/bug: stop at once.
        "retry_minutes": 5,
        "max_consecutive": 5,
        "wait_minutes": 10,
        "max_wait_minutes": 360,
    },
    "restart": {
        # After an unexpected restart (crash, kill, operator stop/start; not a hot reload), a
        # sleeping AI is woken with one line saying how long Station was down and why.
        "wake": True,
        "min_gap_minutes": 30,  # at most one restart wake per this many minutes ...
        "max_per_day": 6,  # ... and this many per 24 hours; beyond that only a notice is left
        "heartbeat_seconds": 60,  # state/heartbeat is touched this often while running
    },
    "reload": {
        # While asleep, watch the station package and station.yaml; on a change run `station check`
        # in a new process and, if it passes, restart into the new code. Templates, docs and
        # credential files are read on use and need no restart.
        "enabled": True,
        "poll_seconds": 5,
        "check_timeout": 120,
    },
    "secrets": {
        # One dotenv per service: tavily.env (TAVILY_API_KEY), openai.env (OPENAI_API_KEY, API platform).
        # The environment wins. Files must be 600 and not symlinks. Re-read on every lookup.
        # Production: ~/.station/auth. The Codex subscription login is separate (codex_auth).
        "dir": "/root/.local/share/secrets/station",
    },
    "browser": {
        # agent-browser CLI and the Chromium it drives; seeded as ~/bin/browser at registration.
        "binary": "/root/.hermes/tools/agent-browser-0.26.0-linux-x64/bin/agent-browser-linux-x64",
        "chrome": "/root/.hermes/tools/chromium-1208/chrome-linux64/chrome",
    },
    "memory": {
        # Search over the diary, conversations and saved notes (docs/memory.md). Vectors from OpenAI's
        # embeddings API through `station api` (metered), stored with sqlite-vec in state/memory.db.
        "enabled": True,
        "model": "text-embedding-3-small",
        "dimensions": 512,  # the model gives 1536; 512 keeps most of the quality at a third of the size
        "sources": ["diary", "chat", "note"],
        "sync_on_sleep": True,  # index new material each time the AI falls asleep ...
        "sync_max_chunks": 200,  # ... at most this many new chunks per sleep (the rest wait for the next)
        "sync_seconds": 60,
        "recall_on_new": 3,  # memories added to a new thread's first message (0 = none); one search per `focus new`
        "recall_timeout": 10,  # seconds the search by meaning may take; then word matches only
        "board_recall": {
            "enabled": True,
            "limit": 3,
            "refresh_seconds": 600,
            "query_max_chars": 800,
            "excerpt_chars": 240,
            "timeout": 2,
        },
    },
    "disk": {
        # Soft quota on the AI's home plus Station's runtime directory, measured with du.
        "enabled": True,
        "allowance_gb": 5,
        "warn_ratio": 0.9,  # the disk panel appears from here
        "check_minutes": 10,  # du at most this often; also how often a pause rechecks
    },
    "credits": {
        # 1 credit = 1 USD of list price times credits.factor (by provider or service, default 1).
        # Subscription routes are priced as if the same tokens went through the API.
        "enabled": True,
        # Choose allowances explicitly in the local runtime YAML before enabling paid calls.
        "allowances": {"daily": None, "weekly": None},
        "reset": {"daily_at": "00:00", "weekly_day": "monday", "weekly_at": "00:00"},
        "warn_ratio": 0.9,  # the credits panel appears on the board from 90% of either allowance
        "limit_ratio": 2.0,  # a notice, then no model or paid API calls at 200% of either, until it resets
        "factor": {},  # by provider (codex, anthropic, ...) or service (tavily, openai, ...); default 1
        # What is actually paid, as a fraction of list price (default 1). Subscriptions cost nothing per call.
        "paid": {"codex": 0.0, "claude-subscription-directsdk-experimental": 0.0},
        # Tavily bills in its own credits (basic search 1, extract 1 per 5 successful urls);
        # pay-as-you-go list price 0.008 USD each (2026-11). Free-plan credits are counted too.
        "tavily_usd": 0.008,
    },
    # USD per million tokens (per_minute: USD per minute of audio), API list prices. Relay billing may differ.
    "prices": {
        # OpenAI API models used through `station api` (2026-11 list prices, from the model pages:
        # audio tokens in / text out). Usage shapes verified live (tests_live/, 2026-11-01); not yet
        # checked against a real bill.
        "gpt-4o-mini-transcribe": {"input": 1.25, "audio_input": 1.25, "output": 5.0},
        "gpt-4o-transcribe": {"input": 2.5, "audio_input": 2.5, "output": 10.0},
        "gpt-transcribe": {"per_minute": 0.0045},
        "whisper-1": {"per_minute": 0.006},
        "text-embedding-3-small": {"input": 0.02},
        "text-embedding-3-large": {"input": 0.13},
        # Standard OpenAI token rates checked 2026-11-03 (sources: dev/model-pricing.md).
        # Above 272k input tokens the whole request costs 2x on input/cache and 1.5x on output.
        # Codex reports no cache writes; do not map OpenAI's 30m/12h writes onto Anthropic's 5m/1h fields.
        "gpt-6-astra": {
            "input": 10.0,
            "output": 50.0,
            "cache_read": 1.0,
            "long_context_tokens": 272000,
            "long_input_x": 2.0,
            "long_output_x": 1.5,
        },
        "gpt-6-luna": {
            "input": 0.10,
            "output": 0.50,
            "cache_read": 0.01,
            "long_context_tokens": 272000,
            "long_input_x": 2.0,
            "long_output_x": 1.5,
        },
        "gpt-6.1-terra": {
            "input": 1.0,
            "output": 5.0,
            "cache_read": 0.05,
            "long_context_tokens": 272000,
            "long_input_x": 2.0,
            "long_output_x": 1.5,
        },
        "gpt-6.1-sol": {
            "input": 2.0,
            "output": 10.0,
            "cache_read": 0.10,
            "long_context_tokens": 272000,
            "long_input_x": 2.0,
            "long_output_x": 1.5,
        },
        # Image generation, Standard rates checked 2026-11-04 (model page): text in 5 (cached 1.25),
        # image in 8 (cached 2), image out 30. Text output is not billed; the response reports none.
        "gpt-image-2.5-flare": {
            "input": 5.0,
            "cache_read": 1.25,
            "image_input": 8.0,
            "image_cache_read": 2.0,
            "output": 30.0,
        },
        "claude-opus-5-5": {
            "input": 4.0,
            "output": 20.0,
            "cache_write_5m": 5.0,
            "cache_write_1h": 8.0,
            "cache_read": 0.4,
        },
    },
}


def merge(base: dict, override: dict | None) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = value
    return out


def load(root: Path) -> dict:
    path = Path(root) / "station.yaml"
    override = yaml.safe_load(path.read_text()) if path.exists() else {}
    return merge(DEFAULTS, override)
