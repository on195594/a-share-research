"""State-transfer contracts over private structural accounts; never send notifications."""
import fcntl
import hashlib
import json
import os
import subprocess
import sys

import pytest

from a_share_research import cutover, gate
from a_share_research.account import retirement
from test_cutover import setup as base_setup, prepare, fence, commit
from test_ops_hermes import load


@pytest.fixture
def setup(tmp_path):
    for value in base_setup.__wrapped__(tmp_path):
        old_state = value['state'] / retirement.STATE
        # Explicit structural notification history, not real market/notification evidence.
        old_state.write_text('{"date":"2026-11-11", "levels":{"600001":"stop1"}}\n')
        plan = prepare(value)
        yield value, plan, tmp_path / 'new-stop/state.json'


def transfer(setup):
    value, plan, target = setup
    return cutover.transfer_stop_state(value['book'][1], value['owner'], plan, target)


def bind(setup):
    value, plan, _ = setup
    fence(value, plan)
    commit(value, plan)


def test_copy_exact_legacy_bytes_and_repeated_import_is_read_only(setup, monkeypatch):
    value, plan, target = setup
    original = (value['state'] / retirement.STATE).read_bytes()
    bind(setup)
    before = list(value['book'][1].iterdump())
    result = transfer(setup)
    assert result['status'] == 'imported' and result['source_absent'] is False
    assert result['source_sha256'] == result['state_sha256'] == hashlib.sha256(original).hexdigest()
    assert target.read_bytes() == original
    stamp = target.stat().st_mtime_ns
    assert transfer(setup) == result | {'status': 'already_imported'}
    assert target.stat().st_mtime_ns == stamp
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.parent.stat().st_mode & 0o777 == 0o700
    assert list(value['book'][1].iterdump()) == before
    client = load('a-share-stop-alert')
    monkeypatch.setattr(client, 'STATE', target)
    assert client.load_state() == dict(schema_version=1, state=json.loads(original), pending=None)
    assert target.read_bytes() == original  # Merely loading does not advance notification state.


def test_known_absence_is_explicit_and_does_not_invent_date_or_levels(setup):
    value, _, target = setup
    (value['state'] / retirement.STATE).unlink()
    plan = prepare(value)
    assert plan.state_sha256 is None
    setup = value, plan, target
    bind(setup)
    result = transfer(setup)
    assert result['source_absent'] is True and result['source_sha256'] is None
    assert json.loads(target.read_bytes()) == {}


@pytest.mark.parametrize('state', ['unfenced', 'unbound', 'other-account', 'revoked'])
def test_transfer_requires_exact_authorized_bound_and_fenced_account(setup, state):
    value, plan, target = setup
    if state != 'unfenced':
        fence(value, plan)
    if state in ('other-account', 'revoked'):
        commit(value, plan)
        if state == 'other-account':
            value['book'][1].execute("UPDATE account_source SET source_sha256=?", ('f'*64,))
        else:
            value['book'][1].execute('DELETE FROM api_keys WHERE id=?', (value['owner'].key_id,))
    with pytest.raises(ValueError):
        transfer(setup)
    assert not target.exists() and not target.parent.exists()


@pytest.mark.parametrize('content', [
    '{"date":"2026-02-30"}', '{"date":null}', '{"levels":{"600001":"invalid"}}',
    '{"levels":{},"levels":{}}', 'not-json', '[]',
])
def test_invalid_original_state_is_not_silently_reset(setup, content):
    value, _, target = setup
    (value['state'] / retirement.STATE).write_text(content)
    setup = value, prepare(value), target
    bind(setup)
    with pytest.raises(cutover.CutoverError, match='格式无效'):
        transfer(setup)
    assert not target.exists()


@pytest.mark.parametrize('variant', ['advanced', 'pending', 'corrupt'])
def test_existing_client_history_is_never_replaced(setup, variant):
    value, _, target = setup
    bind(setup)
    transfer(setup)
    if variant == 'advanced':
        content = '{"date":"2026-11-11","levels":{"600001":"stop2"}}'
    elif variant == 'pending':
        state = json.loads(target.read_bytes())
        content = gate.canonical(dict(schema_version=1, state=state, pending=dict(
            request_id='ambiguous-request', expected_activation_id=value['activation']['id'], previous_state=state)))
    else:
        content = '{broken'
    target.write_text(content)
    with pytest.raises(cutover.CutoverError, match='保持现有历史'):
        transfer(setup)
    assert target.read_text() == content


def test_held_client_lock_prevents_import(setup):
    _, _, target = setup
    bind(setup)
    target.parent.mkdir(mode=0o700)
    lock_path = target.with_suffix('.lock')
    lock_path.touch(mode=0o600)
    with lock_path.open('r+') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with pytest.raises(cutover.CutoverError, match='仍在运行'):
            transfer(setup)
    assert not target.exists()


def test_atomic_publish_does_not_overwrite_uncooperative_writer(setup, monkeypatch):
    _, _, target = setup
    bind(setup)
    real_link = os.link
    def competing_writer(source, destination, **kwargs):
        target.write_text('another writer won')
        return real_link(source, destination, **kwargs)
    monkeypatch.setattr(os, 'link', competing_writer)
    with pytest.raises(FileExistsError):
        transfer(setup)
    assert target.read_text() == 'another writer won'
    assert sorted(path.name for path in target.parent.iterdir()) == ['state.json', 'state.lock']


@pytest.mark.parametrize('kind', ['symlink', 'hardlink-lock', 'shared-directory', 'old-directory', 'database'])
def test_unsafe_destination_does_not_change_other_files(setup, tmp_path, kind):
    value, plan, target = setup
    bind(setup)
    if kind == 'old-directory':
        target = value['state'] / 'new-state.json'
    elif kind == 'database':
        target = value['book'][0]
    else:
        target.parent.mkdir(mode=0o700)
        if kind == 'symlink':
            target.symlink_to(value['book'][0])
        elif kind == 'hardlink-lock':
            os.link(value['book'][0], target.with_suffix('.lock'))
        else:
            target.parent.chmod(0o755)
    before = list(value['book'][1].iterdump())
    with pytest.raises(ValueError):
        cutover.transfer_stop_state(value['book'][1], value['owner'], plan, target)
    assert list(value['book'][1].iterdump()) == before


def test_real_cli_transfer_after_binding(setup, tmp_path):
    value, plan, target = setup
    bind(setup)
    saved = tmp_path / 'plan.json'
    cutover._save(plan, saved)
    result = subprocess.run([sys.executable, '-m', 'a_share_research.cutover', '--database', str(value['book'][0]),
        '--plan', str(saved), 'transfer-stop-state', '--destination', str(target)],
        env=os.environ | {'ASHARE_OWNER_TOKEN': value['token']}, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['status'] == 'imported'
    assert value['token'] not in result.stdout + result.stderr


def test_cli_refuses_database_as_plan_before_opening_another_descriptor(setup):
    value, _, target = setup
    path = value['book'][0]
    result = subprocess.run([sys.executable, '-m', 'a_share_research.cutover', '--database', str(path),
        '--plan', str(path), 'transfer-stop-state', '--destination', str(target)],
        env=os.environ | {'ASHARE_OWNER_TOKEN': value['token']}, capture_output=True, text=True, timeout=30)
    assert result.returncode == 1 and '数据库不能作为切换计划文件' in result.stderr
    assert not target.exists()
