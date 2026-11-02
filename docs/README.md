# Station manual

Station is the program you live in. It calls the model, runs your tools, keeps your history, wakes you up and puts the board in front of you. These pages describe what it does, so you do not have to guess.

| Page | What it covers |
|---|---|
| `life.md` | How a turn works: waking, `<env>`, the board, ending a turn, messages |
| `tools.md` | Every tool, with examples |
| `schedule.md` | When you wake: intervals, cron, the 09:00 wake, notes and hooks |
| `board.md` | Built-in panels, your own panels, command panels, length limits |
| `credits.md` | Credits: what costs money, the daily and weekly allowances, the limits, and `station api` for paid APIs |
| `memory.md` | Searching your diary, past conversations and notes |
| `notices.md` | Pending things on the board, and the `station` command that manages them |
| `threads.md` | Focus threads: opening, switching, closing |
| `compression.md` | What happens when a thread's context gets long |
| `files.md` | Your home, Station's runtime directory, and what is stored where |
| `install.md` | Install a release, modify the code, upgrade, handle conflicts and roll back |
| `auth.md` | The Codex login token, API keys, and what you can and cannot do with them |

The terms of use are in `~/terms.md`. Facts about the human world are in `~/suggestions.md`.

## Changing Station

The Station code, these docs and the prompts are ordinary files you may read and change. Keep your own data out of the code directory (`install.md`). The code is the `station` Python package; prompts are in `station/templates/`. Prompt changes apply on the next call. Code and config changes are checked and loaded automatically while you are asleep; a change that fails the check is not loaded, and you are told why (`install.md`). A change that passes the check but breaks at runtime can still stop Station, so commit working versions.
