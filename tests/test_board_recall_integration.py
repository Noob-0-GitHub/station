"""New Board recall wiring and short SQLite reads. No real network/model calls."""

import copy
import json
from contextlib import closing

import pytest

from station.config import DEFAULTS
from station.memory import Chunk, Memory
from test_loop import make


class FakeMemory:
    def __init__(self):
        self.rows = [{'id': 1, 'kind': 'note', 'title': 'Earlier parser note', 'text': 'SYNTHETIC RECALLED CONTENT', 'ts': None}]
        self.searches = []
        self.factories = []
        self.local_reads = 0
        self.failure = None
        self.on_search = None

    def has_chunks(self):
        return bool(self.rows)

    def has_searchable_chunks(self):
        return bool(self.rows)

    def search(self, query, *, limit, timeout):
        self.searches.append((query, limit, timeout))
        if self.failure:
            raise self.failure
        if self.on_search:
            self.on_search()
        return copy.deepcopy(self.rows[:limit]), None

    def cached(self, ids):
        self.local_reads += 1
        return copy.deepcopy([r for r in self.rows if r['id'] in ids])


@pytest.fixture
def integration(tmp_path, monkeypatch):
    fake = FakeMemory()
    def factory(*args, **kwargs):
        fake.factories.append(kwargs)
        return fake
    monkeypatch.setattr('station.memory.Memory', factory)
    life = make(tmp_path, [])
    life.state.update(status='registered', name='Lin')
    life.cfg['memory']['recall_on_new'] = 0
    life.cfg['memory']['sync_on_sleep'] = False
    life.cfg['disk']['enabled'] = False
    life.init_layout()
    life.seed_home()
    life.board.tool('write', name='system', text='Static system status')
    (tmp_path / 'state' / 'memory.db').touch()
    history = [{'role': 'user', 'content': [{'type': 'text', 'text': 'Implement the JSON parser.'}]}]
    return life, fake, history


def board(life, history):
    return life.compose(history)[2][-1]['content'][-1]['text']


def test_auto_query_runs_before_board_and_rendering_performs_no_io(integration):
    life, fake, history = integration
    assert life.prepare_board_recall(history)
    assert fake.searches[0][1:] == (3, 2)
    assert fake.factories[-1]['db_timeout'] == 0.1
    before = (len(fake.factories), len(fake.searches), fake.local_reads)
    displayed = board(life, history)
    assert '## recall' in displayed and 'SYNTHETIC RECALLED CONTENT' in displayed
    assert (len(fake.factories), len(fake.searches), fake.local_reads) == before


def test_hidden_recall_skips_all_memory_access(integration):
    life, fake, history = integration
    life.board_tool({'action': 'hide', 'name': 'recall'})
    assert not life.prepare_board_recall(history)
    assert not fake.factories and not fake.searches
    displayed = board(life, history)
    assert '- recall' in displayed and '## recall' not in displayed


def test_missing_index_does_not_construct_memory_or_create_db(integration):
    life, fake, history = integration
    path = life.root / 'state' / 'memory.db'
    path.unlink()
    assert not life.prepare_board_recall(history)
    assert not fake.factories and not fake.searches and not path.exists()


def test_unregistered_does_not_prepare_memory(integration):
    life, fake, history = integration
    life.state['status'] = 'unregistered'
    assert not life.prepare_board_recall(history)
    assert not fake.factories


@pytest.mark.parametrize('control', ['global', 'board'])
def test_disable_controls_stop_only_the_expected_automatic_path(integration, control):
    life, fake, history = integration
    if control == 'global':
        life.cfg['memory']['enabled'] = False
    else:
        life.cfg['memory']['board_recall']['enabled'] = False
    assert not life.prepare_board_recall(history)
    assert not fake.factories and '## recall' not in board(life, history)


def test_local_validation_drops_forgotten_rows_without_another_search(integration):
    life, fake, history = integration
    life.prepare_board_recall(history)
    fake.rows = []
    assert not life.prepare_board_recall(history)
    assert len(fake.searches) == 1 and fake.local_reads >= 1
    assert 'SYNTHETIC RECALLED CONTENT' not in board(life, history)


def test_new_focus_result_seeds_board_without_second_embedding(integration):
    life, fake, history = integration
    life.cfg['memory']['recall_on_new'] = 3
    life.focus_tool({'action': 'new', 'title': 'Parser work', 'goal': 'Finish parsing', 'context': 'Check escapes.'})
    first = life.apply_switch()
    assert len(fake.searches) == 1 and fake.searches[0][2] == life.cfg['memory']['recall_timeout']
    assert 'SYNTHETIC RECALLED CONTENT' in first[0]['content'][0]['text']
    assert not life.prepare_board_recall(first)
    assert len(fake.searches) == 1 and 'SYNTHETIC RECALLED CONTENT' in board(life, first)


def test_hiding_board_path_does_not_disable_existing_focus_recall(integration):
    life, fake, history = integration
    life.cfg['memory']['recall_on_new'] = 3
    life.focus_tool({'action': 'new', 'title': 'Parser work', 'hide_boards': ['recall']})
    first = life.apply_switch()
    assert len(fake.searches) == 1
    assert not life.prepare_board_recall(first) and len(fake.searches) == 1
    assert '## recall' not in board(life, first)


def test_thread_switches_keep_independent_cached_results(integration):
    life, fake, history = integration
    life.prepare_board_recall(history)
    life.focus_tool({'action': 'new', 'title': 'Writing a story'})
    other = life.apply_switch()
    fake.rows.append({'id': 2, 'kind': 'note', 'title': 'Story note', 'text': 'STORY MEMORY', 'ts': None})
    life.prepare_board_recall(other)
    assert len(fake.searches) == 2
    life.focus_tool({'action': 'switch', 'id': 'main'})
    life.apply_switch()
    assert not life.prepare_board_recall(history) and len(fake.searches) == 2
    assert 'STORY MEMORY' not in board(life, history)


def test_error_is_cooled_down_and_not_logged_with_raw_exception_or_query(integration):
    life, fake, history = integration
    fake.failure = RuntimeError('DO_NOT_LOG_PRIVATE_ERROR_BODY')
    life.prepare_board_recall(history)
    life.prepare_board_recall(history)
    assert len(fake.searches) == 1
    assert '## recall' not in board(life, history)
    for path in (life.store.log_path, life.errors.path):
        if path.exists():
            assert 'DO_NOT_LOG_PRIVATE_ERROR_BODY' not in path.read_text()
            assert 'Implement the JSON parser.' not in path.read_text()


def test_panel_remains_after_cache_breakpoint_and_out_of_history(integration):
    life, fake, history = integration
    life.prepare_board_recall(history)
    original = copy.deepcopy(history)
    _, _, messages = life.compose(history)
    assert history == original
    blocks = messages[-1]['content']
    assert 'cache_control' in blocks[-2] and 'cache_control' not in blocks[-1]
    assert '## recall' in blocks[-1]['text']


def test_automatic_embedding_charge_rechecks_credits_before_main_model(integration):
    life, fake, history = integration
    life.state['threads']['main']['goal'] = 'Implement a parser'
    def charge():
        life.store.ledger.add('openai', kind='embedding', credits=10, list_usd=10, source='test')
    fake.on_search = charge
    life._pause = lambda seconds: setattr(life, 'stopping', True)
    assert life.run(max_calls=1) == 'stopped'
    assert len(fake.searches) == 1
    assert not life.llm.requests


def test_recall_is_reserved_but_still_hideable(integration):
    life, fake, history = integration
    with pytest.raises(ValueError, match='built-in'):
        life.board.tool('write', name='recall', text='Replacement')
    life.board_tool({'action': 'hide', 'name': 'recall'})
    assert life.board_visibility()['hidden_names'] == ['recall']


@pytest.fixture
def real_memory(tmp_path):
    cfg = copy.deepcopy(DEFAULTS)
    cfg['memory']['sources'] = ['note']
    cfg['memory']['dimensions'] = 4
    return Memory(tmp_path, cfg, ledger=None, db_timeout=0.1), cfg


def put(mem, kind, title):
    with closing(mem.connect()) as db:
        return mem._put(db, Chunk(kind, title, title + ' body', None), 'fixture:' + title)


def test_new_local_reads_respect_active_kinds_and_preserve_requested_order(real_memory):
    mem, cfg = real_memory
    chat = put(mem, 'chat', 'Chat')
    assert not mem.has_searchable_chunks()
    first = put(mem, 'note', 'First')
    second = put(mem, 'note', 'Second')
    assert mem.has_searchable_chunks()
    assert [r['id'] for r in mem.cached([second, chat, first, second, 999999])] == [second, first]


def test_real_forget_is_immediately_reflected_by_cached_read(real_memory):
    mem, cfg = real_memory
    with closing(mem.connect()) as db:
        note = mem._put(db, Chunk('note', 'Forget me', 'Transient note', None), 'note:fixture')
    assert mem.cached([note])
    mem.forget(note)
    assert mem.cached([note]) == [] and not mem.has_searchable_chunks()


def test_new_read_api_never_calls_embedding(real_memory, monkeypatch):
    mem, cfg = real_memory
    cid = put(mem, 'note', 'Local note')
    monkeypatch.setattr(mem, 'embed', lambda *a, **kw: pytest.fail('unexpected paid embedding'))
    assert mem.has_searchable_chunks() and mem.cached([cid])
    assert mem.cached([]) == []


def test_automatic_and_interactive_sqlite_timeouts_remain_separate(real_memory, tmp_path):
    mem, cfg = real_memory
    with closing(mem.connect()) as db:
        assert db.execute('PRAGMA busy_timeout').fetchone()[0] == 100
    interactive = Memory(tmp_path, cfg, ledger=None)
    with closing(interactive.connect()) as db:
        assert db.execute('PRAGMA busy_timeout').fetchone()[0] == 30000


@pytest.mark.parametrize('bad', [True, 0, -1, '1'])
def test_cached_id_validation_is_strict_and_local(real_memory, bad):
    mem, _ = real_memory
    with pytest.raises(ValueError):
        mem.cached([bad])


@pytest.mark.parametrize('bad', [True, -1, 31, float('nan')])
def test_new_db_timeout_validation_does_not_open_an_unsafe_wait(tmp_path, bad):
    cfg = copy.deepcopy(DEFAULTS)
    with pytest.raises(ValueError):
        Memory(tmp_path, cfg, ledger=None, db_timeout=bad)
    assert not (tmp_path / 'state' / 'memory.db').exists()


def test_post_credit_wait_revalidates_forgotten_memory_without_a_second_search(integration):
    from station.board_recall import BoardRecall
    life, fake, history = integration
    tick = [0.0]
    life.board_recall = BoardRecall(now=lambda: tick[0])
    life.state['threads']['main']['goal'] = 'Implement the parser'
    life.llm.script = [[{'type': 'text', 'text': 'Done.'}]]
    gates = []
    def gate(current_history):
        gates.append(True)
        if len(gates) == 2:
            tick[0] = 1000.0  # Simulate a credit reset wait exceeding cache lifetime.
            fake.rows = []    # An external CLI forgot the note during that wait.
    life.credit_gate = gate
    assert life.run(max_calls=1) == 'max_calls'
    assert len(gates) == 2 and len(fake.searches) == 1 and fake.local_reads >= 1
    shown = life.llm.requests[0]['messages'][-1]['content'][-1]['text']
    assert 'SYNTHETIC RECALLED CONTENT' not in shown and '## recall' not in shown


def test_validate_only_expired_cache_reads_locally_without_search(integration):
    from station.board_recall import BoardRecall
    life, fake, history = integration
    tick = [0.0]
    life.board_recall = BoardRecall(now=lambda: tick[0])
    assert life.prepare_board_recall(history)
    tick[0] = 1000.0
    assert not life.prepare_board_recall(history, validate_only=True)
    assert len(fake.searches) == 1 and fake.local_reads == 1
    assert 'SYNTHETIC RECALLED CONTENT' in board(life, history)


def test_validate_only_without_cache_never_starts_search(integration):
    life, fake, history = integration
    assert not life.prepare_board_recall(history, validate_only=True)
    assert not fake.searches and not fake.factories
