"""Isolated synthetic regression tests for batch freeze and rule snapshots."""
import copy
import json
import sqlite3

import pandas as pd
import pytest

from a_share_research import db, screening as s


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket
    monkeypatch.setattr(socket.socket, 'connect', lambda *a, **k: pytest.fail('network forbidden'))


@pytest.fixture
def connections(tmp_path):
    path = tmp_path / 'batches.db'
    worker = db.connect(path)
    web = sqlite3.connect(path, isolation_level=None)
    web.row_factory = sqlite3.Row
    yield worker, web
    web.close()
    worker.close()


def proposed(conn, *, momentum=True):
    candidates = [{'code': f'{i:06d}.SZ', 'name': str(i), 'industry': str(i), 'verdict': '需要谨慎'}
                  | ({'return_60d': 0.0, 'drawdown': False} if momentum else {}) for i in (1, 2)]
    selection = {'draft': {'picks': [{'code': candidates[0]['code']}]}}
    conn.execute('INSERT INTO batches (status,created_at,rule,q20,q5,selection) VALUES (?,?,?,?,?,?)',
                 ('proposed', db.now(), json.dumps(s.RULE), json.dumps(candidates), '[]', json.dumps(selection)))
    return candidates, selection


def snapshot(conn):
    return dict(conn.execute('SELECT * FROM batches WHERE id=1').fetchone())


def test_stale_auto_freeze_cannot_replace_owner_or_tracking(connections, monkeypatch):
    worker, web = connections
    candidates, _ = proposed(worker)
    auto = s._auto_freeze
    after_owner = []

    def freeze_first(conn, batch_id, selection, **kwargs):
        s.freeze(web, batch_id, [candidates[1]['code']], 'owner')
        after_owner.append(snapshot(web))
        return auto(conn, batch_id, selection, **kwargs)

    monkeypatch.setattr(s, '_auto_freeze', freeze_first)
    assert s.tick(worker) is False
    assert snapshot(worker) == after_owner[0]
    worker.execute("UPDATE batches SET status='tracking', entry=?, marks=? WHERE id=1",
                   (json.dumps({'date': '20261104'}), json.dumps({'latest': {'date': '20261104'}})))
    tracking = snapshot(worker)
    assert auto(worker, 1, {'draft': {'picks': [{'code': candidates[0]['code']}]}}, 'proposed') is False
    assert snapshot(worker) == tracking


def test_owner_waits_for_auto_freeze_transaction(connections, monkeypatch):
    import threading
    worker, web = connections
    candidates, selection = proposed(worker)
    real_freeze = s.freeze
    started, write_attempt, finished = threading.Event(), threading.Event(), threading.Event()
    outcomes = []
    path = web.execute('PRAGMA database_list').fetchone()['file']

    def owner():
        conn = sqlite3.connect(path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.set_trace_callback(lambda sql: write_attempt.set() if sql.startswith('UPDATE batches SET l5') else None)
        try:
            started.set()
            real_freeze(conn, 1, [candidates[1]['code']], 'owner')
            outcomes.append('owner-froze')
        except s.service.RequestError:
            outcomes.append('owner-rejected')
        finally:
            conn.close()
            finished.set()

    thread = threading.Thread(target=owner)
    def auto_with_competitor(*args, **kwargs):
        thread.start()
        assert started.wait(2)
        assert write_attempt.wait(2)
        assert not finished.wait(.1)
        return real_freeze(*args, **kwargs)
    monkeypatch.setattr(s, 'freeze', auto_with_competitor)
    assert s._auto_freeze(worker, 1, selection, 'proposed')
    thread.join(5)
    assert not thread.is_alive() and outcomes == ['owner-rejected']
    b = s.load(worker.execute('SELECT * FROM batches').fetchone())
    assert b['l5']['codes'] == [candidates[0]['code']] and b['l5']['frozen_by'] == 'auto'


def test_stale_proposed_selection_cannot_overwrite_new_selection(connections):
    worker, web = connections
    candidates, stale = proposed(worker)
    replacement = {'draft': {'picks': [{'code': candidates[1]['code']}]}}
    web.execute('UPDATE batches SET selection=? WHERE id=1', (json.dumps(replacement),))
    before = snapshot(web)
    assert s._auto_freeze(worker, 1, stale, 'proposed') is False
    assert snapshot(worker) == before


def test_frozen_q20_not_changed_by_stale_momentum_backfill(connections, monkeypatch):
    worker, web = connections
    candidates, _ = proposed(worker, momentum=False)
    monkeypatch.setattr(s, '_pro', lambda: object())
    after_owner = []

    def momentum(pro, as_of, codes, rule):
        s.freeze(web, 1, [candidates[1]['code']], 'owner')
        after_owner.append(snapshot(web))
        return {c: {'return_60d': .1, 'drawdown': False} for c in codes}

    monkeypatch.setattr(s, 'momentum', momentum)
    assert s.tick(worker) is False
    assert snapshot(worker) == after_owner[0]


def test_auto_freeze_exception_is_transactional(connections, monkeypatch):
    worker, _ = connections
    _, selection = proposed(worker)
    worker.execute("UPDATE batches SET status='selecting', selection=NULL WHERE id=1")
    before = snapshot(worker)
    def unexpected(*args, **kwargs):
        raise RuntimeError('synthetic storage failure')
    monkeypatch.setattr(s, 'freeze', unexpected)
    with pytest.raises(RuntimeError, match='synthetic storage failure'):
        s._auto_freeze(worker, 1, selection)
    assert snapshot(worker) == before


def test_stale_exception_after_owner_freeze_cannot_overwrite_marks(connections, monkeypatch):
    worker, web = connections
    candidates, _ = proposed(worker)
    after_owner = []
    def lose_race(*args, **kwargs):
        s.freeze(web, 1, [candidates[1]['code']], 'owner')
        after_owner.append(snapshot(web))
        raise RuntimeError('synthetic stale worker')
    monkeypatch.setattr(s, '_auto_freeze', lose_race)
    assert s.tick(worker) is False
    assert snapshot(worker) == after_owner[0]


def test_stale_comparison_failure_cannot_unfreeze_winner(connections, monkeypatch):
    worker, web = connections
    candidates, _ = proposed(worker)
    worker.execute("UPDATE batches SET status='researching' WHERE id=1")
    monkeypatch.setattr(s, 'research_state', lambda conn, q20: (True, q20))
    after_owner = []
    def lose_race(*args):
        web.execute("UPDATE batches SET status='proposed' WHERE id=1")
        s.freeze(web, 1, [candidates[1]['code']], 'owner')
        after_owner.append(snapshot(web))
        raise RuntimeError('synthetic comparison failure')
    monkeypatch.setattr(s, 'select', lose_race)
    assert s.tick(worker) is False
    assert snapshot(worker) == after_owner[0]


def test_comparison_failure_still_retries_then_fails(connections, monkeypatch):
    worker, _ = connections
    proposed(worker)
    worker.execute("UPDATE batches SET status='researching',selection=NULL WHERE id=1")
    monkeypatch.setattr(s, 'research_state', lambda conn, q20: (True, q20))
    monkeypatch.setattr(s, '_waiting', lambda *args: False)
    def fail(*args):
        raise RuntimeError('synthetic comparison failure')
    monkeypatch.setattr(s, 'select', fail)
    for count in (1, 2):
        assert s.tick(worker)
        b = s.load(worker.execute('SELECT * FROM batches').fetchone())
        assert b['status'] == 'researching' and b['selection']['failures'] == count
        assert b['marks']['attempted']
    assert s.tick(worker)
    b = s.load(worker.execute('SELECT * FROM batches').fetchone())
    assert b['status'] == 'failed' and '连续 3 次失败' in b['error']


def test_queued_batch_uses_saved_rule_through_screen(connections, monkeypatch):
    worker, _ = connections
    codes = [f'{i:06d}.SZ' for i in (1, 2, 3)]
    monkeypatch.setitem(s.RULE, 'weights', {'roe': 1.0, 'cash': 0.0, 'growth': 0.0, 'pe': 0.0})
    monkeypatch.setitem(s.RULE, 'momentum_days', 2)
    s.create(worker, 'synthetic-batch')
    saved = json.loads(snapshot(worker)['rule'])
    monkeypatch.setitem(s.RULE, 'min_total_mv_yi', 200)
    monkeypatch.setitem(s.RULE, 'q20_per_industry', 1)
    monkeypatch.setitem(s.RULE, 'picks', 1)
    monkeypatch.setitem(s.RULE, 'weights', {'roe': 0.0, 'cash': 0.0, 'growth': 0.0, 'pe': 1.0})
    monkeypatch.setitem(s.RULE, 'windfall_flag', {'min_growth': .9, 'max_pe': 10})
    monkeypatch.setitem(s.RULE, 'momentum_days', 100)

    class Replay:
        def query(self, api, **params):
            if api == 'stock_basic':
                return pd.DataFrame([{'ts_code': c, 'name': c, 'industry': 'industrial', 'market': '主板',
                                      'list_date': '20100101'} for c in codes])
            if api == 'daily_basic':
                return pd.DataFrame([{'ts_code': c, 'close': 10, 'pe_ttm': 7 + i,
                                      'total_mv': 150 * 10000} for i, c in enumerate(codes)])
            if api == 'suspend_d':
                return pd.DataFrame(columns=['ts_code'])
            if api == 'index_member_all':
                return pd.DataFrame([{'ts_code': c, 'l1_name': 'industrial'} for c in codes])
            if api == 'daily':
                return pd.DataFrame([{'ts_code': c, 'amount': 200000} for c in codes])
            if api == 'fina_indicator':
                return pd.DataFrame([{'synthetic': 1}])
            if api in ('income', 'cashflow'):
                return pd.DataFrame()
            raise AssertionError(api)

    monkeypatch.setattr(s, '_pro', lambda: Replay())
    monkeypatch.setattr(s, 'latest_session', lambda pro: '20261103')
    monkeypatch.setattr(s, '_trade_days', lambda *a: ['20261102', '20261103'])
    monkeypatch.setattr(s, 'indicator_check', lambda *a: {'status': 'ok'})
    monkeypatch.setattr(s, 'evaluate', lambda base, *a: base | {'status': 'ok', 'roe': float(codes.index(base['code'])),
                                                                 'cash': 1., 'growth': .5})
    seen = []
    def momentum(pro, as_of, selected, rule=None):
        seen.append(copy.deepcopy(rule if rule is not None else s.RULE))
        return {c: {'return_60d': 0.0, 'drawdown': False} for c in selected}
    monkeypatch.setattr(s, 'momentum', momentum)
    monkeypatch.setattr(s, 'link_research', lambda conn, batch_id, q20: q20)
    assert s.tick(worker)
    batch = s.load(worker.execute('SELECT * FROM batches').fetchone())
    assert batch['status'] == 'researching' and batch['rule'] == saved == seen[0]
    assert [r['code'] for r in batch['q20']] == list(reversed(codes))
    assert batch['q5'] == list(reversed(codes))[:2]
    assert all(r['windfall'] == saved['windfall_flag'] for r in batch['q20'])


@pytest.mark.parametrize('change', [
    {'version': 'screen-v99'}, {'industry': 'other'}, {'windfall_flag': None},
])
def test_unsupported_or_incomplete_rules_fail_before_market_calls(monkeypatch, change):
    rule = copy.deepcopy(s.RULE)
    for key, value in change.items():
        if value is None:
            rule.pop(key)
        else:
            rule[key] = value
    monkeypatch.setattr(s, '_pro', lambda: pytest.fail('market must not be called'))
    with pytest.raises(s.ScreenError):
        s.screen(rule=rule)


def test_legacy_rule_without_warning_and_momentum_settings(monkeypatch):
    legacy = copy.deepcopy(s.RULE)
    legacy['version'] = 'screen-v1'
    for key in ('windfall_flag', 'momentum_days', 'drawdown_flag'):
        legacy.pop(key)
    assert s.windfall({'growth': .5, 'pe': 9}, legacy) is None
    dates = [f'{i:08d}' for i in range(61)]
    monkeypatch.setattr(s, '_trade_days', lambda *args: dates)
    class Replay:
        def query(self, api, **params):
            return pd.DataFrame([{'ts_code': '000001.SZ', 'close': 10., 'adj_factor': 1.}])
    result = s.momentum(Replay(), '20261103', ['000001.SZ'], legacy)
    assert result['000001.SZ']['return_60d'] == 0 and result['000001.SZ']['drawdown'] is False
