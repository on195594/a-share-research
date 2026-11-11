"""Structural cutover contracts; no fixture is real broker acceptance evidence."""
import os
import sqlite3
import subprocess
import sys

import pytest

from a_share_research import api_keys, cutover, db
from a_share_research.account import ledger, mirror, retirement
from a_share_research.policy import account_input, registry
from a_share_research.policy.v0 import risk_budget
from test_account import SOURCE_SCHEMA
from test_policy_cutover import book as opening_book, update_analysis
from test_position_store import batch, event


@pytest.fixture
def setup(tmp_path):
    for book in opening_book.__wrapped__(tmp_path):
        path, conn, _, _, _ = book
        old = tmp_path / 'legacy'
        old.mkdir(mode=0o700)
        source = old / 'cache.db'
        content = mirror._read(conn, path.parent)
        with sqlite3.connect(source) as legacy:
            legacy.executescript(SOURCE_SCHEMA)
            for table, columns in mirror.MANIFEST.items():
                legacy.executemany(f'INSERT INTO {table} ({",".join(columns)}) '
                                   f'VALUES ({",".join("?" for _ in columns)})', content['tables'][table])
            legacy.execute('DELETE FROM sqlite_sequence')
            legacy.executemany('INSERT INTO sqlite_sequence VALUES (?,?)', content['sequences'].items())
        source.chmod(0o600)
        (old / 'stop-alert-state.json').write_text('{"structural":true}')
        (old / 'stop-alert-state.json').chmod(0o600)
        token = api_keys.create(conn, 'cutover fixture', role='owner')
        owner = api_keys.authenticate(conn, token)
        parameters = risk_budget.policy() | dict(account_scope='structural-scope', currency='CNY',
            confirmation_ref='structural only', effective_from='2026-01-01T00:00:00+08:00',
            confirmed_at='2026-01-01T00:00:00+08:00')
        manifest = registry.register(conn, owner, parameters)
        activation = registry.activate(conn, owner, manifest['identity'], expected_id=0,
                                       request_id='initial', reason='structural fixture')
        yield dict(book=book, source=source, state=old, owner=owner, token=token, activation=activation)
        # Test-owned temporary directory only: allow pytest cleanup, never operational rollback.
        old.chmod(0o700)
        for file in old.iterdir():
            file.chmod(0o600)


def prepare(setup):
    return cutover.prepare(setup['book'][1], setup['owner'], setup['source'], setup['state'],
                           'a', 'structural-scope')


def fence(setup, plan):
    return cutover.fence(setup['book'][1], setup['owner'], plan)


def commit(setup, plan):
    return cutover.commit(setup['book'][1], setup['owner'], plan)


def test_prepare_is_read_only_and_private_plan_round_trips(setup, tmp_path):
    before = list(setup['book'][1].iterdump())
    old = setup['source'].read_bytes()
    plan = prepare(setup)
    assert list(setup['book'][1].iterdump()) == before
    assert setup['source'].read_bytes() == old
    assert cutover.Plan.model_validate_json(plan.model_dump_json()) == plan
    out = tmp_path / 'plan.json'
    cutover._save(plan, out)
    assert out.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        cutover._save(plan, out)
    with pytest.raises(cutover.CutoverError, match='封锁'):
        cutover._save(plan, setup['state'] / 'plan.json')


def test_bind_once_then_retry_preserves_later_events_and_policy(setup):
    plan = prepare(setup)
    before = list(setup['book'][1].iterdump())
    assert fence(setup, plan)['fenced'] is True
    assert fence(setup, plan)['fenced'] is True
    assert list(setup['book'][1].iterdump()) == before
    result = commit(setup, plan)
    assert result['status'] == 'bound'
    assert result['binding']['mirror_watermark'] == account_input._json(plan.mirror_row[3])
    path, conn, principal, directory, sha = setup['book']
    assert account_input.read(conn, path.parent, cutover._row(conn))['state_sha256'] == plan.opening_sha256
    journal = ledger.submit(conn, principal, 'a', batch((conn, principal, directory, sha), [event()]), directory)
    assert journal['status'] == 'posted'
    current = setup['activation']
    registry.activate(conn, setup['owner'], current['identity'], expected_id=current['id'],
                      request_id='later', reason='later decision must survive retry')
    before_retry = list(conn.iterdump())
    assert commit(setup, plan) == result | {'status': 'already_bound'}
    assert list(conn.iterdump()) == before_retry
    with pytest.raises(mirror.MirrorError, match='切换'):
        mirror.mirror(conn, setup['source'])


def test_binding_requires_persistent_fence(setup):
    plan = prepare(setup)
    with pytest.raises(retirement.RetirementError):
        commit(setup, plan)
    assert cutover._row(setup['book'][1]) == plan.mirror_row


@pytest.mark.parametrize('change', ['mirror', 'analysis', 'policy', 'state', 'source', 'owner'])
def test_changes_before_fence_do_not_touch_old_source(setup, change):
    plan = prepare(setup)
    conn = setup['book'][1]
    if change == 'mirror':
        conn.execute("UPDATE account_source SET updated_at='new mirror'")
    elif change == 'analysis':
        update_analysis(setup['book'], scoped=True)
    elif change == 'policy':
        current = setup['activation']
        registry.activate(conn, setup['owner'], current['identity'], expected_id=current['id'],
                          request_id='later', reason='changed before fence')
    elif change == 'state':
        (setup['state'] / 'stop-alert-state.json').write_text('{}')
    elif change == 'source':
        with sqlite3.connect(setup['source']) as legacy:
            legacy.execute("UPDATE holdings SET name='changed'")
    else:
        conn.execute('DELETE FROM api_keys WHERE id=?', (setup['owner'].key_id,))
    old = setup['source'].read_bytes()
    with pytest.raises(ValueError):
        fence(setup, plan)
    assert setup['source'].read_bytes() == old
    assert setup['state'].stat().st_mode & 0o777 == 0o700


def test_failed_commit_keeps_old_fenced_and_can_reprepare(setup):
    plan = prepare(setup)
    fence(setup, plan)
    update_analysis(setup['book'], scoped=True)
    with pytest.raises(cutover.CutoverError, match='状态已变化'):
        commit(setup, plan)
    assert cutover._row(setup['book'][1])[0] == 'mirror'
    assert retirement.verify(setup['source'], setup['state'], plan.mirror_row[1])['fenced']
    fresh = prepare(setup)
    assert commit(setup, fresh)['status'] == 'bound'


def test_different_binding_and_replaced_database_are_refused(setup):
    plan = prepare(setup)
    fence(setup, plan)
    commit(setup, plan)
    different = plan.model_copy(update={'account_id': 'another'})
    with pytest.raises(cutover.CutoverError, match='不同账户绑定'):
        commit(setup, different)
    wrong_file = plan.model_copy(update={'source_file': cutover.FileIdentity(device=0, inode=0)})
    with pytest.raises(cutover.CutoverError, match='文件身份'):
        commit(setup, wrong_file)


def test_expired_policy_cannot_cutover(setup, monkeypatch):
    current = setup['activation']
    conn = setup['book'][1]
    manifest = registry._version(conn, current['identity'])
    parameters = manifest['parameters'] | {'effective_to': '2027-01-01T00:00:00+08:00'}
    manifest = registry.register(conn, setup['owner'], parameters)
    registry.activate(conn, setup['owner'], manifest['identity'], expected_id=current['id'],
                      request_id='expiring', reason='structural expiry')
    monkeypatch.setattr(db, 'now', lambda: '2027-01-02T00:00:00+08:00')
    with pytest.raises(cutover.CutoverError, match='到期'):
        prepare(setup)


def test_target_inside_sealed_directory_rejected(setup):
    with pytest.raises(cutover.CutoverError, match='新库不能'):
        cutover.prepare(setup['book'][1], setup['owner'], setup['source'], setup['book'][0].parent,
                        'a', 'structural-scope')


def test_cli_real_process_prepare_fence_commit_and_retry(setup, tmp_path):
    command = [sys.executable, '-m', 'a_share_research.cutover', '--database', str(setup['book'][0]),
               '--plan', str(tmp_path / 'prepared.json')]
    environment = os.environ | {'ASHARE_OWNER_TOKEN': setup['token']}
    args = ['prepare', '--source', str(setup['source']), '--state-directory', str(setup['state']),
            '--account-id', 'a', '--account-scope', 'structural-scope']
    for suffix, status in ((args, 'prepared'), (['fence'], None), (['commit'], 'bound'), (['commit'], 'already_bound')):
        result = subprocess.run(command + suffix, env=environment, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert setup['token'] not in result.stdout + result.stderr
        if status:
            assert f'"status":"{status}"' in result.stdout
    assert cutover._row(setup['book'][1])[0] == 'ledger'


def test_identity_check_does_not_release_live_sqlite_locks(tmp_path):
    path = tmp_path / 'locks.db'
    conn = sqlite3.connect(path, isolation_level=None)
    try:
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('CREATE TABLE example (value)')
        conn.execute("INSERT INTO example VALUES ('before')")
        cutover._file(path)
        subprocess.run([sys.executable, '-c',
            "import sqlite3,sys; c=sqlite3.connect(sys.argv[1]); "
            "c.execute(\"UPDATE example SET value='after'\"); c.commit(); c.close()", str(path)],
            check=True, timeout=10)
        assert path.with_name(path.name + '-wal').exists()  # Parent connection still owns its shared lock.
        assert conn.execute('SELECT value FROM example').fetchone()[0] == 'after'
    finally:
        conn.close()
