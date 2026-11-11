"""v0 output parity using recorded canonical stop lines and explicit quote/time variants.

000963's 24.824/23.364 lines are the existing post-dividend fixture in
policy_v0/monitor/test_stop_alert.py. Prices and times below are boundary variants,
not new financial observations or a broker acceptance run.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
import json
import sqlite3
import subprocess
import sys

import pytest

from a_share_research.policy import stop_evaluation
from a_share_research.policy.stop_state import StopState, validate_state
from a_share_research.policy.v0 import commands_holdings, domain, market_quotes, risk_budget


STOP1, STOP2 = 24.824, 23.364


def bundle(price=24.0, now='2026-10-22T10:31:00+08:00', quote_time='10:30:00', state=None):
    return dict(risk_policy=risk_budget.policy(), now=now,
                holdings=[dict(code='000963', name='华东医药', stop_loss_15=STOP1, stop_loss_20=STOP2)],
                quotes={'000963': asdict(market_quotes.PriceQuote(price, '2026-10-22', quote_time, 'boundary-fixture'))},
                previous_state={} if state is None else state)


def evaluate(value):
    return stop_evaluation.evaluate(value, value['risk_policy'])


@pytest.fixture
def legacy(monkeypatch, tmp_path, capsys):
    """Run only the old function against an in-memory fixture and private state path."""
    path = tmp_path / 'stop-alert-state.json'
    @contextmanager
    def local(rows):
        conn = sqlite3.connect(':memory:')
        try:
            conn.execute('CREATE TABLE holdings(code TEXT,name TEXT,stop_loss_15 REAL,stop_loss_20 REAL,exit_date TEXT)')
            conn.executemany('INSERT INTO holdings VALUES(?,?,?,?,NULL)',
                             [(r['code'],r['name'],r['stop_loss_15'],r['stop_loss_20']) for r in rows])
            yield conn
        finally:
            conn.close()
    def run(value):
        path.write_text(json.dumps(value['previous_state']))
        now = datetime.fromisoformat(value['now']).astimezone(domain._CST)
        monkeypatch.setattr(domain, 'cst_now', lambda: now)
        monkeypatch.setattr(commands_holdings.db, 'read_only_db_session', lambda: local(value['holdings']))
        monkeypatch.setattr(commands_holdings.paths, 'state_dir', lambda: tmp_path)
        monkeypatch.setattr(market_quotes, 'fetch_current_price_quotes', lambda codes: {
            c: None if q is None else market_quotes.PriceQuote(**q) for c,q in value['quotes'].items()})
        code = 0
        try:
            commands_holdings.cmd_stop_alert([])
        except SystemExit as exc:
            code = exc.code
        output = capsys.readouterr()
        return dict(exit_code=code, stdout=output.out, stderr=output.err, state=json.loads(path.read_text()))
    return run


@pytest.mark.parametrize('price,level', [(26.21,None),(25.50,'near'),(24.824,'stop1'),(23.0,'stop2')])
def test_canonical_stop_boundary_matches_exact_old_output_and_state(legacy, price, level):
    value = bundle(price)
    result = evaluate(value)
    old = legacy(value)
    assert {k:result[k] for k in old} == old
    assert result['alerts'] == ([] if level is None else [dict(code='000963',level=level)])
    assert result['status'] == ('no_alerts' if level is None else 'alerts')


@pytest.mark.parametrize('now,quote_time,status', [
    ('2026-10-22T12:00:00+08:00','10:30:00','outside_session'),
    ('2026-10-24T10:31:00+08:00','10:30:00','outside_session'),
    ('2026-10-22T15:00:01+08:00','15:00:00','outside_session'),
    ('2026-10-22T10:45:00+08:00','10:30:00','alerts'),
    ('2026-10-22T10:45:01+08:00','10:30:00','unavailable'),
    ('2026-10-22T10:15:00+08:00','10:30:00','alerts'),
    ('2026-10-22T10:14:59+08:00','10:30:00','unavailable'),
    ('2026-10-22T02:31:00+00:00','10:30:00','alerts'),
])
def test_clock_and_absolute_fifteen_minute_window_preserve_old_semantics(legacy, now, quote_time, status):
    value = bundle(now=now, quote_time=quote_time)
    result, old = evaluate(value), legacy(value)
    assert {k:result[k] for k in old} == old
    assert result['status'] == status


@pytest.mark.parametrize('change', [dict(quote_date='2026-10-21'),dict(quote_time=None),dict(price=0.),
                                  dict(conflicted=True),dict(source=''),dict(quote_time='invalid')])
def test_unusable_quote_fails_visibly_without_advancing_state(legacy, change):
    value = bundle(state={'date':'2026-10-22','levels':{'000963':'near'}})
    value['quotes']['000963'].update(change)
    result, old = evaluate(value), legacy(value)
    assert {k:result[k] for k in old} == old
    assert result['status'] == 'unavailable'


def test_duplicate_same_level_suppressed_but_worse_level_and_next_day_alert(legacy):
    near = evaluate(bundle(25.50))
    for price, day, expected in [(25.50,'2026-10-22',[]),(24.0,'2026-10-22',['stop1']),
                                 (25.50,'2026-10-23',['near'])]:
        value = bundle(price, now=f'{day}T10:31:00+08:00', state=near['state'])
        value['quotes']['000963']['quote_date'] = day
        result, old = evaluate(value), legacy(value)
        assert {k:result[k] for k in old} == old
        assert [a['level'] for a in result['alerts']] == expected


def test_missing_empty_and_partial_quotes_and_duplicate_rows_keep_legacy_behavior(legacy):
    for kind in ('empty','missing','partial','duplicate'):
        value = bundle()
        if kind == 'empty': value['holdings'] = []
        elif kind == 'missing': value['quotes'] = {}
        elif kind == 'partial':
            # Missing-quote structural variant; no new financial assertion.
            value['holdings'].append(value['holdings'][0] | {'code':'600036'})
        else:
            value['holdings'].append(value['holdings'][0] | {'stop_loss_15':STOP1+1})
        result, old = evaluate(value), legacy(value)
        assert {k:result[k] for k in old} == old


@pytest.mark.parametrize('value', [None, [], {'date':None}, {'date':'2026-02-30'}, {'date':'20261022'},
                                  {'levels':None}, {'levels':{'':'near'}}, {'levels':{' ':'near'}},
                                  {'levels':{'000963':'invalid'}}, {'unknown':True}])
def test_state_strict_boundary_rejects_corruption(value):
    with pytest.raises(ValueError): validate_state(value)


def test_state_preserves_omitted_fields_and_copies_values():
    assert validate_state({}) == {}
    assert StopState().model_dump(mode='json',exclude_unset=True) == {}
    old = {'date':'2026-10-22','levels':{'000963':'stop1'}}
    result = validate_state(old)
    result['levels']['000963'] = 'stop2'
    assert old['levels']['000963'] == 'stop1'


@pytest.mark.parametrize('mutation', [
    lambda v: v.update(now='2026-10-22T10:31:00'),
    lambda v: v.update(extra=True),
    lambda v: v['holdings'][0].update(extra=True),
    lambda v: v['holdings'][0].update(stop_loss_15=True),
    lambda v: v['quotes']['000963'].update(price=float('nan')),
    lambda v: v['quotes']['000963'].update(price='24.0'),
])
def test_bundle_rejects_untyped_or_nonfinite_input(mutation):
    value = bundle(); mutation(value)
    with pytest.raises(ValueError): evaluate(value)


def test_policy_identity_and_validity_window_are_checked():
    from test_policy_capture import scoped_parameters
    value = bundle()
    with pytest.raises(ValueError,match='does not match'):
        stop_evaluation.evaluate(value,risk_budget.policy(3.0))
    for params in (scoped_parameters(effective_from='2026-11-01T00:00:00+08:00'),
                   scoped_parameters(confirmed_at='2026-11-01T00:00:00+08:00'),
                   scoped_parameters(effective_to='2026-10-01T00:00:00+08:00')):
        value['risk_policy'] = params
        with pytest.raises(ValueError,match='not effective'): evaluate(value)


def test_evaluation_does_not_read_a_clock_database_file_or_provider(monkeypatch):
    value = bundle(); before = deepcopy(value)
    def forbidden(*args,**kwargs): pytest.fail('pure stop evaluation must not read outside its bundle')
    monkeypatch.setattr(domain,'cst_now',forbidden)
    monkeypatch.setattr(commands_holdings.db,'read_only_db_session',forbidden)
    monkeypatch.setattr(commands_holdings.paths,'state_dir',forbidden)
    monkeypatch.setattr(market_quotes,'fetch_current_price_quotes',forbidden)
    assert evaluate(value) == evaluate(value)
    assert value == before


def test_stop_state_import_does_not_load_policy_runtime():
    code = "from a_share_research.policy.stop_state import StopState; import sys; assert not any(m.startswith(('a_share_research.policy.v0','pandas','tushare')) for m in sys.modules)"
    result = subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,timeout=10)
    assert result.returncode == 0, result.stderr
