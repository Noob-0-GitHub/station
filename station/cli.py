"""Command line: station init|run|say|event|outbox|status|errors|check."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import yaml

from . import reload as reload_mod
from .inbox import Inbox
from .loop import Life
from .release import init_repository
from .store import Store

TEMPLATE = """\
# Station runtime config. Defaults: station/config.py
provider: codex     # codex = ChatGPT subscription (dev: Hermes pool; production: codex.token_file)
model: gpt-6.1-sol
effort: medium
credits:
  allowances:
    daily: null   # required: choose a positive number before check/run
    weekly: null
  reset:
    daily_at: "00:00"
    weekly_day: monday
    weekly_at: "00:00"
schedule:
  morning_at: null   # optional local HH:MM daily wake
# provider: anthropic
# model: claude-opus-5-5
# api:
#   base_url: https://api.anthropic.com/v1   # key from $STATION_API_KEY
runner:
  user: null        # run AI commands as this user (requires root)
"""


EXIT_RELOAD = 3  # same value in station/launcher.py


def config_text(root: Path, home: Path | None, docs: Path | None) -> str:
    extra = {}
    if home:
        extra["home"] = str(home.expanduser().resolve())
        extra["secrets"] = {"dir": str(root.resolve() / "auth")}
        extra["codex"] = {"token_file": str(root.resolve() / "auth" / "codex.json")}
    if docs:
        extra["docs_dir"] = str(docs.expanduser().resolve())
    return TEMPLATE + (yaml.safe_dump(extra, sort_keys=False) if extra else "")


def check(root: Path) -> int:
    """Fail if the code on disk cannot import, or the config or templates cannot load. No model call."""
    import importlib
    import pkgutil

    from . import config as config_mod
    from . import loop

    package = Path(__file__).resolve().parent
    for mod in pkgutil.iter_modules([str(package)]):
        importlib.import_module(f"station.{mod.name}")
    raw = yaml.safe_load((root / "station.yaml").read_text()) if (root / "station.yaml").exists() else {}
    if raw is not None and not isinstance(raw, dict):
        raise SystemExit("station.yaml must be a mapping")
    cfg = config_mod.load(root)
    from . import credits as credits_mod

    try:
        credits_mod.validate(cfg["credits"])
        morning = cfg["schedule"].get("morning_at")
        if morning is not None:
            from .schedule import next_morning
            import time
            next_morning(time.time(), morning)
    except ValueError as e:
        print(f"station check: {e}", file=sys.stderr)
        return 2
    for name in ("system.md", "welcome.md", "terms.md", "registered.md", "compress.md", "suggestions.md"):
        if not loop.template(name).strip():
            raise SystemExit(f"template {name} is empty")
    # Construct, but run nothing: board command panels and hooks are the AI's code with side effects.
    life = Life(root, echo=lambda *_: None)
    life.tools()
    life.router.check()  # the main model and every fallback, constructed without a call
    print("ok")
    return 0


def status_text(root: Path, store) -> str:
    """One screen for Anthony: running or not, the model in use, what the AI is doing, money, problems."""
    import time

    from . import clock, config as config_mod, credits as credits_mod
    from .errors import ErrorLog
    from .loop import now_text
    from .notices import Notices

    cfg = config_mod.load(root)
    state = store.load_state()
    now = time.time()
    beat = root / "state" / "heartbeat"
    age = now - beat.stat().st_mtime if beat.exists() else None
    period = cfg["restart"]["heartbeat_seconds"]
    if age is None:
        running = "never started"
    elif age <= 3 * period:
        running = f"running (heartbeat {int(age)} s ago)"
    else:
        running = f"not running (last heartbeat {now_text(now - age)})"
    lines = [f"Station: {running}"]

    from .router import thread_config, routes_for

    tid = state.get("current", "main")
    thread = state.get("threads", {}).get(tid, {})
    effective = thread_config(cfg, state, tid)
    routes = routes_for(effective)
    route = (state if tid == "main" else thread).get("model_route") or {}
    main = f"{routes[0]['provider']}/{routes[0]['model']}"
    fallbacks = [f"{f['provider']}/{f['model']}" for f in routes[1:]]
    active = route.get("active", 0)
    if active and active <= len(fallbacks):
        lines.append(f"Model: {fallbacks[active - 1]} (FALLBACK since {now_text(route.get('since') or now)}; "
                     f"main {main} failed: {route.get('reason', '?')})")
    else:
        lines.append(f"Model: {main}" + (f"; fallback {', '.join(fallbacks)}" if fallbacks else "; no fallback"))

    name = state.get("name") or "(not registered)"
    tid = state.get("current", "main")
    thread = state.get("threads", {}).get(tid, {})
    lines.append(f"AI: {name}, {state.get('status')}; thread {tid} \"{thread.get('title', '')}\""
                 + (f", context {thread['context']:,} tokens" if thread.get("context") else ""))
    sleep = state.get("sleep")
    if sleep:
        nxt = sleep.get("next")
        lines.append("Doing: asleep" + (f", next wake {now_text(max(nxt, now))}" if nxt else ""))
    elif state.get("last_wake"):
        lines.append(f"Doing: awake since {now_text(state['last_wake'])}")
    open_threads = [k for k, t in state.get("threads", {}).items() if t.get("status") == "active"]
    if len(open_threads) > 1:
        lines.append(f"Threads open: {', '.join(open_threads)}")

    try:
        st = credits_mod.status(store.ledger, cfg["credits"], now)
        credit_text = "; ".join(f"{k} {v['used']:.2f}/{v['allowance']:g} ({credits_mod.pct(v['ratio'])})"
                                for k, v in st.items())
    except ValueError as e:
        credit_text = f"not configured ({e})"
    calls, tokens = store.ledger.model_usage(clock.wall(clock.today(), 0, 0))
    lines.append("Credits: " + credit_text + f"; model today {calls} calls, {tokens:,} tokens")

    notices = Notices(root / "state").list(unread=True)
    if notices:
        lines.append(f"Notices unread: {len(notices)}")
        lines += [f"  [{n['priority']}] {n['source']}: {n['text'][:160]}" for n in notices[:5]]
    recent = [e for e in ErrorLog(root / "state").recent(50) if now - e["ts"] < 86400]
    if recent:
        last = recent[-1]
        lines.append(f"Errors in 24 h: {len(recent)}; last {now_text(last['ts'])} "
                     f"{last['where']}/{last['kind']}: {str(last['message'])[:160]}")
    else:
        lines.append("Errors in 24 h: none")
    return "\n".join(lines)


def notify_cmd(root: Path, args) -> int:
    from .notices import Notices, parse_duration

    notices = Notices(root / "state")
    if args.clear:
        n = notices.clear(args.clear, source=args.source if args.source != "manual" else None)
        print(f"cleared {n}")
        return 0
    if not args.text:
        raise SystemExit("station notify: text is required (or --clear KEY)")
    try:
        row = notices.add(args.text, source=args.source, key=args.key, priority=args.priority,
                          ttl=parse_duration(args.ttl))
    except ValueError as e:
        raise SystemExit(f"station notify: {e}")
    print(f"#{row['id']}")
    return 0


def notices_cmd(root: Path, args) -> int:
    from .notices import Notices

    notices = Notices(root / "state")
    if args.action == "read":
        if not args.all and not args.ids:
            raise SystemExit("station notices read: give IDs or --all")
        try:
            count = notices.mark_read(None if args.all else args.ids)
        except ValueError as e:
            raise SystemExit(f"station notices: {e}")
        print(f"marked {count} read")
        return 0
    rows = notices.list(unread=args.unread or args.quiet, min_priority=args.min_priority)
    if args.quiet:
        return 1 if rows else 0
    for row in rows:
        if args.json:
            print(json.dumps(row, ensure_ascii=False))
        else:
            print(("  " if row.get("read") else "* ") + Notices.line(row, width=MAX_LINE))
    if not rows and not args.json:
        print("no notices")
    return 0


MAX_LINE = 2000


def credits_cmd(root: Path, args) -> int:
    from . import config as config_mod
    from . import credits as credits_mod
    from .loop import now_text

    cfg = config_mod.load(root)["credits"]
    ledger = Store(root / "state").ledger
    now = time.time()
    st = credits_mod.status(ledger, cfg, now)
    services = {k: ledger.by_service(v["start"]) for k, v in st.items()}
    rows = ledger.rows(limit=args.log) if args.log else []
    if args.json:
        print(json.dumps({**{k: {**v, "by_service": services[k]} for k, v in st.items()},
                          "warn_ratio": cfg["warn_ratio"], "limit_ratio": cfg["limit_ratio"],
                          **({"log": rows} if args.log else {})}, ensure_ascii=False))
        return 0
    for kind, s in st.items():
        print(f"{kind}: {credits_mod.pct(s['ratio'])} used, resets {now_text(s['reset'])}")
        total = s["used"] or 1.0
        for name, v in services[kind].items():
            extra = f", {v['unpriced']} unpriced" if v["unpriced"] else ""
            print(f"  {name}: {credits_mod.pct(v['credits'] / s['allowance'] if s['allowance'] else 0)} of the allowance"
                  f" ({v['credits'] / total:.0%} of the {kind} use; {v['calls']} calls{extra})")
    print(f"the credits panel shows from {credits_mod.pct(cfg['warn_ratio'])}; "
          f"no model or paid API calls from {credits_mod.pct(cfg['limit_ratio'])}")
    for r in rows:
        price = "unpriced" if r["list_usd"] is None else f"{r['credits']:.4f} credits"
        print(f"{now_text(r['ts'])}  {r['service']:<8} {(r['model'] or r['kind'] or ''):<24} {price}  {r['what'] or ''}")
    return 0


def parse_since(text: str | None) -> float | None:
    if not text:
        return None
    m = re.fullmatch(r"(\d+)([dh])", text.strip())
    if m:
        return time.time() - int(m.group(1)) * (86400 if m.group(2) == "d" else 3600)
    import datetime as dt

    from . import clock

    try:
        return clock.wall(dt.date.fromisoformat(text.strip()), 0, 0)
    except ValueError:
        raise SystemExit("--since takes a date (2026-10-31) or an age (7d, 12h)") from None


def memory_cmd(root: Path, args) -> int:
    from . import config as config_mod
    from .loop import now_text
    from .memory import Memory, MemError

    cfg = config_mod.load(root)
    if not cfg["memory"]["enabled"]:
        print("memory is switched off (memory.enabled)", file=sys.stderr)
        return 2
    try:
        mem = Memory(root, cfg, Store(root / "state").ledger)
        if args.mcmd == "search":
            results, note = mem.search(args.query, args.n, args.kind, parse_since(args.since))
            if args.json:
                print(json.dumps({"results": results, "note": note}, ensure_ascii=False))
                return 0
            if note:
                print(f"({note})")
            for r in results:
                when = now_text(r["ts"]) if r["ts"] else "undated"
                body = r["text"] if args.full else (r["text"][:400].rstrip() + ("…" if len(r["text"]) > 400 else ""))
                print(f"[{r['id']}] {r['title']} · {when} · by {' and '.join(r['how'])}\n{body}\n")
            if not results:
                print("nothing found")
            return 0
        if args.mcmd == "add":
            text = sys.stdin.read() if args.text == "-" else args.text
            print(f"saved as memory {mem.add_note(text)}")
            return 0
        if args.mcmd == "show":
            r = mem.show(args.id)
            print(f"[{r['id']}] {r['title']} · {now_text(r['ts']) if r['ts'] else 'undated'} · "
                  f"from {', '.join(r['sources']) or 'nowhere (about to be removed)'}"
                  f"{'' if r['embedded'] else ' · not embedded yet'}\n{r['text']}")
            return 0
        if args.mcmd == "forget":
            mem.forget(args.id)
            print(f"forgot {args.id}")
            return 0
        if args.mcmd == "sync":
            r = mem.sync(max_chunks=args.max, wait=True)
            print(f"read {r['changed']} changed files, embedded {r['embedded']} chunks, {r['pending']} waiting"
                  + (f"; stopped: {r['error']}" if r["error"] else ""))
            for f in r["failed"]:
                print(f"  could not read {f}", file=sys.stderr)
            return 1 if r["error"] else 0
        if args.mcmd == "status":
            st = mem.status()
            print(f"{st['model']}, {st['dimensions']} dimensions; indexing {', '.join(st['indexed'])}; "
                  f"{st['sources']} files; {st['bytes'] / 1e6:.1f} MB")
            for kind, v in sorted(st["kinds"].items()):
                print(f"  {kind}: {v['chunks']} chunks" + (f", {v['pending']} not embedded yet" if v["pending"] else ""))
            return 0
    except (MemError, ValueError) as e:
        print(f"station memory: {e}", file=sys.stderr)
        return 2
    return 1


def record_cmd(root: Path, cfg: dict, args) -> int:
    """`station api SERVICE --record USD --what ...`: a cost Station could not meter, entered by hand."""
    from . import credits as credits_mod

    name = args.service.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,31}", name) or name == "model":
        print("station api: the service must be a short lowercase name (not 'model')", file=sys.stderr)
        return 2
    if args.method or args.path or args.body is not None or args.form:
        print("station api: --record makes no call; give only SERVICE, --record and --what", file=sys.stderr)
        return 2
    if not (0 <= args.record < 1000):
        print("station api: --record must be between 0 and 1000 US dollars", file=sys.stderr)
        return 2
    c = cfg["credits"]
    row = Store(root / "state").ledger.add(name, what=args.what[:200] or None, units={"usd": args.record},
                                           list_usd=args.record, paid_usd=credits_mod.paid(c, name, args.record),
                                           credits=args.record * credits_mod.factor(c, name), source="manual")
    print(f"recorded {args.record:.4f} USD on {name} ({row['credits']:.4f} credits)")
    return 0


def api_cmd(root: Path, args) -> int:
    from . import api as api_mod
    from . import config as config_mod

    cfg = config_mod.load(root)
    if args.record is not None:
        return record_cmd(root, cfg, args)
    if not args.method or not args.path:
        print("station api: give METHOD and PATH, or --record USD", file=sys.stderr)
        return 2
    body = None
    if args.body is not None:
        raw = sys.stdin.read() if args.body == "-" else (
            Path(args.body[1:]).read_text() if args.body.startswith("@") else args.body)
        try:
            body = json.loads(raw)
        except ValueError as e:
            print(f"--json is not valid JSON: {e}", file=sys.stderr)
            return 2
    form, files = {}, {}
    for item in args.form:
        name, sep, value = item.partition("=")
        if not sep or not name:
            print(f"-F needs NAME=VALUE: {item!r}", file=sys.stderr)
            return 2
        if value.startswith("@"):
            files[name] = Path(value[1:])
            if not files[name].is_file():
                print(f"no such file: {value[1:]}", file=sys.stderr)
                return 2
        else:
            form[name] = value
    if body is not None and (form or files):
        print("use either --json or -F, not both", file=sys.stderr)
        return 2
    try:
        result = api_mod.request(cfg, Store(root / "state").ledger, args.service, args.method, args.path,
                                 json_body=body, form=form, files=files, what=args.what, timeout=args.timeout)
    except api_mod.ApiError as e:
        print(f"station api: {e}", file=sys.stderr)
        if e.body:
            sys.stderr.write(e.body.decode(errors="replace") + "\n")
        return {"usage": 2, "config": 2, "limit": 4}.get(e.kind, 1)
    if args.output:
        args.output.write_bytes(result.body)
    else:
        sys.stdout.buffer.write(result.body)
        if not result.body.endswith(b"\n"):
            sys.stdout.write("\n")
    if result.row is not None:
        cost = "unpriced (no usage in the response)" if result.row["list_usd"] is None else f"{result.row['credits']:.6f} credits"
        print(f"station api: {cost}", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="station")
    env_root = os.environ.get("STATION_ROOT")
    p.add_argument("--root", type=Path, default=Path(env_root) if env_root else None, required=not env_root,
                   help="runtime directory (default: $STATION_ROOT)")
    sub = p.add_subparsers(dest="cmd", required=True)
    init = sub.add_parser("init", help="create the runtime directory and station.yaml")
    init.add_argument("--home", type=Path, help="the AI's home (production: the user's home); also puts API keys in <root>/auth")
    init.add_argument("--docs-dir", type=Path, help="user docs listed on the registration screen")
    init.add_argument("--code-dir", type=Path, help="initialize upstream/main in an extracted release")
    run = sub.add_parser("run")
    run.add_argument("--max-calls", type=int, default=None)
    say = sub.add_parser("say", help="queue a local message with unknown source")
    say.add_argument("text")
    ev = sub.add_parser("event")
    ev.add_argument("--type", required=True)
    ev.add_argument("--source", default="system")
    ev.add_argument("text")
    sub.add_parser("outbox")
    st = sub.add_parser("status", help="is it running, which model, what it is doing, credits, notices, errors")
    st.add_argument("--json", action="store_true", help="the raw state and totals")
    nf = sub.add_parser("notify", help="add or update a notice (shown on the board until read or expired)")
    nf.add_argument("text", nargs="?")
    nf.add_argument("--source", default="manual")
    nf.add_argument("--key", help="same source and key update one notice instead of adding another")
    nf.add_argument("--priority", default="normal", choices=["low", "normal", "high"])
    nf.add_argument("--ttl", help="expire after this long: 30m, 2h, 1d (default: never)")
    nf.add_argument("--clear", metavar="KEY", help="remove the notices with this key (the thing was handled)")
    ns = sub.add_parser("notices", help="list notices; `read ID...` or `read --all` marks them read")
    ns.add_argument("action", nargs="?", choices=["list", "read"], default="list")
    ns.add_argument("ids", nargs="*", type=int)
    ns.add_argument("--all", action="store_true", help="with read: every unread notice")
    ns.add_argument("--unread", action="store_true")
    ns.add_argument("--min-priority", default="low", choices=["low", "normal", "high"])
    ns.add_argument("--json", action="store_true")
    ns.add_argument("--quiet", action="store_true",
                    help="print nothing; exit 1 if any notice matches, else 0 (a schedule hook: 0 skips the wake)")
    cr = sub.add_parser("credits", help="credits used this day and week, against the allowances")
    cr.add_argument("--json", action="store_true")
    cr.add_argument("--log", type=int, metavar="N", help="also list the newest N ledger rows")
    mem = sub.add_parser("memory", help="search your diary, conversations and notes; save notes")
    msub = mem.add_subparsers(dest="mcmd", required=True)
    ms = msub.add_parser("search", help="find memories by meaning and by words")
    ms.add_argument("query")
    ms.add_argument("-n", type=int, default=8)
    ms.add_argument("--kind", choices=["diary", "chat", "note"])
    ms.add_argument("--since", help="only memories from this date on (YYYY-MM-DD) or this far back (7d, 12h)")
    ms.add_argument("--full", action="store_true", help="whole chunks instead of excerpts")
    ms.add_argument("--json", action="store_true")
    ma = msub.add_parser("add", help="save a note to memory ('-' reads stdin)")
    ma.add_argument("text")
    msh = msub.add_parser("show", help="one memory in full, with where it came from")
    msh.add_argument("id", type=int)
    mf = msub.add_parser("forget", help="delete a saved note")
    mf.add_argument("id", type=int)
    my = msub.add_parser("sync", help="index new material now")
    my.add_argument("--max", type=int, default=2000, help="embed at most this many chunks (default 2000)")
    msub.add_parser("status", help="what is indexed, and how much is waiting")
    ap = sub.add_parser("api", help="call a paid API with Station's key (the cost is metered from the response), "
                                    "or --record a cost by hand")
    ap.add_argument("service", help="openai or tavily; with --record, any short lowercase name")
    ap.add_argument("method", nargs="?", help="GET, POST or DELETE")
    ap.add_argument("path", nargs="?", help="e.g. /v1/embeddings")
    ap.add_argument("--record", type=float, metavar="USD",
                    help="make no call; record that this much (US dollars at list price) was spent on SERVICE")
    ap.add_argument("--json", dest="body", metavar="JSON", help="JSON body; @FILE reads it from a file, - from stdin")
    ap.add_argument("-F", "--form", action="append", default=[], metavar="NAME=VALUE",
                    help="multipart field; NAME=@FILE uploads a file (repeatable)")
    ap.add_argument("--what", default="", help="what it is for (shown in `station credits --log`)")
    ap.add_argument("-o", "--output", type=Path, help="write the response body here instead of stdout")
    ap.add_argument("--timeout", type=float, default=600)
    errs = sub.add_parser("errors", help="recent entries of state/errors.jsonl (redacted)")
    errs.add_argument("-n", type=int, default=20)
    errs.add_argument("--json", action="store_true")
    sub.add_parser("check", help="import all code and load config and templates without calling a model")
    args = p.parse_args(argv)

    root: Path = args.root
    if args.cmd == "init":
        code_dir = args.code_dir or Path(__file__).resolve().parent.parent
        if args.code_dir or (code_dir / "RELEASE").is_file():
            init_repository(code_dir)
            from . import launcher

            print(f"launcher: {launcher.install(code_dir.expanduser().resolve())}")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        cfg = root / "station.yaml"
        if cfg.exists():
            print(f"{cfg} exists; left unchanged")
        else:
            cfg.write_text(config_text(root, args.home, args.docs_dir))
        if args.home:
            auth = root.resolve() / "auth"
            if auth.is_symlink():
                raise ValueError(f"auth directory must not be a symlink: {auth}")
            auth.mkdir(mode=0o700, exist_ok=True)
            os.chmod(auth, 0o700)
        Life(root).init_layout()
        print(f"initialized {root}")
        return 0
    if args.cmd == "run":
        result = Life(root).run(max_calls=args.max_calls)
        print(f"loop ended: {result}")
        if result == "restart":
            if os.environ.get("STATION_LAUNCHER"):
                return EXIT_RELOAD  # the launcher checkpoints the new code and starts it
            reload_mod.restart(root)
        return 0
    if args.cmd == "check":
        return check(root)
    if args.cmd == "say":
        Inbox(root / "inbox").put("message", "unknown", args.text)
        return 0
    if args.cmd == "event":
        Inbox(root / "inbox").put(args.type, args.source, args.text)
        return 0
    if args.cmd == "notify":
        return notify_cmd(root, args)
    if args.cmd == "notices":
        return notices_cmd(root, args)
    if args.cmd == "credits":
        return credits_cmd(root, args)
    if args.cmd == "api":
        return api_cmd(root, args)
    if args.cmd == "memory":
        return memory_cmd(root, args)
    if args.cmd == "errors":
        from .errors import ErrorLog
        from .loop import now_text

        for row in ErrorLog(root / "state").recent(args.n):
            if args.json:
                print(json.dumps(row, ensure_ascii=False))
            else:
                flag = " FATAL" if row.get("fatal") else ""
                print(f"{now_text(row['ts'])} {row['where']}/{row['kind']}{flag}: {row['message']}")
        return 0
    store = Store(root / "state")
    if args.cmd == "outbox":
        if store.outbox_path.exists():
            sys.stdout.write(store.outbox_path.read_text())
        return 0
    if args.cmd == "status":
        if not args.json:
            print(status_text(root, store))
            return 0
        state = store.load_state()
        t = store.ledger.totals()
        print(json.dumps({**state, "calls": t["rows"], "est_cost_usd": round(t["list_usd"], 4),
                          "paid_usd": round(t["paid_usd"], 4), "credits": round(t["credits"], 4)},
                         ensure_ascii=False, indent=1))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
