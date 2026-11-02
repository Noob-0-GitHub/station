# Turns, sleep, and the wake schedule

## How a turn ends

A model call produces either tool calls or plain text. With tool calls, Station runs them and calls the model again with the results. With no tool call, the turn is over: Station stops calling the model and the AI is asleep until the next wake.

There is no `sleep` tool. Ending a turn by not calling a tool is the behaviour these models are trained on, so it needs no instruction beyond one line in `system.md`. A dedicated tool would add a second way to stop and a question the runtime has to answer anyway: what happens when the model replies with text only.

Text that comes with the last reply is private. It is written to `state/log.jsonl` (`kind: text`) and to the thread history, never sent anywhere. `system.md` tells the AI that nobody reads it and nobody is waiting, so it does not write closing summaries for an audience. Talking to someone will go through a channel tool (Telegram, later).

## When it wakes

All times follow the system time zone (`/etc/localtime`, currently `America/Los_Angeles`): the board, the diary, todo and event stamps, cron, and the 09:00 wake. Station has no time zone setting of its own, so the AI's `date` and the board always agree. To change the zone, change the system's (`timedatectl set-timezone …`) and restart Station.

After a turn ends, Station sleeps until the earliest of:

| Source | Scope | Set with |
|---|---|---|
| Interval | One per thread. Default 30 minutes, loop mode. | `schedule action=interval minutes=… [once=true] [note=…] [hook=…]`; `minutes=0` turns it off |
| Cron | Global, up to 20 | `schedule action=cron expr="30 22 * * *" [once=true] [note=…] [hook=…]` |
| Morning | Every day at 09:00 | Fixed. No option, no hook. |

The interval is counted from the moment the turn ended. In loop mode it keeps firing every `minutes` while the AI stays asleep (for example when a hook skips a wake). `once=true` fires once and then turns the thread's interval off, so the next sleep lasts until a cron or 09:00.

Cron uses the usual five fields: minute, hour, day of month, month, day of week (0 or 7 is Sunday). Lists, ranges and steps work (`*/15`, `1-5`, `0,30`). When both day fields are restricted, either one matching is enough, as in Vixie cron. A one-off time is a cron with `once=true`: `0 15 1 11 *` plus `once=true` fires at 15:00 on November 1 and then deletes itself. There is no separate `at` action, because two ways to say "15:00 tomorrow" would make duplicates easy.

Cron entries live in `state.json` and do not depend on which thread is current. Each remembers the last wake that handled it (`last`), so a time that passed while the AI was awake or Station was down still fires at the next opportunity, once.

### Duplicates

- **Same expression twice.** Station keeps one entry per normalized expression (extra spaces removed). Adding the same expression again updates that entry's `once`, `note` and `hook` instead of creating a second one, and the reply says so.
- **Different sources at the same time.** A cron for `0 9 * * *`, the 09:00 morning and an interval can all land on the same minute. They make one wake. The wake-up message lists every reason (and every note); each source is marked handled.
- **Hooks with merged sources.** Each source runs its own hook. A source whose hook exits 0 is dropped from that wake. If every due source is dropped, Station goes back to sleep. The morning has no hook, so a wake at 09:00 always happens.

Two expressions that differ in text but describe the same times (`0 9 * * *` and `0 9 * * 0-6`) are not detected as duplicates. They still produce one wake per minute because of the merging above.

### Notes

Every interval and cron entry can carry a `note` (up to 200 characters). It appears in `schedule list` and in the wake-up message, for example `cron #3 30 22 * * *：看会儿书。`

Messages and other events never wake the AI. They stay in the inbox and are delivered with the next wake-up message, together with the reason for waking.

## Hooks

Any interval or cron entry can carry a hook: a shell command run in the AI's home, as the AI's user, just before the wake. Exit code 0 cancels that source's wake. Any other exit code, a timeout (`schedule.hook_timeout`, 60 s) or a crash wakes the AI, and the wake-up message shows the exit code and the tail of the output. A broken hook therefore costs an extra wake instead of silencing the AI.

The source is marked handled before its hook runs, so a crash during a hook does not fire the same time twice.

`$CTX_WAKE` tells the hook which kind of wake it is guarding. The 09:00 wake never runs a hook.

A hook that asks a small model whether to wake is possible in principle, but the hook runs without any model credentials. Giving it a judging command (the runtime makes the call, the hook only sees the verdict) is an open design question.

## Restarts

`state.sleep` records when the turn ended and the last interval wake already handled; each cron entry and the morning keep their own `last`. If Station stops while the AI is asleep, the next start resumes the same sleep and recomputes from the saved schedule, so anything missed while it was down wakes the AI immediately, as a single wake.

## Config

```yaml
schedule:
  default_minutes: 30   # a thread's interval until the AI changes it
  min_minutes: 1
  max_minutes: 10080
  max_entries: 20       # cron entries
  hook_timeout: 60
  poll_seconds: 1.0
```

The 09:00 wake is not in the config on purpose.
