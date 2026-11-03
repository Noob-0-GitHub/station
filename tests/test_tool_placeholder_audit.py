"""Offline audit evidence and regression tests for optional-field compatibility.

Only the cases affected by a behavior change are rerun during development.
Empty values selected as actual updates retain their documented semantics.
"""

import copy

import pytest

from station import router as router_mod
from station.llm import Codex, from_responses_output, to_responses_input
from station.skills import Skills
from station.tools import LIFE_TOOLS, ONBOARDING_TOOLS
from test_loop import make


FOCUS_EMPTY = {
    "title": "", "goal": "", "id": "", "provider": "", "model": "",
    "effort": None, "fallback": [], "inherit": False,
    "hide_boards": [], "hide_tags": [], "context": "",
}


def registered(tmp_path):
    life = make(tmp_path, [])
    life.state["status"] = "registered"
    return life


@pytest.mark.parametrize("action", ["new", "switch", "model", "view", "close"])
def test_full_focus_placeholders_with_no_changes_preserve_settings(tmp_path, action):
    life = registered(tmp_path)
    main = life.state["threads"]["main"]
    main["board_hidden"] = {"names": ["plans"], "tags": ["private"]}
    main["model_override"] = {"effort": "high", "fallback": []}
    life.state["threads"]["t1"] = {"title": "Task", "goal": "", "parent": "main", "status": "active",
                                   "board_hidden": {"names": ["plans"], "tags": ["private"]},
                                   "model_override": {"effort": "low", "fallback": []}}
    life.state["next_thread"] = 2
    if action == "close":
        life.state["current"] = "t1"
    before = copy.deepcopy(life.state)
    args = {**FOCUS_EMPTY, "action": action, "changes": []}
    if action == "new":
        args["title"] = "A task"
    if action == "switch":
        args["id"] = "t1"
    life.focus_tool(args)
    for tid in ("main", "t1"):
        assert life.state["threads"][tid]["board_hidden"] == before["threads"][tid]["board_hidden"]
        assert life.state["threads"][tid]["model_override"] == before["threads"][tid]["model_override"]
    if action == "new":
        created = life.state["threads"][life.pending_switch[0]]
        assert "board_hidden" not in created and "model_override" not in created
    elif action == "close":
        assert life.state["threads"]["t1"]["status"] == "closed"
    elif action in ("view", "model"):
        assert life.state == before


def test_legacy_empty_model_bundle_is_rejected_without_mutation(tmp_path):
    life = registered(tmp_path)
    before = copy.deepcopy(life.state)
    with pytest.raises(ValueError, match="ambiguous empty model settings"):
        life.focus_tool({"action": "model", "provider": "", "model": "",
                         "effort": None, "fallback": [], "inherit": False})
    assert life.state == before


def test_board_hide_ignores_an_empty_unused_tag(tmp_path):
    life = registered(tmp_path)
    life.board_tool({"action": "hide", "name": "plans", "tag": "", "all": False,
                     "text": "", "old": "", "new": ""})
    assert life.state["threads"]["main"]["board_hidden"]["names"] == ["plans"]


def test_board_show_all_ignores_empty_name_and_tag(tmp_path):
    life = registered(tmp_path)
    life.state["threads"]["main"]["board_hidden"] = {"names": ["plans"], "tags": ["private"]}
    life.board_tool({"action": "show", "name": "", "tag": "", "all": True,
                     "text": "", "old": "", "new": ""})
    assert "board_hidden" not in life.state["threads"]["main"]


def test_empty_focus_visibility_lists_mean_clear_not_omit(tmp_path):
    life = registered(tmp_path)
    thread = life.state["threads"]["main"]
    thread["board_hidden"] = {"names": ["plans"], "tags": ["private"]}
    life.focus_tool({"action": "view"})
    assert thread["board_hidden"] == {"names": ["plans"], "tags": ["private"]}
    life.focus_tool({"action": "view", "hide_boards": [], "hide_tags": []})
    assert "board_hidden" not in thread


def test_empty_fallback_list_is_an_explicit_override_not_omit(tmp_path, monkeypatch):
    life = registered(tmp_path)
    life.cfg["fallback"] = [{"provider": "anthropic", "model": "backup-test"}]
    monkeypatch.setattr(router_mod, "make_llm", lambda cfg: object())
    life.focus_tool({"action": "model"})
    assert "model_override" not in life.state["threads"]["main"]
    life.focus_tool({"action": "model", "fallback": []})
    assert life.state["threads"]["main"]["model_override"]["fallback"] == []
    assert len(life.router.routes()) == 1


def test_zero_interval_and_false_once_are_meaningful(tmp_path):
    life = registered(tmp_path)
    life.schedule_tool({"action": "interval", "minutes": 10, "once": False,
                        "expr": "", "id": 0, "note": "", "hook": ""})
    assert life.interval()["minutes"] == 10
    assert life.interval()["once"] is False
    life.schedule_tool({"action": "interval", "minutes": 0, "once": False})
    assert life.interval() is None


@pytest.mark.parametrize("placeholder", [None, "", 0, False, [], {}])
def test_skill_optional_file_placeholders_can_hide_the_file_index(tmp_path, placeholder):
    base = tmp_path / "skills" / "example"
    base.mkdir(parents=True)
    (base / "SKILL.md").write_text("# Example\n")
    (base / "reference.md").write_text("Supporting notes\n")
    skills = Skills(base.parent, tmp_path)
    assert "Other files in this skill: reference.md" in skills.read("example")
    result = skills.read("example", placeholder)
    assert result.startswith("# Example\n")
    assert "Other files in this skill: reference.md" in result


def test_codex_build_preserves_shared_required_fields_without_forcing_strict():
    # Build does not need an initialized credential provider or a network connection.
    client = object.__new__(Codex)
    client.model = "offline-test"
    client.effort = None
    tools = copy.deepcopy(ONBOARDING_TOOLS + LIFE_TOOLS)
    original = copy.deepcopy(tools)
    built = client.build([{"type": "text", "text": "offline"}], tools, [])
    assert tools == original
    assert len(built["tools"]) == len(tools)
    for source, converted in zip(tools, built["tools"]):
        assert converted["parameters"] == source["input_schema"]
        assert "strict" not in converted
        assert converted["parameters"].get("required", []) == source["input_schema"].get("required", [])


def test_codex_argument_decode_and_replay_preserve_empty_values():
    import json

    values = {"null_field": None, "string_field": "", "number_field": 0,
              "boolean_field": False, "array_field": [], "object_field": {}}
    blocks = from_responses_output([{"type": "function_call", "call_id": "offline-call",
                                     "name": "example", "arguments": json.dumps(values)}])
    assert blocks[0]["input"] == values
    replay = to_responses_input([{"role": "assistant", "content": blocks}])
    assert json.loads(replay[0]["arguments"]) == values
