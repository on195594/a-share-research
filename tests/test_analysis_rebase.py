"""Compatibility checks for historical mirror reconciliation receipts."""
import json

import pytest

from a_share_research import gate
from a_share_research.account import analysis, ledger
from test_account_analysis import branch as _branch_fixture

branch = _branch_fixture


def historical_rebase(branch):
    conn, principal, _, sha = branch
    bases = [analysis.initialize(conn, principal, hid) for hid in (1, 2)]
    choices = [dict(holding_id=base['holding_id'], expected_version=0,
                    legacy_hash=gate.digest(base['snapshot']), select='legacy') for base in bases]
    request = dict(choices=choices, evidence_hashes=[sha], reason='Structural mirror reconciliation')
    revisions = []
    for base in bases:
        holding = analysis.read_holding(conn, base['holding_id'])
        envelope = dict(rebase=request, rebase_hash=gate.digest(request), legacy_snapshot=base['snapshot'])
        revisions.append(analysis.insert_revision(conn, holding, 1, base['revision_id'], 'legacy_rebase',
                         principal, envelope, [sha], base['snapshot']))
    return revisions


def test_integrity_detects_tampered_rebase_even_if_envelope_hash_is_recomputed(branch):
    conn, *_ = branch
    first, _ = historical_rebase(branch)
    assert analysis.inspect(conn) == []
    assert analysis.baseline(conn, 1) == first['snapshot']
    row = conn.execute('SELECT request FROM account_analysis_history WHERE id=?', (first['revision_id'],)).fetchone()
    envelope = json.loads(row[0])
    envelope['legacy_snapshot']['holding_l3_conditions'][0]['evidence'] = 'Structural corruption'
    conn.execute('DROP TRIGGER account_analysis_no_update')
    conn.execute('UPDATE account_analysis_history SET request=?,request_hash=? WHERE id=?',
                 (gate.canonical(envelope), gate.digest(envelope), first['revision_id']))
    assert any('基线协调' in problem for problem in analysis.inspect(conn))
    with pytest.raises(ledger.LedgerError, match='基线记录损坏'):
        analysis.baseline(conn, 1)


def test_incomplete_multi_holding_receipt_is_detected(branch):
    conn, *_ = branch
    _, second = historical_rebase(branch)
    conn.execute('DROP TRIGGER account_analysis_no_delete')
    conn.execute('DELETE FROM account_analysis_history WHERE id=?', (second['revision_id'],))
    assert any('缺少持仓周期回执' in item for item in analysis.inspect(conn))
