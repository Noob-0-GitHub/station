# Credits

Everything that costs money is counted in credits: your model calls, web search, and paid APIs. Credits are shown as a percentage of two allowances:

Both allowances are independent values in local `station.yaml`; there is no built-in personal budget. Set positive finite numbers before using enabled credits. Missing or invalid allowances stop `station check`, model calls (including onboarding) and paid API calls rather than silently disabling the limit.

```yaml
credits:
  enabled: true
  allowances:
    daily: null   # required: choose a positive number locally
    weekly: null  # required: choose independently; no fixed daily/weekly ratio
  reset:
    daily_at: "00:00"
    weekly_day: monday
    weekly_at: "00:00"
```

Reset times are local HH:MM; weekday names are lowercase. Midnight/Monday are calendar defaults, not fixed requirements. `enabled: false` explicitly disables enforcement; it is never inferred from missing allowances. Existing installs must add allowances before upgrading to this configuration-based policy.

- Below **90%** of both, nothing is shown.
- From **90%** of either, a `credits` panel is on the board with both percentages and the reset times.
- At **200%** of either, Station makes no model calls and no paid API calls until that allowance resets. A notice tells you when it happened, and the first message after the reset says so too.

```bash
station credits            # both allowances, and where the credits went, by service
station credits --log 20   # the newest 20 entries
station credits --json
```

## Paid APIs: `station api`

Station holds the API keys and meters every call. When you need an API that no tool covers yet, call it through `station api` instead of using a key yourself:

```bash
station api openai POST /v1/embeddings --json '{"model": "text-embedding-3-small", "input": "hello"}' --what "trying embeddings"
station api openai POST /v1/audio/transcriptions -F model=gpt-4o-mini-transcribe -F file=@clip.mp3
station api tavily POST /search --json @query.json -o result.json
```

- Services: `openai` (`https://api.openai.com`), `tavily` (`https://api.tavily.com`). The path is the API's own path.
- `--json` takes JSON, `@FILE` or `-` (stdin); `-F NAME=VALUE` sends a multipart form, `NAME=@FILE` uploads a file.
- The response body goes to stdout (or `-o FILE`); the cost goes to stderr.
- The cost comes from the usage the service reports in its response, at the list prices in `station.yaml`. A call whose cost cannot be read (an endpoint or model without a price) is still recorded, as unpriced; `station credits` shows how many.
- Streaming is not supported. Failed calls are not charged.
- Exit codes: 0 done, 1 the service or network failed, 2 a bad request on your side, 4 the credit limit is reached.

Tools you install, such as `bili transcribe`, use `station api` themselves when Station is there.

Calling a paid API another way (your own script with a key, `curl`) works, but Station cannot count it; record it with `--record` afterwards.

## Recording a cost by hand

For something Station could not meter (another paid service, or a call whose cost came back unpriced and you know it), record it yourself, so your credits stay true:

```bash
station api replicate --record 0.04 --what "one image"   # service, US dollars at list price, what for
```

## How it is computed

One credit is one US dollar of list price. A model call through a subscription, such as Codex, is priced as if the same tokens had gone through the API. The prices are in `station.yaml` under `prices`; `credits.factor` can weight a provider or service differently (default 1).

Every use is a row in `state/ledger.db` (SQLite): when, which service, model, the raw units (tokens, Tavily credits, seconds of audio), the list price, what was actually paid (nothing per call on a subscription), and the credits. Read it with `station credits --log` or `sqlite3`.
