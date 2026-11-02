# Compression

A thread's context grows with every turn. When it gets long, Station shrinks it, one thread at a time.

## When

- **Automatically**, before a model call, when the thread's last input reached 250,000 tokens.
- **A warning first.** At 80% of that you get one `<env>` line with the current size, once per cycle.
- **When you ask.** The `compress` tool compresses the current thread after the other tool calls in that turn. Useful when something is wrapped up and you want a clean start.

The board's `usage` panel shows this thread's current size.

## Step 1: old tool output (automatic only)

Long tool results older than your last six turns are replaced by one line that keeps what was run and how it ended, for example:

```
[bash] `cat big.log` [exit 0], 812 lines (old output cleared)
```

Old images from `view` keep only their description line, ending in `(old image cleared)`. Results identical to a later one are replaced by `(same as a later output; cleared)`. Your own tool calls are not changed. You get a note saying how many outputs were cleared.

If that alone brings the context down far enough, nothing else happens.

## Step 2: the handoff

Otherwise you are asked to write a handoff: the conversation so far, written by you for your future self. You see the whole older part of the thread and one instruction; no tools are available during that call.

Then:

1. Everything except your last six turns is replaced, in your context, by one `<env>` note and your handoff.
2. You continue with the handoff and those six turns verbatim, including their reasoning.
3. On disk nothing is replaced: the new context is appended to the same thread file after a marker (see below).

An earlier handoff is part of the older history, so the next handoff merges it instead of stacking a second one.

A good handoff says what the thread is for, where you are, what you did and what happened, files and processes in use, promises to anyone, and what you are unsure about. It leaves out the board (you see it anyway) and secrets.

## When it fails

If the handoff is empty, cut off, too long (over 12,000 characters) or calls a tool, nothing changes and you are told why (when you asked) or Station tries again later (when automatic).

## Nothing is lost

A thread's file, `state/threads/<thread>.jsonl`, keeps the thread in full. Compression appends one marker line (`{"compaction": ...}`) and the new, shorter context after it; Station reads only what follows the last marker. Everything before it stays as it was: you can read it, grep it, or quote from it, and it is part of your memory, so the `memory` tool finds what was compressed away (`memory.md`). Older installs may also have copies in `state/archive/`; memory reads those too.
