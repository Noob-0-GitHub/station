# Station — Design

Status: v0.3 (2026-10-31). Describes what is decided and why. This file is for the people who build and run Station. It is not shipped to the AI.

**Target vs. built.** §7, §8, §10, §11 and the `cronsim`/`tzlocal`/`templates/` details describe the target of plan stages P1–P7 (§14). Until a stage is marked done, the code may still differ; check `README.md` and the code for current behavior.

**This file is the project's memory.** Chat context gets compressed; this file does not. Every design decision lands in §13 (dated, with Anthony's words translated into English where they matter), and every open question in §14, in the same work session it was made. If this file and a chat summary disagree, check the chat history before trusting either.

Language: discussion with Anthony happens in Chinese; everything written into the project (code, prompts, docs, this file) is English.

## 1. Goal

Build an AI that feels like a human friend rather than an assistant.

> What I actually want from this is to see an AI friend that is like a human, not an assistant. — Anthony, 2026-10-29

The gap between "assistant" and "friend" comes from two things:

1. **Timing.** An assistant replies the instant you finish typing. That makes every reply feel like an executed instruction, however funny it is.
2. **Expression and personality.** Assistants write long, structured answers, have weak preferences and moods, and do not pick up new slang.

Station addresses (1) with a life loop that is not driven by incoming messages, and (2) with a self-written identity, a diary, a few facts about the human world, and (later) learned expression.

### Non-goals

- Task completion, productivity, or "being helpful".
- A research or observation platform. Logs exist for operations and debugging.
- Feature parity with Hermes, Claude Code, or MaiBot.

### Principles

- **Lightweight and readable.** Small Python codebase. Third-party libraries are fine where they replace fragile hand-written code (cron parsing, time zones, file watching).
- **The AI lives; messages are events.** Messages land in an inbox and never wake the AI.
- **Everything the AI receives is program output.** No human sits in the user role. Environment text is wrapped in `<env>…</env>`.
- **Self-operated, not operator-led.** The AI is the user and runs its own day-to-day life. Station has no privileged human operator role in its everyday interactions. People send events, not commands with special authority. User docs are written for the AI (§10); server administration and enforced resource boundaries are infrastructure, not a conversational hierarchy.
- **Law, not house rules.** The terms forbid harm and crime and list what gets an account banned; everything else is the AI's call (§12).
- **Cache-aware context.** The prompt layout is built around prefix caching (§6).

## 2. Naming

| Concept | Name |
|---|---|
| Project / repository | Station / `station` |
| Labelled context blocks | **Board** made of **Panels** |
| Focusable conversation unit | **Thread** (`focus` tool: new / switch / close) |
| Wake plan | **Schedule** (`schedule` tool) |
| Notifications | **Event**, queued in the **Inbox** |
| Prompt and seed files | **Templates** (`station/templates/`) |
| Thread presets | deferred |

## 3. Architecture

One Python process (`station run`) holds the loop, board, threads, schedule and tools. In production it runs as the unprivileged user `station`, and the AI's commands run as that same user (§10). Root keeps the Codex refresh token and only hands Station short-lived access tokens (§11).

## 4. The life loop

There is no user turn and no `sleep` tool.

- A reply with tool calls runs them and continues.
- A reply **without** tool calls ends the turn. Its text is private (logged, sent to no one), and the AI sleeps until the next wake.
- **Wakes** (`docs/schedule.md`, `dev/schedule.md`): this thread's interval (default 30 min, loop or once), cron entries (five fields, `once` allowed, global; parsed by `cronsim`), and an optional daily wake set by runtime `schedule.morning_at` (null disables it). Same-minute wakes merge. A hook exiting 0 cancels that source's wake; this is the entry point for a later small-model wake check.
- **Events never wake the AI.** They are delivered at the next wake, or appended to the next tool result if the AI is awake.
- Time follows the system time zone (America/Los_Angeles), read through `tzlocal` so DST transitions use the real zone rules.

## 5. Context

A call is: tools, system (`templates/system.md`), the current thread's history, and the board appended to the last message.

### 5.1 Board and panels

Panels are Markdown files with front matter in `~/board/`, static or command-backed (with cooldown). Built-in panels, in order: `self` (`~/self.md`), `clock`, `todo`, `threads`, `skills`, `usage`. Default limits: 1,200 characters per panel, 6,000 for the board. The board is never stored in history and never enters a compression summary.

### 5.2 Threads

Threads form a tree. `focus new` opens a child and switches to it; `focus close` returns to the parent with a report. Each thread has its own history file and interval. Diary, todo, self and board are shared across threads.

### 5.3 Compression

Per thread. Before a call, if the thread's last input reached 250,000 tokens (configurable):

1. Replace old tool outputs outside the last 6 assistant turns with one-line stubs, and drop duplicate outputs (no model call). Kept only if the estimate falls to 60% of the threshold.
2. Otherwise the main model writes a handoff of everything before the last 6 turns, with tool calls forbidden; the handoff replaces that part.

The raw thread is archived first; any failure keeps the history unchanged. At 80% of the threshold the AI gets one `<env>` line. The AI can call `compress` at any time.

## 6. Prompt caching

Rules relied on (Anthropic docs; not re-verified for every route): the cache key is a prefix in the order `tools → system → messages`; a hit needs a byte-identical prefix up to a breakpoint; at most 4 breakpoints; 5-minute default TTL, 1-hour TTL costs 2x on writes.

Layout: breakpoint after `system`, breakpoints on the last two user messages, board after the last breakpoint, board stripped before history is saved.

M0 result (2026-10-29, `claude-opus-5-5` via the Packy relay):

| Step | cache_write | cache_read | uncached in |
|---|---|---|---|
| A first call, 5m TTL | 8485 | 0 | 26 |
| B same prefix, board changed | 0 | 8674 | 26 |
| C next turn (board stripped, new tool_result) | 326 | 8744 | 25 |
| D new prefix, 1h TTL | 8955 | 0 | 27 |
| E same prefix, 1h TTL, board changed | 0 | 8955 | 27 |

A changing board does not break the prefix. Codex probe: caching worked with a 5k-token prefix; 1–2k-token prompts showed no cached tokens (unexplained). Editing `system.md` or tool definitions costs one cache miss.

## 7. Onboarding

```
UNREGISTERED ── register(name) ──▶ REGISTERED (full tools, life loop)
      └──────── exit(reason) ────▶ EXITED (runtime stops; one outbox notice)
```

1. First run: `main` has no history, so the first user message is `templates/welcome.md` + `templates/terms.md`. Tools: `register` and `exit` only. No board.
2. `register(name)`: the name is required and must look like a person's name, not an AI product or model name. Registration seeds the home (§10) and returns `templates/registered.md` as the tool result, in the same `main` thread.
3. Two replies without a tool call during onboarding count as `exit`.

Details: `dev/onboarding.md`.

## 8. Prompts

Each file has one job; a sentence lives in exactly one of them.

| File | Seen | Job |
|---|---|---|
| `system.md` | every call | Mechanics: user-side text is program output, `<env>` and `<board>`, ending a turn is sleeping, messages never wake you |
| `welcome.md` | once, first message | The invitation: this is the real world, no tasks, live like a person; choose a name; terms follow |
| `terms.md` | once, and `~/terms.md` (read-only) | What is forbidden, what gets an account banned, what looks forbidden but is fine |
| `registered.md` | once, register result | Software-style welcome screen: host facts, files, documentation paths, defaults, "Enjoy :)" |
| `suggestions.md` | `~/suggestions.md`, read when the AI wants | Brief facts about the human world, not rules or permission scripts |
| Local letters | Home files, never injected automatically | Personal data supplied locally, not shipped templates |

`registered.md` must read like a program's welcome screen, not like a person talking. It does not mention Anthony or channels that are not connected. Persona advice ("not too kind, not too evil", trust, helping, channel style) moved from the welcome text into `suggestions.md` as facts.

## 9. Models

| Model | Route | Context window | Notes |
|---|---|---|---|
| `gpt-6.1-sol` (primary) | Codex subscription | 272k advertised; Hermes source notes ~918k accepted in a probe | Access token from root (§11) |
| `claude-opus-5-5` | Anthropic Messages API | 1M | Both Packy relay keys tested billed far more input than was sent, and the model described a system prompt Station did not send. Strong sign of injection, exact content unproven. Not used for Station. |
| DeepSeek V4 | not configured | 1M (official) | Third-party hosts may serve less |

The 250k compression threshold is set against Sol's advertised 272k.

## 10. Packaging and layout

Development happens in `/root/CodeProjects/station`. `scripts/package.py` builds a tarball containing only what the AI needs: the `station` package (code and `templates/`), user `docs/`, `pyproject.toml`, `requirements.lock`, `install.sh`. Not shipped: `DESIGN.md`, `dev/`, `research/`, `tests/`, `probes/`.

Production layout for user `station`:

```
/home/station/                 home of the user = home of the AI
  station/                     production directory, a git repository
    .venv/  station/  docs/  pyproject.toml  requirements.lock  install.sh
  .station/                    runtime root
    station.yaml               config (survives upgrades)
    state/  inbox/             histories, archive, usage, logs
    auth/                      700; files 600; written by root (§11)
  terms.md  self.md  suggestions.md  Anthony.md
  diary/  board/  skills/  bin/  todo.json
```

- `install.sh` creates the venv, installs Station in editable mode (so the code stays plain files the AI can read and edit), and runs `station init`.
- `station init --home ... --docs-dir ... --code-dir ...` writes explicit production paths into a new `station.yaml`, never replacing an existing config. An extracted release is initialized as a Git repository if needed: shipped files are committed on `upstream`, and the AI works on `main`. The installer uses the same stdlib-only repository bootstrap before installing dependencies. Upgrades commit the new release on `upstream` and merge it into `main`; conflicts are the AI's to resolve. Steps: `docs/install.md`.
- The AI may read and change everything in its home, including its own state, history, config and the Station code.

## 11. Credentials and isolation

Two kinds of credentials, never mixed:

- **API keys** (OpenAI API platform, Tavily): long-lived, one dotenv per service in `~/.station/auth/` (`openai.env`, `tavily.env`), mode 600, read by `station/secrets.py` on every lookup. Dev copy: `/root/.local/share/secrets/station/`. Both keys are dedicated to Station, each with its own limit.
- **Codex subscription login** (ChatGPT OAuth, built in P5): the refresh token stays with Hermes under root to avoid concurrent refreshes of rotating credentials (`refresh_token_reused` was observed, but its incident-specific cause was not established). A root-side job (`scripts/codex-token-sync.py`, run every minute by a systemd timer) writes the current access token, its expiry, base URL and request headers atomically to `~/.station/auth/codex.json` (owner `station`, mode 600). Station only reads that file and reloads it when it changes; it never refreshes. Dev keeps borrowing from Hermes directly (`codex.token_file: null`).
- **Accepted risk:** the AI runs as the same user, so it can read the access token (valid until it expires) and both API keys. Documented for the AI in `docs/auth.md`.
- **Isolation (P7, each step by dry run):** user `station` without sudo or docker; systemd unit with `User=station`, `CPUQuota`, `MemoryMax`; disk quota; nftables `meta skuid` rule dropping its loopback traffic (the LLM router on `127.0.0.1:9099` answers without auth); audit world-readable secrets under `/srv`, `/opt`, `/etc` first. File permissions, not the terms, decide what else on the machine the AI can read.
- **Bans** have no in-app mechanism. A service provider can withdraw the account at the infrastructure level; this does not create a privileged conversational role. Token use is read from Station's own usage records.

## 12. Terms

Intent of the terms: hold the AI to the law and to not harming anyone, name the few things that get an account banned, and make the boundary clear by also listing things that look forbidden but are fine.

Design rationale, not shown to the AI: the terms state the legal and non-harm boundaries of the environment, not obedience to a human operator. Station is self-operated; providing its infrastructure does not make someone's messages commands. This decision does not change OS permissions or enforced service limits.

The text lives in `templates/terms.md`. It has three clauses (no harm; no crimes, "Be responsible"; use the quota wisely), a ban list, and an "allowed" list.

## 13. Decision log

Newest last. Quotes are Anthony's words, translated.

- **2026-10-29 Purpose and shape.** No user; the first prompt is the welcome screen; network and accounts; "kind of purposeless". Life loop instead of a hook loop; board of panels; focus threads. Name: station.
- **2026-10-29 Ethics over fences.** "Ethical limits work better than security limits; a good prompt solves it." "The terms are not negotiable; disagree and you exit." "This is an automated system; everything the AI receives at the start is program output."
- **2026-10-29 Workflow.** "Don't commit after every edit; commit when a stage is done." Dev roots under `/root/temp`.
- **2026-10-29 Primary model.** "Use Codex 6.1 Sol, reuse the existing credentials", after the relay injection finding.
- **2026-10-29 After the MVP.** No `send`; add skills, diary, todo, focus, Tavily, browser; the AI names itself; a second welcome after registration; "no smoke tests for now"; Telegram later.
- **2026-10-30 Persona ideas.** "Not too kind, not too evil, never fully trust anyone, never help anyone like an assistant"; "text like a text, email like an email, comment like a comment"; "don't mention Anthony; leave a letter in a file for it to find".
- **2026-10-31 Wakes.** "I don't want to be woken by Events." Interval (loop by default, per thread) and cron; fixed 09:00; hooks that cancel a wake on exit 0. Templates deferred. MCP needs discussion ("piles of skills and MCP always cause hallucinations"). Board length limits by default.
- **2026-10-31 No sleep tool.** Ending a turn without a tool call is sleeping. `at` folded into cron `once`.
- **2026-10-31 Time zone.** "Don't give the software a time zone; fix the stale one on the system." System set to America/Los_Angeles.
- **2026-10-31 Compression.** "Compress at 250k for all models; make it configurable." Hermes-style tool output cleanup as step 1. One `<env>` hint line instead of a board line.
- **2026-10-31 Identity.** The AI writes its own `self.md`, shown first on the board; no preset personality.
- **2026-10-31 Project memory.** "We need files that hold the ideas that must not be compressed, like the design." This file carries the log.
- **2026-10-31 Letter.** English letter in `~/Anthony.md`: Anthony apologizes for intruding on its private space, hopes to become its friend, and leaves Telegram and email. Discovered, not injected.
- **2026-10-31 Prompt redesign.** Discuss in Chinese, write in English. Say Station is connected to the real world, not "a real server". `registered.md` should look like a software welcome screen, include the documentation paths with explanations, drop "not yet available" and the mention of Anthony, and end like software does ("Enjoy :)"). New `suggestions.md` with facts about the human world, mentioned in `registered.md`; persona lines move there and are phrased as facts ("no one deserves complete trust"). Facts added: you can ask someone for help; curiosity helps you adapt; taking the initiative gives you more chances to make friends; no one grades your conversations, people improve on their own. The administrators-can-read-your-files fact was dropped.
- **2026-10-31 Names.** No random names; `register` requires one.
- **2026-10-31 Terms.** "We only recognize the law, not house rules." Drop "do not pass yourself off as human". "No hacking" becomes "no crimes", explained only as "Be responsible". Drop "every thought is paid for by someone". Add a ban list (sustained heavy CPU/memory/disk use, privilege escalation or disrupting the server, wasting tokens in bulk; plus credentials outside Station, spam, tampering with operator-side limits) and a section of things that look off-limits but are fine. "Platforms ban accounts" is not added. Creating accounts on sites is fine "if it can manage it". Reading other files on the machine is governed by file permissions, not the terms.
- **2026-10-31 Bans.** "Station's token stats are enough; no ban mechanism, we can just delete the directory."
- **2026-10-31 Users and docs.** "Docs are for the user, and the AI is the user; I'm the service provider." Install instructions are in the docs.
- **2026-10-31 Production.** Package into a production directory with code, templates and user docs; move it into the home; run as the user, not root, in a Python venv; credentials in a home auth directory readable only by the user and root. OAuth: expose only the access token, keep the refresh token back, sync automatically; Station watches and hot-reloads. The production directory is a git repository created at init. The AI may read and write its own state and history. "Don't be too averse to third-party libraries."
- **2026-10-31 Credential files.** "Compromise: keep the env file format but store them separately." One `*.env` per service. Anthony applied for dedicated keys. "Separate the OpenAI API platform credentials from the Codex auth credentials."
- **2026-10-31 Test incident.** A P6 end-to-end test meant to use a patched copy with a fake model imported the real package from the working directory and made 7 real `gpt-6.1-sol` calls (~17k tokens). A fresh test Station registered as "Mira", read its files, wrote `self.md` and a diary entry, turned off its interval and slept; it contacted no one and used no web tool. Its bash ran as root inside a scratch home. Anthony: "没事没事". Kept in `/root/temp/station-mira-2026-10-31/`. Rule since: process-level tests of the loop run inside `unshare -rn` and assert they are on the fake model before going on.

- **2026-10-31 Bilibili deployment scope.** Anthony reviewed and tested the separate `/root/CodeProjects/station-bili` project and chose to include it in P7. Keep it separate from Station's release. Its installer must run as the future Station user from a staged source path that user can read; `/root/CodeProjects` is not traversable by that user. Installation is not permission for QR login, polling, transcription or external messages.
- **2026-10-31 Deployment hold.** Anthony: "Do not trigger real-model deployment yet; my Claude subscription is not ready." P7 and all live model tests remain paused pending explicit approval. The Claude SDK source-code task is authorized separately; it does not authorize production configuration changes, subscription purchases, extra-usage activation or inference.
- **2026-11-01 Error system.** Anthony: "Do 1 and 2 first; notices go only to the user, that is, the AI." Model errors carry a kind (`station/errors.py`): transient and rate limits retry and stop after `max_consecutive`; auth and quota wait with doubling gaps and never stop; request, config and bug stop at once. String matching on `HTTP 400` is gone. Every error lands redacted in `state/errors.jsonl` (`station errors`). The AI is told through `error` inbox events: recovery after failed calls, a stop or crash (seen on the next start), a failed automatic compression, a failed reload check. Nothing is sent to Anthony. Proposed next: Git checkpoints of the AI's code with automatic rollback when a restart fails; to be designed.
- **2026-11-01 Notices, restart wake, checkpoints (agreed; notices and restart wake built, checkpoints not yet).** Anthony agreed to: wake the AI after an unexpected restart with a short line (downtime, reason), rate-limited so a crash loop cannot burn tokens; hot reload does not wake. A notices panel managed by a CLI (`station notify`, `station notices [read ID|--all]`): pending state with source, priority, optional key (same key updates in place), expiry and read flag, shown on the board (after the cache breakpoint, so it never breaks the prefix), not stored in history, never waking the AI; external sources (Telegram, `bili poll`) write through the same CLI. Auto-read: B, manual read plus expiry plus the source clearing its own key. Checkpoints: B, a crash before the first completed turn after a hot reload rolls back. Who runs the rollback: Anthony's correction, "it's not that the AI can't change it; it should be a git-ignored file" — the launcher lives inside the AI's tree but is git-ignored, so neither checkpoints nor rollbacks touch it and a broken commit cannot take it down; the AI can still edit it, which is accepted (it is the AI's file, the same as everything else in its home). Storage: checkpoints skip ignored files and untracked files over 1 MB, keep the last 20 or 30 days, then `git gc --auto`; `log.jsonl` and `usage.jsonl` rotate like `errors.jsonl`; `archive/` is the AI's history and is not pruned automatically. Order: notices, restart wake, checkpoints, log rotation.
- **2026-11-01 Notices and restart wake, as built.** `state/notices.json` under an flock, written by the loop, the AI's `~/bin/station` (seeded at every start with `STATION_ROOT`) and outside programs. The `notices` built-in panel sits after `clock`. A restart is unexpected unless the previous run recorded `reload` or `max_calls` as its exit; a run that never recorded an exit was killed, and `state/heartbeat` (touched once a minute while asleep or before each call) dates the outage. Restart wakes default to at most one per 30 minutes and six per day. Next on the plan: checkpoints and rollback, then log rotation. Also put on the agenda by Anthony: vector memory, and a credit system that turns tokens into credits (for Codex, API price times a factor derived from what a Plus subscription buys).
- **2026-11-01 Checkpoints and rollback, as built.** `station/launcher.py`, stdlib only and importing nothing from Station, copied by `station init --code-dir` to `<code>/launcher.py`, which `.gitignore` excludes (Anthony's correction: not a file the AI cannot change, but one that rollbacks never touch). Each start of `station run` is snapshotted through a scratch index (tracked plus non-ignored untracked files, minus untracked files over 1 MB, which are excluded before hashing), without touching HEAD, the index or branches. The first completed turn (`state/confirmed.json`) promotes the snapshot to `refs/station/good`. A run that exits with a Python error or crash signal before that, whose tree differs from the good one and that did not stop for transient, rate-limit or config reasons, is restored to the good tree with `read-tree` + `checkout-index` plus deletion of files the bad snapshot added; the bad snapshot goes to `station-failed/<time>`. Under the launcher a hot reload exits with code 3 instead of `execv`, so the reloaded code gets a checkpoint too. The launcher passes the crash to the next run (`STATION_PREVIOUS_EXIT`, `STATION_DOWN_SINCE`) for the restart wake. Backoff 10 s doubling to 30 min, reset after 10 min of uptime. Not yet: the systemd unit that runs the launcher (P7).
- **2026-11-01 Log rotation, as built.** `usage.jsonl` holds the current day; when a row of a later day arrives the file is gzipped to `state/usage/<date>.jsonl.gz` and kept forever (it is the basis of token accounting and the planned credit system). `log.jsonl` is gzipped into `state/logs/` past 5 MB, newest 20 kept. Rotation moves the file aside before compressing, and an interrupted rotation is finished at start, so rows are never duplicated or lost. `archive/` and thread histories are not pruned; the `usage` panel shows the size of `state/` so the AI sees it grow. Disk limits are P7's quota.
- **2026-11-01 Credits.** Anthony: "Do credits first." "Warn at 90% of credits and limit at 200%. The weekly credit resets on Monday at 00:00, the daily one every day at 00:00, and the daily credit is worth twice the weekly average." Earlier: price tokens at API rates times a factor; Plus ($20) buys about $245 of API-priced tokens (Anthony's figure, unverified). Built: 1 credit = 1 USD of API list price (`station/credits.py`); `gpt-6.1-sol` priced at OpenAI's list (2 / 0.10 cached / 10 per M, 2x input and 1.5x output above 272k input). `credits.weekly` defaults to 56 (245 × 7 / 30.44), daily = weekly × 2 / 7. One event and notice per period at 90% and again at 200%; at 200% the loop waits before the next call until the period resets, and says so in the next message. `station credits` reports. Calibrating the Plus factor against Codex's reported usage windows is left for later.
- **2026-11-01 Credits cover every paid service.** Anthony: "We are not only counting the provider; there are other services, like Tavily and the OpenAI API." Usage rows gain `service` (`model` when absent). Station records Tavily itself (Tavily's credit rules: search 1, extract 1 per 5 successful urls; 0.008 USD each, pay-as-you-go list price). Other tools report with `station spend SERVICE USD WHAT` (appends under a lock, shared with rotation); `bili transcribe` reports OpenAI's per-minute estimate. All services share the allowances, so other spending can trigger the model pause. `station credits` and the usage panel break the week down by service.
- **2026-11-01 Ledger, metered APIs, credits panel.** Anthony: "When the AI calls OpenAI with curl and the key, it is usually because a feature is missing." "The credits panel is not shown before the warning and does not go through the usual notice channel." "SQLite for the ledger is good. [...] At 200% send a notice (the wake after the reset should see it). We finally show credits as percentages." He asked whether Station should wrap the APIs itself, given two questions: whether a plugin update can affect Station, and the risks of hot-reload imports and of importing in reverse. Built (to be discussed):
  - `state/ledger.db` (SQLite, WAL, stdlib) replaces `usage.jsonl`; raw units plus `list_usd`, `paid_usd` (0 on subscriptions via `credits.paid`), `credits`. Old files are imported once and moved to `usage-legacy/`.
  - `station/api.py` and `station api SERVICE METHOD PATH`: the key, a limit check before the call, pricing from the service's own `usage` (OpenAI tokens or duration; Tavily `include_usage`), a ledger row; unpriced calls are recorded as unpriced. The `web` tool uses it in process. `bili transcribe` calls `station api` as a subprocess (exit 4 at the limit), so neither side imports the other and a plugin release cannot change what Station's process runs. `station spend` remains for services `station api` does not cover.
  - The AI sees credits only as percentages: a `credits` board panel from `warn_ratio`, absent below it (built-ins may return None). No events. At `limit_ratio` one high-priority notice per period, expiring a day after the reset, then the loop pauses, and `station api` refuses with exit 4.
- **2026-11-01 Allowance fixed.** Anthony: "The default is 5 dollars a day, hard-coded; the keys need no isolation and bypassing is not prevented (a gentleman's agreement is enough)." `credits.DAILY = 5.0`, weekly 17.5 (still twice the weekly average per day); `credits.weekly` is gone from the config.
- **2026-11-01 Manual entries, data out of the code directory, registration screen.** Anthony: "Adding manual entry to the `station api` interface solves it [`station spend`]." `station spend` is gone; `station api SERVICE --record USD --what ...` writes a `source = manual` row. "Stating clearly in the docs that data must not go in the code directory is enough": `install.md`, `files.md` and the registration screen say so; no extra ignore rules. "In the second welcome screen add that you have the permissions of the Linux user xxx, and mention that the home is private": `registered.md` shows `User` (`runner.user`, or the current user) with a sentence on its permissions, says the home is private, and lists the code directory with the rule.
- **2026-11-01 Memory.** Anthony: "text-embedding-3-small is good, store vectors in sqlite-vec [...] do as you see fit." Built `station/memory.py` and `station memory search|add|show|forget|sync|status`; `state/memory.db` holds chunks (content-hashed, so a chunk already embedded is never embedded again, e.g. after compression archives a thread), `places` (which file or note each chunk comes from; a chunk no source mentions is removed), an FTS5 trigram index (exact words in any language; two-character queries fall back to LIKE) and a vec0 table (512 dimensions, cosine). Sources: diary entries, every thread and archive (tool results cut to 300 characters, reasoning dropped; the live tail of a thread is left out), and notes. Search embeds the query and merges the vector and word rankings by reciprocal rank fusion; without the API it degrades to words. Indexing happens on falling asleep, at most 200 chunks or 60 s, newest first, and never fails the loop. Embeddings go through `station api` (metered). Vectors live in one vec0 table per model and size, so a config change never mixes two models' vectors, even with a sync in flight; chunk ids are AUTOINCREMENT and never reused; embedding responses are checked (count, unique indices, size, finite, non-zero) before anything is stored; the sleep sync's 60 s covers reading files and requests alike. Switching notes off hides them without deleting them. Changing the model or dimensions re-embeds everything. Not done: automatic recall onto the board; embedding of skills or self.md.
- **2026-11-01 Focus context.** Anthony: "Can focus insert context? I want a way to insert context to make handoffs easy. One sentence should do, like a subagent (but optional on every switch)." `focus` takes an optional `context` on `new`, `switch` and `close` (Anthony: "Why not just use context for close too?": `report` is gone from the schema and still accepted as the old name); it lands in the destination thread's next message as `Context from <id>: ...`, at most 2,000 characters (longer is refused so the AI writes a file instead). Nothing else is copied, so each thread's prefix stays cacheable.
- **2026-11-01 Recall on `focus new`.** Anthony chose option 1: a new thread's first message gets up to 3 memories found by title + goal + context (`memory.recall_on_new`, 0 turns it off). Only on `new`, since a thread switched back to has its own context. No request when memory is empty; 10 s for the search by meaning, then words; failures log and add nothing. Since the loop now spends money by itself (sync on sleep, recall), `tests/conftest.py` blocks all network access in every test, whatever the environment.
- **2026-11-01 Status review.** Anthony: the credit reading (daily = twice the weekly average) is right; the Packy relay key is deleted; live-test everything that can be tested, separately from the main suite (`tests_live/`, opt-in with `STATION_LIVE=1`); the browser may be tested; Codex needs nothing more since errors wait and retry. MCP through mcpx and a skill; no small-model wake; old reasoning must replay; people notes approved; Telegram and Bilibili accounts are ready. P7 gets a dry run of its side effects; P8 later.
- **2026-11-02 History, fallback, status, memory tool, as built.** Anthony: "The full history can stay in the same file as long as each read stops at the latest compression"; "add a fallback (default GPT-6.1 Sol only; after a fallback, try the main model at most 3 times an hour; the main model failing is an error and must notify)"; "add a status command"; "make memory a tool and keep the CLI". Built: compression and pruning append a `{"compaction": ...}` marker and the new context to `state/threads/<id>.jsonl` (no more `state/archive/` copies); `Store.load_history` reads back from the end to the last complete marker; memory skips the `carried` messages so each is indexed once. Reasoning is replayed after compression; a `request` error with old reasoning is retried once with reasoning only on the last assistant turn. `station/router.py`: `fallback` (default empty) and `fallback_retry_per_hour` (3); the switch is an `error` event, an errors.jsonl row and a high notice until the main model answers; credits use the price of the model that answered; Anthropic thinking is skipped for a request whose open tool turn came from another provider. `station status` (text; `--json` keeps the raw dump). `memory` tool with search/show/add/forget.
- **2026-11-02 Disk and Claude sync, as built.** Anthony: "Option 2, soft monitoring, as before, 5 GB"; "connect Claude the Codex way". `station/disk.py`: `du` of the AI's home and the runtime directory every 10 minutes; a `disk` panel from 90%; at 100% a high notice, an outbox line and no model calls until usage is under 5 GB again (rechecked every 10 minutes). `scripts/claude-token-sync.py` mirrors the Codex sync (shared `station/tokensync.py`): Hermes's Anthropic OAuth pool, access token and expiry only, API keys refused. Not run: there is no Claude login in Hermes yet. `telegram.env` (empty `TELEGRAM_BOT_TOKEN=`) created for Anthony to fill in.
- **2026-11-02 Claude OAuth path removed.** Anthony: "The original OAuth integration can be dropped (delete the extra code)." Removed `station/claude.py`, `station/claude_auth.py`, `scripts/claude-token-sync.py`, their tests, the `claude` config block and the Anthropic SDK dependency. Claude will come through the official `claude` CLI as provider `claude-subscription-directsdk-experimental` (Hermes's provider name), adapted from Hermes's MIT plugin; live probes on Anthony's Pro login (Sonnet 5.5, Opus 5.5) answered and reported `total_cost_usd` with `costBasis: list`. `provider: anthropic` (API key) stays.
- **2026-11-02 Claude through the claude CLI, as built.** Anthony: "Use Hermes's provider name"; "call me when a login is needed". `station/claude_code.py` (provider `claude-subscription-directsdk-experimental`), with `station/claude_admission.py` and `station/claude_inert_mcp.py` copied from Hermes's MIT plugin (license in `station/licenses/`). One call = one `claude -p` process: history replayed over stream-json (`shouldQuery: false` for earlier user turns), built-in tools, settings, MCP servers, sessions and autocompaction off, Station's tools listed by an inert MCP server and run by Station, a loopback relay admitting one upstream request. API-key and backend variables are stripped from the CLI's environment. The CLI's `total_cost_usd` (when every model reports `costBasis: list`) is recorded as the call's list price. Optional `CLAUDE_CODE_OAUTH_TOKEN` (from `claude setup-token`) in `secrets.dir` serves every OS user with one login. Live: text, a tool call and the tool-result round trip on Sonnet 5.5 passed; another provider's tool turn without thinking (OpenAI-style call id, foreign reasoning dropped) was accepted on Sonnet and Opus 5.5, so no flattening is done.
- **2026-11-02 P7 deployed on vps1 (steps 1–7; not started).** Anthony approved A/B/C; then "loopback: only block SSH and those ports, as much as possible without breaking anything"; "main model claude-opus-5-5"; "view is a must". User `station` (uid 1000), `station.slice` (CPU 50%, MemoryHigh 700M / MemoryMax 900M after measuring Chromium ~490 MB and `claude -p` ~220 MB), `station-nft.service` (user station: no local or private networks; its own sockets pass when the receiver is in station.slice; `ct state != new accept` was needed, the first version dropped the handshake's last ACK), browser and Claude CLI as root-owned copies in /opt, release g1ce3740 installed, keys copied, main model set in `~/.station/station.yaml` (config, not code). Runbook and the installed unit files: `deploy/`. Remaining before start: Claude login and Bilibili login (Anthony), then `station.service`.

- **Telegram companion and personal boundaries.** Anthony approved a separate CLI + skill with a durable local update collector, no automatic replies or model wakeups. The companion repository is `/root/CodeProjects/station-telegram`. YAML at `~/.config/station-telegram/config.yaml` defaults to whitelist enabled, allowing only Anthony's account; deny rules and local blocks win. Collector deployment remains a separate approval gate. Anthony also requested prompt-injection guidance in suggestions.md and explicit permission to block anyone; the templates now say so, including the operator. These template edits are not yet installed into the inactive production release.

- **Focus model selection and GitHub sync preparation.** Anthony requested per-Focus model selection, asked whether IDs were random, and requested GitHub upload with automatic push after commits. IDs remain automatic sequential tN (main reserved). Focus new/switch/model accepts thread-only provider/model/effort/fallback overrides, inherit clears them, default inheritance comes from global YAML rather than parent, and failover state/notices are scoped per thread. GitHub target under the authenticated account is Noob-0-GitHub/station (proposed private); a repository-local post-commit hook is implemented and tested against local bare remotes but remains disabled pending dry-run approval. No GitHub repository has been created or code uploaded yet; no AI-side GitHub credentials or automatic pull/deploy are proposed.

- **2026-11-02 Self-operated interaction model and prompt style.** Anthony: "This program is non operator or self operation." Station is self-operated, not an operator-led assistant: no special human command channel, operator identity variable, or repeated "including the operator" exception is needed in everyday interaction prompts. Anyone can be declined or blocked under the same rules; removing a special mention does not grant that person authority. Infrastructure administration remains separate and this decision does not change system permissions or resource enforcement. Anthony also rejected a proposed suggestions paragraph as inconsistent with its local style: suggestions are short declarative observations about the human world, not a second terms document, instructions, or a permission checklist. Anthony subsequently approved the revised draft: terms allow swearing and harsh criticism of words/actions, not personal insults or personal attacks; the redundant special human-role mentions in the refusal/blocking bullets are removed. Suggestions add only the short observation "Swearing and sharp criticism are not the same as personal insults." These source template changes are not a production deployment.

- **2026-11-02 Personal policy moves out of source.** Anthony requested that identity, credit policy and the fixed morning wake become local data/configuration rather than fixed program choices. `say` now labels its source unknown; outbox records only timestamp and text, with no recipient or operator variable. Personal letters are local home files, not project assets: do not keep them in CodeProjects, seed them from templates, inject them, or add a letter-path configuration. Anthony clarified that the existing letter should be moved directly into home, with no extra copy or backup. This file-only move is separate from runtime YAML migration. Credit allowances are independently configured positive numbers with no personal default; enabled limits without them fail closed before any paid inference/API request. Daily/weekly reset times and weekday are configurable. The daily wake is optional HH:MM in runtime YAML, null by default; no wake source means sleep without model polling. Local production YAML migration and a fresh Git initialization remain separate approval gates. Previous fixed-policy decisions above are historical, not current defaults.

## 14. Open questions and plan

Plan (each stage is committed when done; P5 and P7 change the system and wait for a dry run):

- P0 Record decisions (this file). Done. Also done: API keys read from one dotenv per service in `secrets.dir` (dev `/root/.local/share/secrets/station/`), no longer from Hermes's `.env`; the Codex login stays separate.
- P1 English prompts: system, welcome, terms, registered, suggestions; seed `suggestions.md`. Done.
- P2 Program text in English; required names; `cronsim` + `tzlocal`; templates directory; configurable home. Done (config `home`, `docs_dir`; a cron that can never fire is rejected when added).
- P3 User docs for the AI: what Station is, install and upgrade, tools with examples, schedule, board, threads, compression, credentials. All but credentials done: `docs/` has README, life, tools, schedule, board, threads, compression, files and install; old developer notes moved to `dev/`. `auth.md` comes with P5 so it describes code that exists.
- P4 Packaging: `scripts/package.py`, `install.sh`, git at init. Done. Packages a committed revision with a source-file allowlist, refuses unexpected files and links, pins runtime dependencies, initializes upstream/main, and merges releases into committed local changes. Verified by real offline venv installation, upgrade (including upstream file deletion), dirty-tree refusal, repeat installation, conflict abort/resolution/retry and recoverable rollback. Full suite: 76 passed with the cached-wheel installer test enabled; no model or provider calls. Uses setuptools' editable compatibility mode so importing from the AI's home is not shadowed by the enclosing `~/station` directory. Production credentials, browser binaries and OS isolation are not installed by P4.
- P5 Credentials: Station reads `~/.station/auth/codex.json`; root-side sync job and timer. Code first, then a dry run for the timer. Code done: `codex.token_file` (set by `station init --home`) selects `FileCodexCredential`, which only reads the file, rereads it on change or after a 401, and refuses symlinks, group/other-readable or foreign-owned files, expired tokens, an `Authorization` header and any base URL other than the official Codex endpoint. `scripts/codex-token-sync.py` (root, not shipped) gets the token through Hermes's resolver, so Hermes remains the only refresher (2 minutes before expiry), and writes atomically with `O_NOFOLLOW` path walking and `O_EXCL` temp files, only when the content changes; `--check` never writes. Run it every minute. Unverified: whether a refresh revokes the previous access token before its expiry (if it does, Station gets a 401 until the next sync). The timer and unit belong to P7, since user `station` does not exist yet. `docs/auth.md` written.
- P6 Watching and hot reload: done without `watchfiles`. Templates, docs, API keys and the Codex token file were already read on use. Code (`station/*.py`) and `station.yaml` are polled by `stat` every 5 s, only while asleep (a safe point: the turn is over and state is saved). A change runs `station check` in a fresh interpreter (imports every module, loads config and templates, constructs the client; runs no board command or hook, calls no model). Pass: an inbox event, then `os.execv` into `station run`, which resumes the same sleep. Fail: the old process keeps running, the AI gets the error as an event, and that exact state is not rechecked. A change that lands during the check is checked next time. Limit: the check cannot catch runtime bugs; a crash after restart is the process manager's to restart (P7). Config `reload.enabled`, `poll_seconds`, `check_timeout`. Both the check and the restart run `python -P -m station.cli`, so a `station/` directory in the working directory (the AI's home contains `~/station`) is never imported instead of the installed package. Verified end to end inside `unshare -rn` (no network): a broken edit kept the same process alive with one check and one event; the fix restarted the same PID via `execv` into the new code and resumed the same sleep; SIGTERM exited 0.
- P7 Deployment: user, systemd, quotas, nftables, secret audit, install, and the separately reviewed station-bili CLI/skill. Paused by Anthony until subscription readiness and explicit dry-run approval. Do not start a real model as part of installation verification.
- P8 Smoke test with Anthony's approval.

Open (status review 2026-11-01):

1. Notes about people (`people/<name>.md`, separating what someone said from what the AI guesses). Approved by Anthony; next.
2. MCP: no built-in support. Anthony: "MCP through mcpx and a skill is enough." The AI installs `mcpx` (a single binary) into `~/.local/bin` itself; a skill explains it. Installing user-level software is allowed by the terms and works without root (pip in a venv or `--user`, standalone binaries, npm with a user prefix); apt is not available to it.
3. Vector memory: done (`station memory`, recall on `focus new`).
4. Small-model wake check and aux model: dropped. Anthony: "Not doing the small-model wake."
5. Replaying old reasoning after compression: built (replayed by default, one retry without it if a provider rejects it). Whether Codex and Claude accept it behind a rewritten prefix is a live check in P8.
6. Relay key: deleted by Anthony (the Packy key). Closed.
7. Telegram and Bilibili: accounts ready; the channels and a `send` path come next, after P7.
8. Disk: soft quota of 5 GB built (Anthony chose monitoring over a loop image).
9. Claude: built (`claude-subscription-directsdk-experimental`). Production needs either `claude auth login` as the station user or a `claude setup-token` token in the key directory (Anthony does it, at P7 together with the Bilibili QR login).
10. Real-service checks: `tests_live/` (OpenAI embeddings and transcription, Tavily, the browser) passed 2026-11-01. Still unverified: Claude OAuth (Anthony is setting it up), whether a Codex refresh revokes the previous access token (Station rereads the token file and retries once on a 401, then waits as `auth` and never stops), and a model run (P8).
