# Model pricing coverage

Checked on 2026-11-03 against OpenAI's current pricing and individual model pages. This is a developer accounting note, not a claim about subscription capacity or actual per-request subscription cash charges.

## Canonical model IDs and Standard rates

All figures are USD per million tokens, in input / cached-input / output order:

- `gpt-6-astra`: 10 / 1 / 50.[1][2]
- `gpt-6-luna`: 0.10 / 0.01 / 0.50.[1][3]
- `gpt-6.1-terra`: 1 / 0.05 / 5.[1][4]

These are the exact currently documented IDs, not guessed `gpt-6.1-astra`, `gpt-6.1-luna` or `gpt-6-terra` aliases. No unlisted snapshot or alias is added.

Each model's page says that prompts with **more than 272,000 input tokens** use 2x input/cache rates and 1.5x output rates for the **whole request**.[2][3][4] The threshold counts fresh plus cached input; exactly 272,000 tokens remains Standard short-context pricing. The existing `credits.call_cost` implementation already supports this rule, so this change adds price rows without changing the pricing engine.

Only Standard-equivalent token prices are covered here. OpenAI lists separate Batch/Flex, Fast and other processing prices; this change does not add speed-tier detection, regional premiums or built-in tool fees.[1]

## Codex accounting path

`Codex.call` subtracts cached tokens from total input to produce fresh input, reports cache reads separately, and currently reports zero cache-creation tokens. `Life.record_usage` looks up the actual responding route's model, computes list USD, and multiplies by the provider's configured factor. The same price rows are also available to `station api`'s OpenAI token-usage accounting.

The production Codex factor was already configured to `20/245` under Anthony's USD-normalized budget convention. It remains a subscription-budget assumption, not an OpenAI capacity guarantee. This change does not change provider factors, daily/weekly budgets, selected models, fallbacks, authentication or existing ledger rows.

OpenAI's pricing page lists cache writes with 30-minute and 12-hour retention.[1] Do not encode those as Anthropic's existing `cache_write_5m` / `cache_write_1h` fields. No OpenAI cache-write price is claimed to have been accounted for by the current Codex usage path. If that usage shape changes, add retention-aware accounting and tests rather than charging a guessed retention rate.

## Configuration and production synchronization

The new rows live in `station/config.py` defaults and are merged with local `station.yaml`. Existing local overrides win. Production can inherit these rows through the ordinary code hot reload; no local YAML edit is required if those model names are not overridden.

Source changes and production deployment are separate operations. Synchronize only the reviewed commit after the production approval gate, observe a successful `reload_check`, verify effective prices in a fresh interpreter, and verify that sleep, routing, budget/factors and old ledger entries remain unchanged. Do not wake the AI or spend a live-model probe just to test accounting.

Unknown/unconfigured models remain a separate open issue: the existing model-loop path can record `list_usd=null` with zero credits. Adding these three exact rows does not make pricing complete for every OpenAI model or fix that fail-open behavior.

## Verification

`tests/test_codex_model_pricing.py`: **25 new offline cases passed**, with sockets blocked and a separate network namespace. Coverage includes:

- Exact rates and long-context settings for all three models.
- Short context, exactly-at-threshold, above-threshold, and cached-input-only threshold crossing.
- Codex usage normalization through actual ledger recording, including the configured Codex factor, zero subscription per-call paid USD and token totals.
- OpenAI API token accounting with the same Standard model base rates.
- Local configuration merging without changing the selected model, fallback or budget/factors.

Previously passing tests were not rerun. No real model, credential refresh, provider charge or production mutation was used by these tests.

## Sources

[1] https://developers.openai.com/api/docs/pricing — OpenAI API pricing
[2] https://developers.openai.com/api/docs/models/gpt-6-astra.md
[3] https://developers.openai.com/api/docs/models/gpt-6-luna.md
[4] https://developers.openai.com/api/docs/models/gpt-6.1-terra.md
