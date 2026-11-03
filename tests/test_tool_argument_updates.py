"""New offline coverage for explicit Focus settings and action-scoped empties."""

import copy

import pytest

from station import router as router_mod
from station.tool_args import focus_args
from station.tools import LIFE_TOOLS
from test_loop import make
from test_tool_placeholder_audit import FOCUS_EMPTY


def life_with_settings(tmp_path):
    life = make(tmp_path, [])
    life.state["status"] = "registered"
    thread = life.state["threads"]["main"]
    thread["model_override"] = {"effort": "high"}
    thread["board_hidden"] = {"names": ["plans"], "tags": ["private"]}
    life.cfg["fallback"] = [{"provider": "anthropic", "model": "backup-test"}]
    return life


def test_null_changes_inspects_without_resetting_anything(tmp_path):
    life = life_with_settings(tmp_path)
    before = copy.deepcopy(life.state)
    life.focus_tool({**FOCUS_EMPTY, "action": "model", "changes": None})
    assert life.state == before


@pytest.mark.parametrize(("field", "value"), [
    ("fallback", []), ("effort", None), ("inherit", True), ("inherit", False),
])
def test_selected_model_setting_preserves_explicit_empty_semantics(tmp_path, monkeypatch, field, value):
    life = life_with_settings(tmp_path)
    monkeypatch.setattr(router_mod, "make_llm", lambda cfg: object())
    before_view = copy.deepcopy(life.state["threads"]["main"]["board_hidden"])
    life.focus_tool({**FOCUS_EMPTY, "action": "model", "changes": [field], field: value})
    thread = life.state["threads"]["main"]
    assert thread["board_hidden"] == before_view
    if field == "inherit" and value is True:
        assert "model_override" not in thread
    elif field == "inherit":
        assert thread["model_override"] == {"effort": "high"}
    else:
        assert thread["model_override"][field] == value
        if field == "fallback":
            assert len(life.router.routes()) == 1


def test_selected_visibility_clear_does_not_clear_other_settings(tmp_path):
    life = life_with_settings(tmp_path)
    life.focus_tool({**FOCUS_EMPTY, "action": "view", "changes": ["hide_boards"]})
    thread = life.state["threads"]["main"]
    assert thread["board_hidden"] == {"names": [], "tags": ["private"]}
    assert thread["model_override"] == {"effort": "high"}


def test_selected_provider_and_model_really_change_route(tmp_path, monkeypatch):
    life = life_with_settings(tmp_path)
    monkeypatch.setattr(router_mod, "make_llm", lambda cfg: object())
    life.focus_tool({**FOCUS_EMPTY, "action": "model", "changes": ["provider", "model"],
                     "provider": "anthropic", "model": "offline-test"})
    override = life.state["threads"]["main"]["model_override"]
    assert override == {"provider": "anthropic", "model": "offline-test", "effort": "high"}
    assert len(life.router.routes()) == 2  # placeholder fallback=[] did not disable inheritance


@pytest.mark.parametrize("changes", [False, 0, "fallback", {}, [None], ["missing"], ["effort", "effort"]])
def test_bad_change_masks_fail_before_any_mutation(tmp_path, changes):
    life = life_with_settings(tmp_path)
    before = copy.deepcopy(life.state)
    with pytest.raises(ValueError):
        life.focus_tool({**FOCUS_EMPTY, "action": "model", "changes": changes})
    assert life.state == before


@pytest.mark.parametrize("args", [
    {"action": "model", "changes": ["fallback"]},
    {"action": "model", "changes": ["hide_tags"], "hide_tags": []},
    {"action": "view", "changes": ["fallback"], "fallback": []},
    {"action": "close", "changes": ["inherit"], "inherit": True},
    {"action": "new", "title": "Task", "changes": ["context"], "context": "note"},
    {"action": "model", "changes": [], "provider": "anthropic", "model": "offline-test"},
    {"action": "model", "changes": [], "effort": "low"},
    {"action": "view", "changes": [], "hide_tags": ["private"]},
    {"action": "model", "changes": [], "inherit": True},
    {"action": "model", "changes": ["provider"], "provider": "anthropic"},
    {"action": "model", "changes": ["provider", "model"], "provider": "", "model": ""},
    {"action": "view", "changes": ["hide_tags"], "hide_tags": None},
    {"action": "model", "changes": ["inherit", "fallback"], "inherit": True, "fallback": []},
    {"action": "model", "changes": ["inherit"], "inherit": None},
])
def test_invalid_or_conflicting_updates_never_mutate_state(tmp_path, args):
    life = life_with_settings(tmp_path)
    before = copy.deepcopy(life.state)
    with pytest.raises(ValueError):
        life.focus_tool(args)
    assert life.state == before


def test_legacy_sparse_calls_still_work_with_irrelevant_empty_fields(tmp_path, monkeypatch):
    life = life_with_settings(tmp_path)
    monkeypatch.setattr(router_mod, "make_llm", lambda cfg: object())
    life.focus_tool({"action": "model", "effort": "low", "hide_boards": [], "hide_tags": [], "context": ""})
    assert life.state["threads"]["main"]["model_override"]["effort"] == "low"
    assert life.state["threads"]["main"]["board_hidden"]["names"] == ["plans"]
    life.focus_tool({"action": "view", "provider": "", "model": "", "fallback": [],
                     "effort": None, "inherit": False, "context": ""})
    assert life.state["threads"]["main"]["board_hidden"]["names"] == ["plans"]


def test_legacy_new_empty_id_does_not_request_a_custom_id(tmp_path):
    life = life_with_settings(tmp_path)
    life.focus_tool({"action": "new", "title": "Task", "id": ""})
    assert life.pending_switch[0] == "t1"
    assert "model_override" not in life.state["threads"]["t1"]


def test_new_still_rejects_a_nonempty_custom_id(tmp_path):
    life = life_with_settings(tmp_path)
    before = copy.deepcopy(life.state)
    with pytest.raises(ValueError, match="new assigns"):
        life.focus_tool({"action": "new", "title": "Task", "id": "custom", "changes": []})
    assert life.state == before


def test_focus_normalization_does_not_modify_the_input():
    args = {**FOCUS_EMPTY, "action": "view", "changes": ["hide_tags"]}
    before = copy.deepcopy(args)
    assert focus_args(args) == {"action": "view", "id": "", "title": "", "goal": "", "hide_tags": []}
    assert args == before


@pytest.mark.parametrize("all_value", [None, False])
def test_board_ignores_empty_tag_and_nullable_all(tmp_path, all_value):
    life = life_with_settings(tmp_path)
    life.board_tool({"action": "hide", "name": "news", "tag": "", "all": all_value})
    assert life.state["threads"]["main"]["board_hidden"]["names"] == ["plans", "news"]


@pytest.mark.parametrize("args", [
    {"action": "hide", "name": "plans", "tag": "private", "all": False},
    {"action": "show", "all": True, "name": "plans", "tag": ""},
    {"action": "hide", "name": "plans", "tag": "", "all": 0},
    {"action": "hide", "name": "plans", "tag": "", "all": ""},
    {"action": "hide", "name": "plans", "tag": False},
    {"action": "hide", "name": "plans", "tag": []},
    {"action": "hide", "name": "", "tag": ""},
])
def test_board_real_conflicts_and_bad_types_are_not_ignored(tmp_path, args):
    life = life_with_settings(tmp_path)
    before = copy.deepcopy(life.state)
    with pytest.raises(ValueError):
        life.board_tool(args)
    assert life.state == before


def test_board_intentional_empty_write_and_replacement_remain_valid(tmp_path):
    life = life_with_settings(tmp_path)
    life.board_tool({"action": "write", "name": "example", "text": "delete this"})
    life.board_tool({"action": "patch", "name": "example", "old": "delete", "new": ""})
    assert life.board_tool({"action": "read", "name": "example"}) == " this"
    life.board_tool({"action": "write", "name": "example", "text": ""})
    assert life.board_tool({"action": "read", "name": "example"}) == ""


def test_nonempty_skill_file_remains_an_explicit_read(tmp_path):
    from station.skills import Skills

    base = tmp_path / "skills" / "example"
    base.mkdir(parents=True)
    (base / "SKILL.md").write_text("# Example\n")
    (base / "reference.md").write_text("Supporting notes\n")
    skills = Skills(base.parent, tmp_path)
    assert skills.read("example", "reference.md") == "Supporting notes\n"
    assert "Other files in this skill:" not in skills.read("example", "SKILL.md")


def test_shared_focus_schema_documents_the_mask():
    schema = next(t for t in LIFE_TOOLS if t["name"] == "focus")["input_schema"]
    assert schema["required"] == ["action"]
    mask = schema["properties"]["changes"]
    assert mask["type"] == ["array", "null"]
    assert set(mask["items"]["enum"]) == {"provider", "model", "effort", "fallback", "inherit", "hide_boards", "hide_tags"}


@pytest.mark.parametrize(("field", "value"), [
    ("provider", 0), ("provider", False), ("provider", []), ("provider", {}),
    ("model", 0), ("model", False), ("model", []), ("model", {}),
    ("effort", 0), ("effort", False), ("effort", []), ("effort", {}),
    ("fallback", ""), ("fallback", 0), ("fallback", False), ("fallback", {}),
    ("hide_boards", ""), ("hide_boards", 0), ("hide_boards", False), ("hide_boards", {}),
    ("hide_tags", ""), ("hide_tags", 0), ("hide_tags", False), ("hide_tags", {}),
    ("inherit", 0), ("inherit", ""), ("inherit", []), ("inherit", {}),
    ("context", 0), ("context", False), ("context", []), ("context", {}),
])
def test_wrong_type_falsy_values_are_not_optional_placeholders(field, value):
    with pytest.raises(ValueError):
        focus_args({"action": "model", "changes": [], field: value})


@pytest.mark.parametrize(("field", "value"), [
    ("provider", None), ("provider", ""), ("model", None), ("model", ""),
    ("effort", None), ("effort", ""), ("context", None), ("context", ""),
    ("fallback", None), ("fallback", []), ("hide_boards", None), ("hide_boards", []),
    ("hide_tags", None), ("hide_tags", []), ("inherit", None), ("inherit", False),
])
def test_field_specific_unselected_placeholders_are_accepted(field, value):
    assert focus_args({"action": "model", "changes": [], field: value}) == {"action": "model"}


@pytest.mark.parametrize("action", [None, [], {}, False, 0, "unknown"])
def test_invalid_actions_fail_as_input_errors(action):
    with pytest.raises(ValueError, match="unknown action"):
        focus_args({"action": action, "changes": []})


@pytest.mark.parametrize("args", [
    {"action": "new", "title": "Task", "changes": ["provider"], "provider": "anthropic"},
    {"action": "model", "changes": ["fallback"], "fallback": None},
    {"action": "view", "context": "nonempty", "changes": []},
    {"action": "model", "provider": "", "model": "", "effort": "high", "fallback": []},
    {"action": "model", "changes": ["provider"], "provider": "anthropic", "model": "offline-test"},
])
def test_failed_updates_preserve_disk_state_pending_switch_and_notices(tmp_path, args):
    life = life_with_settings(tmp_path)
    life.state["model_route"] = {"active": 1, "next_main_try": 123}
    life.pending_switch = ("main", "existing pending context")
    life.notices.add("Existing fallback", source="station", key="model-fallback")
    life.store.save_state(life.state)
    before_state = copy.deepcopy(life.state)
    before_disk = life.store.state_path.read_bytes()
    before_notices = life.notices.path.read_bytes()
    with pytest.raises(ValueError):
        life.focus_tool(args)
    assert life.state == before_state
    assert life.pending_switch == ("main", "existing pending context")
    assert life.store.state_path.read_bytes() == before_disk
    assert life.notices.path.read_bytes() == before_notices


def test_legacy_explicit_model_and_empty_fallback_keep_disable_semantics(tmp_path, monkeypatch):
    life = life_with_settings(tmp_path)
    monkeypatch.setattr(router_mod, "make_llm", lambda cfg: object())
    life.focus_tool({"action": "model", "provider": "anthropic", "model": "offline-test", "fallback": []})
    assert life.state["threads"]["main"]["model_override"]["fallback"] == []
    assert len(life.router.routes()) == 1
