"""Registration, focus threads, diary, todo, skills, and home-file safety."""

import json
import os

import pytest

from station.loop import name_problem
from station.notes import Diary, Todo
from station.runner import Runner, safe_read
from station.skills import Skills

from test_loop import make, use


def registered(tmp_path, name="Lin Xia", extra=()):
    life = make(tmp_path, [[use("register", "r", name=name)], *extra])
    return life


def test_register_names_and_second_welcome(tmp_path):
    life = registered(tmp_path, extra=[[use("bash", "b", command="true")]])
    life.run(max_calls=2)
    result = life.llm.requests[1]["messages"][-1]["content"][0]["content"]
    assert "Welcome to Station, Lin Xia." in result and "Enjoy :)" in result and "$" not in result
    import pwd, os
    user = pwd.getpwuid(os.getuid()).pw_name
    assert f"You have the permissions of the Linux user {user}" in result
    assert "is your private space" in result
    assert "Keep your data (notes, downloads, projects) elsewhere in your home" in result
    assert "~/suggestions.md" in result and "Documentation\n  (not installed)" in result
    assert life.state["name"] == "Lin Xia"
    home = tmp_path / "home"
    assert (home / "terms.md").exists() and (home / "skills" / "board" / "SKILL.md").exists()
    assert (home / "suggestions.md").read_text().strip()
    assert "prompt-injection attacks" in (home / "suggestions.md").read_text()
    terms = (home / "terms.md").read_text()
    suggestions = (home / "suggestions.md").read_text()
    assert "Blocking anyone, or leaving a conversation or group" in terms
    assert "Declining requests, ignoring messages, not replying, or losing interest." in terms
    assert "including the operator" not in terms and "This includes the operator" not in terms
    assert "Swearing and harshly criticizing someone's words or actions, without personal insults or personal attacks." in terms
    assert "- Swearing and sharp criticism are not the same as personal insults." in suggestions
    assert os.access(home / "bin" / "browser", os.X_OK)


@pytest.mark.parametrize("bad", ["GPT-5", "Claude", "sol", "小助手", "a<b", ""])
def test_product_names_are_rejected(bad):
    assert name_problem(bad)


def test_rejected_name_keeps_onboarding(tmp_path):
    life = make(tmp_path, [[use("register", "r", name="ChatGPT")], [use("register", "r2")],
                           [use("register", "r3", name="Lin")]])
    life.run(max_calls=3)
    for i in (1, 2):
        assert life.llm.requests[i]["messages"][-1]["content"][0]["is_error"]
    assert life.state["status"] == "registered" and life.state["name"] == "Lin"


def test_docs_listing(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "README.md").write_text("# Station manual\n")
    (docs / "schedule.md").write_text("# Waking up\n")
    life = make(tmp_path, [])
    life.cfg["docs_dir"] = str(docs)
    lines = life.docs_listing().splitlines()
    assert lines[0].split() == [str(docs / "README.md"), "Station", "manual"]
    assert lines[1].endswith("Waking up")


def test_registration_lists_the_shipped_docs(tmp_path):
    from pathlib import Path

    from station.tools import LIFE_TOOLS

    docs = Path(__file__).resolve().parent.parent / "docs"
    life = make(tmp_path, [[use("register", "r", name="Lin")], [use("bash", "b", command="true")]])
    life.cfg["docs_dir"] = str(docs)
    life.run(max_calls=2)
    result = life.llm.requests[1]["messages"][-1]["content"][0]["content"]
    for page in ("README.md", "tools.md", "schedule.md", "board.md", "threads.md", "compression.md", "files.md", "life.md"):
        assert str(docs / page) in result
    assert str(docs / "install.md") in result and str(docs / "auth.md") in result
    tools_doc = (docs / "tools.md").read_text()
    for tool in LIFE_TOOLS:
        assert f"## {tool['name']}\n" in tools_doc
    for page in docs.glob("*.md"):
        assert "assets/" not in page.read_text()


def test_focus_new_close_returns_with_report(tmp_path):  # `report` is the old name of close's context
    life = registered(tmp_path, extra=[
        [use("focus", "f1", action="new", title="Reading", goal="Finish chapter one")],
        [use("diary", "d1", action="write", text="Started reading")],
        [use("focus", "f2", action="close", report="Done, it was good")],
        [use("bash", "b", command="true")],
    ])
    life.run(max_calls=5)
    reqs = life.llm.requests
    in_thread = reqs[2]["messages"]
    assert len(in_thread) == 1 and "the new thread \"Reading\"" in in_thread[0]["content"][0]["text"]
    back = reqs[4]["messages"][-1]["content"]
    texts = " ".join(str(b.get("text") or b.get("content")) for b in back)
    assert "\"Reading\" is closed." in texts and "Done, it was good" in texts
    assert reqs[4]["messages"][0]["content"][0]["text"].startswith("Welcome to Station.")
    assert life.state["current"] == "main" and life.state["threads"]["t1"]["status"] == "closed"
    assert (tmp_path / "state" / "threads" / "t1.jsonl").exists()


def test_focus_switch_and_board_threads(tmp_path):
    life = registered(tmp_path, extra=[
        [use("focus", "f1", action="new", title="A")],
        [use("focus", "f2", action="switch", id="main")],
        [use("bash", "b", command="true")],
    ])
    life.run(max_calls=4)
    board = life.llm.requests[3]["messages"][-1]["content"][-1]["text"]
    assert "- main everyday  ← here" in board and "- t1 A" in board
    with pytest.raises(ValueError):
        life.focus_tool({"action": "close"})


def test_focus_carries_context_on_every_action(tmp_path):
    life = registered(tmp_path, extra=[
        [use("focus", "f1", action="new", title="Story", context="Anthony asked for a ghost story.")],
        [use("focus", "f2", action="switch", id="main", context="Opening paragraph drafted in ~/story.md.")],
        [use("focus", "f3", action="switch", id="t1", context="Back to it after lunch.")],
        [use("focus", "f4", action="close", context="Done. He liked the ending.")],
        [use("bash", "b", command="true")],
    ])
    life.run(max_calls=6)
    reqs = life.llm.requests

    def last_text(i):
        return " ".join(str(b.get("text") or b.get("content")) for b in reqs[i]["messages"][-1]["content"])

    opened = reqs[2]["messages"][0]["content"][0]["text"]
    assert "Context from main: Anthony asked for a ghost story." in opened
    assert "Context from t1: Opening paragraph drafted in ~/story.md." in last_text(3)
    assert "Context from main: Back to it after lunch." in last_text(4)
    closed = last_text(5)
    assert "\"Story\" is closed.\nContext from t1: Done. He liked the ending." in closed
    assert life.state["threads"]["t1"]["report"] == "Done. He liked the ending."
    # Only the context crosses: t1's own conversation is not in main.
    assert "This is the new thread" not in json.dumps(reqs[3]["messages"])


def test_focus_without_context_is_unchanged(tmp_path):
    life = registered(tmp_path, extra=[[use("focus", "f1", action="new", title="A")], [use("bash", "b", command="true")]])
    life.run(max_calls=3)
    assert "Context from" not in json.dumps(life.llm.requests[2]["messages"])


def test_focus_refuses_overlong_context(tmp_path):
    life = registered(tmp_path, extra=[])
    with pytest.raises(ValueError, match="under 2000"):
        life.focus_tool({"action": "new", "title": "A", "context": "x" * 2001})
    assert life.pending_switch is None and "t1" not in life.state["threads"]


def test_events_follow_the_switch(tmp_path):
    from station.inbox import Inbox

    def new_thread(_messages):
        Inbox(tmp_path / "inbox").put("message", "Anthony", "you there?")
        return [use("focus", "f1", action="new", title="A")]

    life = registered(tmp_path, extra=[new_thread, [use("bash", "b", command="true")]])
    life.run(max_calls=3)
    first = life.llm.requests[2]["messages"][0]["content"]
    assert any("Anthony: you there?" in b["text"] for b in first)


def test_diary_and_todo(tmp_path):
    r = Runner(tmp_path)
    diary = Diary(tmp_path / "diary", r, tmp_path)
    diary.tool("write", "first")
    diary.tool("write", "second")
    assert diary.count_today() == 2 and "second" in diary.tool("read")
    todo = Todo(tmp_path / "todo.json", r, tmp_path)
    todo.tool("add", "groceries")
    todo.tool("add", "reply")
    todo.tool("done", id=1)
    text = todo.render()
    assert "- [ ] #2 reply" in text and "- [x] #1 groceries" in text
    with pytest.raises(ValueError):
        todo.tool("done", id=1)


def test_skills_index_and_read(tmp_path):
    (tmp_path / "skills" / "cook").mkdir(parents=True)
    (tmp_path / "skills" / "cook" / "SKILL.md").write_text("---\nname: cook\ndescription: cooking\n---\nwash the vegetables")
    (tmp_path / "skills" / "cook" / "list.txt").write_text("salt")
    s = Skills(tmp_path / "skills", tmp_path)
    assert "- cook: cooking" in s.render()
    assert "wash the vegetables" in s.read("cook") and "list.txt" in s.read("cook")
    assert s.read("cook", "list.txt") == "salt"
    with pytest.raises(ValueError):
        s.read("cook", "../../../etc/passwd")


def test_symlinks_out_of_home_are_refused(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    secret = tmp_path / "secret.env"
    secret.write_text("KEY=1")
    (home / "board").mkdir()
    os.symlink(secret, home / "board" / "leak.md")
    with pytest.raises(ValueError):
        safe_read(home / "board" / "leak.md", home)
    os.symlink(tmp_path, home / "diary")
    with pytest.raises(ValueError):
        Diary(home / "diary", Runner(home), home).write("x")
    assert not (tmp_path / "2026").exists()
    assert json.loads(json.dumps({"ok": True}))


def test_self_panel(tmp_path):
    life = registered(tmp_path, extra=[[use("bash", "b", command="echo 'I am Lin Xia. I talk little.' > self.md")],
                                       [use("bash", "b2", command="true")]])
    life.run(max_calls=3)
    first = life.llm.requests[1]["messages"][-1]["content"][-1]["text"]
    assert "## self · built-in\n(not written yet" in first
    later = life.llm.requests[2]["messages"][-1]["content"][-1]["text"]
    assert later.index("## self · built-in\nI am Lin Xia. I talk little.") < later.index("## clock")


def test_local_letters_are_not_seeded_or_injected(tmp_path):
    life = registered(tmp_path, extra=[[use("bash", "b", command="true")]])
    life.run(max_calls=2)
    home = tmp_path / "home"
    assert {p.name for p in home.glob("*.md")} == {"self.md", "terms.md", "suggestions.md"}
    letter = home / "friend.md"
    letter.write_text("A private local letter, not a source template.\n")
    life.seed_home()
    assert letter.read_text() == "A private local letter, not a source template.\n"
    assert all("A private local letter" not in json.dumps(r) for r in life.llm.requests)


def test_onboarding_load_order(tmp_path):
    from station.loop import template

    life = registered(tmp_path, extra=[[use("bash", "b", command="true")]])
    life.run(max_calls=2)
    first, second = life.llm.requests
    assert first["system"][0]["text"] == template("system.md")
    welcome = first["messages"][0]["content"][0]["text"]
    assert welcome == template("welcome.md") + "\n---\n\n" + template("terms.md")
    assert first["tools"] == ["register", "exit"]
    assert "<board>" not in json.dumps(first["messages"])
    assert "Welcome to Station, Lin Xia." in second["messages"][-1]["content"][0]["content"]
    assert "<board>" in second["messages"][-1]["content"][-1]["text"]
    assert "bash" in second["tools"] and "register" not in second["tools"]
    assert life.state["current"] == "main"
