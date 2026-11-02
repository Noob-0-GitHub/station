"""Claude through the official Claude Code CLI: provider `claude-subscription-directsdk-experimental`.

The name, the approach and most of the transport come from Hermes's official plugin
claude-subscription-directsdk (NousResearch/hermes-official-plugins, MIT; see
station/licenses/hermes-official-plugins.txt). Station never sees a Claude credential: the `claude`
CLI of the user Station runs as owns its login (`claude auth login`) and refreshes it itself.

One model call is one short-lived `claude -p` process:

- Station's history is replayed in order over stream-json. Earlier user turns are sent with
  `shouldQuery: false` (the CLI acknowledges each with a zero-turn result); the last one is answered.
- The CLI's built-in tools, skills, settings files, MCP servers, session files and autocompaction
  are off. Station's tools are listed through an inert MCP server (station/claude_inert_mcp.py) and
  in the request body; the CLI never runs them. Tool calls come back to Station, which runs them.
- A loopback relay (station/claude_admission.py) forwards only the first Messages request to
  Anthropic and refuses any retry or recovery request the CLI would add, so one call is one request.
- The CLI reports the list-price cost of the call (`total_cost_usd`, `costBasis: list`). Station
  records that as the call's list price instead of its own price table. It is an estimate in API
  dollars, not what the subscription charges.
"""

from __future__ import annotations

import copy
import json
import math
import os
import queue
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from .errors import classify_http, redact
from .llm import ANTHROPIC_BLOCKS, LLMError

PROVIDER = "claude-subscription-directsdk-experimental"
PREFIX = "mcp__station__"  # MCP server "station"; the CLI names its tools mcp__<server>__<tool>
INERT_MCP = Path(__file__).with_name("claude_inert_mcp.py")

# Native routes (from the plugin's model_catalog.py). Behind a custom base URL the CLI uses a 200K
# window unless a 1M model is selected explicitly with `[1m]`.
CONTEXT_WINDOWS = {
    "claude-sonnet-5-5": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    "claude-haiku-5-5": 1_000_000,
    "claude-haiku-4-5-20251001": 200_000,
    "claude-opus-5-5": 1_000_000,
    "claude-opus-5": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-fable-5-1": 1_000_000,
}
ALIASES = {"sonnet": "claude-sonnet-5-5", "haiku": "claude-haiku-5-5", "claude-haiku-4-5": "claude-haiku-4-5-20251001",
           "opus": "claude-opus-5-5", "fable": "claude-fable-5-1"}
NO_ADAPTIVE_THINKING = {"claude-haiku-4-5-20251001"}
EFFORTS = {"low", "medium", "high", "xhigh", "max"}

# Errors the CLI answers itself, without an upstream HTTP status.
NATIVE_ERROR_KIND = {
    "rate_limit": "quota",  # a plan or session limit: wait for it to reset, do not count retries
    "billing_error": "quota",
    "authentication_failed": "auth",
    "overloaded": "transient",
    "server_error": "transient",
}
# Never inherited by the CLI: they would switch it to API-key billing or another backend.
STRIP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_FOUNDRY_API_KEY",
             "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
             "CLAUDE_CODE_EXTRA_BODY", "CLAUDE_CODE_EFFORT_LEVEL")
LOGIN_HINT = ("Claude Code has no usable login for the user Station runs as. The operator must run "
              "`claude auth login` as that user.")


def native_model(model: str) -> str:
    base = model.removesuffix("[1m]")
    canonical = ALIASES.get(base, base)
    window = CONTEXT_WINDOWS.get(canonical)
    if window == 1_000_000:
        return canonical + "[1m]"
    return canonical if window else model


def resolve_command(command: str | None, env: dict) -> list[str] | None:
    head = command or "claude"
    if os.path.isabs(head):
        return [head] if os.access(head, os.X_OK) else None
    found = shutil.which(head, path=env.get("PATH") or os.defpath)
    if not found and head == "claude" and env.get("HOME"):
        found = shutil.which("claude", path=os.pathsep.join(
            os.path.join(env["HOME"], p) for p in (".local/bin", ".claude/local", "bin", ".npm-global/bin")))
    return [found] if found else None


# ---- history ------------------------------------------------------------------


def _clean(block: dict) -> dict:
    return {k: v for k, v in block.items() if k != "cache_control"}


def frames(messages: list, names: set[str]) -> list[dict]:
    """Station history (Anthropic-style blocks) as stream-json frames for the CLI."""
    out: list[dict] = []
    for m in messages:
        role = m["role"]
        blocks = []
        for b in m["content"]:
            kind = b.get("type")
            if kind not in ANTHROPIC_BLOCKS:
                continue  # e.g. another provider's reasoning; tool turns without thinking are accepted (live-checked)
            if kind == "text" and not b.get("text"):
                continue
            b = _clean(b)
            if kind == "tool_use":
                b["name"] = PREFIX + b["name"] if b["name"] in names else b["name"]
            if kind == "tool_result" and isinstance(b.get("content"), list):
                b["content"] = [_clean(x) for x in b["content"]]
            blocks.append(b)
        if not blocks:
            continue
        if out and out[-1]["type"] == role:
            out[-1]["message"]["content"].extend(blocks)
        else:
            out.append({"type": role, "message": {"role": role, "content": blocks}})
    if not out or out[-1]["type"] != "user":
        raise LLMError("history must end with a user message", "bug")
    return out


def tool_specs(tools: list) -> tuple[list, list, set[str]]:
    manifest, wire, names = [], [], set()
    for t in tools:
        schema = dict(t.get("input_schema") or {"type": "object"})
        for key in ("oneOf", "allOf", "anyOf"):
            schema.pop(key, None)
        schema.setdefault("type", "object")
        if schema["type"] == "object" and not isinstance(schema.get("properties"), dict):
            schema["properties"] = {}
        manifest.append({"name": t["name"], "description": t.get("description", ""), "inputSchema": schema})
        wire.append({"name": PREFIX + t["name"], "description": t.get("description", ""), "input_schema": schema})
        names.add(t["name"])
    return manifest, wire, names


# ---- the client -----------------------------------------------------------------


class ClaudeCode:
    def __init__(self, cfg: dict):
        c = cfg.get("claude_code") or {}
        self.model = cfg["model"]
        self.native = native_model(self.model)
        self.effort = cfg.get("effort")
        if self.effort is not None and self.effort not in EFFORTS:
            raise LLMError(f"effort must be one of {', '.join(sorted(EFFORTS))} for {PROVIDER}", "config")
        self.max_tokens = cfg.get("max_tokens")
        self.command = c.get("command")
        self.config_dir = c.get("config_dir")
        self.timeout = float(c.get("timeout") or 600)
        self.telemetry = c.get("telemetry", True)
        self.workdir = Path(c["workdir"]).expanduser() if c.get("workdir") else None
        self.cfg = cfg

    # The CLI writes its working directory into every request; one stable directory keeps the
    # prompt cache prefix the same from call to call.
    def _workdir(self) -> str:
        path = self.workdir or Path(tempfile.gettempdir()) / f"station-claude-cwd-{os.getuid()}"
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = os.lstat(path)
        if not path.is_dir() or path.is_symlink() or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise LLMError(f"{path} must be a private directory owned by Station's user", "config")
        return str(path)

    def _env(self) -> dict:
        from . import secrets

        env = {k: v for k, v in os.environ.items() if k not in STRIP_ENV}
        # A token from `claude setup-token` in secrets.dir (CLAUDE_CODE_OAUTH_TOKEN) lets one login serve
        # every OS user; without it the CLI uses its own login of the user Station runs as.
        try:
            token = secrets.load(self.cfg.get("secrets", {}).get("dir")).get("CLAUDE_CODE_OAUTH_TOKEN")
        except secrets.SecretError as e:
            raise LLMError(f"secrets: {e}", "config") from None
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
        if self.config_dir:
            env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(self.config_dir)
        env.update(ENABLE_TOOL_SEARCH="false", CLAUDE_CODE_MAX_RETRIES="0", DISABLE_AUTO_COMPACT="1",
                   DISABLE_COMPACT="1", CLAUDE_CODE_TOTAL_TOKENS_REMINDER="off",
                   DISABLE_AUTOUPDATER="1", DISABLE_FEEDBACK_COMMAND="1")
        if not self.telemetry:
            env.update(CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_TELEMETRY="1", DISABLE_ERROR_REPORTING="1")
        return env

    def _body(self, wire_tools: list, tool_choice: str | None) -> dict:
        body: dict = {"tools": wire_tools}
        if tool_choice == "none":
            body["tool_choice"] = {"type": "none"}
        if self.max_tokens:
            body["max_tokens"] = int(self.max_tokens)
        if self.effort:
            if ALIASES.get(self.model, self.model).removesuffix("[1m]") not in NO_ADAPTIVE_THINKING:
                body["thinking"] = {"type": "adaptive"}
            body["output_config"] = {"effort": self.effort}
        return body

    def call(self, system: list, tools: list, messages: list, tool_choice: str | None = None) -> dict:
        from .claude_admission import Admission

        manifest, wire_tools, names = tool_specs(tools)
        replay = frames(messages, names)
        body = json.dumps(self._body(wire_tools, tool_choice), separators=(",", ":"), allow_nan=False)
        env = self._env()
        command = resolve_command(self.command, env)
        if command is None:
            raise LLMError("Claude Code is not installed (no `claude` on PATH); "
                           "install it with `npm install -g @anthropic-ai/claude-code`", "config")
        admission = proc = reader = None
        try:
            with tempfile.TemporaryDirectory(prefix="station-claude-") as tmp:
                root = Path(tmp)
                (root / "tools.json").write_text(json.dumps(manifest, ensure_ascii=False))
                (root / "system.md").write_text("\n\n".join(b["text"] for b in system if b.get("text")))
                (root / "settings.json").write_text(json.dumps({"env": {"CLAUDE_CODE_EXTRA_BODY": body}}))
                mcp = {"mcpServers": {"station": {"command": sys.executable,
                                                  "args": ["-I", str(INERT_MCP), str(root / "tools.json")]}}}
                admission = Admission("https://api.anthropic.com", self.timeout,
                                      queried=replay[-1]["message"]["content"])
                env["ANTHROPIC_BASE_URL"] = admission.url
                if self.max_tokens:
                    env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(int(self.max_tokens))
                argv = command + [
                    "-p", "--model", self.native, "--input-format", "stream-json", "--output-format", "stream-json",
                    "--verbose", "--include-partial-messages", "--tools", "", "--system-prompt-file", str(root / "system.md"),
                    "--settings", str(root / "settings.json"), "--setting-sources", "", "--strict-mcp-config",
                    "--disable-slash-commands", "--max-turns", "1", "--permission-mode", "dontAsk",
                    "--no-session-persistence", "--mcp-config", json.dumps(mcp)]
                if self.effort:
                    argv += ["--effort", self.effort]
                proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        text=True, encoding="utf-8", cwd=self._workdir(), env=env, start_new_session=True)
                events: queue.Queue = queue.Queue()

                def read():
                    try:
                        for line in proc.stdout:
                            if line.strip():
                                events.put(json.loads(line))
                    except Exception as e:  # a stray line that is not JSON: the call fails, Station retries
                        events.put(e)
                    finally:
                        proc.wait()
                        events.put(None)

                reader = threading.Thread(target=read, daemon=True)
                reader.start()
                return self._converse(proc, events, replay, admission, names)
        except BrokenPipeError:
            raise LLMError("Claude Code exited while Station was sending the history", "transient") from None
        except OSError as e:
            raise LLMError(f"Claude Code could not be run: {e}", "transient") from None
        finally:
            if proc is not None and proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            if admission is not None:
                admission.close()
            if proc is not None:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                for pipe in (proc.stdin, proc.stdout):
                    if pipe and not pipe.closed:
                        try:
                            pipe.close()
                        except OSError:
                            pass
            if reader is not None:
                reader.join(timeout=5)

    def _converse(self, proc, events, replay, admission, names) -> dict:
        deadline = [time.monotonic() + self.timeout]

        def receive():
            while True:
                remaining = deadline[0] - time.monotonic()
                if remaining <= 0:
                    raise LLMError(f"Claude Code sent nothing for {self.timeout:g} s", "transient")
                try:
                    event = events.get(timeout=min(remaining, 0.5))
                except queue.Empty:
                    continue
                if isinstance(event, Exception):
                    raise LLMError(f"Claude Code wrote something that is not stream-json: {str(event)[:200]}", "transient")
                deadline[0] = time.monotonic() + self.timeout
                return event

        for i, frame in enumerate(replay):
            last = i == len(replay) - 1
            if frame["type"] == "user" and not last:
                frame = {**frame, "shouldQuery": False}
            proc.stdin.write(json.dumps(frame, ensure_ascii=False, allow_nan=False) + "\n")
            proc.stdin.flush()
            if frame.get("shouldQuery") is False:
                while True:
                    ack = receive()
                    if ack is None:
                        raise LLMError("Claude Code exited while replaying the history", "transient")
                    if ack.get("type") == "result":
                        if ack.get("num_turns") != 0 or ack.get("is_error"):
                            raise LLMError("this Claude Code version does not accept history replay", "config")
                        break
        proc.stdin.close()

        assistants, results, stopped = [], [], False
        native_error = native_code = None
        while True:
            event = receive()
            if event is None:
                break
            kind = event.get("type")
            if kind == "assistant":
                if event.get("error") or event.get("message", {}).get("error"):
                    native_error = "\n".join(b.get("text", "") for b in event.get("message", {}).get("content", [])
                                             if b.get("type") == "text")
                    native_code = event.get("error")
                else:
                    assistants.append(event["message"])
            elif kind == "result":
                results.append(event)
            elif kind == "stream_event" and event.get("event", {}).get("type") == "message_stop":
                stopped = True
        proc.wait(timeout=max(0.1, deadline[0] - time.monotonic()))

        if admission.used:
            if admission.status != 200 or not admission.capture.complete:
                said = admission.error_text()[:500]
                status = admission.status if isinstance(admission.status, int) and admission.status >= 400 else None
                text = (f"Claude HTTP {status}: {said}" if status else
                        f"Claude response incomplete (relay: {admission.failure or 'stream cut'})")
                kind = classify_http(status, said) if status else "transient"
                raise LLMError(redact(text), kind, status=status)
            assistants = [admission.capture.message]
            stopped = True
        handled = admission.denied or (admission.used and assistants[0].get("stop_reason") == "refusal")
        if native_error and not handled:
            if native_code == "authentication_failed" and not admission.used:
                raise LLMError(f"{LOGIN_HINT} ({native_error[:200]})", "auth")
            raise LLMError(f"Claude Code: {native_error[:500]}", NATIVE_ERROR_KIND.get(str(native_code), "transient"))
        if len(results) != 1 or not assistants or not stopped:
            raise LLMError("Claude Code returned an incomplete response", "transient")
        final = results[0]

        blocks = [copy.deepcopy(b) for a in assistants for b in a.get("content") or []]
        calls = False
        for b in blocks:
            if b.get("type") == "tool_use":
                calls = True
                b["name"] = b["name"].removeprefix(PREFIX)
        boundary = calls and final.get("subtype") == "error_max_turns" and proc.returncode == 1
        if not boundary and not handled and (proc.returncode != 0 or final.get("is_error")
                                             or final.get("subtype") != "success"):
            raise LLMError(f"Claude Code request failed: {final.get('subtype')}", "transient")
        usage = assistants[0].get("usage") if admission.used else final.get("usage")
        if not isinstance(usage, dict) or not all(isinstance(usage.get(k), (int, float)) for k in ("input_tokens", "output_tokens")):
            raise LLMError("Claude Code returned no token usage", "transient")

        content = [b for b in blocks if b.get("type") in ANTHROPIC_BLOCKS]
        refused = any(a.get("stop_reason") == "refusal" for a in assistants)
        if refused:  # a tool call cut off by a refusal must not run
            content = [b for b in content if b.get("type") != "tool_use"]
            details = next((a["stop_details"] for a in assistants if isinstance(a.get("stop_details"), dict)), {})
            reason = details.get("explanation") or details.get("category") or "no reason given"
            content.append({"type": "text", "text": f"[The model refused to answer: {reason}]"})
            calls = False
        out = {
            "input_tokens": usage.get("input_tokens") or 0,
            "output_tokens": usage.get("output_tokens") or 0,
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens") or 0,
            "cache_read_input_tokens": usage.get("cache_read_input_tokens") or 0,
        }
        if isinstance(usage.get("cache_creation"), dict):
            out["cache_creation"] = usage["cache_creation"]
        cost, by_model = final.get("total_cost_usd"), final.get("modelUsage") or {}
        if (isinstance(cost, (int, float)) and not isinstance(cost, bool) and math.isfinite(cost) and cost >= 0
                and by_model and all(isinstance(v, dict) and v.get("costBasis") == "list" for v in by_model.values())):
            out["list_usd"] = float(cost)  # the CLI's own list-price figure for this call
        long = any(a.get("stop_reason") in ("max_tokens", "model_context_window_exceeded") for a in assistants)
        stop = "tool_use" if calls else "max_tokens" if long else "end_turn"
        return {"content": content or [{"type": "text", "text": ""}], "usage": out, "stop_reason": stop}
