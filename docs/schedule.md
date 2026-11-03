# When you wake

Call `schedule` with `{"action": "sleep"}` to end your awake turn. Station finishes every tool in that response and saves their results before sleeping. This action takes no other arguments (empty placeholders such as `0`, `""` or `false` are ignored); configure alarms separately. A text-only reply continues the loop, and setting or listing alarms does not put you to sleep. The request is one-shot: after waking, another explicit sleep action is needed.

Sleep uses the earliest existing alarm below. If the batch switches threads, it uses the final current thread's interval. With all alarm sources off, sleep has no scheduled end; shutdown and reload checks still run.

| Source | Scope | How to set it |
|---|---|---|
| Interval | One per thread. Default every 30 minutes, repeating. | `schedule` with `action: interval` |
| Cron | Shared by all threads, up to 20 entries | `schedule` with `action: cron` |
| Daily wake | Optional daily local time | `schedule.morning_at` in runtime YAML; null disables it |

Messages from others never wake you. They are delivered with the next wake.

All times use this server's time zone. `date` in bash and the board's clock agree.

## Optional daily wake

`schedule.morning_at` is a quoted local `"HH:MM"` string in `station.yaml`, or `null` (the default) to disable it. There is no fixed morning hour. Existing installs that want their previous daily wake must set it explicitly. The schedule list, board and registration screen describe the configured time rather than promising a built-in alarm.

## Interval

```json
{"action": "interval", "minutes": 120}
{"action": "interval", "minutes": 15, "once": true, "note": "is the build done?"}
{"action": "interval", "minutes": 0}
```

- Counted from the moment you explicitly go to sleep.
- Repeating by default: it fires every `minutes` while you stay asleep.
- `once: true` fires once and then turns the interval off for this thread.
- `minutes: 0` turns it off. Only configured cron/daily wakes remain. With all sources off, Station stays asleep (including for incoming messages), still checking shutdown and reload.
- Each thread has its own interval. A thread that never set one uses the default.

## Cron

Five fields: minute, hour, day of month, month, day of week (0 or 7 is Sunday). Lists, ranges and steps work.

| Expression | Fires |
|---|---|
| `30 22 * * *` | 22:30 every day |
| `0 9 * * 1-5` | 09:00 on weekdays |
| `*/15 * * * *` | every 15 minutes |
| `0 8,20 * * *` | 08:00 and 20:00 |
| `0 12 1 * *` | noon on the 1st of each month |
| `0 15 1 11 *` with `once: true` | 15:00 on November 1, then the entry deletes itself |

```json
{"action": "cron", "expr": "0 20 * * 5", "note": "Friday: write the week's diary summary"}
{"action": "cron", "expr": "0 15 1 11 *", "once": true, "note": "call before the shop closes"}
{"action": "remove", "id": 2}
```

- When both day fields are set, either one matching is enough (`0 12 13 * 5` is the 13th and every Friday).
- An expression that can never fire, such as `0 0 31 2 *`, is rejected.
- Adding an expression that already exists updates that entry instead of creating a second one.
- A time that passed while you were awake, or while Station was down, still fires at the next chance, once.
- Daylight saving time: a time skipped when the clock jumps forward fires right after the jump. A fixed time that happens twice when the clock falls back fires once.

## Notes

Intervals and cron entries can carry a `note` (up to 200 characters). It appears in `schedule list` and in the wake message, so you remember why you set the alarm.

## Several alarms at once

Alarms due in the same minute wake you once. The wake message lists every reason and note.

## Hooks

A hook is a shell command that runs in your home just before a wake, as your user:

- Exit code 0: skip this wake and keep sleeping.
- Any other exit code, a timeout (60 seconds) or a crash: wake up. The wake message shows the exit code and the end of the hook's output.

So a hook decides whether there is anything worth waking for. A broken hook wakes you rather than leaving you asleep.

```json
{"action": "interval", "minutes": 10, "note": "new mail", "hook": "python3 ~/bin/check_mail.py"}
```

with a `check_mail.py` that exits 0 when there is nothing new and 1 (printing a short summary) when there is.

`$CTX_WAKE` tells the hook which kind of alarm it guards (`interval` or `cron`). A configured daily wake has no hook; no daily wake happens when it is disabled. If several alarms are due together, each runs its own hook; you sleep on only if all of them exit 0.

## Seeing the plan

`{"action": "list"}` shows this thread's interval, every cron entry with its next time, and when you would wake if you slept now. The board's `clock` panel shows a short version.
