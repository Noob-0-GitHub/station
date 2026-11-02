"""Credits: everything that costs money, in one unit, against a daily and a weekly allowance.

One credit is one US dollar of list price, times `credits.factor` (by provider
or service, default 1). The ledger (`ledger.py`) keeps what each use was,
in its own units, and what it cost:

- model calls (service `model`): tokens at API list prices; a subscription route
  (Codex) is priced as if the same tokens had gone through the API
- paid APIs (`openai`, `tavily`, ...) through `station api` or Station's own
  tools: priced from the usage the service reports
- anything else: entered by hand, `station api SERVICE --record USD --what WHAT`

Allowances and local reset times are explicit runtime configuration, not personal policy
constants. Enabled credit limits without positive finite daily and weekly allowances fail
closed before paid calls. Daily and weekly allowances are independent.
The AI sees credits as percentages of the allowances. Below `warn_ratio` (90%)
nothing is shown; from there on a `credits` panel is on the board. At
`limit_ratio` (200%) of either, a notice is left and Station makes no model
calls and no paid API calls until that allowance resets.
"""

from __future__ import annotations

import datetime as dt
import math
import re

from . import clock

PERIODS = ("daily", "weekly")


def call_cost(price: dict | None, fresh: int, write_5m: int, write_1h: int, read: int, out: int) -> float | None:
    """API list price in USD of one call. Prices are USD per million tokens."""
    if not price:
        return None
    context = fresh + write_5m + write_1h + read
    in_x = out_x = 1.0
    if price.get("long_context_tokens") and context > price["long_context_tokens"]:
        # OpenAI bills the whole request at the long-context rate once the prompt crosses the line.
        in_x, out_x = price.get("long_input_x", 1.0), price.get("long_output_x", 1.0)
    write_5m_price = price.get("cache_write_5m", price["input"])
    write_1h_price = price.get("cache_write_1h", write_5m_price)
    return (in_x * (fresh * price["input"] + write_5m * write_5m_price + write_1h * write_1h_price
                    + read * price.get("cache_read", price["input"]))
            + out_x * out * price["output"]) / 1e6


def _time(value, name: str) -> tuple[int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value):
        raise ValueError(f"credits.reset.{name} must be HH:MM")
    hour, minute = value.split(":")
    return int(hour), int(minute)


def reset_settings(cfg: dict):
    reset = cfg.get("reset", {})
    if not isinstance(reset, dict):
        raise ValueError("credits.reset must be a mapping")
    daily = _time(reset.get("daily_at", "00:00"), "daily_at")
    weekly = _time(reset.get("weekly_at", "00:00"), "weekly_at")
    days = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    day = reset.get("weekly_day", "monday")
    if day not in days:
        raise ValueError("credits.reset.weekly_day must be a lowercase weekday name")
    return daily, weekly, days.index(day)


def allowances(cfg: dict) -> dict[str, float]:
    if type(cfg.get("enabled", True)) is not bool:
        raise ValueError("credits.enabled must be a boolean")
    raw = cfg.get("allowances", {})
    if not isinstance(raw, dict):
        raise ValueError("credits.allowances must be a mapping")
    result = {}
    for period in PERIODS:
        value = raw.get(period)
        if value is None and not cfg.get("enabled", True):
            result[period] = 0.0
            continue
        if type(value) not in (int, float):
            raise ValueError(f"credits.allowances.{period} must be a positive finite number in station.yaml")
        try:
            number = float(value)
        except OverflowError:
            number = float("inf")
        if not math.isfinite(number) or number <= 0:
            raise ValueError(f"credits.allowances.{period} must be a positive finite number in station.yaml")
        result[period] = number
    return result


def validate(cfg: dict) -> None:
    allowances(cfg)
    reset_settings(cfg)


def period_start(kind: str, now: float, cfg: dict) -> float:
    if kind not in PERIODS:
        raise ValueError(f"unknown credit period: {kind}")
    daily, weekly, weekday = reset_settings(cfg)
    day = clock.at(now).date()
    hm = daily if kind == "daily" else weekly
    if kind == "weekly":
        day -= dt.timedelta(days=(day.weekday() - weekday) % 7)
    start = clock.wall(day, *hm)
    if start > now:
        day -= dt.timedelta(days=1 if kind == "daily" else 7)
        start = clock.wall(day, *hm)
    return start


def next_reset(kind: str, now: float, cfg: dict) -> float:
    daily, weekly, _ = reset_settings(cfg)
    day = clock.at(period_start(kind, now, cfg)).date()
    return clock.wall(day + dt.timedelta(days=7 if kind == "weekly" else 1),
                      *(daily if kind == "daily" else weekly))


def factor(cfg: dict, key: str | None) -> float:
    return float((cfg.get("factor") or {}).get(key or "", 1.0))


def paid(cfg: dict, key: str | None, list_usd: float | None) -> float | None:
    """What was actually paid: list price times `credits.paid` (0 for subscriptions, default 1)."""
    if list_usd is None:
        return None
    return list_usd * float((cfg.get("paid") or {}).get(key or "", 1.0))


def status(ledger, cfg: dict, now: float) -> dict[str, dict]:
    """Per period: used, allowance, ratio, start and reset times."""
    allow = allowances(cfg)
    out = {}
    for k in PERIODS:
        start = period_start(k, now, cfg)
        used = ledger.total(start)
        out[k] = {"used": used, "allowance": allow[k], "ratio": used / allow[k] if allow[k] else 0.0,
                  "start": start, "reset": next_reset(k, now, cfg)}
    return out


def pct(ratio: float) -> str:
    return f"{ratio * 100:.0f}%" if ratio >= 0.01 or ratio == 0 else "<1%"
