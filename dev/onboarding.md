# Startup and registration

`R` below is the runtime root, not the source repository. The AI's home is `R/home`.

## 1. Init (no model call)

`station --root R init` writes `R/station.yaml` if absent, reads it over `station/config.py` defaults, and prepares `home/`, `inbox/`, and `state/threads/`. The default in-memory state is `unregistered`, current thread `main` (title: 日常). No conversation or registration files are seeded yet. Registration does not create a Linux account; `runner.user` must be provisioned separately.

## 2. First run

`station --root R run` loads saved state and the current thread's JSONL history. On a fresh root, `main` has no history, so `recover()` reads `station/templates/welcome.md` and `terms.md`, joins them with a separator, and writes that as the first `user` message to `R/state/threads/main.jsonl`.

The first model call contains:

- System: `station/templates/system.md` (environment and turn semantics).
- Tools: only `register(name)` and `exit(reason)`.
- User/history: `welcome.md` + `terms.md`.
- No board before registration.

`DESIGN.md`, README and this document are development records, not automatically sent to the Station model.

## 3. Register or exit

A `register` call validates the name (required; blank or product-like names are rejected and the AI can retry), sets the status/name, and calls `seed_home()`:

- Creates `board/`, `diary/`, `skills/`, `bin/`.
- Creates empty `self.md` if absent.
- Does not seed personal letters. Letters are local home files, not source templates; existing local files are preserved and never injected automatically.
- Seeds the system panel and bundled skills if absent.
- Copies `terms.md` read-only; writes a browser wrapper if configured.

`templates/registered.md` is expanded with the name, time, home path and host facts and returned **as the result of the register tool call**, in the same `main` thread. There is no separate registration conversation.

The following model call uses the full life tool set and appends the fresh board after the register result. `self.md` is read for the first panel on every normal registered call (subject to board limits); it is not written by a separate model call or forced by the program. The AI chooses what to do next.

`exit` stops the loop and writes a local outbox notice. Two consecutive onboarding replies without a tool call are treated as exit (the first gets a reminder).

## 4. Later starts and compression

Normal restarts load saved state/history; they do not re-register or replay a new welcome. A pending tool round is repaired with a result-loss notice rather than automatically rerunning its calls. Existing sleeping state resumes against the schedule.

`system.md` is loaded for each request. Welcome and registration messages are stored history, not reloaded on every request; they can be summarized by compression. The terms and letter remain on disk. The board (including the current `self.md`) is freshly rendered and not stored in history or included in the summary request. See `compression.md`.

Tests in `tests/test_life.py` exercise the first two request payloads, absence of automatic letter seeding, and preservation of local letters without injection using a scripted fake model. No live provider is needed.
