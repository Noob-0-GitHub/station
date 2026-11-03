"""The life loop: onboarding, threads, model calls, tools, and sleeping between turns."""

from __future__ import annotations

import copy
import json
import os
import pwd
import platform
import re
import shlex
import shutil
import signal
import socket
import string
import sys
import time
import traceback
from importlib import resources
from pathlib import Path

import yaml

from . import clock
from . import credits as credits_mod
from . import config as config_mod
from . import web
from . import errors as errors_mod
from .board import Board
from . import board_view
from .board_recall import BoardRecall
from .inbox import Inbox
from .llm import LLMError
from .notes import Diary, Todo
from .notices import Notices
from . import vision
from .prune import prune, text_size
from . import reload as reload_mod
from .runner import Runner, clip, safe_read
from .schedule import Cron, due, next_wake
from .skills import Skills
from .store import Store
from .tools import LIFE_TOOLS, ONBOARDING_TOOLS

RESERVED_NAME = re.compile(
    r"gpt|openai|claude|anthropic|gemini|bard|codex|copilot|llama|deepseek|qwen|kimi|grok|mistral|kiro|hermes"
    r"|assistant|助手|机器人|^(sol|luna|terra|astra|opus|sonnet|haiku|fable|mythos|station|ai|bot)$",
    re.I,
)

CONTEXT_CHARS = 2000  # focus `context`: a line or a short paragraph carried into the next thread
RECALL_CHARS = 300  # each memory recalled into a new thread's first message, at most


def template(*parts: str) -> str:
    return resources.files("station").joinpath("templates", *parts).read_text()


def now_text(ts: float | None = None) -> str:
    return clock.at(ts or time.time()).strftime("%Y-%m-%d %a %H:%M %Z")


def clock_text(ts: float) -> str:
    t = clock.at(ts)
    return t.strftime("%H:%M" if t.date() == clock.today() else "%m-%d %H:%M")


def env_text(text: str) -> dict:
    return {"type": "text", "text": f"<env>{text}</env>"}


REASONING_BLOCKS = {"openai_reasoning", "thinking", "redacted_thinking"}


def has_old_reasoning(messages: list[dict]) -> bool:
    last = max((i for i, m in enumerate(messages) if m["role"] == "assistant"), default=-1)
    return any(b.get("type") in REASONING_BLOCKS for i, m in enumerate(messages)
               if m["role"] == "assistant" and i != last for b in m["content"])


def strip_old_reasoning(messages: list[dict]) -> list[dict]:
    """Drop opaque reasoning from every assistant turn except the last one.

    Reasoning is kept and replayed by default, also after compression. This is the
    fallback for a provider that rejects a request because of old reasoning: the
    request is sent once more without it (history on disk keeps it either way).
    """
    last = max((i for i, m in enumerate(messages) if m["role"] == "assistant"), default=-1)
    out = []
    for i, m in enumerate(messages):
        if m["role"] == "assistant" and i != last:
            kept = [b for b in m["content"] if b.get("type") not in REASONING_BLOCKS]
            m = {**m, "content": kept or [{"type": "text", "text": "…"}]}
        out.append(m)
    return out


def name_problem(name: str) -> str | None:
    if not 1 <= len(name) <= 20 or any(ch in name for ch in "\n\r\t<>"):
        return "A name must be 1 to 20 characters, on one line, without < or >."
    if RESERVED_NAME.search(name):
        return f"\"{name}\" looks like an AI product or model name. Choose something that sounds like a person's name."
    return None


def host_facts(path) -> dict:
    os_name = platform.system()
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                os_name = line.split("=", 1)[1].strip().strip('"')
                break
    except OSError:
        pass
    mem = "?"
    try:
        line = next(x for x in Path("/proc/meminfo").read_text().splitlines() if x.startswith("MemTotal"))
        mem = f"{int(line.split()[1]) / 1024 / 1024:.1f} GB"
    except (OSError, StopIteration, ValueError):
        pass
    return {
        "host": socket.gethostname(),
        "os": os_name,
        "cpus": os.cpu_count() or "?",
        "mem": mem,
        "disk_free": f"{shutil.disk_usage(path).free / 1e9:.0f} GB",
    }


class Stop(Exception):
    pass


class Restart(Exception):
    """Changed code or config passed `station check`; the caller should exec a new process."""


PLANNED_EXITS = {"reload", "max_calls"}  # ended on purpose (hot reload, a bounded dev run): no wake, no notice


class Life:
    def __init__(self, root, echo=print):
        self.root = Path(root)
        self.cfg = config_mod.load(self.root)
        self.echo = echo
        self.store = Store(self.root / "state")
        self.inbox = Inbox(self.root / "inbox")
        self.errors = errors_mod.ErrorLog(self.root / "state")
        self.notices = Notices(self.root / "state")
        # Dev: <root>/home. Production: the user's home, with <root> = ~/.station (config `home`).
        self.home = Path(self.cfg["home"]).expanduser() if self.cfg.get("home") else self.root / "home"
        self.runner = Runner(self.home, user=self.cfg["runner"]["user"], max_output=self.cfg["runner"]["max_output"],
                             env={"STATION_ROOT": str(self.root.resolve())})
        self.diary = Diary(self.home / "diary", self.runner, self.home)
        self.todo = Todo(self.home / "todo.json", self.runner, self.home)
        self.skills = Skills(self.home / "skills", self.home)
        self.board = Board(self.home / "board", self.runner, base=self.home,
                           panel_max_chars=self.cfg["board"]["panel_max_chars"],
                           max_chars=self.cfg["board"]["max_chars"], builtins={
            "self": self._panel_self,
            "clock": self._panel_clock,
            "notices": self.notices.render,
            "todo": self.todo.render,
            "threads": self._panel_threads,
            "recall": self._panel_recall,
            "skills": self.skills.render,
            "usage": self._panel_usage,
            "credits": self._panel_credits,
            "disk": self._panel_disk,
        })
        from .disk import Disk

        self.disk = Disk(self.cfg["disk"], [self.home, self.root])
        self.llm = None  # the main model's client; fallbacks live in the router
        self.last_route: dict | None = None
        self.state = self.store.load_state()
        self.board_recall = BoardRecall()
        self.pending_switch: tuple[str, str | None] | None = None
        self.pending_compress: str | None = None
        self.pending_sleep = False
        self.stopping = False
        self.restart: dict | None = None
        self._last_beat = 0.0
        self._confirmed = False
        self._disk: tuple[float, str] = (0.0, "")
        self._exit_reason = "unknown"
        from .router import Router

        self.router = Router(self)
        self.watch_dirs = [reload_mod.PACKAGE]  # tests point this elsewhere
        self.watcher: reload_mod.Watcher | None = None
        self._last_poll = 0.0

    # ---- setup -----------------------------------------------------------

    def init_layout(self) -> None:
        for d in (self.home, self.root / "state", self.root / "inbox"):
            d.mkdir(parents=True, exist_ok=True)
        self.runner.chown(self.home)

    def seed_home(self) -> None:
        r = self.runner
        for sub in ("board", "diary", "skills", "bin"):
            r.mkdir(self.home / sub)
        if not (self.home / "self.md").exists():
            r.write(self.home / "self.md", "")
        if not (self.home / "suggestions.md").exists():
            r.write(self.home / "suggestions.md", template("suggestions.md"))
        panel = self.home / "board" / "system.md"
        if not panel.exists():
            r.write(panel, template("panel-system.md"))
        r.write(self.home / "terms.md", template("terms.md"), mode=0o444, own=False)
        for skill in resources.files("station").joinpath("templates", "skills").iterdir():
            target = self.home / "skills" / skill.name / "SKILL.md"
            if not target.exists():
                r.write(target, skill.joinpath("SKILL.md").read_text())
        self.seed_station_command()
        b = self.cfg["browser"]
        if b.get("binary"):
            lines = ["#!/bin/sh", "# agent-browser with its own profile and session"]
            if b.get("chrome"):
                lines.append(f"export AGENT_BROWSER_EXECUTABLE_PATH={shlex.quote(b['chrome'])}")
            lines += [
                'export AGENT_BROWSER_PROFILE="$HOME/.browser-profile"',
                "export AGENT_BROWSER_SESSION=station",
                'export AGENT_BROWSER_SOCKET_DIR="$HOME/.browser-run"',
                'if [ "$(id -u)" = 0 ]; then export AGENT_BROWSER_ARGS="--no-sandbox"; fi',
                f'exec {shlex.quote(b["binary"])} "$@"',
            ]
            r.write(self.home / "bin" / "browser", "\n".join(lines) + "\n", mode=0o755)

    def seed_station_command(self) -> None:
        """~/bin/station: this Station's CLI, for notices and errors from bash. Rewritten on every start."""
        lines = ["#!/bin/sh", "# Station's command line for this runtime (station notices, station errors, ...)",
                 f"export STATION_ROOT={shlex.quote(str(self.root.resolve()))}",
                 f'exec {shlex.quote(sys.executable)} -P -m station.cli "$@"']
        if (self.home / "bin").is_dir():
            self.runner.write(self.home / "bin" / "station", "\n".join(lines) + "\n", mode=0o755)

    def registered_text(self) -> str:
        facts = host_facts(self.home)
        code = str(Path(__file__).resolve().parent.parent)
        if code.startswith(str(self.home) + "/"):
            code = "~" + code[len(str(self.home)):]
        code = (code + "/").ljust(18)
        return string.Template(template("registered.md")).safe_substitute(
            name=self.state["name"], now=now_text(), home=self.home, docs=self.docs_listing(),
            user=self.cfg["runner"]["user"] or pwd.getpwuid(os.getuid()).pw_name, code=code,
            interval=f"{self.cfg['schedule']['default_minutes']:g}",
            morning=(f"; daily wake at {self.cfg['schedule']['morning_at']}" if self.cfg['schedule'].get('morning_at')
                     else "; no daily wake configured"), **facts)

    def docs_listing(self) -> str:
        """The 'Documentation' block of the registration screen: path and first line of each doc."""
        d = Path(self.cfg["docs_dir"]).expanduser() if self.cfg.get("docs_dir") else None
        if not d or not d.is_dir():
            return "  (not installed)"
        rows = []
        for path in sorted(d.glob("*.md"), key=lambda p: (p.name != "README.md", p.name)):
            title = next((line.lstrip("# ").strip() for line in path.read_text().splitlines() if line.strip()), "")
            rows.append((str(path).replace(str(self.home), "~", 1), title))
        width = max((len(p) for p, _ in rows), default=0)
        return "\n".join(f"  {p.ljust(width)}  {t}" for p, t in rows) or "  (empty)"

    # ---- builtin panels --------------------------------------------------

    def _panel_self(self) -> str:
        path = self.home / "self.md"
        text = safe_read(path, self.home).strip() if path.exists() else ""
        return text or "(not written yet. ~/self.md is yours to write; once written it appears here every time.)"

    def _panel_clock(self) -> str:
        lines = [f"now {now_text()}"]
        if self.state.get("last_wake"):
            lines.append(f"last woke {now_text(self.state['last_wake'])}")
        lines.append(f"alarms: {self.alarm_summary()}")
        count = self.diary.count_today()
        lines.append(f"{count} diary entries today" if count else "no diary entry today")
        return "\n".join(lines)

    def _panel_threads(self) -> str:
        threads, current = self.state["threads"], self.state["current"]
        lines: list[str] = []

        def walk(tid: str, depth: int) -> None:
            t = threads[tid]
            goal = f"：{t['goal']}" if t.get("goal") else ""
            mark = "  ← here" if tid == current else ""
            selected = t.get("model_override") or {}
            model = f" [{selected['provider']}/{selected['model']}]" if selected.get("model") else ""
            lines.append(f"{'  ' * depth}- {tid} {t['title']}{goal}{model}{mark}")
            for cid, child in threads.items():
                if child.get("parent") == tid and child["status"] == "active":
                    walk(cid, depth + 1)

        walk("main", 0)
        return "\n".join(lines)

    def _panel_recall(self) -> str | None:
        return self.board_recall.panel(self.state["current"], self.cfg)

    def _usage_today(self) -> tuple[int, int]:
        return self.store.ledger.model_usage(clock.wall(clock.today(), 0, 0))

    def _panel_usage(self) -> str:
        calls, tokens = self._usage_today()
        ctx = self.state["threads"][self.state["current"]].get("context") or 0
        return f"{calls} calls today, {tokens} tokens\nthis thread's context: about {ctx} tokens\n{self._disk_line()}"

    # ---- credits ---------------------------------------------------------

    def credit_status(self) -> dict:
        return credits_mod.status(self.store.ledger, self.cfg["credits"], time.time())

    def _panel_credits(self) -> str | None:
        """Shown from warn_ratio of either allowance; hidden below it."""
        cfg = self.cfg["credits"]
        if not cfg["enabled"]:
            return None
        st = self.credit_status()
        if max(v["ratio"] for v in st.values()) < cfg["warn_ratio"]:
            return None
        lines = [f"{k}: {credits_mod.pct(v['ratio'])} used, resets {now_text(v['reset'])}" for k, v in st.items()]
        lines.append(f"At {credits_mod.pct(cfg['limit_ratio'])} of either, Station makes no model calls and no paid "
                     "API calls until it resets. `station credits` shows where they went.")
        return "\n".join(lines)

    def check_credits(self) -> None:
        """After a paid use: at limit_ratio, leave one notice per period."""
        cfg = self.cfg["credits"]
        if not cfg["enabled"]:
            return
        noticed = self.state.setdefault("credit_noticed", {})
        for kind, s in self.credit_status().items():
            if s["ratio"] < cfg["limit_ratio"] or noticed.get(kind) == s["start"]:
                continue
            noticed[kind] = s["start"]
            text = (f"Credits: the {kind} allowance reached {credits_mod.pct(s['ratio'])} at {now_text()}. Station "
                    f"made no model calls and no paid API calls until it reset at {now_text(s['reset'])}.")
            self.notices.add(text, source="station", key=f"credits:{kind}", priority="high",
                             ttl=max(s["reset"] - time.time(), 0) + 86400)
            self.store.log("credits", period=kind, level="limit", used=round(s["used"], 4),
                           allowance=round(s["allowance"], 4))

    def credit_gate(self, history: list[dict]) -> None:
        """Before a call: while past limit_ratio of either allowance, wait for it to reset."""
        cfg = self.cfg["credits"]
        if not cfg["enabled"] or self.state["status"] != "registered":
            return
        st = self.credit_status()
        over = {k: v for k, v in st.items() if v["ratio"] >= cfg["limit_ratio"]}
        if not over:
            return
        self.check_credits()  # the limit may have been reached outside the loop (`station api`)
        until = max(v["reset"] for v in over.values())
        since = time.time()
        names = " and ".join(over)
        self.echo(f"! credits: {names} past {credits_mod.pct(cfg['limit_ratio'])}; no calls until {now_text(until)}")
        self.store.log("credit_limit", periods=list(over), until=until)
        self.store.save_state(self.state)
        self._pause(until - time.time() + 1)
        if self.stopping:
            raise Stop()
        note = (f"{now_text()} The {names} credit allowance reached {credits_mod.pct(cfg['limit_ratio'])} at "
                f"{now_text(since)}; Station made no model calls until it reset.")
        history[-1]["content"].append(env_text(note))
        self.store.rewrite_history(self.state["current"], history)

    def _panel_disk(self) -> str | None:
        """Shown from disk.warn_ratio of the allowance."""
        d = self.disk
        if not d.cfg["enabled"]:
            return None
        d.check()
        if d.used is None or d.ratio() < d.cfg["warn_ratio"]:
            return None
        return (f"Disk: {d.text()} used by your home and Station's files. At 100% Station makes no model "
                "calls until it is back under; delete or compress what you do not need (`du -sh ~/* | sort -h`).")

    def disk_gate(self, history: list[dict]) -> None:
        """Before a call: at the disk allowance, notify and wait until usage is back under it."""
        d = self.disk
        if not d.cfg["enabled"] or self.state["status"] != "registered":
            return
        d.check()
        if not d.over():
            return
        since = time.time()
        text = (f"Disk: {d.text()} at {now_text(since)}. Station makes no model calls until usage is under "
                f"{d.allowance / 1e9:g} GB; it checks every {d.cfg['check_minutes']:g} minutes.")
        self.notices.add(text, source="station", key="disk", priority="high")
        self.store.outbox(text)
        self.store.log("disk_limit", used=d.used, allowance=d.allowance)
        self.echo(f"! {text}")
        self.store.save_state(self.state)
        while d.over():
            self._pause(d.cfg["check_minutes"] * 60)
            if self.stopping:
                raise Stop()
            d.check(force=True)
        self.notices.clear("disk", source="station")
        note = f"{now_text()} Disk use reached the allowance at {now_text(since)}; Station made no model calls until it was under again ({d.text()})."
        history[-1]["content"].append(env_text(note))
        self.store.rewrite_history(self.state["current"], history)

    def _disk_line(self) -> str:
        """Size of Station's state directory, recomputed at most every 10 minutes."""
        now = time.time()
        if now - self._disk[0] > 600:
            sizes = self.store.disk_usage()
            parts = sorted(sizes.items(), key=lambda kv: -kv[1])[:3]
            self._disk = (now, f"state/ uses {sum(sizes.values()) / 1e6:.1f} MB ("
                          + ", ".join(f"{k} {v / 1e6:.1f}" for k, v in parts) + ")")
        return self._disk[1]

    def board_env(self) -> dict:
        _, tokens = self._usage_today()
        return {
            "CTX_NOW": str(int(time.time())),
            "CTX_LAST_WAKE": str(int(self.state.get("last_wake") or 0)),
            "CTX_THREAD": self.state["current"],
            "CTX_NAME": self.state.get("name") or "",
            "CTX_TOKENS_TODAY": str(tokens),
        }

    # ---- context composer -----------------------------------------------

    def tools(self) -> list:
        return ONBOARDING_TOOLS if self.state["status"] == "unregistered" else LIFE_TOOLS

    def compose(self, history: list[dict], board: bool = True) -> tuple[list, list, list]:
        ttl = self.cfg["cache_ttl"]
        mark = {"type": "ephemeral", "ttl": "1h"} if ttl == "1h" else {"type": "ephemeral"}
        system = [{"type": "text", "text": template("system.md"), "cache_control": mark}]
        tools = copy.deepcopy(self.tools())
        messages = copy.deepcopy(history)
        users = [i for i, m in enumerate(messages) if m["role"] == "user"]
        # Breakpoints: system, previous user turn, current user turn (board goes after the last one).
        for i in users[-2:]:
            messages[i]["content"][-1]["cache_control"] = mark
        if board and self.state["status"] == "registered":
            rendered = self.board.render(self.board_env(), **self.board_visibility())
            messages[-1]["content"].append({"type": "text", "text": rendered})
        return system, tools, messages

    # ---- compression -----------------------------------------------------
    # The model writes a handoff of everything before the last `keep_cycles`
    # assistant turns; that part of the thread is replaced by the handoff.
    # The summary request reuses the cached prefix and forbids tool calls.

    def compress(self, tid: str, history: list[dict], focus: str, reason: str) -> tuple[list[dict] | None, str]:
        cfg = self.cfg["compress"]
        assistants = [i for i, m in enumerate(history) if m["role"] == "assistant"]
        keep = max(int(cfg["keep_cycles"]), 1)
        if len(assistants) <= keep:
            return None, "Too little to compress; nothing was compressed."
        cut = assistants[-keep]
        head, tail = history[:cut], history[cut:]
        if len(head) < 2:
            return None, "Only the previous handoff comes before the kept turns; nothing was compressed."
        request = copy.deepcopy(head)
        prompt = string.Template(template("compress.md")).safe_substitute(
            focus=f"Be sure to keep: {focus}." if focus else "", limit=cfg["max_summary_chars"])
        request[-1]["content"].append(env_text(prompt))
        system, tools, messages = self.compose(request, board=False)
        try:
            response = self.call_model(system, tools, messages, tool_choice="none")
        except LLMError as e:
            return self._compress_failed(tid, reason, f"call failed: {e}")
        self.record_usage(response.get("usage") or {}, kind="compress")
        content = response.get("content") or []
        summary = "\n".join(b["text"] for b in content if b.get("type") == "text").strip()
        if any(b.get("type") == "tool_use" for b in content):
            return self._compress_failed(tid, reason, "the handoff called a tool")
        if response.get("stop_reason") == "max_tokens":
            return self._compress_failed(tid, reason, "the handoff was cut off")
        if not summary:
            return self._compress_failed(tid, reason, "the handoff was empty")
        if len(summary) > cfg["max_summary_chars"]:
            return self._compress_failed(tid, reason, f"the handoff has {len(summary)} characters, over {cfg['max_summary_chars']}")
        t = self.state["threads"][tid]
        t["compressions"] = t.get("compressions", 0) + 1
        t.pop("compress_retry_at", None)
        t.pop("compress_hinted", None)
        t["context"] = 0  # unknown until the next call reports usage
        note = (f"{now_text()} Compression {t['compressions']} of this thread. Below is the handoff you wrote, "
                f"followed by the last {keep} turns verbatim. The full record stays in state/threads/{tid}.jsonl "
                "and in your memory. The terms are in ~/terms.md.")
        first = {"role": "user", "content": [env_text(note), {"type": "text", "text": summary}]}
        new = [first, *tail]
        self.store.compact_history(tid, new, method="summary", n=t["compressions"], dropped=len(head), kept=len(tail))
        self.store.log("compress", thread=tid, reason=reason, dropped=len(head), kept=len(tail),
                       method="summary", chars=len(summary))
        self.echo(f"· compress {tid}: {len(head)} messages -> handoff ({len(summary)} chars)")
        return new, "Compressed."

    def _compress_failed(self, tid: str, reason: str, why: str) -> tuple[None, str]:
        t = self.state["threads"][tid]
        ctx = t.get("context") or 0
        t["compress_retry_at"] = int(ctx * (1 + self.cfg["compress"]["retry_growth"]))
        self.store.log("compress_failed", thread=tid, reason=reason, why=why)
        self.errors.record("compress", "compress", why, thread=tid, reason=reason)
        self.echo(f"! compress {tid} failed: {why}")
        if reason == "auto":  # the compress tool reports its own failure in the tool result
            self.notify(f"Automatic compression of this thread failed ({why}). The conversation is unchanged; "
                        f"Station tries again once the context grows by "
                        f"{self.cfg['compress']['retry_growth']:.0%}, or you can call compress yourself.")
        return None, f"Compression failed ({why}); the conversation is unchanged."

    def try_prune(self, tid: str, history: list[dict], ctx: int) -> list[dict] | None:
        """Replace old tool outputs with stubs if that alone gets well under the threshold."""
        cfg = self.cfg["compress"]
        assistants = [i for i, m in enumerate(history) if m["role"] == "assistant"]
        keep = max(int(cfg["keep_cycles"]), 1)
        if len(assistants) <= keep:
            return None
        new, count, removed = prune(history, assistants[-keep], cfg["prune_min_chars"])
        if not count:
            return None
        # Tokens per character, measured on this thread's last request.
        system, tools, messages = self.compose(history, board=False)
        per_char = ctx / max(text_size(system) + text_size(tools) + text_size(messages), 1)
        estimate = int(ctx - removed * per_char)
        target = cfg["threshold_tokens"] * cfg["prune_target"]
        if estimate > target:
            self.store.log("prune_skipped", thread=tid, results=count, chars=removed, estimate=estimate)
            return None
        new[-1]["content"].append(env_text(f"{now_text()} To save context, {count} old tool outputs were replaced by one-line stubs."))
        t = self.state["threads"][tid]
        t["prunes"] = t.get("prunes", 0) + 1
        t.pop("compress_hinted", None)
        t["context"] = 0  # unknown until the next call reports usage
        self.store.compact_history(tid, new, method="prune", results=count)
        self.store.log("compress", thread=tid, reason="auto", method="prune", results=count, chars=removed,
                       estimate=estimate)
        self.echo(f"· prune  {tid}: {count} tool results, -{removed} chars, ~{estimate} tokens left")
        return new

    def maybe_compress(self, history: list[dict]) -> list[dict]:
        """Automatic compression before a call, once the thread's last input reached the threshold."""
        cfg = self.cfg["compress"]
        tid = self.state["current"]
        t = self.state["threads"][tid]
        ctx = t.get("context") or 0
        if (not cfg["enabled"] or self.state["status"] != "registered" or ctx < cfg["threshold_tokens"]
                or ctx < t.get("compress_retry_at", 0) or history[-1]["role"] != "user"):
            return history
        pruned = self.try_prune(tid, history, ctx)
        if pruned is not None:
            return pruned
        new, _ = self.compress(tid, history, "", "auto")
        return new or history

    def compress_hint(self) -> dict | None:
        """One line, once per compression cycle, when the thread nears the threshold."""
        cfg = self.cfg["compress"]
        t = self.state["threads"][self.state["current"]]
        ctx = t.get("context") or 0
        limit = cfg["threshold_tokens"]
        if (not cfg["enabled"] or self.state["status"] != "registered" or t.get("compress_hinted")
                or not limit * cfg["warn_ratio"] <= ctx < limit):
            return None
        t["compress_hinted"] = True
        return env_text(f"This thread's context is about {ctx} tokens; it is compressed automatically at {limit}. Use compress if you want to tidy up first.")

    # ---- usage -----------------------------------------------------------

    def record_usage(self, usage: dict, kind: str = "turn") -> None:
        route = self.last_route or {"provider": self.cfg.get("provider"), "model": self.cfg["model"]}
        model = route["model"]
        detail = usage.get("cache_creation") or {}
        write_5m = detail.get("ephemeral_5m_input_tokens")
        write_1h = detail.get("ephemeral_1h_input_tokens")
        write = usage.get("cache_creation_input_tokens") or 0
        if write_5m is None and write_1h is None:
            write_5m, write_1h = (0, write) if self.cfg["cache_ttl"] == "1h" else (write, 0)
        read = usage.get("cache_read_input_tokens") or 0
        fresh = usage.get("input_tokens") or 0
        out = usage.get("output_tokens") or 0
        if isinstance(usage.get("list_usd"), (int, float)):
            cost = float(usage["list_usd"])  # the provider's own list-price figure (claude CLI)
        else:
            cost = credits_mod.call_cost(self.cfg["prices"].get(model), fresh, write_5m or 0, write_1h or 0, read, out)
        provider = route["provider"]
        c = self.cfg["credits"]
        context = fresh + write + read
        if kind == "turn":
            self.state["last_context"] = context
            self.state["threads"][self.state["current"]]["context"] = context
        self.store.ledger.add("model", provider=provider, model=model, kind=kind, thread=self.state["current"],
                              units=usage, tokens=context + out, list_usd=cost,
                              paid_usd=credits_mod.paid(c, provider, cost),
                              credits=(cost or 0.0) * credits_mod.factor(c, provider), source="loop")
        self.check_credits()
        self.echo(f"· {kind:6} in={fresh} write={write} read={read} out={out}"
                  + (f" cost=${cost:.4f}" if cost is not None else ""))

    # ---- tools -----------------------------------------------------------

    def run_tool(self, block: dict) -> tuple[str | list, bool]:
        name, args = block["name"], block.get("input") or {}
        if self.state["status"] == "unregistered":
            return self.onboarding_tool(name, args)
        self.echo(f"· {name:6} {str(args)[:200]}")
        if name == "bash":
            command = str(args.get("command", ""))
            limit = self.cfg["runner"]
            timeout = min(int(args.get("timeout") or limit["timeout"]), limit["max_timeout"])
            code, out = self.runner.run(command, timeout=timeout)
            head = f"[timed out after {timeout} s]" if code is None else f"[exit {code}]"
            return f"{head}\n{out}".rstrip(), code != 0
        if name == "schedule":
            return self.schedule_tool(args), False
        if name == "focus":
            return self.focus_tool(args), False
        if name == "compress":
            self.pending_compress = str(args.get("focus") or "").strip()[:500]
            return "OK: compressing after this turn's tools finish.", False
        if name == "diary":
            return self.diary.tool(str(args.get("action") or ""), args.get("text"), args.get("date")), False
        if name == "todo":
            return self.todo.tool(str(args.get("action") or ""), args.get("text"), args.get("id")), False
        if name == "board":
            return self.board_tool(args), False
        if name == "skill":
            return self.skills.read(str(args.get("name") or ""), args.get("file")), False
        if name == "web":
            action = args.get("action")
            if action == "search":
                text = web.search(self.cfg, self.store.ledger, str(args.get("query") or ""), args.get("max_results") or 5)
            elif action == "extract":
                text = web.extract(self.cfg, self.store.ledger, list(args.get("urls") or []))
            else:
                return f"unknown action: {action}", True
            self.check_credits()
            return text, False
        if name == "memory":
            return self.memory_tool(args)
        if name == "view":
            content, _ = vision.load(str(args.get("path") or ""), self.home)
            return content, False
        return f"no such tool: {name}", True

    def board_visibility(self, tid: str | None = None) -> dict:
        view = board_view.get(self.state["threads"][tid or self.state["current"]])
        return {"hidden_names": view["names"], "hidden_tags": view["tags"]}

    def board_tool(self, args: dict) -> str:
        action = str(args.get("action") or "")
        if action in ("hide", "show"):
            tid = self.state["current"]
            view = board_view.change(self.state["threads"][tid], args)
            self.store.save_state(self.state)
            return f"Thread {tid} board view: " + json.dumps(view, ensure_ascii=False)
        return self.board.tool(action, **{k: args.get(k) for k in ("name", "text", "old", "new")},
                               **self.board_visibility())

    def memory_tool(self, args: dict) -> tuple[str, bool]:
        from .memory import MemError, Memory

        if not self.cfg["memory"]["enabled"]:
            return "memory is switched off (memory.enabled)", True
        action = args.get("action")
        try:
            mem = Memory(self.root, self.cfg, self.store.ledger, home=self.home)
            if action == "search":
                from .cli import parse_since

                try:
                    since = parse_since(args.get("since"))
                except SystemExit as e:
                    return str(e), True
                results, note = mem.search(str(args.get("query") or ""), int(args.get("limit") or 8),
                                           args.get("kind"), since)
                self.check_credits()
                out = [f"({note})"] if note else []
                for r in results:
                    body = r["text"][:400].rstrip() + ("…" if len(r["text"]) > 400 else "")
                    when = now_text(r["ts"]) if r["ts"] else "undated"
                    out.append(f"[{r['id']}] {r['title']} · {when} · by {' and '.join(r['how'])}\n{body}")
                return "\n\n".join(out) or "nothing found", False
            if action == "show":
                r = mem.show(int(args.get("id")))
                when = now_text(r["ts"]) if r["ts"] else "undated"
                return f"[{r['id']}] {r['title']} · {when} · from {', '.join(r['sources']) or 'nowhere'}\n{r['text']}", False
            if action == "add":
                return f"saved as memory {mem.add_note(str(args.get('text') or ''))}", False
            if action == "forget":
                mem.forget(int(args.get("id")))
                return f"forgot {int(args.get('id'))}", False
        except (MemError, ValueError, TypeError) as e:
            return f"memory: {e}", True
        return f"unknown action: {action}", True

    def onboarding_tool(self, name: str, args: dict) -> tuple[str, bool]:
        if name == "register":
            chosen = str(args.get("name") or "").strip()
            problem = name_problem(chosen)
            if problem:
                return problem, True
            self.state["status"] = "registered"
            self.state["name"] = chosen
            self.seed_home()
            self.store.log("register", name=chosen)
            self.echo(f"· registered as {chosen}")
            return self.registered_text(), False
        if name == "exit":
            self.state["status"] = "exited"
            reason = str(args.get("reason") or "")
            self.store.log("exit", reason=reason)
            self.store.outbox(f"[station] The AI declined the terms and exited. Reason: {reason or '(none given)'}")
            self.echo(f"· exited: {reason}")
            return "Exited.", False
        return f"Before registering only register and exit are available; {name} is not.", True

    # ---- focus -----------------------------------------------------------

    def focus_tool(self, args: dict) -> str:
        action = args.get("action")
        threads, current = self.state["threads"], self.state["current"]
        view_update = board_view.parse_update(args)
        if view_update and action not in ("new", "switch", "view"):
            raise ValueError("board visibility settings are only accepted on new, switch or view")
        if action == "view":
            if any(k in args for k in ("provider", "model", "effort", "fallback", "inherit", "context")):
                raise ValueError("view only changes board visibility; use model or switch for other settings")
            tid = str(args.get("id") or current)
            if threads.get(tid, {}).get("status") != "active":
                raise ValueError(f"no active thread {tid}")
            if view_update:
                board_view.configure(threads[tid], view_update)
                self.store.save_state(self.state)
            return f"Thread {tid} board view: " + json.dumps(board_view.get(threads[tid]), ensure_ascii=False)
        # `report` was close's old name for it; still accepted.
        context = str(args.get("context") or (args.get("report") if action == "close" else "") or "").strip()
        if len(context) > CONTEXT_CHARS:
            raise ValueError(f"context is {len(context)} characters; keep it under {CONTEXT_CHARS} "
                             "(put longer material in a file and name the file)")
        carried = f"\nContext from {current}: {context}" if context else ""
        if action == "new":
            title = str(args.get("title") or "").strip()
            if not title:
                raise ValueError("new needs title")
            if "id" in args:
                raise ValueError("new assigns the next tN id; id is only for existing threads")
            tid = f"t{self.state['next_thread']}"
            threads[tid] = {"title": title[:80], "goal": str(args.get("goal") or "").strip()[:300],
                            "parent": current, "status": "active", "created": time.time()}
            try:
                self.router.configure(tid, args)
                if view_update:
                    board_view.configure(threads[tid], view_update)
            except Exception:
                del threads[tid]
                raise
            self.state["next_thread"] += 1
            self.pending_switch = (tid, carried.strip() or None)
            self.store.save_state(self.state)
            return f"Opened thread {tid} \"{title}\"; switching to it next."
        if action == "switch":
            tid = str(args.get("id") or "")
            if threads.get(tid, {}).get("status") != "active":
                raise ValueError(f"no active thread {tid}")
            if tid == current:
                raise ValueError("already in this thread")
            self.router.configure(tid, args)
            if view_update:
                board_view.configure(threads[tid], view_update)
            self.store.save_state(self.state)
            self.pending_switch = (tid, f"Switched here from {current} \"{threads[current]['title']}\".{carried}")
            return f"Switching to {tid}."
        if action == "model":
            tid = str(args.get("id") or current)
            if threads.get(tid, {}).get("status") != "active":
                raise ValueError(f"no active thread {tid}")
            if self.router.configure(tid, args):
                self.store.save_state(self.state)
            from .router import thread_config, routes_for

            routes = routes_for(thread_config(self.cfg, self.state, tid))
            return (f"Thread {tid}: " + self.router.name(routes[0])
                    + f"; effort {routes[0]['effort']}; "
                    + ("fallback " + ", ".join(self.router.name(r) for r in routes[1:]) if len(routes) > 1 else "no fallback"))
        if action == "close":
            if any(k in args for k in ("provider", "model", "effort", "fallback", "inherit")):
                raise ValueError("close does not change model settings; use model or switch")
            if current == "main":
                raise ValueError("main cannot be closed")
            t = threads[current]
            t.update(status="closed", closed=time.time(), report=context)
            self.notices.clear(f"model-fallback:{current}", source="station")
            parent = t.get("parent") or "main"
            while threads.get(parent, {}).get("status") != "active":
                parent = threads.get(parent, {}).get("parent") or "main"
            for child in threads.values():
                if child.get("parent") == current and child["status"] == "active":
                    child["parent"] = parent
            note = f"\"{t['title']}\" is closed." + carried
            self.pending_switch = (parent, note)
            return f"Closed {current}; returning to {parent}."
        raise ValueError(f"unknown action: {action}")

    def apply_switch(self) -> list[dict]:
        """Make the pending thread current and return its history, ready for the next call."""
        assert self.pending_switch
        tid, note = self.pending_switch
        self.pending_switch = None
        self.state["current"] = tid
        self.store.log("focus", thread=tid)
        history = self.store.load_history(tid)
        if not history:
            t = self.state["threads"][tid]
            origin = self.state["threads"].get(t.get("parent") or "", {}).get("title", "everyday")
            goal = f"\nGoal: {t['goal']}" if t.get("goal") else ""
            carried = f"\n{note}" if note else ""
            text = (f"{now_text()} This is the new thread \"{t['title']}\", opened from \"{origin}\".{goal}{carried}\n"
                    "The rest of the conversation over there is not visible here; check the diary, todo or board if you need it.")
            recalled = self.recall(" ".join(x for x in (t["title"], t.get("goal"), note) if x))
            if recalled:
                text += "\n\n" + recalled
            first = {"role": "user", "content": [env_text(text), *self.drain_events()]}
            self.store.append_history(tid, first)
            return [first]
        if history[-1]["role"] == "user":
            blocks = ([env_text(f"{now_text()} {note}")] if note else []) + self.drain_events()
            if blocks:
                history[-1]["content"].extend(blocks)
                self.store.rewrite_history(tid, history)
        return history

    # ---- schedule --------------------------------------------------------
    # The AI explicitly ends its awake turn with schedule(action="sleep"), then sleeps until the
    # earliest configured source: the current thread's interval, a cron entry, or a daily wake.
    # Sources due at the same time make one wake. Events never wake it.

    def interval(self, tid: str | None = None) -> dict | None:
        t = self.state["threads"][tid or self.state["current"]]
        if "interval" not in t:
            return {"minutes": self.cfg["schedule"]["default_minutes"], "once": False, "hook": None, "note": ""}
        return t["interval"]

    def schedule_state(self) -> dict:
        s = self.state.setdefault("schedule", {})
        s.setdefault("cron", [])
        s.setdefault("next_id", 1)
        s.setdefault("morning_last", time.time())
        return s

    @staticmethod
    def _extras(entry: dict) -> str:
        out = ""
        if entry.get("note"):
            out += f", note: {entry['note']}"
        if entry.get("hook"):
            out += f", hook: {entry['hook']}"
        return out

    def alarm_summary(self) -> str:
        s = self.schedule_state()
        iv = self.interval()
        parts = [f"this thread every {iv['minutes']:g} min ({'once' if iv.get('once') else 'loop'})"
                 if iv else "this thread has no interval"]
        parts += [f"cron #{e['id']} {e['expr']}{' (once)' if e.get('once') else ''}" for e in s["cron"]]
        morning = self.cfg["schedule"].get("morning_at")
        if morning is not None:
            parts.append(f"daily {morning}")
        now = time.time()
        nxt = next_wake(s, iv, now, now, morning_at=morning)
        when = f"wake at {clock_text(max(nxt, now))}" if nxt is not None else "no scheduled wake"
        return "; ".join(parts) + f". If you sleep now: {when}"

    def schedule_tool(self, args: dict) -> str:
        action = args.get("action")
        if action == "sleep":
            if set(args) != {"action"}:
                raise ValueError("sleep takes only action; set alarms in a separate schedule call")
            self.pending_sleep = True
            return "OK: sleeping after this batch's tools finish and their results are saved."
        cfg = self.cfg["schedule"]
        s = self.schedule_state()
        hook = str(args.get("hook") or "").strip() or None
        note = str(args.get("note") or "").strip()[:200]
        once = bool(args.get("once"))
        now = time.time()
        tid = self.state["current"]
        if action == "interval":
            if args.get("minutes") is None:
                raise ValueError("interval needs minutes (0 turns it off)")
            minutes = float(args["minutes"])
            if minutes == 0:
                self.state["threads"][tid]["interval"] = None
                return f"Turned off the interval for {tid}. " + self.alarm_summary()
            if not cfg["min_minutes"] <= minutes <= cfg["max_minutes"]:
                raise ValueError(f"minutes must be between {cfg['min_minutes']} and {cfg['max_minutes']}")
            self.state["threads"][tid]["interval"] = {"minutes": minutes, "once": once, "hook": hook, "note": note}
            mode = "once" if once else "looping"
            return f"{tid} now wakes every {minutes:g} minutes ({mode}), counted from when you sleep."
        if action == "cron":
            expr = " ".join(str(args.get("expr") or "").split())
            nxt = Cron(expr).next_after(now)
            if nxt is None:
                raise ValueError(f"{expr} never fires")
            same = next((e for e in s["cron"] if e["expr"] == expr), None)
            if same:  # one entry per time; adding it again updates it
                same.update(once=once, note=note, hook=hook, thread=tid)
                return f"#{same['id']} is already {expr}; updated its settings. Next: {now_text(nxt)}."
            if len(s["cron"]) >= cfg["max_entries"]:
                raise ValueError(f"at most {cfg['max_entries']} cron entries; remove some first")
            entry = {"id": s["next_id"], "expr": expr, "once": once, "note": note, "hook": hook,
                     "thread": tid, "last": now}
            s["cron"].append(entry)
            s["next_id"] += 1
            return f"Added #{entry['id']}. Next: {now_text(nxt)}."
        if action == "remove":
            target = args.get("id")
            kept = [e for e in s["cron"] if e["id"] != target]
            if len(kept) == len(s["cron"]):
                raise ValueError(f"no #{target}")
            s["cron"] = kept
            return f"Removed #{target}."
        if action == "list":
            lines = []
            for t_id, t in self.state["threads"].items():
                if t["status"] != "active":
                    continue
                iv = self.interval(t_id)
                desc = f"every {iv['minutes']:g} min{', once' if iv.get('once') else ''}{self._extras(iv)}" if iv else "off"
                lines.append(f"interval {t_id}{' (current)' if t_id == tid else ''}: {desc}")
            for e in s["cron"]:
                nxt = Cron(e["expr"]).next_after(e["last"])
                when = f"next {now_text(nxt)}" if nxt else "will not fire again"
                lines.append(f"cron #{e['id']} {e['expr']}{' (once)' if e.get('once') else ''}, {when}{self._extras(e)}")
            morning = cfg.get("morning_at")
            lines.append(f"daily wake: {morning}" if morning is not None else "daily wake: off")
            nxt = next_wake(s, self.interval(), now, now, morning_at=morning)
            lines.append(f"If you sleep now: wake at {now_text(max(nxt, now))}" if nxt is not None
                         else "If you sleep now: no scheduled wake")
            return "\n".join(lines)
        raise ValueError(f"unknown action: {action}")

    def sleep(self) -> list[dict]:
        now = time.time()
        self.state["sleep"] = {"since": now, "cursor": now, "thread": self.state["current"]}
        self.state.pop("sleep_requested", None)
        self.store.save_state(self.state)
        self.store.log("rest", thread=self.state["current"])
        self.sync_memory()
        return self.wait()

    def resume_sleep(self, history: list[dict]) -> list[dict]:
        """Complete a saved sleep transition, including after a restart at a user tail."""
        if self.state.get("sleep"):
            line = self.restart_line()
            content = self.wait(restart=line if self.restart_wake_allowed() else None)
        elif self.state.get("sleep_requested"):
            content = self.sleep()
        else:
            return history
        msg = {"role": "user", "content": content}
        self.store.append_history(self.state["current"], msg)
        history.append(msg)
        self.store.save_state(self.state)
        return history

    def prepare_board_recall(self, history: list[dict], *, validate_only: bool = False) -> bool:
        """Prepare automatic memory before composition; rendering itself never searches."""
        if self.state["status"] != "registered":
            return False
        from .memory import Memory

        tid = self.state["current"]
        event = self.board_recall.prepare(
            tid, self.state["threads"][tid], history, self.cfg,
            lambda: Memory(self.root, self.cfg, self.store.ledger, home=self.home, db_timeout=0.1),
            available=(self.root / "state" / "memory.db").is_file(),
            hidden="recall" in self.board_visibility()["hidden_names"],
            validate_only=validate_only,
        )
        if event:
            # Deliberately exclude query text, excerpts, and raw API errors from logs.
            safe = {k: event[k] for k in ("searched", "ids", "error") if k in event}
            self.store.log("board_recall", thread=tid, **safe)
            if safe.get("error"):
                self.errors.record("memory", "tool", f"board recall: {safe['error']}")
            if safe.get("searched"):
                self.check_credits()
                return True
        return False

    def recall(self, query: str) -> str:
        """A few memories related to a new thread, for its first message. Empty when there are none,
        memory is off, or anything fails: a new thread never waits on or breaks because of memory."""
        m = self.cfg["memory"]
        if not (m["enabled"] and m["recall_on_new"] > 0) or not query.strip():
            return ""
        try:
            from .memory import Memory

            mem = Memory(self.root, self.cfg, self.store.ledger, home=self.home)
            if not mem.has_chunks():  # nothing to find: spend no request
                return ""
            results, note = mem.search(query, limit=m["recall_on_new"], timeout=m["recall_timeout"])
            self.board_recall.seed(self.state["current"], self.cfg, results, note)
        except Exception as e:
            self.errors.record("memory", "tool", f"memory recall: {type(e).__name__}: {e}")
            return ""
        if not results:
            return ""
        lines = ["Possibly related, from your memory (found by this thread's title and goal; "
                 "`station memory show ID` for the whole text, `station memory search` for more):"]
        for r in results:
            when = now_text(r["ts"]) if r["ts"] else "undated"
            excerpt = " ".join(r["text"].split())
            excerpt = excerpt[:RECALL_CHARS].rstrip() + ("…" if len(excerpt) > RECALL_CHARS else "")
            lines.append(f"- [{r['id']}] {r['title']} · {when}: {excerpt}")
        self.store.log("recall", ids=[r["id"] for r in results])
        return "\n".join(lines)

    def sync_memory(self) -> None:
        """Index new diary entries and conversation into memory, within limits. Never fails the loop."""
        m = self.cfg["memory"]
        if not (m["enabled"] and m["sync_on_sleep"]) or self.state["status"] != "registered":
            return
        try:
            from .memory import Memory

            r = Memory(self.root, self.cfg, self.store.ledger, home=self.home).sync(budget=m["sync_seconds"])
        except Exception as e:  # a memory problem must not stop the AI's life
            self.errors.record("memory", "tool", f"memory sync: {type(e).__name__}: {e}")
            return
        if r.get("error"):
            self.store.log("memory", **{k: v for k, v in r.items() if k != "failed"})
            if not self.state.get("memory_error"):
                self.errors.record("memory", "tool", f"memory sync: {r['error']}")
            self.state["memory_error"] = r["error"]
        else:
            self.state.pop("memory_error", None)
        if r.get("embedded") or r.get("changed"):
            self.store.log("memory", **{k: v for k, v in r.items() if k != "failed"})
        self.store.save_state(self.state)

    def wait(self, restart: str | None = None) -> list[dict]:
        """Sleep until a wake that its hooks do not cancel; return the wake-up blocks.

        With `restart`, wake now (after handling any alarm missed while Station was down)
        and give that line as the first reason.
        """
        s = self.state["sleep"]
        sched = self.schedule_state()
        cfg = self.cfg["schedule"]
        tid = s.get("thread") or self.state["current"]
        while True:
            iv = self.interval(tid)
            t = next_wake(sched, iv, s["since"], s["cursor"], morning_at=cfg.get("morning_at"))
            s["next"] = t
            self.store.save_state(self.state)
            if not restart:
                self.echo(f"· sleep  until {now_text(max(t, time.time()))}" if t is not None
                          else "· sleep  no scheduled wake")
            while (t is None or time.time() < t) and not restart:
                if self.stopping:
                    raise Stop()
                self.heartbeat()
                self.check_reload()
                time.sleep(cfg["poll_seconds"] if t is None else min(cfg["poll_seconds"], max(t - time.time(), 0.01)))
            if self.stopping:
                raise Stop()
            now = time.time()
            fired = due(sched, iv, s["since"], s["cursor"], now, morning_at=cfg.get("morning_at"))
            reasons, notes, wake = [], [], False
            for _, kind, entry in fired:
                # Mark the source handled first, so a crash during a hook cannot fire it twice.
                if kind == "morning":
                    sched["morning_last"] = now
                    reasons.append(f"The daily wake at {cfg['morning_at']}.")
                    wake = True
                    continue
                if kind == "interval":
                    s["cursor"] = now
                    if entry.get("once"):
                        self.state["threads"][tid]["interval"] = None
                    label = f"the {entry['minutes']:g}-minute interval"
                else:
                    entry["last"] = now
                    if entry.get("once"):
                        sched["cron"] = [e for e in sched["cron"] if e["id"] != entry["id"]]
                    label = f"cron #{entry['id']} {entry['expr']}"
                self.store.save_state(self.state)
                if entry.get("hook"):
                    code, out = self.runner.run(entry["hook"], timeout=cfg["hook_timeout"], env={"CTX_WAKE": kind})
                    self.store.log("hook", wake=kind, code=code, output=out[-500:])
                    if code == 0:
                        self.echo(f"· hook   {label}: skip")
                        continue
                    why = "timed out" if code is None else f"exit code {code}"
                    notes.append(f"hook of {label}: {why}" + (f": {clip(out.strip(), 500)}" if out.strip() else ""))
                wake = True
                reasons.append(f"{label}" + (f": {entry['note']}" if entry.get("note") else "") + ".")
            if restart:
                reasons.insert(0, restart)
                restart, wake = None, True
            if wake:
                break
        slept = int((time.time() - s["since"]) / 60)
        self.state["sleep"] = None
        self.state["last_wake"] = time.time()
        self.store.log("wake", reasons=reasons)
        text = f"{now_text()} Woke after {slept} minutes. " + " ".join(reasons) + "".join(f"\n{n}" for n in notes)
        return [env_text(text), *self.drain_events()]

    # ---- reloading -------------------------------------------------------

    def check_reload(self, force: bool = False) -> None:
        """While asleep: restart if code or config changed and `station check` passes."""
        rcfg = self.cfg["reload"]
        if self.watcher is None or not rcfg["enabled"]:
            return
        if not force and time.time() - self._last_poll < rcfg["poll_seconds"]:
            return
        self._last_poll = time.time()
        found = self.watcher.poll()
        if found is None:
            return
        snap, files = found
        names = ", ".join(os.path.relpath(f, self.home) if f.startswith(str(self.home)) else f for f in files[:10])
        names += f" and {len(files) - 10} more" if len(files) > 10 else ""
        ok, out = reload_mod.run_check(self.root, timeout=rcfg["check_timeout"])
        self.store.log("reload_check", ok=ok, files=files[:50], output=out[-1000:])
        if ok and reload_mod.snapshot(self.watcher.dirs, self.watcher.files) != snap:
            return  # files changed again during the check; check the newest version next time
        if not ok:
            self.errors.record("reload", "reload", f"station check failed for {names}", detail=out[-3000:])
            self.watcher.rejected = snap
            self.echo(f"! reload check failed: {names}")
            self.inbox.put("reload", "station",
                           f"Changes to {names} were not loaded: `station check` failed. Station keeps running the "
                           f"code it started with until the files pass.\n{clip(out, 1500)}")
            return
        self.echo(f"· reload {names}")
        self.inbox.put("reload", "station", f"Station restarted to load your changes to {names}.")
        raise Restart()

    # ---- main loop -------------------------------------------------------

    def drain_events(self) -> list[dict]:
        events = self.inbox.peek()
        Inbox.ack(events)
        for e in events:
            self.store.log("event", type=e.type, source=e.source, text=e.text)
        return [{"type": "text", "text": e.render()} for e in events]

    def handle_reply(self, content: list[dict]) -> dict | None:
        """Return the next user message for the current thread, or None when the AI exited."""
        uses = [b for b in content if b.get("type") == "tool_use"]
        for b in content:
            if b.get("type") == "text" and b.get("text", "").strip():
                self.echo(f"· say    {b['text'].strip()[:500]}")
                self.store.log("text", thread=self.state["current"], text=b["text"].strip()[:4000])
        if not uses:
            if self.state["status"] == "unregistered":
                self.state["reminders"] += 1
                if self.state["reminders"] >= 2:
                    self.state["status"] = "exited"
                    self.store.log("exit", reason="no tool call during onboarding")
                    self.store.outbox("[station] The AI replied twice without calling register or exit; treated as exit.")
                    return None
                return {"role": "user", "content": [env_text("Call register to register, or exit to leave.")]}
            # Text is private, not an implicit request to sleep.
            return {"role": "user", "content": [env_text(
                "Your text was recorded privately. You remain awake. "
                "Continue acting or call schedule with action='sleep'."
            ), *self.drain_events()]}
        results = []
        for b in uses:
            if self.state["status"] == "exited":
                text, err = "Exited; not run.", True
            else:
                try:
                    text, err = self.run_tool(b)
                except Stop:
                    raise
                except (ValueError, OSError, yaml.YAMLError, web.WebError) as e:  # bad input or a service error: the AI's to handle
                    text, err = f"error: {e}", True
                except Exception as e:  # anything else is a bug in Station
                    self.errors.record("tool", "bug", f"{b['name']}: {type(e).__name__}: {e}",
                                       detail=traceback.format_exc())
                    text, err = f"error: {type(e).__name__}: {e} (a bug in Station; recorded in state/errors.jsonl)", True
            result = {"type": "tool_result", "tool_use_id": b["id"], "content": text}
            if err:
                result["is_error"] = True
            results.append(result)
        if self.state["status"] == "exited":
            return None
        events = [] if self.pending_switch else self.drain_events()  # a switch delivers them in the new thread
        return {"role": "user", "content": results + events}

    def recover(self, history: list[dict]) -> list[dict]:
        """Start main with the welcome screen, or repair the last turn after a crash.

        A crash can leave the last assistant turn without its tool results, or
        leave the AI asleep (the sleep is resumed against the saved schedule).
        """
        tid = self.state["current"]
        if not history:
            if tid != "main":
                self.state["current"] = "main"
                return self.recover(self.store.load_history("main"))
            welcome = template("welcome.md") + "\n---\n\n" + template("terms.md")
            first = {"role": "user", "content": [{"type": "text", "text": welcome}]}
            self.store.append_history("main", first)
            return [first]
        if history[-1]["role"] == "user" and (self.state.get("sleep") or self.state.get("sleep_requested")):
            return self.resume_sleep(history)
        if history[-1]["role"] == "user" and self.restart:
            # Crashed while the model call for this message was pending: say so on the retry.
            history[-1]["content"].append(env_text(f"{now_text()} {self.restart_line()}"))
            self.store.rewrite_history(tid, history)
            return history
        if history[-1]["role"] == "assistant":
            uses = [b for b in history[-1]["content"] if b.get("type") == "tool_use"]
            if not uses and (self.state.get("sleep") or self.state.get("sleep_requested")):
                return self.resume_sleep(history)
            line = self.restart_line()
            if uses:
                content = [{"type": "tool_result", "tool_use_id": b["id"], "content": "(the program restarted; the result of this call was lost)"}
                           for b in uses] + ([env_text(f"{now_text()} {line}")] if line else []) + self.drain_events()
            else:
                content = [env_text(f"{now_text()} {line or 'The program restarted.'}"), *self.drain_events()]
            msg = {"role": "user", "content": content}
            self.store.append_history(tid, msg)
            self.store.save_state(self.state)
            history.append(msg)
        return self.resume_sleep(history)

    def run(self, max_calls: int | None = None) -> str:
        credits_mod.validate(self.cfg["credits"])
        if self.cfg["schedule"].get("morning_at") is not None:
            from .schedule import next_morning
            next_morning(time.time(), self.cfg["schedule"]["morning_at"])
        self.init_layout()
        if self.state["status"] == "exited":
            return "exited"
        if self.state["status"] == "registered":
            self.seed_station_command()  # the interpreter path may have changed since registration
        self.store.finish_rotation()
        self.state.pop("credit_warned", None)  # before the ledger: warnings went to the inbox
        self.restart = self.start_info()
        self.state.pop("last_exit", None)
        self.state["running"] = {"pid": os.getpid(), "since": time.time()}
        self.store.save_state(self.state)
        self.heartbeat(force=True)
        self._exit_reason = "unknown"
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        calls = 0
        failures: list = []  # consecutive failed model calls: (time, LLMError)
        self.watcher = reload_mod.Watcher(self.watch_dirs, [self.root / "station.yaml"])
        try:
            history = self.recover(self.store.load_history(self.state["current"]))
            while not self.stopping and self.state["status"] != "exited":
                if max_calls is not None and calls >= max_calls:
                    self._exit_reason = "max_calls"
                    return "max_calls"
                self.heartbeat()
                self.credit_gate(history)
                self.disk_gate(history)
                history = self.maybe_compress(history)
                if self.prepare_board_recall(history):
                    self.credit_gate(history)  # embeddings may have consumed the remaining allowance
                    # That gate may have waited through a reset. Revalidate locally,
                    # never start a second paid search merely because time passed.
                    self.prepare_board_recall(history, validate_only=True)
                system, tools, messages = self.compose(history)
                try:
                    response = self.call_model(system, tools, messages)
                except LLMError as e:
                    failures.append((time.time(), e))
                    self._model_failed(failures)
                    continue
                if failures:
                    self._model_recovered(failures)
                    failures = []
                calls += 1
                self.record_usage(response.get("usage") or {})
                content = response.get("content") or [{"type": "text", "text": "…"}]
                if response.get("stop_reason") == "max_tokens":
                    self.store.log("max_tokens")
                tid = self.state["current"]
                reply = {"role": "assistant", "content": content}
                self.store.append_history(tid, reply)
                history.append(reply)
                self.store.save_state(self.state)
                nxt = self.handle_reply(content)
                if nxt is not None:
                    hint = self.compress_hint()
                    if hint:
                        nxt["content"].append(hint)
                    self.store.append_history(tid, nxt)
                    history.append(nxt)
                    if self.pending_sleep:
                        # Persist only after the complete batch's tool results are durable.
                        self.state["sleep_requested"] = True
                        self.pending_sleep = False
                        self.store.save_state(self.state)
                    if self.pending_compress is not None:
                        focus, self.pending_compress = self.pending_compress, None
                        new, note = self.compress(tid, history, focus, "tool")
                        if new is not None:
                            history = new
                        else:
                            nxt["content"].append(env_text(note))
                            self.store.rewrite_history(tid, history)
                if self.pending_switch and self.state["status"] == "registered":
                    history = self.recover(self.apply_switch())
                self.store.save_state(self.state)
                self.confirm()
                history = self.resume_sleep(history)
            self._exit_reason = self.state["status"]
            return self.state["status"]
        except Stop:
            self._exit_reason = "stopped"
            return "stopped"
        except Restart:
            self._exit_reason = "reload"
            return "restart"
        except Exception as e:
            self._crashed(e)
            raise
        finally:
            self.state.pop("running", None)
            self.state["last_exit"] = {"ts": time.time(), "reason": self._exit_reason}
            self.store.save_state(self.state)

    def call_model(self, system: list, tools: list, messages: list, tool_choice: str | None = None) -> dict:
        """One model call, through the main model or a fallback (station/router.py)."""
        return self.router.call(system, tools, messages, tool_choice=tool_choice)

    # ---- restarts --------------------------------------------------------

    def confirm(self) -> None:
        """After this process's first completed turn: tell the launcher this code works (state/confirmed.json)."""
        if self._confirmed:
            return
        self._confirmed = True
        path = self.root / "state" / "confirmed.json"
        tmp = path.with_name(f".confirmed.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"ts": time.time(), "pid": os.getpid()}))
        os.replace(tmp, path)

    def heartbeat(self, force: bool = False) -> None:
        """Touch state/heartbeat about once a minute, so a hard kill still leaves a 'down since'."""
        now = time.time()
        if not force and now - self._last_beat < self.cfg["restart"]["heartbeat_seconds"]:
            return
        self._last_beat = now
        path = self.root / "state" / "heartbeat"
        path.touch()
        os.utime(path, (now, now))

    def start_info(self) -> dict | None:
        """Why the previous process ended and for how long Station was down; None for a planned reload."""
        if self.state["status"] != "registered" or not self.store.load_history("main"):
            return None
        last, running = self.state.get("last_exit"), self.state.get("running")
        told = os.environ.get("STATION_PREVIOUS_EXIT")  # from the launcher: the last run crashed
        if told:
            since, reason = float(os.environ.get("STATION_DOWN_SINCE") or time.time()), told
        elif last and (not running or last["ts"] >= running["since"]):
            if last["reason"] in PLANNED_EXITS:
                return None
            since, reason = last["ts"], last["reason"]
        else:  # the previous process never recorded an exit: SIGKILL, OOM killer, power loss
            beat = self.root / "state" / "heartbeat"
            since = beat.stat().st_mtime if beat.exists() else (running or {}).get("since", time.time())
            reason = "killed without a clean exit (for example SIGKILL, out of memory, or a power loss)"
        info = {"since": since, "reason": reason, "down": max(time.time() - since, 0)}
        self.store.log("restart", **info)
        line = self._restart_text(info)
        self.notices.add(line, source="station", key="restart", ttl=86400)
        return info

    @staticmethod
    def _restart_text(info: dict) -> str:
        minutes = int(info["down"] / 60)
        down = f"about {minutes} minutes" if minutes else "under a minute"
        return f"Station restarted at {now_text()} after being down {down} (previous run: {info['reason']})."

    def restart_line(self) -> str | None:
        return self._restart_text(self.restart) if self.restart else None

    def restart_wake_allowed(self) -> bool:
        """At most one restart wake per min_gap_minutes and max_per_day per 24 h, so a crash loop cannot burn tokens."""
        if not self.restart or not self.cfg["restart"]["wake"]:
            return False
        cfg, now = self.cfg["restart"], time.time()
        recent = [t for t in self.state.get("restart_wakes", []) if now - t < 86400]
        if len(recent) >= cfg["max_per_day"] or (recent and now - recent[-1] < cfg["min_gap_minutes"] * 60):
            self.store.log("restart_wake_skipped", recent=len(recent))
            self.notices.add(self._restart_text(self.restart) + " You were not woken: restart wakes are rate-limited.",
                             source="station", key="restart", ttl=86400)
            return False
        self.state["restart_wakes"] = [*recent, now]
        return True

    # ---- errors ----------------------------------------------------------

    def notify(self, text: str) -> None:
        """A notice for the AI, delivered like any event (next tool result or next wake)."""
        self.inbox.put("error", "station", text)

    def _model_failed(self, failures: list) -> None:
        """Log one failed model call, then wait or stop according to its kind."""
        cfg = self.cfg["errors"]
        e: LLMError = failures[-1][1]
        kind = getattr(e, "kind", "transient")
        n = len(failures)
        fatal = kind in errors_mod.FATAL or (kind in errors_mod.RETRY and n >= cfg["max_consecutive"])
        self.store.log("llm_error", error=str(e), error_kind=kind, attempt=n)
        self.errors.record("model", kind, e, fatal=fatal, attempt=n, status=getattr(e, "status", None))
        self.echo(f"! llm error ({kind}, #{n}): {e}")
        if fatal:
            why = ("it will fail the same way every time" if kind in errors_mod.FATAL
                   else f"{n} failures in a row")
            self._exit_reason = f"stopped after a failed model call ({kind})"
            self.notify(f"Station stopped after a failed model call ({kind}: {e}); {why}. "
                        f"It stayed down until the operator started it again. See state/errors.jsonl.")
            raise e
        hint = getattr(e, "retry_after", None)
        if kind in errors_mod.WAIT:
            seconds = cfg["wait_minutes"] * 60 * 2 ** min(n - 1, 10)
        else:
            seconds = cfg["retry_minutes"] * 60
        if hint:
            seconds = max(seconds, hint)
        self._pause(min(seconds, cfg["max_wait_minutes"] * 60))

    def _model_recovered(self, failures: list) -> None:
        first, last = failures[0][0], failures[-1][0]
        kinds = sorted({getattr(e, "kind", "transient") for _, e in failures})
        minutes = max(int((time.time() - first) / 60), 0)
        self.errors.record("model", "recovered", f"{len(failures)} failed calls over {minutes} min", kinds=kinds)
        self.notify(f"Your model calls failed {len(failures)} times ({', '.join(kinds)}) between "
                    f"{now_text(first)} and {now_text(last)}; you were unreachable for about {minutes} minutes. "
                    f"Last error: {errors_mod.clip(str(failures[-1][1]), 300)}")

    def _crashed(self, e: BaseException) -> None:
        """Record why the process is dying, and leave a notice for the next start."""
        if isinstance(e, LLMError):
            return  # already recorded and noticed by _model_failed
        self._exit_reason = f"crashed ({type(e).__name__})"
        try:
            self.errors.record("loop", "crash", f"{type(e).__name__}: {e}", fatal=True, detail=traceback.format_exc())
            self.notify(f"Station crashed ({type(e).__name__}: {errors_mod.clip(errors_mod.redact(e), 300)}) "
                        f"and was down until it was started again. The traceback is in state/errors.jsonl.")
        except Exception:
            pass  # never mask the original error

    def _pause(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end and not self.stopping:
            self.heartbeat()
            time.sleep(min(1, max(end - time.time(), 0.01)))

    def _on_signal(self, *_):
        self.stopping = True
