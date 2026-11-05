# Deploying Station on a server (operator runbook)

How Station was deployed on vps1 (P7, 2026-11-02), written so it can be repeated on a new server. Everything here runs as root unless it says `as station`. The files in this directory are the exact ones installed.

## What gets set up

| Piece | Where | Purpose |
|---|---|---|
| user `station` | `/home/station` (700) | the AI's account: no sudo, no docker, no password |
| `station.slice` | `/etc/systemd/system/` | CPU 50%, memory high 700M / max 900M, swap 1G, 256 tasks |
| `station.nft` + `station-nft.service` | `/etc/station/`, `/etc/systemd/system/` | network rules for user `station` |
| browser | `/opt/station-browser/` (root-owned, read-only) | agent-browser + Chromium, copied from Hermes's tools |
| Claude Code CLI | `/opt/station-claude/claude` (root-owned) | the binary for provider `claude-subscription-directsdk-experimental` |
| Station code | `/home/station/station` (Git: `upstream` = releases, `main` = the AI's work) | installed from a release tarball |
| runtime | `/home/station/.station` (`station.yaml`, `state/`, `auth/` 700) | config, history, ledger, keys |
| `station.service` | `/etc/systemd/system/` | starts the launcher; **this is the go-live step** |

Requirements: Debian 12 or similar with systemd and cgroup v2, nftables, Python 3.11+ with venv, git, curl, ffmpeg. `kernel.unprivileged_userns_clone = 1` for Chromium's sandbox as a normal user.

## Several instances on one machine (station@NAME)

New deployments run each AI as its own instance: Unix user `NAME` with `/home/NAME`, service `station@NAME.service` (template `station@.service`), slice `station-NAME.slice` nested under `station.slice` (the limits for all instances together), and its uid in `/etc/station/instances.nft`, which the shared network rule (`station-instances.nft`, installed as `/etc/station/station.nft`) includes. `NAME` is lowercase letters and digits, no dashes (systemd nests slices by dashes).

```bash
deploy/station-provision NAME --dry-run     # shows every change, makes none
deploy/station-provision NAME               # user, slice, uid set, shared units; starts nothing
# then install Station as NAME (step 6 with /home/NAME), keys, logins, `station check`
systemctl enable --now station@NAME         # go live, separately
```

A machine set up with the single-instance files below (`station.nft` naming user `station`, a one-AI `station.slice`) is refused until `--migrate`, which backs those files up as `*.legacy-TIMESTAMP` first. Instances are isolated by user, slice and the network rule, but not from each other's ports 10000 and up. Each instance needs its own Telegram bot and Bilibili account; a model subscription can be shared, along with its rate limits. `station-codex-sync` still targets user `station`; adjust `--user`/`--dest` per instance.

The single-instance steps below are how vps1 was deployed (user `station`, `station.service`, `station.slice`, `station.nft`) and stay valid there.

## Steps

### 1. User

```bash
useradd --create-home --home-dir /home/station --shell /bin/bash --user-group station
chmod 700 /home/station
```

### 2. Resource limits

```bash
install -m 644 deploy/station.slice /etc/systemd/system/
systemctl daemon-reload && systemctl start station.slice
```

Measured on vps1: Chromium through agent-browser peaks at about 490 MB, one `claude -p` at about 220 MB, Station itself about 50 MB. Above `MemoryHigh` the kernel reclaims and swaps (slow, nothing killed); `MemoryMax` is the hard stop. On a bigger server raise both.

Disk is a soft limit inside Station (5 GB for home + runtime, `du` every 10 minutes, model calls paused at 100%), not a filesystem quota.

### 3. Network rules

```bash
install -d -m 755 /etc/station
install -m 644 deploy/station.nft /etc/station/
install -m 644 deploy/station-nft.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now station-nft.service
```

User `station` can reach the internet. On local addresses (loopback and the server's own public addresses), TCP/UDP destination ports below 10000 are rejected; ports 10000 and above are allowed. Private, link-local (cloud metadata) and CGNAT destinations remain blocked. Keep operator services the AI must not reach below 10000. A service such as code-server on 44329 is reachable and must enforce its own authentication.

Claude's relay and Chromium CDP use random high ports. Verify `net.ipv4.ip_local_port_range` on each new server instead of assuming their ports are fixed. This rule no longer inspects cgroups: plain `sudo -u station` works for manual networking tests; use `station.slice` to enforce resource limits. ufw and nftables.service are untouched (separate table `inet station`).

DNS: vps1 uses 8.8.8.8. A local resolver on port 53 would be rejected; review a narrow DNS exception before using this rule on such a server.

Check as station: own high-port listener works; `127.0.0.1:22`, `:6379`, `:8642`, public IP `:22`, and `169.254.169.254:80` fail; external HTTPS remains reachable.

### 4. Secret audit

Before a second user exists, look for world-readable files with secrets:

```bash
find /srv /opt /etc /var/lib /usr/local -xdev -type f -perm -o+r -size -2M \
  | grep -v -E '/(\.git|node_modules|site-packages|dist-packages|target|__pycache__)/' > /root/temp/p7-readable.txt
xargs -a /root/temp/p7-readable.txt -d '\n' grep -l -I -E \
  '(sk-(proj|live|ant)-[A-Za-z0-9_-]{16,}|tvly-[A-Za-z0-9]{16,}|ghp_[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|[0-9]{8,10}:AA[A-Za-z0-9_-]{30,})'
```

On vps1 the matching paths were under hermes-agent tests/docs and quarto JS; the contents were not individually reviewed, so this was not a completed secret audit. `/root` must stay 700.

### 5. Browser and Claude CLI for `station`

```bash
install -d -o root -g root -m 755 /opt/station-browser /opt/station-claude
cp -a /root/.hermes/tools/agent-browser-*-linux-x64 /root/.hermes/tools/chromium-* /opt/station-browser/
chown -R root:root /opt/station-browser && chmod -R go-w,a+rX /opt/station-browser
install -m 755 "$(readlink -f /root/.local/bin/claude)" /opt/station-claude/claude
```

On a server without Hermes, install agent-browser and Chromium (`npx playwright install chromium` or agent-browser's installer) and Claude Code (`npm install -g @anthropic-ai/claude-code`, or the native installer) and copy the binaries the same way. Root-owned copies mean updates to root's tools never break Station and the AI cannot modify them. Update the Claude CLI by re-copying (it does not auto-update there; Station sets `DISABLE_AUTOUPDATER=1`).

### 6. Install Station (as station)

```bash
cd /root/CodeProjects/station && .venv/bin/python scripts/package.py --out /root/temp/station-release
install -o station -g station -m 644 /root/temp/station-release/station-*.tar.gz /home/station/
systemd-run --quiet --wait --pipe --slice=station.slice --uid=station --gid=station \
  -p WorkingDirectory=/home/station -E HOME=/home/station -E PATH=/usr/local/bin:/usr/bin:/bin \
  /bin/bash -c 'mkdir -p ~/releases && tar xzf ~/station-*.tar.gz -C ~/releases && rm ~/station-*.tar.gz \
                && ~/releases/station-*/install.sh ~/station'
```

`install.sh` creates the venv (dependencies from PyPI, pinned in `requirements.lock`), the Git repository and `~/.station`. Then put the production config in place (it is not code; change the model here, never in `station/config.py`):

```bash
install -o station -g station -m 600 deploy/station.yaml.example /home/station/.station/station.yaml
```

### 7. Keys

```bash
for f in openai.env tavily.env telegram.env; do
  install -o station -g station -m 600 /root/.local/share/secrets/station/$f /home/station/.station/auth/
done
```

The AI can read these (same user); that is accepted and stated in its docs. The deployed fallback is Codex / gpt-6.1-sol. Install its root-owned access-token sync units (refresh tokens stay with Hermes):

```bash
install -m 644 deploy/station-codex-sync.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemctl start station-codex-sync.service
systemctl enable --now station-codex-sync.timer
```

The timer invokes Hermes's resolver every minute and may refresh its OAuth grant, but performs no model inference. Verify `Result=success`, token file owner station/mode 600, and timer state active/waiting. Never dump the token file. On a new server update the root repository/runtime paths in the service and configure the root Codex account first.

### 8. Logins (Anthony, interactive)

Both are needed before the start.

Claude, one of:

- A token from `claude setup-token`, stored as `CLAUDE_CODE_OAUTH_TOKEN` in `/home/station/.station/auth/claude-code.env` (600, owner station). On vps1 Anthony configured it in root's `~/.claude/settings.json`; a copy is installed for Station. Its entitlement/expiry have not been inference-tested; Anthony reports the Opus allowance is exhausted. Do not assume validity or send more probes while exhausted.
- or `systemd-run --pty --slice=station.slice --uid=station -E HOME=/home/station /opt/station-claude/claude auth login` (the login stays in `/home/station/.claude`; the CLI refreshes it).

The provider disables user settings via `--setting-sources ""`, so root's settings are not automatically inherited by station. Station loads the token from `secrets.dir` and passes it to its child CLI. A bare `claude auth status` without that environment does not verify this bridge.

Bilibili (station-bili, `/root/CodeProjects/station-bili`): copy it to the station user, run its `install.sh` as station, then `~/bin/bili login` and scan the QR code.

### 9. Install the unit, then start separately (go live)

```bash
install -m 644 deploy/station.service /etc/systemd/system/
systemctl daemon-reload
# STOP HERE until Bilibili login and explicit go-live approval.
# systemctl enable --now station.service
```

The first run is the welcome screen and registration; after that the AI lives on its schedule. Watch with `sudo -u station -H ~/station/.venv/bin/station --root ~/.station status` and `journalctl -u station -f`. Stop: `systemctl disable --now station`.

## Checks after steps 1–7

```bash
sudo -u station -H bash -c 'cd ~ && ~/station/.venv/bin/station --root ~/.station check'    # "ok"
sudo -u station -H bash -c 'cd ~ && ~/station/.venv/bin/station --root ~/.station status'
```

and a browser round trip inside the slice (open a page, `get title`, `screenshot`, `view` the PNG). On vps1: title `Example Domain`, screenshot 1280x577, slice memory peak 486 MB.

## Undo

```bash
systemctl disable --now station.service station-nft.service station-codex-sync.timer
rm /etc/systemd/system/station{,-nft}.service /etc/systemd/system/station-codex-sync.{service,timer} /etc/systemd/system/station.slice && systemctl daemon-reload
rm -rf /etc/station /opt/station-browser /opt/station-claude
userdel -r station        # deletes everything the AI made: back up /home/station first
```

## Moving to a new server

Back up `/home/station` (the AI's home, code repository with its own commits, `.station` runtime with history, memory and ledger) with ownership preserved, for example `tar --numeric-owner -cpzf`. On the new server do steps 1–5, restore the home (uid may differ: `chown -R station:station /home/station`), re-create the venv if Python changed (`~/station/install.sh` from the same release, or `python3 -m venv --clear` + `pip install -r requirements.lock`), redo the logins that are tied to the machine (Bilibili cookies usually move with the home; Claude: a setup-token moves, a `claude auth login` should be redone), then step 9.

## Current deployment gate

Station is installed with main Opus 5.5 and GPT-6.1 Sol fallback. station.service is installed but disabled/inactive; the Codex sync timer alone is enabled. Bilibili login and explicit go-live approval remain outstanding. Telegram API access is available, but no automatic inbox/outbox bridge is implemented. No live failover was exercised in this deployment step.
