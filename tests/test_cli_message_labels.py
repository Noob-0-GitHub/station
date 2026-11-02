"""Local message labels do not pretend to authenticate or deliver to a person."""

import json

from station.cli import main
from station.inbox import Inbox
from station.store import Store


def test_say_defaults_to_unknown_without_operator_config(tmp_path):
    assert main(['--root', str(tmp_path), 'say', 'A local message']) == 0
    (event,) = Inbox(tmp_path / 'inbox').peek()
    assert event.type == 'message'
    assert event.source == 'unknown'
    assert event.text == 'A local message'
    assert 'unknown: A local message' in event.render()
    assert not (tmp_path / 'station.yaml').exists()


def test_outbox_appends_timestamp_and_text_without_recipient(tmp_path, capsys):
    store = Store(tmp_path / 'state')
    store.outbox('A local runtime notice')
    row = json.loads(store.outbox_path.read_text())
    assert set(row) == {'ts', 'text'}
    assert isinstance(row['ts'], (float, int))
    assert row['text'] == 'A local runtime notice'
    assert main(['--root', str(tmp_path), 'outbox']) == 0
    assert json.loads(capsys.readouterr().out) == row


def test_outbox_reader_preserves_legacy_records(tmp_path, capsys):
    store = Store(tmp_path / 'state')
    legacy = {'ts': 1, 'to': 'legacy-label', 'text': 'Old notice'}
    store.outbox_path.write_text(json.dumps(legacy) + '\n')
    store.outbox('New notice')
    assert main(['--root', str(tmp_path), 'outbox']) == 0
    rows = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rows[0] == legacy  # do not rewrite or delete historical data
    assert set(rows[1]) == {'ts', 'text'}
