#!/usr/bin/env python3
"""Root-side: copy the current Codex access token from Hermes to Station.

Usage (as root):
  scripts/codex-token-sync.py --user station --dest /home/station/.station/auth/codex.json [--model gpt-6.1-sol]
  scripts/codex-token-sync.py ... --check      # report what would change; write nothing

Hermes keeps the refresh token; this job only writes the short-lived access
token, its expiry, the base URL and request headers. Selecting the credential
goes through Hermes's own resolver, which refreshes under Hermes's lock when
the token is within 2 minutes of expiry, so Hermes stays the only refresher.
Run it every minute: each run is also what triggers that refresh when Hermes
itself is idle, and Station's copy of the old token stays valid until then.
`--check` still goes through the resolver, so it can trigger that same
near-expiry refresh; it only never writes the destination file.

The destination directory is owned by the Station user, who could try to plant
symlinks. Every path component is opened with O_NOFOLLOW from `/`, the file is
created with O_EXCL in that directory handle, and renamed into place relative
to the same handle. Writes happen only when the content changes. Output never
contains the token.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from station.codex_auth import FILE_VERSION, CodexAuthError, HermesCodexCredential, parse_token_file  # noqa: E402

from station.tokensync import current_text, open_dir_nofollow, publish, write_atomic  # noqa: E402,F401

HERMES_DEFAULTS = {"hermes_root": "/usr/local/lib/hermes-agent", "hermes_home": "/root/.hermes", "python": None}


def render(cred: dict) -> str:
    body = {"version": FILE_VERSION, "access_token": cred["token"], "expires_at": int(cred["exp"]),
            "base_url": cred["base_url"], "headers": cred["headers"]}
    return json.dumps(body, indent=1, sort_keys=True) + "\n"


def sync(dest: Path, user: str, cred: dict, check: bool = False) -> str:
    text = render(cred)
    parse_token_file(text)  # never publish something Station would reject
    return publish(dest, user, text, check=check)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Sync the Hermes Codex access token to Station.")
    p.add_argument("--user", required=True, help="Station's OS user")
    p.add_argument("--dest", required=True, type=Path, help="absolute path of codex.json")
    p.add_argument("--model", default="gpt-6.1-sol")
    p.add_argument("--hermes-root", default=HERMES_DEFAULTS["hermes_root"])
    p.add_argument("--hermes-home", default=HERMES_DEFAULTS["hermes_home"])
    p.add_argument("--check", action="store_true", help="report only; write nothing")
    args = p.parse_args(argv)
    if os.geteuid() != 0 and not args.check:
        raise SystemExit("run as root (or use --check)")
    try:
        cred = HermesCodexCredential({"hermes_root": args.hermes_root, "hermes_home": args.hermes_home,
                                      "python": None}, args.model).get(force=False)
        result = sync(args.dest, args.user, cred, check=args.check)
    except CodexAuthError as e:
        print(f"codex-token-sync: {e}", file=sys.stderr)
        return 1
    hours = (cred["exp"] - time.time()) / 3600
    print(f"codex-token-sync: {result}; token valid for {hours:.1f} h")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
