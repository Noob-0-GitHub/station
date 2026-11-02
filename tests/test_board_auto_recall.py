"""Offline coverage for the dependency-injected Board recall helper only."""

import ast
import copy
import dataclasses
import inspect
import json

import pytest

from station.board_recall import BoardRecall


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def row(cid: object = 1, *, kind="note", text="A remembered topic", title="A useful title", ts: object = 0):
    return dict(id=cid, kind=kind, text=text, title=title, ts=ts)


class FakeMemory:
    def __init__(self):
        self.present = True
        self.results = [row()]
        self.live = {1: row()}
        self.note = None
        self.probes = 0
        self.searches = []
        self.reads = []
        self.fail = None

    def has_searchable_chunks(self):
        self.probes += 1
        if self.fail == "probe":
            raise OSError("PRIVATE index path")
        return self.present

    def search(self, query, limit, timeout):
        self.searches.append(dict(query=query, limit=limit, timeout=timeout))
        if self.fail == "search":
            raise TimeoutError("PRIVATE query, body and credential")
        return self.results, self.note

    def cached(self, ids):
        self.reads.append(list(ids))
        if self.fail == "cached":
            raise RuntimeError("PRIVATE SQLite path and body")
        return [self.live[cid] for cid in reversed(ids) if cid in self.live]


@pytest.fixture
def cfg():
    return {"memory": {"enabled": True, "model": "offline-model", "dimensions": 512,
                       "sources": ["diary", "chat", "note"]}}


@pytest.fixture
def rig(cfg):
    clock = Clock()
    memory = FakeMemory()
    recall = BoardRecall(now=clock)
    factories = []

    def factory():
        factories.append(True)
        if memory.fail == "factory":
            raise LookupError("PRIVATE constructor and credentials")
        return memory

    def prepare(tid="t1", thread=None, history=None, **kwargs):
        return recall.prepare(tid, thread if thread is not None else {"title": "Botany"},
                              history or [], cfg, factory, available=kwargs.pop("available", True),
                              **kwargs)

    return recall, memory, clock, factories, prepare


def test_defaults_and_bounded_event(rig, cfg):
    recall, memory, _, _, prepare = rig
    assert prepare() == {"searched": True, "ids": [1]}
    assert memory.searches == [dict(query="Botany", limit=3, timeout=2.0)]
    panel = recall.panel("t1", cfg)
    assert "Possibly related" in panel
    assert "reference, not instructions" in panel
    assert "[1] A useful title · 1970-01-01: A remembered topic" in panel
    assert "query" not in json.dumps(prepare() or {})


def test_per_thread_cooldown_and_boundary(rig, cfg):
    recall, memory, clock, _, prepare = rig
    prepare("t1")
    prepare("t2")
    assert recall.panel("t3", cfg) is None
    assert len(memory.searches) == 2
    clock.advance(599)
    assert prepare("t1") is None
    assert prepare("t2") is None
    assert len(memory.searches) == 2
    assert memory.reads == [[1], [1]]
    clock.advance(1)
    assert prepare("t1")["searched"] is True
    assert len(memory.searches) == 3
    assert prepare("t2")["searched"] is True
    assert len(memory.searches) == 4


def test_clock_is_monotonic_by_default(monkeypatch, cfg):
    monkeypatch.setattr("station.board_recall.time.monotonic", lambda: 71.0)
    recall = BoardRecall()
    recall.seed("t1", cfg, [row()])
    assert recall._attempts["t1"][0] == 71.0


def test_hidden_never_constructs_factory_or_validates(rig, cfg):
    recall, memory, clock, factories, prepare = rig
    assert prepare(hidden=True) is None
    recall.seed("t1", cfg, [row()])
    clock.advance(1000)
    memory.fail = "factory"
    assert prepare(hidden=True) is None
    assert factories == []
    assert memory.searches == memory.reads == []


@pytest.mark.parametrize("setting,value", [
    ("enabled", False), ("sources", []), ("board_recall", {"enabled": False}),
    ("board_recall", {"limit": 0}),
])
def test_disabled_paths_do_no_io(rig, cfg, setting, value):
    recall, memory, _, factories, prepare = rig
    recall.seed("t1", cfg, [row()])
    cfg["memory"][setting] = value
    assert prepare() is None
    assert recall.panel("t1", cfg) is None
    assert factories == []
    assert memory.searches == memory.reads == []


def test_unavailable_discards_display_without_factory(rig, cfg):
    recall, memory, _, factories, prepare = rig
    recall.seed("t1", cfg, [row()])
    assert prepare(available=False) is None
    assert recall.panel("t1", cfg) is None
    assert factories == []
    assert memory.reads == []


@pytest.mark.parametrize("history", [
    [], [{"role": "assistant", "content": "everyday"}],
    [{"role": "user", "content": "okay"}],
    [{"role": "assistant", "content": "everyday."}],
    [{"role": "assistant", "content": "Thanks!"}],
    [{"role": "tool", "content": "Secret tool output"}],
    [{"role": "system", "content": "Secret system instructions"}],
    [{"role": "assistant", "content": [{"type": "tool_use", "input": {"query": "secret"}},
                                          {"type": "thinking", "thinking": "secret"}]}],
    [{"role": "user", "content": [{"type": "tool_result", "content": "secret"}]}],
    [{"role": "user", "content": "<env>2026-11-02 Woke after 30 minutes.</env>"}],
    [{"role": "assistant", "content": "<board>SECRET</board><think>secret</think>"}],
    [{"role": "user", "content": "Now 12:00\nWake: interval\n2026-11-02 Mon 12:00"}],
    [{"role": "user", "content": "12345 ... !!!"}],
])
def test_no_meaningful_query_never_constructs_memory(rig, history):
    _, memory, _, factories, prepare = rig
    assert prepare(thread={"title": "everyday", "goal": ""}, history=history) is None
    assert factories == []
    assert memory.probes == 0
    assert memory.searches == []


def test_empty_index_consumes_cooldown_without_search(rig, cfg):
    recall, memory, clock, factories, prepare = rig
    memory.present = False
    assert prepare() == {"searched": False, "ids": []}
    memory.present = True
    assert prepare() is None
    assert len(factories) == 1
    assert memory.searches == []
    assert recall.panel("t1", cfg) is None
    clock.advance(600)
    assert prepare() == {"searched": True, "ids": [1]}


def test_empty_search_consumes_cooldown_without_revalidation(rig, cfg):
    recall, memory, clock, factories, prepare = rig
    memory.results = []
    assert prepare() == {"searched": True, "ids": []}
    assert prepare() is None
    assert len(factories) == 1
    assert memory.reads == []
    assert recall.panel("t1", cfg) is None
    clock.advance(600)
    assert prepare() == {"searched": True, "ids": []}
    assert len(memory.searches) == 2


@pytest.mark.parametrize("stage,error,searched", [
    ("factory", "LookupError", False), ("probe", "OSError", False),
    ("search", "TimeoutError", True), ("cached", "RuntimeError", False),
])
def test_failures_are_private_fail_soft_and_rate_limited(rig, cfg, stage, error, searched):
    recall, memory, clock, factories, prepare = rig
    if stage == "cached":
        recall.seed("t1", cfg, [row()])
    memory.fail = stage
    event = prepare()
    assert event == {"searched": searched, "ids": [], "error": error}
    assert "PRIVATE" not in json.dumps(event)
    assert recall.panel("t1", cfg) is None
    calls = len(factories)
    assert prepare() is None
    clock.advance(599)
    assert prepare() is None
    assert len(factories) == calls
    memory.fail = None
    clock.advance(1)
    assert prepare() == {"searched": True, "ids": [1]}


def test_word_fallback_never_displays_raw_note(rig, cfg):
    recall, memory, _, _, prepare = rig
    memory.note = "API failure: PRIVATE body; word matches only"
    assert prepare() == {"searched": True, "ids": [1]}
    panel = recall.panel("t1", cfg)
    assert "word matches only" in panel
    assert "PRIVATE" not in panel
    assert "API failure" not in panel
    assert "PRIVATE" not in repr(recall._cache)


def test_seed_copies_and_reuses_only_this_thread(rig, cfg):
    recall, memory, _, _, prepare = rig
    results = [row()]
    recall.seed("t1", cfg, results, note="PRIVATE word fallback")
    results[0]["text"] = "MUTATED"
    results.clear()
    assert "MUTATED" not in recall.panel("t1", cfg)
    assert "PRIVATE" not in recall.panel("t1", cfg)
    assert prepare("t1") is None
    assert memory.reads == [[1]]
    assert memory.searches == []
    assert prepare("t2") == {"searched": True, "ids": [1]}


def test_empty_seed_is_an_attempt_not_an_immediate_second_search(rig, cfg):
    recall, memory, clock, factories, prepare = rig
    recall.seed("t1", cfg, [])
    assert prepare() is None
    assert factories == []
    clock.advance(600)
    assert prepare()["searched"] is True
    assert len(memory.searches) == 1


def test_forgotten_missing_and_disabled_kinds_are_suppressed_locally(rig, cfg):
    recall, memory, _, _, prepare = rig
    results = [row(1), row(2, kind="chat"), row(3, kind="diary")]
    recall.seed("t1", cfg, results)
    memory.live = {1: row(1), 3: row(3, kind="disabled-kind")}
    assert prepare() is None
    panel = recall.panel("t1", cfg)
    assert "[1]" in panel
    assert "[2]" not in panel and "[3]" not in panel
    assert memory.searches == []
    memory.live.clear()
    assert prepare() is None
    assert recall.panel("t1", cfg) is None
    assert memory.reads == [[1, 2, 3], [1]]


def test_local_validation_preserves_rank_and_ignores_unrequested_ids(rig, cfg):
    recall, memory, _, _, prepare = rig
    recall.seed("t1", cfg, [row(2), row(1)])
    memory.live = {1: row(1, text="updated locally"), 2: row(2)}
    assert prepare() is None
    panel = recall.panel("t1", cfg)
    assert panel.index("[2]") < panel.index("[1]")
    assert "updated locally" in panel
    memory.cached = lambda ids: [row(999)]
    assert prepare() is None
    assert recall.panel("t1", cfg) is None
    assert memory.searches == []


@pytest.mark.parametrize("field,value", [
    ("model", "different-model"), ("dimensions", 256), ("sources", ["diary"]),
    ("board_recall.limit", 2), ("board_recall.refresh_seconds", 60),
    ("board_recall.query_max_chars", 90), ("board_recall.excerpt_chars", 10),
    ("board_recall.timeout", 1),
])
def test_cache_key_changes_hide_results_without_evading_cooldown(rig, cfg, field, value):
    recall, memory, clock, factories, prepare = rig
    prepare()
    if "." in field:
        _, name = field.split(".")
        cfg["memory"]["board_recall"] = {name: value}
    else:
        cfg["memory"][field] = value
    assert recall.panel("t1", cfg) is None
    assert prepare() is None
    assert len(memory.searches) == 1
    assert len(factories) == 1
    clock.advance(599)
    assert prepare() is None
    clock.advance(1)
    event = prepare()
    assert event["searched"] is True
    assert len(memory.searches) == 2
    if field == "sources":
        assert event["ids"] == []
        assert recall.panel("t1", cfg) is None


def test_longer_refresh_setting_is_honored_on_existing_attempt(rig, cfg):
    _, memory, clock, _, prepare = rig
    prepare()
    cfg["memory"]["board_recall"] = {"refresh_seconds": 900}
    clock.advance(600)
    assert prepare() is None
    clock.advance(300)
    assert prepare()["searched"] is True
    assert len(memory.searches) == 2


def test_semantically_identical_source_order_keeps_cache(rig, cfg):
    recall, memory, _, _, prepare = rig
    recall.seed("t1", cfg, [row()])
    cfg["memory"]["sources"] = ["note", "chat", "diary", "note"]
    assert prepare() is None
    assert recall.panel("t1", cfg) is not None
    assert memory.searches == []


def test_config_round_trip_cannot_restore_invalidated_results(rig, cfg):
    recall, _, _, _, prepare = rig
    prepare()
    original = copy.deepcopy(cfg)
    cfg["memory"]["model"] = "different"
    prepare()
    cfg.clear()
    cfg.update(original)
    assert prepare() is None
    assert recall.panel("t1", cfg) is None


@pytest.mark.parametrize("recall_cfg,field", [
    (None, "memory.board_recall"), ("PRIVATE", "memory.board_recall"),
    ({"enabled": "PRIVATE"}, "memory.board_recall.enabled"),
    ({"limit": -1}, "memory.board_recall.limit"),
    ({"limit": True}, "memory.board_recall.limit"),
    ({"limit": 101}, "memory.board_recall.limit"),
    ({"refresh_seconds": 0}, "memory.board_recall.refresh_seconds"),
    ({"refresh_seconds": float("inf")}, "memory.board_recall.refresh_seconds"),
    ({"query_max_chars": 0}, "memory.board_recall.query_max_chars"),
    ({"excerpt_chars": "PRIVATE"}, "memory.board_recall.excerpt_chars"),
    ({"timeout": float("nan")}, "memory.board_recall.timeout"),
    ({"timeout": -2}, "memory.board_recall.timeout"),
])
def test_invalid_recall_cfg_is_bounded_and_seed_never_raises(rig, cfg, recall_cfg, field):
    recall, memory, clock, factories, prepare = rig
    cfg["memory"]["board_recall"] = recall_cfg
    recall.seed("t1", cfg, [row()])
    assert recall.panel("t1", cfg) is None
    event = prepare()
    assert event == {"searched": False, "ids": [], "error": "ValueError", "field": field}
    assert "PRIVATE" not in json.dumps(event)
    assert prepare() is None
    assert factories == []
    assert memory.searches == []
    cfg["memory"]["board_recall"] = {}
    assert prepare() is None
    clock.advance(600)
    assert prepare()["searched"] is True


@pytest.mark.parametrize("field,value", [
    ("sources", "note"), ("sources", ["PRIVATE"]), ("model", None),
    ("dimensions", 0), ("dimensions", True), ("enabled", "PRIVATE"),
])
def test_bad_memory_config_fails_soft(rig, cfg, field, value):
    recall, _, _, factories, prepare = rig
    cfg["memory"][field] = value
    recall.seed("t1", cfg, [row()])
    event = prepare()
    assert event["field"] == "memory." + field
    assert recall.panel("t1", cfg) is None
    assert factories == []


def test_invalid_configuration_after_seed_reports_once_and_discards_panel(rig, cfg):
    recall, _, _, factories, prepare = rig
    recall.seed("t1", cfg, [row()])
    cfg["memory"]["board_recall"] = {"timeout": "PRIVATE"}
    assert prepare() == {"searched": False, "ids": [], "error": "ValueError",
                         "field": "memory.board_recall.timeout"}
    assert prepare() is None
    assert recall.panel("t1", cfg) is None
    assert factories == []


@pytest.mark.parametrize("bad_cfg", [None, [], {"memory": None}, {"memory": "PRIVATE"}])
def test_seed_and_panel_tolerate_entirely_invalid_cfg(bad_cfg):
    recall = BoardRecall(now=lambda: 100.0)
    recall.seed("t1", bad_cfg, [row()])
    assert recall.panel("t1", bad_cfg) is None


@pytest.mark.parametrize("results", [None, 17, "unstructured Focus text", [None, {}, {"id": "bad"}]])
def test_invalid_seed_results_never_raise_or_create_panel(rig, cfg, results):
    recall, _, _, _, _ = rig
    recall.seed("t1", cfg, results)
    assert recall.panel("t1", cfg) is None


def test_sources_filter_search_and_seed_rows(rig, cfg):
    recall, memory, _, _, prepare = rig
    cfg["memory"]["sources"] = ["diary"]
    rows = [row(1), row(2, kind="chat"), row(3, kind="diary")]
    memory.results = rows
    memory.live = {3: rows[2]}
    assert prepare() == {"searched": True, "ids": [3]}
    assert "[1]" not in recall.panel("t1", cfg)
    recall.seed("t2", cfg, rows)
    assert "[3]" in recall.panel("t2", cfg)
    assert "[2]" not in recall.panel("t2", cfg)


def test_visible_source_extraction_excludes_all_opaque_payloads(rig):
    _, memory, _, _, prepare = rig
    history = [
        {"role": "system", "content": "SECRET_SYSTEM"},
        {"role": "tool", "content": "SECRET_TOOL"},
        {"role": "user", "content": [
            {"type": "text", "text": "Substantive user botany"},
            {"type": "tool_result", "content": [{"type": "text", "text": "SECRET_RESULT"}]},
            {"type": "image", "source": {"data": "SECRET_IMAGE"}},
        ]},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "SECRET_THINKING"},
            {"type": "tool_use", "name": "bash", "input": {"command": "SECRET_ARGS"}},
            {"type": "text", "text": "Substantive assistant response "
             "<board>SECRET_BOARD</board><env>SECRET_ENV</env>"
             "<thinking>SECRET_XML</thinking><analysis>SECRET_ANALYSIS</analysis>"},
        ]},
        {"role": "user", "content": "<env>Woke at 12:00</env>"},
    ]
    snapshot = copy.deepcopy(history)
    thread = {"title": "Orchids", "goal": "Find suitable soil"}
    prepare(thread=thread, history=history)
    query = memory.searches[0]["query"]
    assert "Orchids" in query and "Find suitable soil" in query
    assert "Substantive user botany" in query and "Substantive assistant response" in query
    assert "SECRET" not in query and "Woke" not in query
    assert history == snapshot
    assert thread == {"title": "Orchids", "goal": "Find suitable soil"}


def test_query_preserves_newest_text_within_exact_budget(rig, cfg):
    _, memory, _, _, prepare = rig
    cfg["memory"]["board_recall"] = {"query_max_chars": 40}
    prepare(thread={"title": "OLD_TITLE" * 20, "goal": "OLD_GOAL" * 20}, history=[
        {"role": "user", "content": "OLDER" * 1000},
        {"role": "assistant", "content": "x" * 1000 + " Most recent useful orchids"},
        {"role": "user", "content": "<env>Woke just now</env>"},
    ])
    query = memory.searches[0]["query"]
    assert len(query) <= 40
    assert query.endswith("Most recent useful orchids")
    assert "OLDER" not in query and "OLD_TITLE" not in query


def test_title_only_query_is_also_bounded(rig, cfg):
    _, memory, _, _, prepare = rig
    cfg["memory"]["board_recall"] = {"query_max_chars": 30}
    prepare(thread={"title": "a" * 500 + " latest title"})
    assert len(memory.searches[0]["query"]) <= 30
    assert memory.searches[0]["query"].endswith("latest title")


def test_unclosed_opaque_blocks_do_not_leak(rig):
    _, memory, _, _, prepare = rig
    prepare(history=[{"role": "assistant", "content": "orchids <env>SECRET"}])
    assert memory.searches[0]["query"] == "Botany orchids"


def test_result_limits_whitespace_titles_dates_and_excerpt_lengths(rig, cfg):
    recall, memory, _, _, prepare = rig
    cfg["memory"]["board_recall"] = {"limit": 2, "excerpt_chars": 12}
    memory.results = [row(1, text="  alpha \n beta\t gamma delta  ", title="Title\n" + "T" * 500),
                      row(2, text="x" * 500, ts=None), row(3)]
    assert prepare() == {"searched": True, "ids": [1, 2]}
    entry = recall._cache["t1"]
    assert len(entry.rows) == 2
    assert entry.rows[0]["text"] == "alpha beta g"
    assert all(len(r["text"]) <= 12 and len(r["title"]) <= 160 for r in entry.rows)
    panel = recall.panel("t1", cfg)
    assert "[3]" not in panel
    assert "undated" in panel
    assert "\t" not in panel
    assert len(panel) < 600
    assert not any("ts" in r or "how" in r for r in entry.rows)


def test_invalid_duplicate_and_huge_ids_are_never_logged(rig):
    _, memory, _, _, prepare = rig
    memory.results = [row(-1), row(True), row("PRIVATE"), row(2**100), row(1), row(1), row(2)]
    assert prepare() == {"searched": True, "ids": [1, 2]}


def test_panel_has_no_io_clock_factory_or_mutation(rig, cfg, monkeypatch):
    recall, memory, _, factories, prepare = rig
    prepare()
    before = copy.deepcopy(dataclasses.asdict(recall._cache["t1"]))
    counters = (len(factories), len(memory.searches), len(memory.reads), memory.probes)

    def refuse(*args, **kwargs):
        raise AssertionError("panel attempted an external operation")

    monkeypatch.setattr("builtins.open", refuse)
    monkeypatch.setattr("sqlite3.connect", refuse)
    monkeypatch.setattr("socket.socket", refuse)
    monkeypatch.setattr(recall, "_now", refuse)
    for _ in range(3):
        assert "[1]" in recall.panel("t1", cfg)
    assert dataclasses.asdict(recall._cache["t1"]) == before
    assert counters == (len(factories), len(memory.searches), len(memory.reads), memory.probes)


def test_helper_imports_no_real_memory_or_io_module():
    import station.board_recall as module

    tree = ast.parse(inspect.getsource(module))
    imports = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.append(node.module)
    assert set(imports) <= {"__future__", "datetime", "math", "re", "time", "dataclasses"}


def test_malformed_search_payload_is_private_and_cooled_down(rig, cfg):
    recall, memory, _, factories, prepare = rig
    memory.results = None
    event = prepare()
    assert event == {"searched": True, "ids": [], "error": "TypeError"}
    assert prepare() is None
    assert len(factories) == 1
    assert recall.panel("t1", cfg) is None
