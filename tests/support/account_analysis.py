"""Structural analysis requests, history and cycle transitions."""

from a_share_research.account import analysis, cycle_analysis


def condition(**overrides):
    return dict(condition='Structural replacement condition', scope='core_driver', action='review',
                materiality_basis='Structural materiality', temporary_exit_rule='Structural exit rule',
                evidence_cadence='quarterly') | overrides


def rewrite(**overrides):
    return dict(kind='thesis', l1='Structural replacement L1', l2='Structural replacement L2',
                rewrite_reason='Structural rewrite', retire_l3_ids=[1], new_l3=[condition(), condition(scope='non_core')]) | overrides


def open_alert(reason_code='structural-new', **overrides):
    return dict(kind='alert_open', level='red', category='unverified', reason_code=reason_code,
                reason='Structural alert reason', evidence='Structural alert evidence', review_due='2026-11-12') | overrides


def insert(conn, identity, holding, version=0, parent=None, **scope):
    values=dict(id=identity,holding_id=holding,company_key='600001',version=version,parent_id=parent,
        request_hash=f'request-{identity}',source='structural',actor_key_id=None,
        created_at='2026-11-11T10:00:00+08:00',snapshot_hash=f'snapshot-{identity}',
        evidence_hashes='["原始证据"]',request='{ "preserve whitespace": true }',snapshot='{"state": "原值"}')
    values.update(scope)
    conn.execute(f"INSERT INTO account_analysis_history ({','.join(values)}) VALUES ({','.join('?' for _ in values)})",tuple(values.values()))


def legacy_rows(conn):
    return {t: [tuple(r) for r in conn.execute(f'SELECT * FROM {t} ORDER BY 1')]
            for t in (*analysis.TABLES,'holdings','holding_events')}


def request(branch, changes=None, version=0, **extra):
    return analysis.Update.model_validate(dict(expected_version=version, evidence_hashes=[branch[3]],
        reason='Structural transition', changes=changes or [dict(kind='l3',id=1,status='watch',
            as_of='2026-11-11',evidence='Structural evidence')]) | extra)


def start_cycle(branch, holding_id=1):
    conn, principal, directory, sha = branch
    legacy = analysis.initialize(conn, principal, holding_id)
    return cycle_analysis.initialize(conn, principal, 'a', f'legacy:{holding_id}', dict(
        expected_account_version=0, evidence_hashes=[sha], legacy_revision_id=legacy['revision_id']), directory)


def append(branch, value, holding_id=1):
    return cycle_analysis.update(branch[0], branch[1], 'a', f'legacy:{holding_id}', value, branch[2])


def historical_revision(branch, holding_id, value):
    """Seed a pre-cutover revision alongside a journal branch for historical reader tests."""
    conn, principal, *_ = branch
    holding = analysis.read_holding(conn, holding_id)
    latest = analysis.current(conn, principal, holding_id)
    snapshot = latest['snapshot']
    analysis.apply_changes(conn, holding, snapshot, value.changes)
    return analysis.insert_revision(conn, holding, latest['version'] + 1, latest['revision_id'],
        'assistant', principal, value.model_dump(mode='json'), value.evidence_hashes, snapshot)
