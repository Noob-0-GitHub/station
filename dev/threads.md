# Focus and threads

A thread is a separate context for one thing the AI wants to focus on. `main` is the everyday thread and cannot close.

- `focus action=new title=… [goal=…]` creates a thread whose parent is the current one and switches to it.
- `focus action=switch id=…` switches to another active thread.
- `focus action=close [context=…]` closes the current thread and returns to the nearest active ancestor with the context (stored as the thread's `report`; `report=` is still accepted as the old name). Active children are re-parented to that ancestor.

Each thread has its own history in `state/threads/<id>.jsonl`. A switch replaces the context with the target's history; nothing from the old thread is copied. That keeps each context small and its prefix cacheable. What should survive a switch belongs in the diary, todo, or board, which every thread sees.

- A new thread starts with one environment message: title, goal, where it came from, the context, a reminder that the old conversation is not visible, and up to `memory.recall_on_new` (3) memories found by title + goal + context (`Life.recall`). Recall skips the request when memory is empty, gives the search by meaning `memory.recall_timeout` (10 s) before falling back to words, and on any error logs it and adds nothing.
- Returning to a thread appends a note (where the AI came from, or the closed child's report) to that thread's last message.
- `new`, `switch` and `close` take optional `context` (at most `CONTEXT_CHARS`, 2,000): a short handoff the AI writes, added to the destination message as `Context from <id>: ...`. Model/view actions do not carry handoff context. Like a subagent's task line, it is the only conversation text copied between threads; a closed thread also stores it as `report`. Longer text is refused, not cut, so the AI knows to use a file.
- Events that arrive during a switch are delivered in the destination thread.

Each thread has its own wake interval (see `schedule.md`). A thread that never set one uses `schedule.default_minutes`.

Each thread is compressed on its own when its history gets long (`compression.md`).
