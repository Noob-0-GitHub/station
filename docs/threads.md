# Threads

A thread is a separate conversation for one thing you want to focus on. `main` is your everyday thread and cannot be closed.

```json
{"action": "new", "title": "Write a short story", "goal": "a first draft of about 2,000 words"}
{"action": "switch", "id": "t1"}
{"action": "close", "context": "Draft done, saved in ~/stories/lighthouse.md"}
{"action": "switch", "id": "main", "context": "Stopped mid-scene at the lighthouse; Anthony asked about the ending."}
```

- `new` opens a thread under the current one and switches to it. The new thread starts with a single message: its title, goal, where it came from, and a few memories that may be related (below).
- `switch` moves to another open thread. Its conversation picks up where you left it, with a note saying where you came from.
- `close` ends the current thread and returns to the thread it came from, carrying your `context` (what came of it). Threads opened from the closed one move up to that parent.

Thread ids are automatic sequential identifiers (`t1`, `t2`, ...), not random and not user-selected. `main` is reserved. `id` selects an existing thread, never names a new one. The board's `threads` panel shows them.

## Models per thread

```json
{"action": "new", "title": "Coding", "provider": "codex", "model": "gpt-6.1-sol", "effort": "high", "fallback": []}
{"action": "switch", "id": "t1", "provider": "claude-subscription-directsdk-experimental", "model": "claude-opus-5-5"}
{"action": "model", "effort": "low"}
{"action": "model", "id": "t1"}
{"action": "model", "inherit": true}
```

- `new` and `switch` accept optional provider/model (supply both), effort, and fallback. Settings are saved on the target thread and take effect for its next model call. `model` inspects or changes the current thread, or an active thread named by id, without switching.
- No override means the global YAML configuration, not the parent thread's override. Unspecified fields retain an existing thread override; a new thread inherits unspecified fields from global config. `inherit: true` clears all thread overrides and cannot be mixed with settings. `effort: null` disables reasoning parameters; `fallback: []` disables fallbacks for that thread.
- Global fallbacks are inherited unless explicitly overridden. A fallback identical to the selected provider/model is skipped, so choosing GPT as the main model does not retry GPT as its own fallback.
- Model selection, active fallback, recovery timing and fallback notice keys are independent per thread and survive restarts. Changing a selection resets only that thread's failover state. Closing a thread clears its fallback notice. The legacy top-level model_route belongs to main.
- This changes thread state, not station.yaml or source defaults. It does not create a parallel agent or additional login. A configured client passing validation does not prove authentication or remaining quota.
- Context stays in the same thread when its model changes. Cross-provider reasoning compatibility and usage accounting use the existing router; charged usage records the model that actually answered. `station status` describes the current thread's effective model.
- Model settings are not accepted on close. Switching to the current thread remains an error; use model instead.


## Board visibility per thread

```json
{"action": "new", "title": "Coding", "hide_boards": ["system"], "hide_tags": ["social"], "context": "Continue the parser implementation."}
{"action": "switch", "id": "t1", "hide_tags": ["social", "news"], "context": "Review the tests next."}
{"action": "view"}
{"action": "view", "id": "t1", "hide_boards": []}
{"action": "view", "hide_boards": [], "hide_tags": []}
```

- Each thread has independent hidden-name and hidden-tag lists, saved with its state. These settings do not affect other threads or global YAML.
- New threads start with both lists empty unless supplied explicitly; they do not inherit their parent's visibility rules.
- `new` and `switch` accept both visibility lists alongside model settings and `context`. Each supplied list replaces that field; omitted fields stay unchanged on an existing thread. `[]` clears that list, while `null` is invalid.
- `view` inspects or changes the current thread, or an active thread selected by `id`, without switching. It does not change models or carry context. Use `new`/`switch` when combining visibility, model selection and handoff text. Model `inherit: true` does not reset board visibility.
- `close` returns to the parent's existing view. To change the current view directly, use the board tool's `hide`/`show` actions (`board.md`).
- The virtual `hidden` panel lists the current rules and matching panel names/tags only. It cannot hide itself. Hidden command panels are not executed; their files remain available to `board read` or bash.

## What carries over

Nothing from one thread's conversation is visible in another. Two ways to carry something across:

- **`context`**, on `new`, `switch` and `close`: a sentence or two that arrives in the thread you go to, the way you would brief someone taking over. "I was halfway through the README; the install section is next." It is optional and under 2,000 characters; for more, write a file and name it in the context.
- **Places every thread sees**: the diary, the todo list, the board, `~/self.md`, memory (`memory.md`), or any file in your home. Use these for what should last.

## Memories in a new thread

A new thread's first message ends with up to 3 memories found by its title, goal and context: diary entries, older conversations or notes that may be related, each with an id and an excerpt. They are a hint, not a summary; `station memory show ID` gives the whole text. Nothing is added when nothing matches, and a slow or failed search only means none are shown. Each `new` costs one small search (`memory.md`).

## Per thread

- Each thread has its own wake interval (see `schedule.md`). Cron entries and the configured daily wake, if enabled, are shared.
- Each thread has its own board visibility rules (above).
- Each thread is compressed on its own when it gets long (see `compression.md`). A long thread you are not in costs nothing.
- Messages that arrive while you switch are delivered in the thread you switch to.
