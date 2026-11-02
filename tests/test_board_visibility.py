"""Board visibility is a per-call projection, with no stored policy or live tools."""

from unittest.mock import Mock

import pytest

from station.board import Board
from station.runner import Runner


@pytest.fixture
def board(tmp_path, monkeypatch):
    runner = Runner(tmp_path)
    # Only storage uses the real Runner; all command execution is fake.
    monkeypatch.setattr(runner, "run", Mock(return_value=(0, "COMMAND_SECRET")))
    return Board(tmp_path / "board", runner)


def test_no_rules_preserves_render_and_lists_reserved_panel(board):
    board.builtins.update({"clock": lambda: "12:00", "silent": lambda: None})
    board.tool("write", "notes", text="---\ntags: [work, later]\n---\nVisible body")
    expected = ("<board>\n## clock · built-in\n12:00\n\n"
                "## notes [work, later]\nVisible body\n</board>")
    assert board.render({}) == expected
    assert board.render({}, hidden_names=[], hidden_tags=[]) == expected
    assert board.tool("list").splitlines() == [
        "hidden · built-in", "clock · built-in", "silent · built-in",
        "notes · static · tags=['work', 'later']",
    ]


def test_name_hiding_is_exact_and_unhiding_restores_without_mutation(board):
    board.tool("write", "note", text="PRIVATE_BODY")
    board.tool("write", "notebook", text="Visible notebook")
    names = ["note", "missing", "note"]
    hidden = board.render({}, hidden_names=names)
    assert "## note\n" not in hidden
    assert "PRIVATE_BODY" not in hidden
    assert "## notebook\nVisible notebook" in hidden
    assert "- note · tags=[]" in hidden
    assert "missing" in hidden  # Rules need not match an existing panel.
    assert names == ["note", "missing", "note"]
    shown = board.render({})
    assert "## note\nPRIVATE_BODY" in shown
    assert "## hidden" not in shown
    assert not board._cache


@pytest.mark.parametrize("rule", ["work", "urgent"])
def test_any_exact_tag_hides_a_panel_with_multiple_tags(board, rule):
    board.tool("write", "notes", text="---\ntags: [work, urgent]\n---\nPRIVATE_BODY")
    board.tool("write", "other", text="---\ntags: [working, Work]\n---\nOther body")
    tags = [rule]
    rendered = board.render({}, hidden_tags=tags)
    assert "PRIVATE_BODY" not in rendered
    assert "## notes" not in rendered
    assert "- notes · tags=['work', 'urgent']" in rendered
    assert "## other [working, Work]\nOther body" in rendered
    assert tags == [rule]
    assert "PRIVATE_BODY" in board.render({})


def test_multiple_name_and_tag_rules_are_a_union(board):
    for name, tag in [("named", "plain"), ("first", "work"), ("second", "urgent"), ("open", "plain")]:
        board.tool("write", name, text=f"---\ntags: [{tag}]\n---\nBODY_{name}")
    out = board.render({}, hidden_names=["named"], hidden_tags=["work", "urgent"])
    assert "BODY_open" in out
    for name in ("named", "first", "second"):
        assert f"BODY_{name}" not in out
        assert f"- {name} · tags=" in out


def test_hidden_builtin_callback_is_not_invoked(board):
    callback = Mock(return_value="BUILTIN_SECRET")
    board.builtins["clock"] = callback
    hidden = board.render({}, hidden_names=["clock"])
    callback.assert_not_called()
    assert "- clock · built-in" in hidden
    assert "BUILTIN_SECRET" not in hidden
    assert "## clock" not in hidden
    assert "## clock · built-in\nBUILTIN_SECRET" in board.render({})
    callback.assert_called_once_with()


def test_tag_rules_do_not_hide_builtins(board):
    callback = Mock(return_value="Clock body")
    board.builtins["clock"] = callback
    out = board.render({}, hidden_tags=["clock"])
    callback.assert_called_once_with()
    assert "## clock · built-in\nClock body" in out
    assert "Matched panels (names/tags only):\n(none)" in out


@pytest.mark.parametrize("rules", [{"hidden_names": ["probe"]}, {"hidden_tags": ["private"]}])
def test_hidden_command_never_runs_and_summary_has_only_metadata(board, rules):
    source = ("---\ntags: [private, work]\ncommand: DO_NOT_RUN_COMMAND\n"
              "cooldown: 0\n---\nSTATIC_SECRET")
    board.tool("write", "probe", text=source)
    for _ in range(2):
        out = board.render({"EXAMPLE": "value"}, **rules)
        assert out.startswith("<board>\n## hidden · built-in\n")
        assert "- probe · tags=['private', 'work']" in out
        for secret in ("DO_NOT_RUN_COMMAND", "COMMAND_SECRET", "STATIC_SECRET"):
            assert secret not in out
    board.runner.run.assert_not_called()
    assert not board._cache
    assert board.tool("read", "probe", **rules) == source
    assert "COMMAND_SECRET" in board.render({"EXAMPLE": "value"})
    board.runner.run.assert_called_once_with(
        "DO_NOT_RUN_COMMAND", timeout=5.0, env={"EXAMPLE": "value", "CTX_PANEL": "probe"},
    )


def test_hiding_does_not_refresh_clear_or_expose_cached_command(board, monkeypatch):
    board.tool("write", "probe", text="---\ncommand: fake\ncooldown: 60\n---\n")
    now = [1000.0]
    monkeypatch.setattr("station.board.time.time", lambda: now[0])
    assert "COMMAND_SECRET" in board.render({})
    cached = board._cache["probe"].copy()
    now[0] = 1010.0
    hidden = board.render({}, hidden_names=["probe"])
    assert "COMMAND_SECRET" not in hidden
    assert board._cache["probe"] == cached
    assert "COMMAND_SECRET" in board.render({})
    assert board.runner.run.call_count == 1  # Showing within cooldown uses the old cache.
    now[0] = 1100.0
    board.render({}, hidden_names=["probe"])
    assert board._cache["probe"] == cached
    assert board.runner.run.call_count == 1
    board.render({})
    assert board.runner.run.call_count == 2  # An expired cooldown resumes on show.


def test_list_marks_effective_hidden_entries_and_manual_read_works(board):
    callback = Mock(return_value="SECRET")
    board.builtins["clock"] = callback
    board.tool("write", "named", text="Named secret")
    tagged = "---\ntags: [work, urgent]\ncommand: fake\n---\nTagged secret"
    board.tool("write", "tagged", text=tagged)
    board.tool("write", "visible", text="Visible body")
    rules = {"hidden_names": ["clock", "named"], "hidden_tags": ["urgent"]}
    assert board.tool("list", **rules).splitlines() == [
        "hidden · built-in", "clock · built-in · hidden",
        "named · static · tags=[] · hidden",
        "tagged · command · tags=['work', 'urgent'] · hidden",
        "visible · static · tags=[]",
    ]
    assert board.tool("read", "named", **rules) == "Named secret"
    assert board.tool("read", "tagged", **rules) == tagged
    assert " · hidden" not in board.tool("list")
    callback.assert_not_called()
    board.runner.run.assert_not_called()


def test_hidden_panel_is_first_and_cannot_hide_itself(board):
    callback = Mock(return_value="Clock body")
    board.builtins["clock"] = callback
    out = board.render({}, hidden_names=["hidden"], hidden_tags=["hidden"])
    assert out.startswith("<board>\n## hidden · built-in\n")
    assert out.count("## hidden · built-in") == 1
    assert "- hidden ·" not in out
    assert out.index("## hidden") < out.index("## clock")
    assert board.tool("list", hidden_names=["hidden"]).splitlines()[0] == "hidden · built-in"
    assert "## hidden" not in board.render({})


@pytest.mark.parametrize("action", ["read", "write", "patch", "delete"])
def test_hidden_is_reserved_for_all_file_actions(board, action):
    assert "hidden" in board.builtins
    with pytest.raises(ValueError, match="hidden is a built-in panel"):
        board.tool(action, "hidden", text="body", old="a", new="b")


def test_hiding_does_not_change_file_actions_or_add_actions(board):
    rules = {"hidden_names": ["notes"]}
    assert board.tool("write", "notes", text="First body", **rules) == "wrote notes"
    assert board.tool("patch", "notes", old="First", new="Second", **rules) == "patched notes"
    assert board.tool("read", "notes", **rules) == "Second body"
    assert board.tool("delete", "notes", **rules) == "deleted notes"
    with pytest.raises(ValueError, match="no panel notes"):
        board.tool("read", "notes", **rules)
    for action in ("hide", "show"):
        with pytest.raises(ValueError, match=f"unknown action: {action}"):
            board.tool(action, "notes")
    assert list(board.dir.iterdir()) == []  # No visibility policy or extra state files.


def test_visibility_does_not_leak_between_calls(board):
    board.tool("write", "a", text="FIRST_SECRET")
    board.tool("write", "b", text="SECOND_SECRET")
    first = board.render({}, hidden_names=["a"])
    second = board.render({}, hidden_names=["b"])
    assert "FIRST_SECRET" not in first and "SECOND_SECRET" in first
    assert "SECOND_SECRET" not in second and "FIRST_SECRET" in second
    assert "FIRST_SECRET" in board.render({}) and "SECOND_SECRET" in board.render({})


def test_hidden_overview_and_visible_panels_still_use_board_budget(board):
    board.max_chars = 700
    board.tool("write", "private", text="HIDDEN_SECRET")
    board.tool("write", "a", text="x" * 1500)
    board.tool("write", "z", text="LAST_PANEL_BODY")
    out = board.render({}, hidden_names=["private"])
    assert out.startswith("<board>\n## hidden · built-in\n")
    assert "HIDDEN_SECRET" not in out
    assert "LAST_PANEL_BODY" not in out
    assert "(the board is over 700 characters:" in out
    assert "cut a" in out and "hid z" in out
    # fit() intentionally adds a short overflow notice and the enclosing tags.
    assert len(out) <= board.max_chars + 250


def test_unmatched_rules_still_show_overview_and_metadata_is_clipped(board):
    out = board.render({}, hidden_names=["missing"], hidden_tags=["unused"])
    assert "Hidden names: ['missing']" in out
    assert "Hidden tags: ['unused']" in out
    assert "Matched panels (names/tags only):\n(none)" in out
    board.panel_max_chars = 100
    out = board.render({}, hidden_names=[f"missing_{i}" for i in range(50)])
    assert "## hidden · built-in" in out
    assert "characters omitted" in out
    assert len(out) < 250
