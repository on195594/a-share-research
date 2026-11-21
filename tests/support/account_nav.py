"""Structural account NAV observations, requests and ledger postings."""

from a_share_research import gate
from a_share_research.account import evidence, ledger, nav
from a_share_research.account.reconciliation import Event, Snapshot


def observe(book, stamp, value):
    receipt = ledger.observe(book['conn'], book['principal'], 'a', ledger.Observation(
        observed_at='2026-11-11T16:00:00+08:00', portfolio_value_as_of=stamp, total_assets=value),
        book['directory'], book['sha'])
    return {'kind': 'observation', 'id': receipt['observation_id'], 'reason': 'Explicit structural valuation selection'}


def capture(book):
    return nav.capture(book['conn'], book['principal'], 'a')


def request(book, **changes):
    return nav.Request.model_validate(dict(expected_state_sha256=capture(book)['state_sha256'],
        start=book['start'], end=book['end'], flows=[], coverage=dict(
            start='2026-01-05T15:00:00+08:00', end='2026-01-08T15:00:00+08:00',
            currency='CNY', scope='cash_equity_no_liabilities', complete_external_flows=True,
            no_security_transfers=True, evidence_hashes=[book['sha']], explanation='Structural complete flow declaration'),
        benchmark_evidence_hash=evidence.store(book['directory'], gate.canonical(book['index']).encode())) | changes)


def evaluate(book, value=None):
    return nav.evaluate(book['conn'], book['principal'], 'a', value or request(book), book['directory'])


def post(book, *, cash='100', kind='deposit', voids=(), balance='1100', code=None):
    event = Event(kind=kind, cash=cash, code=code, occurred_at='2026-01-06T10:00:00+08:00', broker_id='flow')
    version = ledger.state(book['conn'], book['principal'], 'a')['version']
    receipt = ledger.submit(book['conn'], book['principal'], 'a', ledger.Batch(expected_version=version,
        events=[event], void_event_ids=list(voids), evidence_hashes=[book['sha']],
        snapshot=Snapshot(positions={}, cash=balance, market_values={}, total_assets=balance)), book['directory'])
    assert receipt['status'] == 'posted'
    return book['conn'].execute('SELECT max(id) FROM account_journal').fetchone()[0]
