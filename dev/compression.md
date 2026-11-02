# Compression

Each thread is compressed on its own. When a thread's history gets long, Station first tries to clean up old tool outputs, which needs no model call. If that is not enough, the model writes a handoff of the older part, and that part is replaced by the handoff. The most recent turns stay verbatim. Research behind this design: `research/compression.md`.

## When

- **Automatic.** Before each model call, if the thread's last reported input (`input + cache read + cache write`) reached `compress.threshold_tokens` (default 250,000), Station compresses that thread first. The board shows the current size.
- **A hint first.** Once the thread reaches `warn_ratio` (80%) of the threshold, the next message gets one `<env>` line with the size, the threshold, and that `compress` exists. Once per compression cycle, not on the board.
- **The `compress` tool.** The AI can compress early, for example when something is finished. An optional `focus` names what the handoff must keep. Compression runs after the other tool calls of that turn.

Only the current thread is checked. A long thread that the AI is not in costs nothing and is compressed when the AI returns to it.

250,000 leaves headroom under the 272,000-token window that the Codex subscription advertises for `gpt-6.1-sol`: one tool output is capped at 12,000 characters and the board at 6,000. If you raise the threshold, check the window of the model and route you use.

## Step 1: clean up old tool outputs

Borrowed from Hermes (`agent/context_compressor.py`, `_prune_old_tool_results`). Only automatic compression does this step; the `compress` tool always writes a handoff.

Outside the last `keep_cycles` assistant turns:

- A tool result longer than `prune_min_chars` (default 2000) becomes one line that keeps what was run and how it ended, for example ``[bash] `cat big.log` [exit 0], 812 lines (old output cleared)`` or `[web extract] https://…, 5321 chars (old output cleared)`. Failed results are marked.
- A tool result identical to a later one (at least 200 characters) becomes `(same as a later output; cleared)`. The newest copy stays in full, even inside the kept turns.

Tool calls are not changed, so every call keeps its result. The AI sees a note at the end of the next message saying how many outputs were cleaned.

Station keeps this result only if it is enough: it estimates the next input from the characters removed and the tokens per character of the last request, and needs it to fall to `prune_target` (default 0.6) of the threshold, 150,000 with the defaults. Otherwise nothing is written and step 2 runs on the original history (`kind: prune_skipped` in the log). This keeps cleaning from turning into many small cache breaks.

## Step 2: the handoff

1. Split the thread before the last `keep_cycles` assistant turns (default 6). A cycle is one assistant turn with all its tool calls and the message that answers them, so a tool call is never separated from its result.
2. Ask the model for a handoff: the older part of the history, unchanged, plus one instruction (`templates/compress.md`). This reuses the cached prefix. The board is left out. `tool_choice` is `none`.
3. Check the reply. It must be text only, not cut off by `max_tokens`, not empty, and at most `max_summary_chars` (12,000). Any tool call in the reply is never executed.
4. Append a marker line `{"compaction": {"ts", "carried", "method", ...}}` to `state/threads/<thread>.jsonl`, then one environment message (compression count, where the full record is, where the terms are) and the handoff, followed by the kept turns, in one write. `Store.load_history` reads from the end of the file back to the last complete marker line, so loading costs only the live part; a marker cut by a crash does not count. `carried` is the number of messages after the marker that repeat or summarize earlier ones; memory skips them so each message is indexed once. Pruning (step 1) writes a marker the same way, so the full tool outputs stay before it.
5. Reasoning in the kept turns is replayed as it is. If a provider rejects a request that carries old reasoning (`request` error), Station sends it once more with reasoning only on the last assistant turn (`reasoning_dropped` in the log); the file keeps everything.

An earlier handoff is part of the older history, so the next handoff merges it instead of stacking a second one. The instruction asks for that explicitly.

Opaque reasoning (`openai_reasoning`, Anthropic `thinking`) is dropped from kept turns except the last assistant turn. Whether a provider accepts old reasoning after the start of the conversation has changed is not verified, so Station keeps only the reasoning a pending tool loop may need.

## Failure

If the call fails or the reply fails a check, the history stays as it was and the reason goes to `state/log.jsonl` (`kind: compress_failed`). Automatic compression then waits until the thread has grown by another `retry_growth` (10%) before trying again. A failed `compress` tool call tells the AI why in the next message.

If there are not more than `keep_cycles` assistant turns, there is nothing to compress and nothing is called.

## Cost

Step 1 costs nothing but one cache break. Step 2 is one extra model call: its input is the older history, mostly a cache hit when the cache is warm, and its output is the handoff. After either step the next normal call writes a new, shorter cache. Both steps archive the raw thread first.

## Config

```yaml
compress:
  enabled: true            # automatic compression; the tool works either way
  threshold_tokens: 250000
  keep_cycles: 6
  warn_ratio: 0.8          # one hint line at this share of the threshold
  prune_min_chars: 2000    # step 1: tool outputs longer than this become a one-line stub
  prune_target: 0.6        # step 1 is kept only if the estimate falls to this share of the threshold
  max_summary_chars: 12000
  retry_growth: 0.1
```

## Not done

- No proactive cleaning below the threshold. Hermes has it off by default because each pass breaks the cache, and Station's tool outputs are already capped at 12,000 characters.
- No native compaction from the provider (Responses `context_management` or Anthropic compaction). It could come later as an optional step.
