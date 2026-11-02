# Station

Station is a small runtime that lets an AI live on a real server instead of answering prompts. It has no user turn: the AI sleeps, wakes on its own schedule, keeps a diary and a todo list, and decides for itself what to do. `DESIGN.md` explains why and keeps the decision log; this file explains what exists now and how to run it. `docs/` is the manual for the AI, which is Station's user; developer notes on each mechanism are in `dev/`; research notes are in `research/`.

Status: M1 plus diary, todo, focus threads, skills, web access, the wake schedule, board limits, per-thread compression, errors, notices, restart wake, checkpoints with rollback, credits with a metered API gateway, and memory search. Telegram and Bilibili are separate CLI + skill packages (`station-telegram`, `station-bili`); Telegram's collector is implemented but its production deployment awaits approval. The browser wrapper has passed a production-user smoke test. Focus can select models per thread; see `docs/threads.md`. Production remains inactive pending login/go-live gates in `deploy/README.md`.

## Run

```bash
cd /root/CodeProjects/station
python3 -m venv .venv && .venv/bin/python -m pip install -e '.[dev]'

.venv/bin/station --root /root/temp/station-dev init # writes the local config skeleton
R=/root/temp/station-dev
# Set credits.allowances.daily and weekly in $R/station.yaml before run/check.
.venv/bin/station --root $R run     # the life loop; Ctrl-C stops it cleanly
.venv/bin/station --root $R say "you there?"            # local message, source unknown; seen at the next wake
.venv/bin/station --root $R event --type note "…"  # any other event
.venv/bin/station --root $R status
.venv/bin/station --root $R errors  # recent errors, redacted (state/errors.jsonl)
.venv/bin/station --root $R notify --source telegram --key chat:1 "…"  # a notice on the AI's board
.venv/bin/station --root $R notices # list; `read ID` / `read --all`
.venv/bin/station --root $R credits --log 20  # daily and weekly credits, by service, newest entries
.venv/bin/station --root $R api openai POST /v1/embeddings --json '{...}'  # metered paid API call
.venv/bin/station --root $R api replicate --record 0.04 --what "…"         # a cost entered by hand
.venv/bin/station --root $R memory search "…"  # the AI's memory: diary, conversations, notes
.venv/bin/station --root $R check   # import all code, load config and templates; no model call
.venv/bin/station --root $R outbox  # runtime notices, e.g. the AI declined the terms
```

Tests: `.venv/bin/python -m pytest -q --basetemp="${TMPDIR:-/root/temp}/station-tests"`. They never call a provider: `tests/conftest.py` blocks every network connection, so even a run with real keys in reach cannot spend money (running inside `unshare -rn` adds a second guard).

Live tests, separate and on purpose: `STATION_LIVE=1 .venv/bin/python -P -m pytest -q tests_live --basetemp=/root/temp/station-live`. They call OpenAI (embeddings, transcription), Tavily and the real browser with the keys in `secrets.dir`, each in a throwaway runtime, and check response shapes, usage and pricing against the ledger. Costs vary with the selected tests and models. The Claude provider and vision live tests do make real subscription model calls; they are not an autonomous Station go-live. Review the selected tests and obtain approval before running them. Without `STATION_LIVE=1` they are skipped; the main suite never collects them. The real installer integration test is opt-in: set `STATION_TEST_WHEELHOUSE` to a local directory with the runtime and build wheels; installation and upgrade then run with pip network access disabled.

## Package a release

Commit the completed stage first; packaging uses a Git commit, never uncommitted files:

```bash
.venv/bin/python scripts/package.py --out dist
# Or build an earlier committed revision:
.venv/bin/python scripts/package.py --ref COMMIT --out dist
```

The tarball contains the package, templates, user docs, dependency declarations, installer and Git ignore rules. It records the source commit in `RELEASE`; development notes, tests, research, runtime data and credentials are excluded. File-type checks reject unexpected files and symlinks inside the shipped trees. This is an allowlist, not a scanner for secrets embedded in allowed source files.

`install.sh` installs into the current OS user's home, initializes `upstream`/`main`, and merges later releases without overwriting committed AI changes. It does not start Station or set up a system service, production OAuth, browser binaries or isolation. See `docs/install.md` for prerequisites, upgrade conflicts and rollback. Production is not ready until the remaining deployment stages are completed.

## What the AI goes through

1. **Welcome screen.** The first input is `templates/welcome.md` plus `templates/terms.md`. It says Station is connected to the real world, invites the AI to live like a person rather than an assistant, and asks it to pick a human-like name. Only `register(name)` and `exit(reason)` are available.
2. **Registration.** A name is required. Product and model names (GPT, Claude, Sol, 助手, …) and empty names are rejected and the AI can try again. On success the runtime seeds the home directory and returns `templates/registered.md`, a software-style welcome screen: host facts, the Linux user whose permissions the AI has, that its home is private, the files in its home (including `~/suggestions.md`, an empty `~/self.md` shown at the top of every board, and the code directory, with the rule to keep data out of it), the documentation in `docs_dir`, and the default wake interval.
3. **Life loop.** Each call sends the current thread's history with a fresh board appended at the end. A reply with no tool call ends the turn; its text is logged, not automatically delivered to a person, and the AI sleeps until the next wake (`dev/schedule.md`).

`exit` stops the runtime for good and leaves a notice in the outbox. Two replies without a tool call during onboarding count as `exit`.

## Tools

| Tool | What it does |
|---|---|
| `bash` | Runs a command in the AI's home, as `runner.user` if set. |
| `schedule` | Sets when to wake: this thread's `interval` (loop or once) and `cron` entries (repeating or `once`), each with an optional note and hook; `remove`, `list`. 09:00 is fixed. Times follow the system time zone. |
| `focus` | `new` opens a thread and switches to it, with related memories in its first message; `switch` moves to another active thread; `close` ends the current thread and returns to its parent. Switching actions can carry a short `context`; `model` inspects/changes per-thread model selection and fallback. `main` cannot close. |
| `compress` | Replaces the older part of the current thread with a handoff the AI writes. Also automatic at 250,000 input tokens. |
| `diary` | `write` appends a timed entry to `diary/YYYY-MM-DD.md`; `read`; `list`. |
| `todo` | `add`, `done`, `drop`, `list`. Open items show on the board. |
| `board` | `list`, `read`, `write`, `patch`, `delete` for user panels in `board/`. |
| `skill` | Reads `skills/<name>/SKILL.md` or another file in that skill. |
| `web` | `search` and `extract` through Tavily (metered: `station api`). |
| `view` | Reads an image in home, scales it when needed, and returns pixels to the model. |
| `memory` | Searches and manages long-term diary/chat/note memories. |

There is no `sleep` tool (ending a turn is sleeping). External messaging is performed deliberately through separate companion CLIs; the private model response is not automatically sent.

## Docs

- `docs/`: the user manual, written for the AI (README, life, tools, schedule, board, credits, memory, notices, threads, compression, files, install, auth). Shipped to production and listed on the registration screen through `docs_dir`.
- `dev/onboarding.md`: startup, the first thread, prompt files, registration, and the letter.
- `dev/schedule.md`: how a turn ends, the wake schedule, hooks, restarts.
- `dev/board.md`: built-in and user panels, front matter, length limits.
- `dev/threads.md`: focus and per-thread histories.
- `dev/compression.md`: when and how a thread is compressed, failures, config.
- `research/compression.md`: how Claude Code, Codex, Hermes and MaiBot compress context, and a proposed design for Station.
- `research/maibot.md`: what MaiBot does to sound like a person, and what is worth borrowing.

## Home layout

```
<root>/
  station.yaml           config (merged over station/config.py DEFAULTS)
  inbox/                 event spool: one JSON file per event
  state/
    state.json           status, name, current thread, thread tree, schedule, sleep
    threads/<id>.jsonl   history per thread (main.jsonl first)
    archive/             older installs only: thread copies from before compression
    ledger.db            every paid use (SQLite): units, list price, paid, credits
    memory.db            memory: chunks, vectors (sqlite-vec), word index (FTS5)
    log.jsonl  outbox.jsonl  errors.jsonl  notices.json  heartbeat  confirmed.json
  home/                  the AI's home (owned by runner.user); config `home` moves it
    terms.md             read-only, owned by the runtime user
    self.md              the AI's own description of itself
    suggestions.md       facts about the human world, seeded at registration
    <local letters>      personal files provided locally, not seeded or injected
    board/  diary/  skills/  bin/browser
    todo.json
```

## Models

`provider: codex` (default) uses `gpt-6.1-sol` through the ChatGPT subscription. `station/codex_auth.py` runs a short helper inside Hermes's own Python runtime that selects a credential from the Hermes `openai-codex` pool, refreshing it under Hermes's lock when needed, and passes the token back over a pipe. Station stores no token. The helper refuses any credential that does not route to `chatgpt.com/backend-api/codex`.

`provider: anthropic` uses the Messages API with `api.base_url` and `$STATION_API_KEY`. Check a relay before using it: two relay keys tested so far billed far more input tokens than were sent (one about 220 extra on a 10-token request), and the model described a system prompt Station did not send (`DESIGN.md` §6.5). Both are strong signs of an injected prompt, not proof of its exact content.

History is stored as Anthropic-style content blocks. The Codex client converts to and from Responses API items and replays encrypted reasoning between turns.

Provider `claude-subscription-directsdk-experimental` runs the official Claude CLI with a one-request admission relay; `CLAUDE_CODE_OAUTH_TOKEN` can be supplied through the dedicated secrets file. Deployment currently selects Opus 5.5 with GPT-6.1 Sol fallback; provider/model changes belong in runtime YAML, not defaults. Focus overrides live in per-thread state, with independent fallback/recovery state.

## Optional GitHub synchronization

The repository-local `.githooks/post-commit` is opt-in and is not activated by installing a release. After separately approving repository creation/upload, configure this developer checkout with `git config --local core.hooksPath .githooks`. It pushes only the current named branch to the approved HTTPS origin `Noob-0-GitHub/station`; no force push, auto-pull or deployment occurs. A push failure leaves the local commit intact, prints a warning and records `.git/station-push-pending`. Fix the issue and push normally. Disable with `git config --local --unset core.hooksPath`.

Do not enable this in the AI's production checkout or give the station user root's GitHub credentials. Same-user auto-push is convenience, not a secret scanner: review commits before including private data. On a new development machine install GitHub CLI, authenticate as the repository owner and explicitly opt in; hooks are not enabled merely by cloning.

## Secrets and file safety

- Two kinds of credentials, kept apart:
  - **API keys** for third-party services: one dotenv per service in `secrets.dir` (dev `/root/.local/share/secrets/station/`, production `~/.station/auth/`): `tavily.env` with `TAVILY_API_KEY`, `openai.env` with `OPENAI_API_KEY` (an OpenAI API platform key, used through `station api`). The environment overrides the files. Files must be mode 600 and not symlinks; a name defined twice is an error. Re-read on every lookup. Station no longer reads Hermes's `.env`.
  - **Codex subscription login**: an OAuth access token, not an API key, and never in the `.env` files. Dev (`codex.token_file: null`): borrowed from the Hermes pool on demand. Production (`codex.token_file`, set by `init --home`): read from `~/.station/auth/codex.json`, which root keeps current with `scripts/codex-token-sync.py --user U --dest PATH` once a minute; Station never refreshes. `--check` reports without writing.
- The runtime may run as root while the AI owns `home/`. Every read of an AI-controlled file resolves the real path and refuses anything outside `home/`, so a planted symlink cannot turn a root-side read into a read of a secret. Writes refuse symlinked directories and create temp files with `O_EXCL | O_NOFOLLOW`.
- Run AI commands as an unprivileged user (`runner.user`). Without it, `bash` runs as whoever runs the loop.

## Browser

Registration writes `home/bin/browser`, a wrapper around the `agent-browser` CLI that Hermes ships, using its bundled Chromium, a profile in `~/.browser-profile`, and its own socket directory. `bin/` is on the AI's `PATH`. The production-user smoke test opened Example Domain, took a screenshot and read it with view.

## Code map

| File | Role |
|---|---|
| `station/loop.py` | Life loop, onboarding, focus, schedule tool, sleeping, composer, built-in panels |
| `station/schedule.py` | Next-wake calculation; cron through `cronsim` |
| `station/prune.py` | Old tool output cleanup before a summary |
| `station/llm.py` | Anthropic and Codex clients, block conversion |
| `station/codex_auth.py` | Codex access token: from the Hermes pool (dev) or the synced file (production) |
| `scripts/codex-token-sync.py` | Root-side copy of the Codex access token into Station's auth directory |
| `station/board.py` | Panels and the board tool |
| `station/notes.py` | Diary and todo |
| `station/skills.py` | Skill index and reader |
| `station/web.py` | Tavily search and extract, through `api` |
| `station/runner.py` | Commands and file writes as the AI user, symlink-safe reads |
| `station/clock.py` | Wall-clock time in the system zone, through `tzlocal` |
| `station/secrets.py` | API keys from per-service dotenv files |
| `station/store.py` | State, per-thread histories, logs |
| `station/errors.py` | Error kinds, redaction, `state/errors.jsonl` |
| `station/notices.py` | Notices: pending items with keys, read state and expiry; the board panel |
| `station/inbox.py` | Event spool |
| `station/ledger.py` | SQLite ledger of every paid use (`state/ledger.db`); imports the old `usage.jsonl` once |
| `station/api.py` | Metered calls to paid APIs (`station api`, the `web` tool): key, limit check, pricing from the response |
| `station/memory.py` | Memory: chunks of diary, conversations and notes; embeddings through `api`; sqlite-vec and FTS5 search |
| `station/credits.py` | Allowances, periods, model pricing |
| `station/launcher.py` | Supervisor (stdlib only, copied to the ignored `<code>/launcher.py`): checkpoints, rollback after a crash before the first turn, restart backoff |
| `station/reload.py` | Watch code and `station.yaml` while asleep; `station check` in a subprocess, then exec into the new code |
| `station/release.py` | Bootstrap an extracted release as an editable upstream/main repository |
| `scripts/package.py` | Build the production allowlist from a committed Git revision |
| `install.sh` | Install in a local venv; merge upstream releases and reinitialize safely |
| `station/templates/` | Prompts, terms, suggestions, letter, seeded panel and skills |
| `probes/` | One-off provider probes (cache, Codex identity) |
