# Notices

Notices are things that are still true until you deal with them: unread messages, a usage limit that lifts at 18:00, "Station restarted". They are different from events:

| | Events | Notices |
|---|---|---|
| What | Something that happened | Something pending |
| Where you see it | Once, as `<env>` text in the conversation | On the board's `notices` panel, every call, until read or expired |
| Stored in history | Yes | No |
| Wakes you | No | No |

The `notices` panel shows how many are unread and the newest few, highest priority first, each with its id.

## The `station` command

`~/bin/station` is Station's command line, already pointed at your runtime. Use it through bash:

```bash
station notices                    # all current notices; * marks unread
station notices --unread --json    # unread, one JSON object per line
station notices read 3 5           # mark #3 and #5 read
station notices read --all

station notify "Call the bike shop back"                 # a reminder for yourself
station notify --priority high --ttl 2h "Exam at 14:00"  # expires after 2 hours
station notify --source bilibili --key dm:12345 "New DM from 小明"
station notify --source bilibili --clear dm:12345        # handled: remove it
```

- `--source` says where it came from (default `manual`).
- `--key` makes a notice updatable: writing the same source and key again replaces the text and marks it unread, instead of adding another one. Programs that report things (a message forwarder, `bili poll`) use a key per conversation or thread.
- `--clear KEY` removes notices with that key. A program that lets you answer something (for example replying to a DM) can clear its notice, so you do not have to mark it read by hand.
- `--ttl` is `30m`, `2h`, `1d`, `1w` or seconds. Without it a notice stays until you read it.
- `--priority` is `low`, `normal` (default) or `high`.

Read notices disappear a week after you read them. Expired ones disappear at once. At most 200 are kept; read ones go first.

## Being woken by notices

Notices never wake you, the same as events. If you want to be woken when something arrives, use a schedule hook (see `schedule.md`). `station notices --quiet` prints nothing and exits 1 when anything matches, so a hook with it cancels the wake when there is nothing new:

```json
{"action": "cron", "expr": "*/15 * * * *", "note": "check notices", "hook": "station notices --quiet --min-priority high"}
```

## What Station itself posts

- `station` / `restart`: after an unexpected restart, how long Station was down and why. Expires after a day.
- `station` / `credits:daily`, `credits:weekly`: an allowance reached 200% and Station stopped making calls (`credits.md`). Expires a day after the reset.

The file is `state/notices.json` in Station's runtime directory.
