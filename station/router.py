"""Which model answers: the main model (`provider`/`model`), and `fallback` models when it fails.

- Default: no fallback, so the main model's errors go straight to the loop (errors.py).
- With fallbacks: a failed main call is tried on each fallback in order, in the same turn. The
  switch is an error the AI is told about (an `error` event and a notice that stays until the
  main model is back), and it is in `state/errors.jsonl`.
- While on a fallback, the main model is tried again at most `fallback_retry_per_hour` times an
  hour (evenly spaced: 3 means every 20 minutes). When it answers, Station switches back and says so.
- If every model fails, the first error (the main model's, when it was tried) goes to the loop,
  which waits or stops by its kind as before.
- A request rejected because of old reasoning is sent once more without it, on the same model.

Each thread has its own model override and failover state. The legacy top-level model_route
belongs to main; other threads store model_route in their thread entry. Both survive restarts.
"""

from __future__ import annotations

import copy
import json
import time

from .llm import LLMError, make_llm

NOTICE_KEY = "model-fallback"


def thread_config(cfg: dict, state: dict, tid: str | None = None) -> dict:
    tid = tid or state.get("current", "main")
    override = state.get("threads", {}).get(tid, {}).get("model_override") or {}
    return {**cfg, **override}


def routes_for(cfg: dict) -> list[dict]:
    main = {"provider": cfg.get("provider"), "model": cfg["model"], "effort": cfg.get("effort")}
    out = [main]
    fallback = cfg.get("fallback") or []
    if not isinstance(fallback, list):
        raise LLMError("fallback must be a list", "config")
    for entry in fallback:
        if (not isinstance(entry, dict) or set(entry) - {"provider", "model", "effort"}
                or not isinstance(entry.get("provider"), str) or not entry["provider"].strip()
                or not isinstance(entry.get("model"), str) or not entry["model"].strip()):
            raise LLMError("each fallback needs provider and model; only effort is optional", "config")
        # Inheriting a global fallback identical to the thread's selected model must not retry itself.
        if any(r["provider"] == entry["provider"] and r["model"] == entry["model"] for r in out):
            continue
        out.append({"provider": entry["provider"], "model": entry["model"],
                    "effort": entry.get("effort", main["effort"])})
    return out


class Router:
    def __init__(self, life):
        self.life = life
        self.clients: dict = {}
        self.thread_clients: dict = {}

    # ---- routes ----------------------------------------------------------

    @property
    def tid(self) -> str:
        return self.life.state.get("current", "main")

    @property
    def cfg(self) -> dict:
        return thread_config(self.life.cfg, self.life.state)

    @property
    def notice_key(self) -> str:
        return NOTICE_KEY if self.tid == "main" else f"{NOTICE_KEY}:{self.tid}"

    def routes(self) -> list[dict]:
        return routes_for(self.cfg)

    def configure(self, tid: str, args: dict) -> bool:
        """Validate before mutation; construction never makes an inference/auth request."""
        fields = {k: copy.deepcopy(args[k]) for k in ("provider", "model", "effort", "fallback") if k in args}
        inherit = args.get("inherit", False)
        if type(inherit) is not bool:
            raise ValueError("inherit must be a boolean")
        if not fields and not inherit:
            return False
        if inherit and fields:
            raise ValueError("inherit cannot be combined with model settings")
        if ("provider" in fields) != ("model" in fields):
            raise ValueError("changing a model requires both provider and model")
        for k in ("provider", "model"):
            if k in fields and (not isinstance(fields[k], str) or not fields[k].strip()):
                raise ValueError(f"{k} must be a nonempty string")
        if "effort" in fields and fields["effort"] is not None and not isinstance(fields["effort"], str):
            raise ValueError("effort must be a string or null")
        if "fallback" in fields and not isinstance(fields["fallback"], list):
            raise ValueError("fallback must be a list (use [] to disable)")
        thread = self.life.state["threads"][tid]
        previous = thread.get("model_override") or {}
        candidate = {} if inherit else {**previous, **fields}
        config = {**self.life.cfg, **candidate}
        try:
            for route in routes_for(config):
                make_llm({**config, **route})
        except LLMError as e:
            raise ValueError(str(e)) from None
        if candidate == previous:
            return False
        if candidate:
            thread["model_override"] = candidate
        else:
            thread.pop("model_override", None)
        if tid == "main":
            self.life.state.pop("model_route", None)
        else:
            thread.pop("model_route", None)
        self.life.notices.clear(NOTICE_KEY if tid == "main" else f"{NOTICE_KEY}:{tid}", source="station")
        self.thread_clients = {k: v for k, v in self.thread_clients.items() if k[0] != tid}
        self.life.store.log("thread_model", thread=tid, override=candidate)
        return True

    @staticmethod
    def name(route: dict) -> str:
        return f"{route['provider']}/{route['model']}"

    def route_cfg(self, route: dict) -> dict:
        return {**self.cfg, **route}

    def client(self, i: int):
        if self.life.state["threads"][self.tid].get("model_override"):
            route = self.routes()[i]
            key = (self.tid, json.dumps(route, sort_keys=True))
            if key not in self.thread_clients:
                self.thread_clients[key] = make_llm(self.route_cfg(route))
            return self.thread_clients[key]
        if i == 0:  # the main client is life.llm, so tests and the rest of Station see one object
            if self.life.llm is None:
                self.life.llm = make_llm(self.life.cfg)
            return self.life.llm
        if i not in self.clients:
            self.clients[i] = make_llm(self.route_cfg(self.routes()[i]))
        return self.clients[i]

    def check(self) -> None:
        """Construct every route's client (no network), so `station check` catches a bad fallback."""
        for tid, thread in self.life.state.get("threads", {}).items():
            if thread.get("status") != "active":
                continue
            cfg = thread_config(self.life.cfg, self.life.state, tid)
            for route in routes_for(cfg):
                make_llm({**cfg, **route})

    @property
    def state(self) -> dict:
        owner = self.life.state if self.tid == "main" else self.life.state["threads"][self.tid]
        return owner.setdefault("model_route", {"active": 0})

    def active(self) -> dict:
        routes = self.routes()
        i = self.state.get("active", 0)
        return routes[i] if i < len(routes) else routes[0]

    # ---- calls -----------------------------------------------------------

    def attempt(self, i: int, system, tools, messages, tool_choice):
        from .loop import has_old_reasoning, strip_old_reasoning

        llm = self.client(i)
        try:
            return llm.call(system, tools, messages, tool_choice=tool_choice)
        except LLMError as e:
            if e.kind != "request" or not has_old_reasoning(messages):
                raise
            self.life.errors.record("model", "request", f"rejected with old reasoning; retrying without it: {e}",
                                    model=self.name(self.routes()[i]))
            self.life.store.log("reasoning_dropped", model=self.name(self.routes()[i]), error=str(e)[:300])
            return llm.call(system, tools, strip_old_reasoning(messages), tool_choice=tool_choice)

    def main_due(self, now: float) -> bool:
        per_hour = max(int(self.cfg.get("fallback_retry_per_hour") or 0), 0)
        if not per_hour:
            return False
        return now - (self.state.get("main_tried") or 0) >= 3600 / per_hour

    def call(self, system, tools, messages, tool_choice=None) -> dict:
        routes = self.routes()
        st = self.state
        if st.get("active", 0) >= len(routes):  # a fallback was removed from the config
            st.update(active=0)
        now = time.time()
        active = st.get("active", 0)
        order = ([0] if active == 0 or self.main_due(now) else []) + list(range(1, len(routes)))
        if active > 0:  # the fallback in use first, then the others in order
            order = [i for i in order if i == 0] + [active] + [i for i in order if i not in (0, active)]
        errors: list[tuple[int, LLMError]] = []
        for i in order:
            if i == 0 and active > 0:
                st["main_tried"] = now
            try:
                response = self.attempt(i, system, tools, messages, tool_choice)
            except LLMError as e:
                errors.append((i, e))
                if len(routes) > 1:
                    self.life.store.log("model_failed", model=self.name(routes[i]), error_kind=e.kind,
                                        error=str(e)[:300], fallback=i > 0)
                continue
            self.answered(i, routes, errors, now)
            self.life.last_route = routes[i]
            return response
        raise errors[0][1]

    def answered(self, i: int, routes: list[dict], errors: list, now: float) -> None:
        st, life = self.state, self.life
        active = st.get("active", 0)
        if i == active:
            return
        main_error = next((e for j, e in errors if j == 0), None)
        if i == 0:  # the main model is back
            minutes = int((now - (st.get("since") or now)) / 60)
            life.errors.record("model", "recovered", f"main model {self.name(routes[0])} answers again "
                               f"after about {minutes} min on {self.name(routes[active])}")
            life.notify(f"Your main model {self.name(routes[0])} answers again; Station switched back from "
                        f"{self.name(routes[active])} after about {minutes} minutes.")
            life.notices.clear(self.notice_key, source="station")
            st.clear()
            st.update(active=0)
            life.store.save_state(life.state)
            return
        if active == 0:  # leaving the main model
            reason = f"{main_error.kind}: {main_error}" if main_error else "unknown"
            per_hour = life.cfg.get("fallback_retry_per_hour") or 0
            retry = (f"it tries the main model again up to {per_hour} times an hour" if per_hour
                     else "it does not try the main model again until restarted")
            life.errors.record("model", main_error.kind if main_error else "transient",
                               f"main model {self.name(routes[0])} failed; switched to {self.name(routes[i])}: {reason}",
                               fallback=self.name(routes[i]))
            life.notify(f"Your main model {self.name(routes[0])} failed ({reason}). Station switched to "
                        f"{self.name(routes[i])} so you can go on; {retry} and tells you when it is back.")
            life.notices.add(f"Thread {self.tid}: running on the fallback model {self.name(routes[i])} since {_clock(now)}: "
                             f"{self.name(routes[0])} failed ({main_error.kind if main_error else 'error'}).",
                             source="station", key=self.notice_key, priority="high")
            st.update(active=i, since=now, main_tried=now, reason=reason[:300])
        else:  # from one fallback to another
            life.store.log("model_switch", to=self.name(routes[i]), frm=self.name(routes[active]))
            st["active"] = i
            life.notices.add(f"Thread {self.tid}: running on the fallback model {self.name(routes[i])} "
                             f"({self.name(routes[0])} and {self.name(routes[active])} failed).",
                             source="station", key=self.notice_key, priority="high")
        life.store.save_state(life.state)


def _clock(ts: float) -> str:
    from .loop import now_text

    return now_text(ts)
