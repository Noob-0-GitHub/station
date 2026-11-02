"""Runtime choices must not be silently filled with personal policy constants."""
import copy
import datetime as dt

import pytest

from station import clock, credits
from station.api import ApiError, check_limit
from station.config import DEFAULTS
from station.ledger import Ledger
from test_loop import make


def test_credit_allowances_have_no_personal_default():
    assert DEFAULTS['credits']['allowances'] == {'daily': None, 'weekly': None}
    with pytest.raises(ValueError, match='allowances.daily'):
        credits.validate(DEFAULTS['credits'])


@pytest.mark.parametrize('value', [None, 0, -1, True, '10', float('nan'), float('inf'), 10 ** 1000])
def test_credit_allowances_reject_invalid_values(value):
    with pytest.raises(ValueError, match='positive finite'):
        credits.allowances({'allowances': {'daily': value, 'weekly': 23}})


def test_credit_weekly_allowance_is_independent():
    cfg = {'allowances': {'daily': 3, 'weekly': 100}}
    assert credits.allowances(cfg) == {'daily': 3, 'weekly': 100}
    cfg['allowances']['daily'] = 8
    assert credits.allowances(cfg)['weekly'] == 100


def test_credit_disabled_is_explicit_and_not_infinite_by_accident():
    credits.validate({'enabled': False})
    assert credits.allowances({'enabled': False}) == {'daily': 0, 'weekly': 0}
    with pytest.raises(ValueError, match='boolean'):
        credits.validate({'enabled': 0})


def test_missing_credit_policy_blocks_onboarding_before_model_call(tmp_path):
    life = make(tmp_path, [])
    life.cfg['credits']['allowances'] = {'daily': None, 'weekly': None}
    with pytest.raises(ValueError, match='allowances.daily'):
        life.run(max_calls=1)
    assert not life.llm.requests


def test_missing_credit_policy_blocks_paid_api_before_network(tmp_path):
    cfg = copy.deepcopy(DEFAULTS)
    with pytest.raises(ApiError, match='allowances.daily') as caught:
        check_limit(cfg, Ledger(tmp_path / 'ledger.db'))
    assert caught.value.kind == 'config'


def test_credit_reset_times_and_weekday_are_configurable(tmp_path):
    cfg = {'allowances': {'daily': 3, 'weekly': 100},
           'reset': {'daily_at': '06:30', 'weekly_day': 'thursday', 'weekly_at': '14:15'}}
    before = clock.wall(dt.date(2026, 11, 5), 5, 0)
    assert clock.at(credits.period_start('daily', before, cfg)).strftime('%a %H:%M') == 'Wed 06:30'
    assert clock.at(credits.next_reset('daily', before, cfg)).strftime('%a %H:%M') == 'Thu 06:30'
    noon = clock.wall(dt.date(2026, 11, 5), 12, 0)
    assert clock.at(credits.period_start('weekly', noon, cfg)).strftime('%Y-%m-%d %H:%M') == '2026-10-29 14:15'
    assert clock.at(credits.next_reset('weekly', noon, cfg)).strftime('%Y-%m-%d %H:%M') == '2026-11-05 14:15'
    exact = clock.wall(dt.date(2026, 11, 5), 14, 15)
    assert credits.period_start('weekly', exact, cfg) == exact
    assert clock.at(credits.next_reset('weekly', exact, cfg)).strftime('%Y-%m-%d %H:%M') == '2026-11-12 14:15'
    ledger = Ledger(tmp_path / 'ledger.db')
    ledger.add('model', ts=before, credits=2)
    later = clock.wall(dt.date(2026, 11, 5), 7, 0)
    st = credits.status(ledger, cfg, later)
    assert st['daily']['used'] == 0 and st['weekly']['used'] == 2


@pytest.mark.parametrize('reset', [{'daily_at': '6:30'}, {'daily_at': '24:00'},
                                   {'weekly_at': '10:60'}, {'weekly_day': 'Monday'}])
def test_invalid_credit_reset_config_fails(reset):
    with pytest.raises(ValueError):
        credits.reset_settings({'reset': reset})


def test_no_wake_sources_waits_without_model_polling(tmp_path, monkeypatch):
    import time
    from station.loop import Stop
    life = make(tmp_path, [])
    life.state['threads']['main']['interval'] = None
    life.cfg['schedule']['morning_at'] = None
    now = time.time()
    life.state['sleep'] = {'since': now, 'cursor': now, 'thread': 'main'}
    life.schedule_state()['cron'] = []
    polls = []
    def stop_after_poll():
        polls.append(True)
        life.stopping = True
    monkeypatch.setattr(life, 'check_reload', stop_after_poll)
    monkeypatch.setattr('station.loop.time.sleep', lambda _: None)
    with pytest.raises(Stop):
        life.wait()
    assert polls == [True] and life.state['sleep']['next'] is None
    assert not life.llm.requests
    assert 'no scheduled wake' in life.alarm_summary()
    assert 'daily wake: off' in life.schedule_tool({'action': 'list'})


def test_custom_daily_wake_reaches_real_loop_without_model_call(tmp_path, monkeypatch):
    life = make(tmp_path, [])
    target = clock.wall(dt.date(2026, 11, 5), 7, 45)
    life.cfg['schedule']['morning_at'] = '07:45'
    life.state['threads']['main']['interval'] = None
    life.state['sleep'] = {'since': target - 300, 'cursor': target - 300, 'thread': 'main'}
    schedule = life.schedule_state()
    schedule['morning_last'] = clock.wall(dt.date(2026, 11, 4), 7, 45)
    schedule['cron'] = []
    monkeypatch.setattr('station.loop.time.time', lambda: target)
    wake = life.wait()
    assert 'daily wake at 07:45' in wake[0]['text']
    assert '09:00' not in wake[0]['text'] and not life.llm.requests


def test_status_and_init_are_available_before_budget_configuration(tmp_path, capsys):
    import yaml
    from station.cli import main
    assert main(['--root', str(tmp_path), 'init']) == 0
    cfg = yaml.safe_load((tmp_path / 'station.yaml').read_text())
    assert cfg['credits']['allowances'] == {'daily': None, 'weekly': None}
    assert cfg['schedule']['morning_at'] is None
    assert main(['--root', str(tmp_path), 'status']) == 0
    assert 'Credits: not configured' in capsys.readouterr().out


def test_check_rejects_invalid_daily_wake_before_constructing_models(tmp_path, monkeypatch):
    import yaml
    from station.cli import main
    make(tmp_path, [])
    path = tmp_path / 'station.yaml'
    cfg = yaml.safe_load(path.read_text())
    cfg['schedule']['morning_at'] = '7:45'
    path.write_text(yaml.safe_dump(cfg))
    def forbidden(_):
        raise AssertionError('model client must not be constructed for invalid policy')
    monkeypatch.setattr('station.router.make_llm', forbidden)
    assert main(['--root', str(tmp_path), 'check']) == 2
