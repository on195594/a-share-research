"""Explicit, evidence-backed reconciliation of a shadow branch with a refreshed mirror."""
import json
from pathlib import Path

from a_share_research import db, gate
from a_share_research.account import analysis, evidence, ledger
from a_share_research.account.analysis import Rebase, baseline


def preview(conn, principal, holding_id):
    with db.transaction(conn, immediate=False):
        ledger.authorize(conn, principal)
        holding = analysis.read_holding(conn, holding_id)
        latest = analysis.current(conn, principal, holding_id)
        if holding['company_key'] != latest['company_key']:
            raise ledger.LedgerError('持仓周期公司身份变化，不能协调为同一周期')
        original, legacy = baseline(conn, holding_id), analysis.capture(conn, holding_id)
        shadow = latest['snapshot']
        return dict(holding_id=holding_id, version=latest['version'], baseline=original, legacy=legacy, shadow=shadow,
                    legacy_hash=gate.digest(legacy), legacy_changed=legacy != original, shadow_changed=shadow != original,
                    mode='shadow', selections=['legacy','shadow'])


def _check_ids(conn, replacements):
    """Reject any future projection collision, including legacy closed cycles and other shadow branches."""
    seen = {table:{} for table in analysis.TABLES}
    for holding in conn.execute('SELECT id,exit_date FROM holdings ORDER BY id'):
        hid = holding['id']
        if hid in replacements:
            snapshot = replacements[hid]
        else:
            latest = analysis.latest_revision(conn, hid) if holding['exit_date'] is None else None
            snapshot = json.loads(latest['snapshot']) if latest else analysis.capture(conn, hid)
        for table in analysis.TABLES:
            for row in snapshot[table]:
                identity = row.get('id', row['holding_id'])
                if identity in seen[table]:
                    raise ledger.LedgerError(f'{table} 编号 {identity} 在持仓周期 {seen[table][identity]} 与 {hid} 冲突；须同批协调')
                seen[table][identity] = hid


def rebase(conn, principal, request: Rebase, evidence_directory: Path):
    request = Rebase.model_validate(request)
    data = request.model_dump(mode='json')
    client_hash = gate.digest(data)
    with db.transaction(conn):
        ledger.authorize(conn, principal)
        previous = conn.execute("SELECT * FROM account_analysis_history WHERE source='legacy_rebase' AND account_id=''"
                                " AND json_extract(request,'$.rebase_hash')=? ORDER BY holding_id", (client_hash,)).fetchall()
        if previous:
            if {r['holding_id'] for r in previous} != {c.holding_id for c in request.choices}:
                raise ledger.LedgerError('协调回执不完整，须检查完整性')
            return {'rebase_hash':client_hash, 'revisions':[analysis.revision_result(r) for r in previous]}
        for sha in request.evidence_hashes:
            evidence.verify(evidence_directory, sha)
        prepared, replacements = [], {}
        for choice in request.choices:
            holding = analysis.read_holding(conn, choice.holding_id)
            if holding['exit_date'] is not None:
                raise ledger.LedgerError('已关闭周期不参与活动影子协调')
            latest = analysis.latest_revision(conn, choice.holding_id)
            if latest is None:
                raise ledger.LedgerError('尚未建立分析基线', 404)
            if holding['company_key'] != latest['company_key']:
                raise ledger.LedgerError('持仓周期公司身份变化，不能协调为同一周期')
            if choice.expected_version != latest['version']:
                raise ledger.LedgerError(f"持仓周期 {choice.holding_id} 版本冲突，当前 {latest['version']}")
            legacy = analysis.capture(conn, choice.holding_id)
            if gate.digest(legacy) != choice.legacy_hash:
                raise ledger.LedgerError(f'持仓周期 {choice.holding_id} 镜像已变化，请重新查看差异')
            selected = legacy if choice.select == 'legacy' else json.loads(latest['snapshot'])
            replacements[choice.holding_id] = selected
            prepared.append((holding, latest, legacy, selected))
        _check_ids(conn, replacements)
        results = []
        for holding, latest, legacy, selected in prepared:
            envelope = dict(rebase=data, rebase_hash=client_hash, legacy_snapshot=legacy)
            results.append(analysis.insert_revision(conn, holding, latest['version']+1, latest['id'], 'legacy_rebase', principal,
                                           envelope, request.evidence_hashes, selected))
        return {'rebase_hash':client_hash, 'revisions':sorted(results, key=lambda r:r['holding_id'])}
