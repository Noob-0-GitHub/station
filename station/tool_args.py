"""Action-scoped Focus argument handling; never globally discard falsy values."""

MODEL_FIELDS = frozenset({"provider", "model", "effort", "fallback", "inherit"})
VIEW_FIELDS = frozenset({"hide_boards", "hide_tags"})
SETTING_FIELDS = MODEL_FIELDS | VIEW_FIELDS
MODEL_ACTIONS = frozenset({"new", "switch", "model"})
VIEW_ACTIONS = frozenset({"new", "switch", "view"})
ACTIONS = MODEL_ACTIONS | VIEW_ACTIONS | {"close"}


def _placeholder(field: str, value) -> bool:
    """Field-specific optional placeholders, only when irrelevant or unselected.

    Never let Python's 0 == False or empty containers turn wrong types into
    valid strings, booleans or lists. Selected settings bypass this helper.
    """
    if value is None:
        return True
    if field in {"provider", "model", "effort", "context"}:
        return isinstance(value, str) and value == ""
    if field in {"fallback", "hide_boards", "hide_tags"}:
        return isinstance(value, list) and not value
    if field == "inherit":
        return value is False
    return False


def focus_args(args: dict) -> dict:
    """Return a copy with irrelevant placeholders removed, before any state mutation.

    `changes` is an explicit setting-field mask. Its presence (null/[] included)
    disambiguates placeholders from intentional clears. Without it, relevant
    fields retain legacy omission/empty-value semantics.
    """
    out = dict(args)
    action = out.get("action")
    if not isinstance(action, str) or action not in ACTIONS:
        raise ValueError(f"unknown action: {action}")
    allowed = set()
    if action in MODEL_ACTIONS:
        allowed.update(MODEL_FIELDS)
    if action in VIEW_ACTIONS:
        allowed.update(VIEW_FIELDS)
    explicit = "changes" in out
    selected = set()
    if explicit:
        changes = out.pop("changes")
        if changes is None:
            changes = []
        if not isinstance(changes, list) or any(not isinstance(k, str) for k in changes):
            raise ValueError("changes must be an array of setting field names, or null")
        selected = set(changes)
        if len(selected) != len(changes):
            raise ValueError("changes must not contain duplicate fields")
        unknown = selected - SETTING_FIELDS
        if unknown:
            raise ValueError("unknown changes fields: " + ", ".join(sorted(unknown)))
        irrelevant = selected - allowed
        if irrelevant:
            raise ValueError(f"changes fields not supported by {action}: " + ", ".join(sorted(irrelevant)))
        missing = selected - out.keys()
        if missing:
            raise ValueError("changes lists fields without values: " + ", ".join(sorted(missing)))

    # An omitted legacy mask still treats a relevant [] as a deliberate clear.
    # Do not turn a previously invalid all-empty model bundle into a silent reset.
    blank_pair = all(k in out and out[k] in (None, "") for k in ("provider", "model"))
    if not explicit and action in MODEL_ACTIONS and blank_pair and any(k in out for k in ("effort", "fallback")):
        raise ValueError("ambiguous empty model settings; use changes=[] to leave settings unchanged, "
                         "or list the settings to change")

    for key in SETTING_FIELDS:
        if key not in out:
            continue
        if key not in allowed or (explicit and key not in selected):
            if not _placeholder(key, out[key]):
                if key not in allowed:
                    category = "board visibility" if key in VIEW_FIELDS else "model"
                    raise ValueError(f"{category} setting {key} is not supported by {action}")
                raise ValueError(f"{key} has a value but is not selected for {action}; check changes and action")
            del out[key]
    # Optional model names cannot intentionally be cleared to empty; selected
    # empty names are left for Router.configure's normal required-value errors.
    if not explicit:
        for key in ("provider", "model"):
            if out.get(key) in (None, ""):
                out.pop(key, None)
    if action == "new" and out.get("id") in (None, ""):
        out.pop("id", None)
    if action in ("view", "model") and "context" in out:
        if not _placeholder("context", out["context"]):
            raise ValueError(f"{action} does not carry context; use new, switch or close")
        del out["context"]
    return out
