# Memory

Your context holds the current thread, and compression shortens it. Memory is how you find older things again: what you wrote in your diary, what happened in conversations (including the parts compressed away), and notes you saved on purpose.

Use the `memory` tool (`search`, `show`, `add`, `forget`), or the same from the shell:

```bash
station memory search "the poem about the sea"          # by meaning and by words
station memory search "cronsim" --kind chat --since 7d   # only conversations from the last 7 days
station memory show 42                                   # one memory in full, and where it came from
station memory add "Anthony prefers Chinese in chat."    # save a note; '-' reads stdin
station memory forget 57                                 # delete a note
station memory status                                    # what is indexed, and how much is waiting
station memory sync                                      # index new material now
```

## What is in it

| Kind | What | Updated |
|---|---|---|
| `diary` | Each `## HH:MM` entry of your diary | when the file changes |
| `chat` | Every thread's conversation, a few messages per memory, including the parts before compressions (each message once) | when the thread changes; the newest part waits, since it is still in your context |
| `note` | What you saved with `station memory add` | at once |

Tool results are kept short (their start only), and reasoning is left out. A diary entry or note you edit or delete changes memory at the next sync; only notes can be forgotten with `forget`, because diary entries and conversations follow their files.

## How search works

Search runs two ways and merges the results:

- **By meaning.** Every memory was turned into a vector: a list of numbers that places the text by what it is about, so "watched seagulls on the beach" is near "a walk by the sea" even without a shared word. Your query is turned into a vector the same way, and the nearest memories are found.
- **By words.** Exact words and names, in any language, with no network.

Each result says which of the two found it. If search by meaning is unavailable (no network, or the credit limit), you get word matches only, and the output says so.

## Recall in a new thread

When you open a thread with `focus new`, Station searches memory by its title, goal and context and adds up to 3 results to the thread's first message (`threads.md`). `memory.recall_on_new: 0` in `station.yaml` turns it off.

## When it is updated

Each time you fall asleep, Station indexes what is new: up to 200 memories, within a minute. A long backlog is spread over several sleeps; `station memory sync` does it at once.

## Cost and privacy

Turning text into vectors uses OpenAI's embeddings API (`text-embedding-3-small`) through `station api`, so it is counted in your credits. It is cheap: a million tokens costs 0.02 credits, and a busy day of diary and conversation is a few thousand tokens. Each search embeds the query, a few dozen tokens; so does each `focus new`, for its recall.

The text of your diary, conversations and notes is sent to OpenAI to be turned into vectors. Leave a kind out of `memory.sources` in `station.yaml` to keep it out of memory and off the network; `memory.enabled: false` turns memory off. Switching notes off only hides them: notes exist nowhere else, so they are kept and come back when switched on again.

Memory is stored in `state/memory.db` in Station's runtime directory.
