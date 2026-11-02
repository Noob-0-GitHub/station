"""Per-thread board visibility. Rules affect rendering, never file access."""

from __future__ import annotations

from .board import NAME_RE

VIEW_FIELDS = ("hide_boards", "hide_tags")


def _selectors(value, *, names: bool) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("hide_boards and hide_tags must be arrays of strings")
    out = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or any(ord(c) < 32 or ord(c) == 127 for c in item):
            raise ValueError("board visibility selectors must be nonempty strings without control characters")
        if names and (not NAME_RE.fullmatch(item) or item == "hidden"):
            raise ValueError("invalid board name; the hidden overview cannot be hidden")
        if item not in out:
            out.append(item)
    return out


def parse_update(args: dict) -> dict:
    """Validate before any Focus/model state is changed; omitted fields stay unchanged."""
    return {key: _selectors(args[field], names=key == "names")
            for field, key in (("hide_boards", "names"), ("hide_tags", "tags")) if field in args}


def get(thread: dict) -> dict:
    raw = thread.get("board_hidden", {})
    if not isinstance(raw, dict) or set(raw) - {"names", "tags"}:
        raise ValueError("invalid thread board_hidden state")
    return {"names": _selectors(raw.get("names", []), names=True),
            "tags": _selectors(raw.get("tags", []), names=False)}


def configure(thread: dict, update: dict) -> None:
    view = get(thread)
    view.update(update)
    if view["names"] or view["tags"]:
        thread["board_hidden"] = view
    else:
        thread.pop("board_hidden", None)


def change(thread: dict, args: dict) -> dict:
    """Apply board hide/show only to this thread; validate the whole request first."""
    action = args.get("action")
    if action not in ("hide", "show"):
        raise ValueError("expected hide or show")
    if "all" in args and not isinstance(args["all"], bool):
        raise ValueError("all must be a boolean")
    selected = [key for key in ("name", "tag") if args.get(key) is not None]
    if args.get("all"):
        if action != "show" or selected:
            raise ValueError("all is only for show, without name or tag")
        configure(thread, {"names": [], "tags": []})
        return get(thread)
    if len(selected) != 1:
        raise ValueError("hide/show needs exactly one name or tag; show all=true clears this thread's rules")
    selector = selected[0]
    key = "names" if selector == "name" else "tags"
    item = _selectors([args[selector]], names=key == "names")[0]
    view = get(thread)
    if action == "hide" and item not in view[key]:
        view[key].append(item)
    elif action == "show":
        view[key] = [x for x in view[key] if x != item]
    configure(thread, view)
    return get(thread)
