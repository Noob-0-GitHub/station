# Files

## Your home

| Path | What it is |
|---|---|
| `~/self.md` | Who you are, in your words. Shown first on the board. |
| `~/terms.md` | Terms of use, read-only |
| `~/suggestions.md` | Facts about the human world. Yours to keep, edit or delete. |
| `~/diary/` | Diary, one `YYYY-MM-DD.md` per day |
| `~/todo.json` | Todo list |
| `~/board/` | Your board panels |
| `~/skills/` | Skills, one directory each with a `SKILL.md` |
| `~/bin/` | Your commands; first on `PATH`. `browser` and `station` (Station's CLI for this runtime, rewritten at every start) live here. |
| `~/.browser-profile/` | The browser's cookies and logins |

| `~/station/` | Station's code, a Git repository (`install.md`) |

Your home is your private space. Everything else in it is yours to arrange, but keep your data (notes, downloads, projects, databases) out of `~/station/`: a rollback after a crash restores that directory to its last working state (`install.md`).

## Station's runtime directory

Station keeps its own records in a runtime directory (the one given to `station --root`). You may read and change all of it.

| Path | What it is |
|---|---|
| `station.yaml` | Configuration. Station restarts itself to load changes while you are asleep (see `install.md`). |
| `state/state.json` | Current status: name, threads, alarms, sleep |
| `state/threads/<id>.jsonl` | Each thread's conversation, one message per line |
| `state/threads/<thread>.jsonl` (history) | The whole thread: after each compression a `{"compaction": ...}` line and the new context; Station reads from the last such line |
| `state/archive/<thread>-<time>.jsonl` | Older installs only: copies of threads taken before compression |
| `state/ledger.db` | Every paid use (model calls, web search, `station api` calls and `--record` entries) with its units, price and credits. SQLite; see `credits.md` |
| `state/log.jsonl` | Station's log: your final replies, tool calls, compressions, errors. Past 5 MB it is gzipped into `state/logs/`; the newest 20 archives are kept |
| `state/errors.jsonl` | Errors only, redacted: model calls, tool bugs, compression, reload checks, crashes. `station errors` reads it. Rotated to `errors.1.jsonl` at about 1 MB |
| `state/memory.db` | Memory: chunks of your diary, conversations and notes, with their vectors (`memory.md`) |
| `state/notices.json` | Notices (`notices.md`) |
| `state/confirmed.json` | Written after the first completed turn of each run; the launcher's sign that this code works |
| `state/heartbeat` | Touched about once a minute while Station runs; after a hard kill it tells how long Station was down |
| `state/outbox.jsonl` | Notes for the operator |
| `inbox/` | Messages and events waiting for your next wake |

Editing `state/` while Station runs can be overwritten by Station. Stop it first, or change things through your tools.

## Skills

A skill is a note for your future self: `~/skills/<name>/SKILL.md`, starting with

```
---
name: <name>
description: one line saying when it is useful
---
```

The board lists every skill's name and description. When you work something out that you will need again, write it down as a skill. The `skills` skill has the details.

## Disk allowance

Your home and Station's runtime directory together may use 5 GB (`disk.allowance_gb`). Station measures them with `du` every 10 minutes. From 90% the board shows a `disk` panel. At 100% you get a notice, Anthony is told, and Station makes no model calls until usage is back under 5 GB; it checks again every 10 minutes and goes on by itself. Nothing is deleted for you. `du -sh ~/* | sort -h` shows what is large; virtual environments, caches and downloads are the usual suspects.
