"""Focus model overrides and per-thread recovery state. Fake models only, no credentials/network."""

import copy

import pytest

from station.llm import LLMError
from station.router import routes_for
from test_loop import FakeLLM, make
from test_router import Flaky, ok


MODEL = {"provider": "claude-subscription-directsdk-experimental", "model": "claude-opus-5-5"}


def open_thread(life, **args):
    life.focus_tool({"action": "new", "title": "Coding", **args})
    life.apply_switch()
    return life.state["current"]


def test_ids_are_sequential_not_random_or_custom(tmp_path):
    life = make(tmp_path, [])
    assert open_thread(life) == "t1"
    assert open_thread(life) == "t2"
    with pytest.raises(ValueError):
        life.focus_tool({"action": "new", "title": "Other", "id": "my-id"})
    assert life.state["next_thread"] == 3


def test_selected_model_used_and_main_returns_unchanged(tmp_path, monkeypatch):
    life = make(tmp_path, [])
    main = life.llm = FakeLLM([ok("main")])
    selected = FakeLLM([ok("selected")])
    monkeypatch.setattr("station.router.make_llm", lambda cfg: selected)
    original = copy.deepcopy(life.cfg)
    tid = open_thread(life, **MODEL, effort="high", fallback=[])
    assert tid == "t1" and life.router.routes()[0] == {**MODEL, "effort": "high"}
    assert life.call_model([], [], [{"role": "user", "content": []}])["content"][0]["text"] == "selected"
    assert life.last_route["model"] == MODEL["model"]
    assert MODEL["model"] in life._panel_threads()
    life.focus_tool({"action": "switch", "id": "main"})
    life.apply_switch()
    assert life.call_model([], [], [{"role": "user", "content": []}])["content"][0]["text"] == "main"
    assert life.cfg == original and len(main.requests) == 1


def test_new_threads_inherit_global_not_parent_overrides(tmp_path, monkeypatch):
    monkeypatch.setattr("station.router.make_llm", lambda cfg: FakeLLM([]))
    life = make(tmp_path, [])
    open_thread(life, **MODEL)
    open_thread(life)
    assert life.router.routes()[0]["model"] == life.cfg["model"]


def test_model_action_changes_existing_without_switch_and_resets(tmp_path, monkeypatch):
    monkeypatch.setattr("station.router.make_llm", lambda cfg: FakeLLM([]))
    life = make(tmp_path, [])
    open_thread(life)
    life.focus_tool({"action": "model", "id": "main", **MODEL, "effort": "low"})
    assert life.state["current"] == "t1" and not life.pending_switch
    assert life.state["threads"]["main"]["model_override"]["model"] == MODEL["model"]
    life.focus_tool({"action": "model", **MODEL, "effort": "high"})
    assert life.router.routes()[0]["effort"] == "high"
    life.focus_tool({"action": "model", "effort": None})
    assert life.router.routes()[0]["effort"] is None
    text = life.focus_tool({"action": "model", "inherit": True})
    assert "model_override" not in life.state["threads"]["t1"]
    assert life.cfg["model"] in text


def test_bad_model_is_atomic_and_does_not_consume_id(tmp_path):
    life = make(tmp_path, [])
    original = copy.deepcopy(life.state)
    with pytest.raises(ValueError):
        life.focus_tool({"action": "new", "title": "Bad", "provider": "nope", "model": "x"})
    assert life.state == original and life.pending_switch is None
    with pytest.raises(ValueError):
        life.focus_tool({"action": "model", "model": "x"})
    assert life.state == original
    with pytest.raises(ValueError):
        life.focus_tool({"action": "model", "inherit": True, **MODEL})
    assert life.state == original


def test_switch_can_select_model_before_target_answers(tmp_path, monkeypatch):
    monkeypatch.setattr("station.router.make_llm", lambda cfg: FakeLLM([ok("new target")]))
    life = make(tmp_path, [])
    open_thread(life)
    life.focus_tool({"action": "switch", "id": "main", **MODEL})
    life.apply_switch()
    assert life.router.routes()[0]["model"] == MODEL["model"]
    assert life.call_model([], [], [{"role": "user", "content": []}])["content"][0]["text"] == "new target"


def test_fallback_state_is_independent_even_without_overrides(tmp_path, monkeypatch):
    life = make(tmp_path, [])
    life.cfg['fallback'] = [MODEL]
    life.cfg['fallback_retry_per_hour'] = 0
    life.llm = Flaky([], LLMError('main down', 'quota'))
    life.router.clients[1] = FakeLLM([ok('backup main'), ok('backup thread'), ok('backup thread again')])
    msg = [{"role": "user", "content": []}]
    life.call_model([], [], msg)
    assert life.state['model_route']['active'] == 1
    open_thread(life)
    assert life.router.state['active'] == 0  # no global failover state inherited
    life.call_model([], [], msg)
    assert life.state['threads']['t1']['model_route']['active'] == 1
    assert {n['key'] for n in life.notices.list()} == {'model-fallback', 'model-fallback:t1'}
    before = len(life.llm.requests)
    life.call_model([], [], msg)
    assert len(life.llm.requests) == before
    life.focus_tool({'action': 'close'})
    life.apply_switch()
    assert life.router.state['active'] == 1
    assert {n['key'] for n in life.notices.list()} == {'model-fallback'}


def test_overrides_failover_and_recovery_do_not_reset_main_state(tmp_path, monkeypatch):
    life = make(tmp_path, [])
    main_state = life.state['model_route'] = {'active': 0, 'sentinel': 'keep'}
    primary = Flaky([ok('recovered')], LLMError('thread quota', 'quota'))
    backup = FakeLLM([ok('thread backup')])
    def factory(cfg):
        return primary if cfg['model'] == MODEL['model'] else backup
    monkeypatch.setattr('station.router.make_llm', factory)
    open_thread(life, **MODEL, fallback=[{'provider': 'codex', 'model': 'gpt-6.1-sol'}])
    msg = [{"role": "user", "content": []}]
    life.call_model([], [], msg)
    assert life.router.state['active'] == 1 and life.state['model_route'] == main_state
    primary.down = False
    life.router.state['main_tried'] = 0
    assert life.call_model([], [], msg)['content'][0]['text'] == 'recovered'
    assert life.router.state == {'active': 0} and life.state['model_route'] == main_state


def test_override_and_route_survive_restart(tmp_path, monkeypatch):
    selected = Flaky([], LLMError('quota', 'quota'))
    backup = FakeLLM([ok('first'), ok('after restart')])
    monkeypatch.setattr('station.router.make_llm', lambda cfg: selected if cfg['model'] == MODEL['model'] else backup)
    life = make(tmp_path, [])
    open_thread(life, **MODEL, fallback=[{'provider': 'codex', 'model': 'gpt-6.1-sol'}])
    life.call_model([], [], [{"role": "user", "content": []}])
    life.store.save_state(life.state)
    again = make(tmp_path, [])
    assert again.router.routes()[0]['model'] == MODEL['model'] and again.router.state['active'] == 1
    assert again.call_model([], [], [{"role": "user", "content": []}])['content'][0]['text'] == 'after restart'


def test_duplicate_global_fallback_not_used_as_own_backup():
    routes = routes_for({'provider': 'codex', 'model': 'gpt-6.1-sol', 'fallback': [
        {'provider': 'codex', 'model': 'gpt-6.1-sol'}, MODEL, MODEL]})
    assert len(routes) == 2


def test_status_shows_current_thread_selection(tmp_path, monkeypatch, capsys):
    from station.cli import main
    monkeypatch.setattr('station.router.make_llm', lambda cfg: FakeLLM([]))
    life = make(tmp_path, [])
    open_thread(life, **MODEL, fallback=[])
    life.store.save_state(life.state)
    assert main(['--root', str(tmp_path), 'status']) == 0
    assert 'Model: claude-subscription-directsdk-experimental/claude-opus-5-5; no fallback' in capsys.readouterr().out
