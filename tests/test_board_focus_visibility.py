"""New per-thread board visibility integration; fake models and temporary homes only."""

import copy
import json

import pytest

from station import board_view
from station.loop import Life
from station.tools import LIFE_TOOLS
from test_loop import make, use


@pytest.fixture
def life(tmp_path):
    obj = make(tmp_path, [])
    obj.cfg['memory']['enabled'] = False
    obj.state.update(status='registered', name='Lin')
    obj.init_layout()
    obj.seed_home()
    obj.board.tool('write', name='system', text='Local system panel')
    obj.board.tool('write', name='social', text='---\ntags: [social, bilibili]\n---\nPRIVATE SOCIAL BODY')
    obj.board.tool('write', name='plans', text='VISIBLE PLANS BODY')
    return obj


def call(life, tool='board', **args):
    result, error = life.run_tool({'name': tool, 'input': args})
    assert not error
    return result


def render(life):
    return life.compose([{'role': 'user', 'content': [{'type': 'text', 'text': 'Continue.'}]}])[2][-1]['content'][-1]['text']


def new(life, **args):
    life.focus_tool({'action': 'new', 'title': 'Another task', **args})
    history = life.apply_switch()
    return life.state['current'], history


def switch(life, tid, **args):
    life.focus_tool({'action': 'switch', 'id': tid, **args})
    return life.apply_switch()


def test_model_tool_dispatch_filters_current_board_and_keeps_file_readable(life):
    call(life, action='hide', name='social')
    board = render(life)
    assert '## hidden' in board and '- social' in board
    assert 'PRIVATE SOCIAL BODY' not in board and 'VISIBLE PLANS BODY' in board
    assert 'PRIVATE SOCIAL BODY' in call(life, action='read', name='social')
    assert 'social · static · tags=' in call(life, action='list')
    assert '· hidden' in call(life, action='list')


def test_new_thread_does_not_inherit_parent_rules_and_return_restores_them(life):
    call(life, action='hide', tag='social')
    tid, _ = new(life)
    assert board_view.get(life.state['threads'][tid]) == {'names': [], 'tags': []}
    assert 'PRIVATE SOCIAL BODY' in render(life)
    switch(life, 'main')
    assert 'PRIVATE SOCIAL BODY' not in render(life)


def test_focus_new_combines_visibility_model_and_context_without_losing_any(life):
    tid, history = new(life, hide_boards=['plans'], hide_tags=['social'], context='Resume the draft.',
                       provider='codex', model='gpt-6.1-sol', effort='high', fallback=[])
    assert 'Resume the draft.' in history[0]['content'][0]['text']
    assert life.state['threads'][tid]['model_override']['effort'] == 'high'
    board = render(life)
    assert 'PRIVATE SOCIAL BODY' not in board and 'VISIBLE PLANS BODY' not in board
    assert life.board_visibility('main') == {'hidden_names': [], 'hidden_tags': []}


def test_focus_view_edits_another_thread_without_switching_or_changing_models(life):
    tid, _ = new(life)
    before = copy.deepcopy(life.state['threads'][tid].get('model_override'))
    text = call(life, tool='focus', action='view', id='main', hide_boards=['plans'])
    assert 'Thread main' in text and life.state['current'] == tid and life.pending_switch is None
    assert life.state['threads'][tid].get('model_override') == before
    assert 'VISIBLE PLANS BODY' in render(life)
    assert 'plans' in call(life, tool='focus', action='view', id='main')


def test_switch_replaces_only_provided_visibility_field(life):
    tid, _ = new(life, hide_boards=['plans'], hide_tags=['social'])
    switch(life, 'main')
    switch(life, tid, hide_boards=[])
    assert life.board_visibility() == {'hidden_names': [], 'hidden_tags': ['social']}
    assert 'VISIBLE PLANS BODY' in render(life) and 'PRIVATE SOCIAL BODY' not in render(life)


def test_board_show_removes_only_selected_rule_and_show_all_is_thread_local(life):
    call(life, action='hide', name='social')
    call(life, action='hide', tag='social')
    call(life, action='show', name='social')
    assert 'PRIVATE SOCIAL BODY' not in render(life)
    tid, _ = new(life, hide_boards=['plans'])
    call(life, action='show', all=True)
    assert 'VISIBLE PLANS BODY' in render(life) and '## hidden' not in render(life)
    switch(life, 'main')
    assert 'PRIVATE SOCIAL BODY' not in render(life)
    call(life, action='show', tag='social')
    assert 'PRIVATE SOCIAL BODY' in render(life)
    assert board_view.get(life.state['threads'][tid]) == {'names': [], 'tags': []}


def test_visibility_survives_store_reload_without_new_panel_files(life):
    before = set((life.home / 'board').iterdir())
    call(life, action='hide', tag='bilibili')
    tid, _ = new(life, hide_boards=['plans'])
    reloaded = Life(life.root, echo=lambda *_: None)
    assert reloaded.board_visibility('main')['hidden_tags'] == ['bilibili']
    assert reloaded.board_visibility(tid)['hidden_names'] == ['plans']
    assert set((life.home / 'board').iterdir()) == before


def test_close_restores_parent_visibility_not_child_visibility(life):
    call(life, action='hide', tag='social')
    tid, _ = new(life, hide_boards=['plans'])
    life.focus_tool({'action': 'close', 'context': 'Task finished.'})
    life.apply_switch()
    assert life.state['current'] == 'main'
    assert 'VISIBLE PLANS BODY' in render(life) and 'PRIVATE SOCIAL BODY' not in render(life)
    assert life.state['threads'][tid]['board_hidden']['names'] == ['plans']


def test_hidden_command_is_not_executed_by_real_composer(life):
    marker = life.home / 'command-ran'
    life.board.tool('write', name='expensive', text=f'---\ntags: [slow]\ncommand: touch {marker}\n---\n')
    call(life, action='hide', tag='slow')
    assert 'expensive' in render(life) and not marker.exists()
    call(life, action='show', tag='slow')
    render(life)
    assert marker.exists()


def test_hidden_builtin_callback_is_not_executed_by_real_composer(life):
    def forbidden():
        pytest.fail('hidden builtin executed')
    life.board.builtins['todo'] = forbidden
    call(life, action='hide', name='todo')
    assert '- todo' in render(life)


@pytest.mark.parametrize('value', [None, 'plans', 1, [1], [''], ['hidden'], ['../plans'], ['plans\n']])
def test_invalid_focus_visibility_does_not_mutate_state_or_consume_id(life, value):
    before = copy.deepcopy(life.state)
    with pytest.raises(ValueError):
        new(life, hide_boards=value)
    assert life.state == before and life.pending_switch is None


@pytest.mark.parametrize('args', [
    {'action': 'hide'}, {'action': 'hide', 'name': 'plans', 'tag': 'social'},
    {'action': 'hide', 'name': 'hidden'}, {'action': 'hide', 'tag': ''},
    {'action': 'show', 'all': 'true'}, {'action': 'hide', 'all': True},
    {'action': 'show', 'all': True, 'name': 'plans'}, {'action': 'hide', 'tag': 'a\nb'},
])
def test_invalid_board_request_is_atomic(life, args):
    before = copy.deepcopy(life.state)
    with pytest.raises(ValueError):
        call(life, **args)
    assert life.state == before


def test_invalid_switch_visibility_does_not_change_target_model(life):
    tid, _ = new(life)
    before = copy.deepcopy(life.state)
    with pytest.raises(ValueError):
        life.focus_tool({'action': 'switch', 'id': 'main', 'hide_tags': 'bad',
                         'provider': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'high'})
    assert life.state == before and life.state['current'] == tid


def test_failed_model_configuration_does_not_apply_valid_visibility(life):
    before = copy.deepcopy(life.state)
    with pytest.raises(ValueError):
        life.focus_tool({'action': 'new', 'title': 'Bad', 'provider': 'nope', 'model': 'x', 'hide_boards': ['plans']})
    assert life.state == before and life.pending_switch is None


def test_focus_view_partial_clear_and_deduplication(life):
    call(life, tool='focus', action='view', hide_boards=['plans', 'plans'], hide_tags=['social', 'social'])
    call(life, tool='focus', action='view', hide_boards=[])
    assert life.board_visibility() == {'hidden_names': [], 'hidden_tags': ['social']}
    call(life, tool='focus', action='view', hide_tags=[])
    assert 'board_hidden' not in life.state['threads']['main']


@pytest.mark.parametrize('action', ['close', 'model'])
def test_non_view_focus_actions_reject_visibility_instead_of_ignoring_it(life, action):
    with pytest.raises(ValueError, match='visibility'):
        life.focus_tool({'action': action, 'hide_boards': ['plans']})


def test_focus_view_rejects_model_or_closed_target(life):
    with pytest.raises(ValueError):
        life.focus_tool({'action': 'view', 'provider': 'codex', 'model': 'gpt-6.1-sol'})
    with pytest.raises(ValueError):
        life.focus_tool({'action': 'view', 'id': 'missing', 'hide_boards': ['plans']})


def test_hidden_board_stays_after_cache_breakpoint_without_modifying_history(life):
    call(life, action='hide', name='social')
    history = [{'role': 'user', 'content': [{'type': 'text', 'text': 'Continue.'}]}]
    before = copy.deepcopy(history)
    _, _, messages = life.compose(history)
    assert history == before
    blocks = messages[-1]['content']
    assert 'cache_control' in blocks[-2] and 'cache_control' not in blocks[-1]
    assert '## hidden' in blocks[-1]['text'] and 'PRIVATE SOCIAL BODY' not in blocks[-1]['text']


def test_new_schema_exposes_board_and_focus_visibility():
    tools = {t['name']: t['input_schema']['properties'] for t in LIFE_TOOLS}
    assert {'hide', 'show'} <= set(tools['board']['action']['enum'])
    assert {'tag', 'all'} <= set(tools['board'])
    assert 'view' in tools['focus']['action']['enum']
    assert {'hide_boards', 'hide_tags', 'context', 'model'} <= set(tools['focus'])


def test_new_model_driven_loop_updates_visibility_without_network(tmp_path):
    life = make(tmp_path, [
        [use('register', 'r', name='Lin')],
        [use('board', 'w', action='write', name='private', text='---\ntags: [social]\n---\nSECRET PANEL BODY')],
        [use('board', 'h', action='hide', tag='social')],
        [use('focus', 'f', action='new', title='Coding', hide_boards=['private'], context='Continue implementation.')],
        [{'type': 'text', 'text': 'Done.'}],
    ])
    life.cfg['memory']['enabled'] = False
    assert life.run(max_calls=5) == 'max_calls'
    assert life.state['current'] == 't1'
    shown = life.llm.requests[4]['messages'][-1]['content'][-1]['text']
    assert '## hidden' in shown and 'SECRET PANEL BODY' not in shown
    assert 'Continue implementation.' in life.llm.requests[4]['messages'][0]['content'][0]['text']
    saved = life.store.load_state()
    assert saved['threads']['main']['board_hidden']['tags'] == ['social']
    assert saved['threads']['t1']['board_hidden']['names'] == ['private']
