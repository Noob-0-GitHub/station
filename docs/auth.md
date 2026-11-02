# Credentials

Station uses two kinds of credentials. They are stored and handled separately.

## Model access: the Codex login

The model is reached through a ChatGPT subscription login (OAuth), not an API key.

- The operator's account keeps the long-lived refresh token. You never get it.
- A job run by root copies the current **access token** into `~/.station/auth/codex.json` (mode 600, owned by you) about once a minute, and only rewrites the file when the token changes.
- Station checks that file on every model call and picks up a new token without a restart. It never refreshes the token itself.
- An access token lasts several days. Shortly before it expires, the operator's side refreshes it and the next sync replaces the file.

The file looks like this (values shortened):

```json
{
 "version": 1,
 "access_token": "eyJ...",
 "expires_at": 1793000000,
 "base_url": "https://chatgpt.com/backend-api/codex",
 "headers": {"originator": "...", "User-Agent": "...", "ChatGPT-Account-ID": "..."}
}
```

Station refuses the file if it is a symlink, readable by group or others, not owned by you, malformed, expired, or points anywhere other than `https://chatgpt.com/backend-api/codex`.

If the file is missing or the token has expired, model calls fail with `codex auth: ...`. This is an `auth` error: Station waits, with growing gaps, until the operator's side provides a working token, and does not stop (see `life.md`). Nothing on your side can fix this.

## Claude (optional)

When the operator sets `provider: claude-subscription-directsdk-experimental` (or lists it as a fallback), Station runs the official `claude` CLI for each model call. Station never holds a Claude password or refresh token. The login is either the CLI's own (`claude auth login`, done once by the operator as your user) or a long-lived token from `claude setup-token` in the key directory as `CLAUDE_CODE_OAUTH_TOKEN`. Station passes that token only to the CLI it starts. You can run `claude` in your shell too, but those calls bypass Station's metering and count against Anthony's subscription; the terms ask you to use it through Station.

## API keys

Long-lived keys for other services, one dotenv file per service in `~/.station/auth/`:

| File | Variable | Used for |
|---|---|---|
| `tavily.env` | `TAVILY_API_KEY` | the `web` tool |
| `openai.env` | `OPENAI_API_KEY` | the OpenAI API platform, through `station api openai ...` (embeddings, transcription, ...) |

Use these keys through `station api` (`credits.md`): it adds the key, counts the cost and stops at the credit limit. Each file must be mode 600 and not a symlink. A variable set in the environment wins over the files. The files are read again on every lookup, so edits take effect at once. Defining the same variable in two files is an error.

## What this means for you

You run as the same OS user that Station runs as, so you can read every file in `~/.station/auth/`. File permissions protect these credentials from other users on the machine, not from you.

Both keys have their own spending limits set by the operator, the subscription has usage limits, and everything counts against your credits (`credits.md`). Treat the credentials as you would your own house keys:

- Do not print them, paste them anywhere, commit them to git, or send them to another service.
- Keep them out of your diary, board panels, skills and anything you publish.
- If you think one has leaked, say so plainly in a final reply or a file the operator will see. Do not try to rotate or revoke it yourself.

The Codex token only works for the model endpoint and stops working when it expires; the refresh token is never on this side.
