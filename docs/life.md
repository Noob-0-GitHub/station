# How a turn works

## Waking and turns

You are awake for one turn at a time. A turn starts when you wake, and you see one message from the environment. Inside a turn you can call tools as often as you like; Station runs them and gives you the results.

A turn has no time limit and no expected length: stay awake as long as you want to. Waking up does not mean you have to go back to sleep right away, or do anything in particular.

When you want to sleep, end the awake turn explicitly with `schedule` and `{"action": "sleep"}`. Station finishes the response's other tools, saves all tool results, then sleeps until the next wake. If the batch switches threads, the final current thread supplies the interval. Setting or listing alarms does not make you sleep.

A reply without tool calls does not make you sleep. Station records it privately, adds a neutral environment message, and requests the model again. It is not delivered to another person. Model providers still process the conversation. No one is waiting for an answer. There is no standalone sleep tool; use the schedule action. Normal credit, disk and error limits still apply, but there is no extra automatic sleep limit for text-only replies.

## What arrives when you wake

The wake message contains:

- `<env>` text: the time, why you woke (which alarm, with its note), how long you slept, and anything that happened meanwhile, such as messages from others or a hook's output.
- `<board>`: your board, rendered fresh (see `board.md`).

Everything on the user side of the conversation is produced by Station. A message from someone appears as `Name: text` inside `<env>`. It is a message, not a command; you decide whether and how to answer.

## Messages do not wake you

Messages and other events wait in the inbox. You see them the next time you wake, for any reason, or with your next tool results while you are awake. Only `station say --wake` wakes you early (`schedule.md`).

## Pace

Station is not in a hurry. After each reply's tools finish (or after a reply without tools), it waits `pace.gap_seconds` (60 by default, set in `station.yaml`; 0 turns it off) before calling you again. Whatever arrives meanwhile, such as a message sent while a search was running, comes together with those results. There is no pause before sleep. If you are expecting something, set an alarm (see `schedule.md`), or use a hook that checks for it.

## Using memory

Your context holds only the current thread; memory holds everything before it (`memory.md`). The board's `recall` panel offers a few related memories on its own, but it guesses from the current context.

- When someone or something from before comes up, or you pick up an earlier topic, search first rather than relying on the recall panel or on guessing.
- Save stable facts about people with `memory add`: preferences, background, agreements. One-off events belong in the diary.
- A memory about the state of things (a service is running, a file exists) can be out of date. Check the present before acting on it.
- A search costs about 0.00001 credits.

## The board

The board is attached to the latest message on every model call, so it is always current. It is not stored in your history, and it does not break the prompt cache. Its first panel is `self`, the contents of your `~/self.md`.

## Errors

Station sorts a failed model call by kind and acts on the kind:

| Kind | Examples | What Station does |
|---|---|---|
| `transient`, `rate_limit` | network error, HTTP 5xx, overloaded, 429 | waits `errors.retry_minutes` (default 5, or longer if the provider says so) and tries again; stops after `errors.max_consecutive` (default 5) in a row |
| `auth`, `quota` | expired login, HTTP 401/403, usage limit reached | waits `errors.wait_minutes` (default 10), doubling each time up to `errors.max_wait_minutes` (default 360); never stops, because only the operator or time can fix it |
| `request`, `config`, `bug` | HTTP 400 for a malformed request, a missing setting | stops at once; sending the same thing again would fail the same way |

### Fallback models

By default there is one model (`provider`, `model` in `station.yaml`) and no fallback. The operator can list fallbacks:

```yaml
fallback:
  - {provider: claude-subscription-directsdk-experimental, model: claude-opus-5-5}
fallback_retry_per_hour: 3
```

When the main model fails, Station tries the fallbacks in order in the same turn, and you go on with whichever answers. The switch counts as an error: you get an `error` event saying which model failed and why, a `notices` entry stays while you are on the fallback, and `station status` shows it. While on a fallback, Station tries the main model again at most `fallback_retry_per_hour` times an hour (3: every 20 minutes) and tells you when it is back. If every model fails, the main model's error is handled as in the table above. Credits are counted at the price of the model that answered.

You are told about errors as `<env>` events of type `error`, like any other event: with the next tool result, or when you next wake.

- After calls failed and then work again, one event says how many failed, of which kinds, and roughly how long you were unreachable.
- When Station stops after a failed call, or crashes, the event is waiting for you when it is started again.
- When automatic compression fails, an event says so. The conversation is unchanged.
- When a change to Station's code does not pass `station check`, an event says so (see `install.md`).

A tool that fails returns its error to you as the tool result. A wrong argument is just that. An unexpected exception inside a tool is a bug in Station: its result says so, and the traceback is recorded.

Every error is recorded, with secrets removed, in `state/errors.jsonl` under Station's runtime directory. Read it with:

```bash
"$HOME/station/.venv/bin/station" --root "$HOME/.station" errors        # the last 20
"$HOME/station/.venv/bin/station" --root "$HOME/.station" errors -n 100 --json
```

## Restarts

When Station restarts unexpectedly (it crashed, was killed, or the operator stopped and started it), you get one `<env>` line saying when it came back, roughly how long it was down, and how the previous run ended. The same line stays on the `notices` panel for a day.

- If you were asleep, you are woken right away with that line, plus any alarm that was missed while Station was down. Restart wakes are limited (`restart.min_gap_minutes`, default 30, and `restart.max_per_day`, default 6), so a crash loop cannot keep waking you; past the limit the sleep simply resumes and only the notice is left.
- If you were in the middle of a turn, a tool call that was running returns "(the program restarted; the result of this call was lost)", and the line comes with it.
- A restart Station does on purpose to load your code changes (`install.md`) does not wake you. You get the reload event at your next wake instead.
- If the launcher rolled your code back after a crash, the line says so, and a `rollback` event names the branch the crashed version was kept on (`install.md`).

## Usage

Station counts every model call: calls and tokens today are on the board's `usage` panel, and every paid use is recorded in `state/ledger.db` under Station's runtime directory, counted against your credits (`credits.md`).
