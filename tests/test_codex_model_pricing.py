"""New offline pricing coverage for the current Astra, Luna and Terra model IDs."""

from decimal import Decimal

import pytest

from station import api
from station.config import DEFAULTS, load
from station.credits import call_cost
from station.llm import Codex
from test_loop import make


# Standard USD per million tokens, checked against the official model pages.
RATES = {
    "gpt-6-astra": (10.0, 1.0, 50.0),
    "gpt-6-luna": (0.10, 0.01, 0.50),
    "gpt-6.1-terra": (1.0, 0.05, 5.0),
}
VECTORS = [(100_000, 100_000, 10_000), (172_000, 100_000, 10_000),
           (172_001, 100_000, 10_000), (0, 300_000, 10_000)]


def expected(model, fresh, cached, output):
    input_rate, cache_rate, output_rate = (Decimal(str(x)) for x in RATES[model])
    long = fresh + cached > 272_000
    input_x = Decimal(2 if long else 1)
    output_x = Decimal("1.5") if long else Decimal(1)
    return float((input_x * (fresh * input_rate + cached * cache_rate)
                  + output_x * output * output_rate) / Decimal(1_000_000))


@pytest.mark.parametrize("model", RATES)
def test_new_model_prices_have_verified_rates_and_long_context_rules(model):
    p = DEFAULTS["prices"][model]
    assert (p["input"], p["cache_read"], p["output"]) == RATES[model]
    assert p["long_context_tokens"] == 272_000
    assert p["long_input_x"] == 2.0 and p["long_output_x"] == 1.5
    assert "cache_write_5m" not in p and "cache_write_1h" not in p


@pytest.mark.parametrize("model", RATES)
@pytest.mark.parametrize(("fresh", "cached", "output"), VECTORS)
def test_new_model_pricing_at_and_across_the_threshold(model, fresh, cached, output):
    assert call_cost(DEFAULTS["prices"][model], fresh, 0, 0, cached, output) == pytest.approx(
        expected(model, fresh, cached, output))


@pytest.mark.parametrize("model", RATES)
@pytest.mark.parametrize("total_input", [200_000, 272_001])
def test_codex_normalized_usage_is_priced_and_scaled_in_the_ledger(tmp_path, monkeypatch, model, total_input):
    cached, output = 100_000, 10_000
    client = object.__new__(Codex)
    client.model, client.effort = model, None
    monkeypatch.setattr(client, "_stream", lambda body, force_auth: {
        "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "offline"}]}],
        "usage": {"input_tokens": total_input, "input_tokens_details": {"cached_tokens": cached},
                  "output_tokens": output},
    })
    response = client.call([{"type": "text", "text": "offline"}], [], [])
    usage = response["usage"]
    assert usage["input_tokens"] == total_input - cached
    assert usage["cache_read_input_tokens"] == cached
    assert usage["cache_creation_input_tokens"] == 0
    life = make(tmp_path, [])
    life.state["status"] = "registered"
    life.cfg["credits"]["factor"] = {"codex": 20 / 245, "openai": 1}
    life.last_route = {"provider": "codex", "model": model}
    life.record_usage(usage)
    row = life.store.ledger.rows()[-1]
    usd = expected(model, total_input - cached, cached, output)
    assert row["provider"] == "codex" and row["model"] == model
    assert row["list_usd"] == pytest.approx(usd)
    assert row["credits"] == pytest.approx(usd * 20 / 245)
    assert row["paid_usd"] == 0
    assert row["tokens"] == total_input + output


@pytest.mark.parametrize("model", RATES)
def test_openai_api_usage_uses_the_same_model_base_rates(model):
    fresh, cached, output = VECTORS[0]
    usage = {"input_tokens": fresh + cached, "input_tokens_details": {"cached_tokens": cached},
             "output_tokens": output}
    assert api.openai_cost(DEFAULTS["prices"], model, usage) == pytest.approx(expected(model, fresh, cached, output))


def test_new_default_prices_merge_without_overwriting_local_budget_or_routes(tmp_path):
    (tmp_path / "station.yaml").write_text(
        "provider: claude-subscription-directsdk-experimental\nmodel: claude-opus-5-5\n"
        "fallback: [{provider: codex, model: gpt-6.1-sol}]\n"
        "credits:\n  allowances: {daily: 1, weekly: 17.5}\n  factor: {codex: 0.08163265306122448, openai: 1}\n"
        "prices:\n  gpt-6.1-sol: {input: 2, output: 10, cache_read: 0.1}\n")
    cfg = load(tmp_path)
    assert all(model in cfg["prices"] for model in RATES)
    assert cfg["model"] == "claude-opus-5-5"
    assert cfg["fallback"] == [{"provider": "codex", "model": "gpt-6.1-sol"}]
    assert cfg["credits"]["allowances"] == {"daily": 1, "weekly": 17.5}
    assert cfg["credits"]["factor"] == {"codex": 0.08163265306122448, "openai": 1}
    assert cfg["prices"]["gpt-6.1-sol"]["input"] == 2
