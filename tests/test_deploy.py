"""Release guards on captured runs in an isolated database; never stops services."""

import json
import logging
import os
from pathlib import Path
import sqlite3
from contextlib import closing

import pytest

from a_share_research import db, deploy, gate
from a_share_research.policy import identity
from support.continuation import load


@pytest.mark.parametrize('tables', [(), ('policy_versions',), ('policy_activations',),
                                  ('policy_versions', 'policy_activations')])
def test_policy_installed_status_without_active_policy_is_read_only(tmp_path, tables):
    database = tmp_path / 'policy.db'
    with closing(sqlite3.connect(database)) as conn:
        for table in tables:
            conn.execute(f'CREATE TABLE {table} (id INTEGER PRIMARY KEY, identity TEXT, manifest TEXT)')
    before = database.read_bytes()
    assert deploy.policy_installed_status(database) is None
    assert database.read_bytes() == before


@pytest.fixture
def policy_database(tmp_path):
    database = tmp_path / 'app.db'
    with closing(db.connect(database)):
        pass
    return database


def _activate_manifest(database, manifest):
    with closing(sqlite3.connect(database)) as conn:
        conn.execute('INSERT OR IGNORE INTO policy_versions VALUES (?,?,?,?,?)',
                     (manifest['identity'], 'v0', db.now(), 1, json.dumps(manifest)))
        conn.execute('INSERT INTO policy_activations(policy,activated_at,activated_by,identity) VALUES (?,?,?,?)',
                     ('v0', db.now(), 'structural-test', manifest['identity']))
        conn.commit()


def _stale_manifest(manifest):
    # Explicit stale-source variant of the real installation, with consistent archived digests.
    stale = json.loads(json.dumps(manifest))
    stale['files']['code']['a_share_research/deploy.py'] = '0' * 64
    stale['code_hash'] = gate.digest(stale['files']['code'])
    del stale['identity']
    stale['identity'] = 'policy:' + gate.digest(stale)
    identity.verify(stale)
    return stale


def test_policy_installed_status_checks_latest_activation_without_writes(policy_database):
    manifest = identity.build({'source': 'explicit-identity-test'})
    stale = _stale_manifest(manifest)
    with pytest.raises(identity.IdentityError, match='当前安装'):
        identity.check_installed(stale)
    for selected, expected in ((stale, False), (manifest, True), (stale, False)):
        _activate_manifest(policy_database, selected)
        before = deploy.fingerprints(policy_database)
        assert deploy.policy_installed_status(policy_database) is expected
        assert deploy.fingerprints(policy_database) == before


@pytest.mark.parametrize('installed', [None, True, False])
def test_deploy_records_policy_status_and_warns_only_when_invalid(
        policy_database, tmp_path, monkeypatch, caplog, installed):
    if installed is not None:
        manifest = identity.build({'source': 'explicit-identity-test'})
        _activate_manifest(policy_database, manifest if installed else _stale_manifest(manifest))
    original = deploy.fingerprints(policy_database)
    events = []

    def run(*args):
        if args[:2] == ('git', 'rev-parse'):
            return 'release'
        if args[:2] == ('docker', 'ps'):
            return 'tools-container'
        if 'config' in args:
            return f'{args[-1]} config-hash'
        return ''

    def tools_control(action):
        events.append(action)
        return {'paused': action == 'pause', 'in_flight': 0}

    def verify(*args, **kwargs):
        events.append('verified')
        return {}

    def ready():
        events.append('ready')
        return {}

    check = deploy.policy_installed_status

    def check_after_ready(database):
        assert events[-3:] == ['verified', 'resume', 'ready']
        return check(database)

    # Isolate Docker/backup side effects; exercise real policy reads and release manifest writes.
    monkeypatch.setattr(deploy, 'run', run)
    monkeypatch.setattr(deploy, '_tools_config', lambda: None)
    monkeypatch.setattr(deploy, 'verify_images', lambda code: {'web': 'new-web', 'worker': 'new-worker'})
    monkeypatch.setattr(deploy, 'describe', lambda service: {
        'running': True, 'image': 'old', 'config_hash': 'config-hash',
        'mounts': {'/data': [str(tmp_path), True]}})
    monkeypatch.setattr(deploy, 'tools_control', tools_control)
    monkeypatch.setattr(deploy, '_verify_release', verify)
    monkeypatch.setattr(deploy, 'wait_tools_ready', ready)
    monkeypatch.setattr(deploy, 'create_release_backup', lambda *args: {})
    monkeypatch.setattr(deploy, 'prune_backups', lambda *args: [])
    monkeypatch.setattr(deploy, 'prune_images', lambda *args: {})
    monkeypatch.setattr(deploy, 'policy_installed_status', check_after_ready)
    evidence = tmp_path / 'release'
    with caplog.at_level(logging.WARNING, logger=deploy.__name__):
        deploy.deploy('release', evidence)
    assert json.loads((evidence / 'manifest.json').read_text())['policy_installed'] is installed
    assert deploy.fingerprints(policy_database) == original
    assert ('installed=false' in caplog.text) is (installed is False)
    if installed is False:
        assert 'README' in caplog.text and '保持参数不变' in caplog.text and '重新登记并激活' in caplog.text


def test_idle_guard_and_fingerprints_on_captured_records(tmp_path):
    database = tmp_path / "app.db"
    conn = db.connect(database)
    try:
        load(conn)
        assert deploy._source_version(database) == len(db.MIGRATIONS)
        original = deploy.fingerprints(database)
        deploy.idle(database)
        assert deploy.fingerprints(database) == original
        for status in ("queued", "running"):
            conn.execute("UPDATE runs SET status=? WHERE id=14", (status,))
            with pytest.raises(RuntimeError, match="尚未空闲"):
                deploy.idle(database)
        conn.execute("UPDATE runs SET status='done' WHERE id=14")
        deploy.idle(database)
        conn.execute("UPDATE runs SET status='running' WHERE id=14")
        changed = deploy.fingerprints(database)
        assert changed["runs"] != original["runs"]
        assert all(changed[table] == original[table] for table in original if table != "runs")
    finally:
        conn.close()


def test_prune_backups_keeps_the_newest_three_with_their_sidecars(tmp_path):
    names = [f"backup-pre-{code}-20261031-0{i}0000.db" for i, code in enumerate(("aaa", "bbb", "ccc", "ddd", "eee"))]
    for i, name in enumerate(names):
        for suffix in ("", "-shm", "-wal"):
            path = tmp_path / (name + suffix)
            path.write_bytes(b"")
            os.utime(path, (1_000 + i, 1_000 + i))
    (tmp_path / "app.db").write_bytes(b"")
    (tmp_path / "app.db-wal").write_bytes(b"")
    assert deploy.prune_backups(tmp_path) == [names[1], names[0]]
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
        ["app.db", "app.db-wal"] + [n + s for n in names[2:] for s in ("", "-shm", "-wal")])
    assert deploy.prune_backups(tmp_path) == []


IMAGES = json.loads((Path(__file__).parent / "data" / "deploy-images-20261108.json").read_text())


def _tags(images, image):
    return set(images[image]["tags"])


def test_prune_images_keeps_current_and_two_rollback_levels_on_the_captured_host():
    images, current = IMAGES["images"], IMAGES["current"]
    previous = next(i for i, info in images.items() if "rollback-5b0b950" in info["tags"])
    older = next(i for i, info in images.items() if "rollback-c34b854" in info["tags"])
    removed = set(deploy.images_to_prune(images, current))
    kept = {current, previous, older}
    assert not removed & set().union(*(_tags(images, i) for i in kept))
    assert removed == set().union(*(_tags(images, i) for i in images if i not in kept))
    # Current 5b0b950, level 1 c34b854, level 2 8f0e70f; 09c3124 and everything older goes.
    assert {"latest", "5b0b950", "rollback-5b0b950", "c34b854", "rollback-c34b854", "8f0e70f"}.isdisjoint(removed)
    assert {"rollback-8f0e70f", "09c3124", "rollback-daa7244", "daa7244"} <= removed


def test_prune_images_never_selects_an_unreleased_build_or_a_dangling_layer():
    images = {**IMAGES["images"],
              # Marked variant: a later build not yet released, and an untagged leftover.
              "sha256:" + "a" * 64: {"created": "2026-11-08T15:00:00.123456789+08:00", "tags": ["feedbee"]},
              "sha256:" + "b" * 64: {"created": "2026-10-01T10:00:00+08:00", "tags": ["<none>"]}}
    removed = deploy.images_to_prune(images, IMAGES["current"])
    assert "feedbee" not in removed and "<none>" not in removed
    # With fewer rollback targets than requested, only those are kept and nothing newer is lost.
    only = {i: info for i, info in IMAGES["images"].items()
            if i == IMAGES["current"] or "rollback-5b0b950" in info["tags"]}
    assert deploy.images_to_prune(only, IMAGES["current"]) == []


def test_closed_batch_is_idle_even_with_old_step(tmp_path):
    captured = json.loads((Path(__file__).parent / 'data/hypotheses_v3.json').read_text())['batch']
    database = tmp_path / 'closed.db'
    conn = db.connect(database)
    try:
        # Lifecycle-only variant of a captured batch; closed never resumes its saved step.
        conn.execute('INSERT INTO batches(status,created_at,rule,entry,marks,step) VALUES (?,?,?,?,?,?)',
                     ('closed', db.now(), json.dumps(captured['rule']),
                      json.dumps(captured['entry']), json.dumps(captured['marks']), 'old-step'))
        deploy.idle(database)
        conn.execute("UPDATE batches SET status='tracking'")
        with pytest.raises(RuntimeError, match='尚未空闲'):
            deploy.idle(database)
        conn.execute('UPDATE batches SET step=NULL')
        deploy.idle(database)
    finally:
        conn.close()


def test_describe_includes_tmpfs_missing_from_captured_docker_mounts(monkeypatch):
    # Actual Compose container inspect; only the private physical /data path is normalized.
    captured = json.loads((Path(__file__).parent / 'data/deploy-tools-inspect-20261111.json').read_text())
    assert [item['Destination'] for item in captured['Mounts']] == ['/data']
    monkeypatch.setattr(deploy, 'run', lambda *args: json.dumps([captured]))
    assert deploy.describe('tools')['mounts'] == {
        '/data': ['/private/rehearsal-data', True], '/run/ashare-tools': ['tmpfs', True]}


@pytest.mark.parametrize('listed', [False, True])
def test_describe_preserves_readonly_tmpfs_and_normalizes_both_docker_shapes(monkeypatch, listed):
    captured = json.loads((Path(__file__).parent / 'data/deploy-tools-inspect-20261111.json').read_text())
    # Explicit read-only/alternate API-shape variants of the captured metadata, not live mounts.
    captured['HostConfig']['Tmpfs']['/run/ashare-tools'] += ',ro'
    if listed:
        captured['Mounts'].append(dict(Destination='/run/ashare-tools', Type='tmpfs', Source='', RW=False))
    monkeypatch.setattr(deploy, 'run', lambda *args: json.dumps([captured]))
    assert deploy.describe('tools')['mounts']['/run/ashare-tools'] == ['tmpfs', False]


def test_describe_accepts_null_tmpfs_on_containers_without_it(monkeypatch):
    captured = json.loads((Path(__file__).parent / 'data/deploy-tools-inspect-20261111.json').read_text())
    captured['HostConfig']['Tmpfs'] = None
    monkeypatch.setattr(deploy, 'run', lambda *args: json.dumps([captured]))
    assert deploy.describe('web')['mounts'] == {'/data': ['/private/rehearsal-data', True]}


# Captured from `docker compose --profile tools config --format json` on the production host (2026-11-14).
_COMPOSE_CONFIG = {'services': {
    'web': {'volumes': [{'type': 'bind', 'source': '/home/lin/a-share-research-data', 'target': '/data', 'bind': {}}]},
    'worker': {'volumes': [
        {'type': 'bind', 'source': '/home/lin/a-share-research-data', 'target': '/data', 'bind': {}},
        {'type': 'bind', 'source': '/home/lin/.pi', 'target': '/home/lin/.pi', 'bind': {}},
        {'type': 'bind', 'source': '/home/lin/wiki/concepts/company-analysis', 'target': '/method-cards',
         'read_only': True, 'bind': {}}]},
    'tools': {'volumes': [{'type': 'bind', 'source': '/home/lin/a-share-research-data', 'target': '/data', 'bind': {}}],
              'tmpfs': ['/run/ashare-tools:uid=1000,gid=1000,mode=0700']}}}
_WORKER_BEFORE = {'/data': ['/home/lin/a-share-research-data', True], '/home/lin/.pi': ['/home/lin/.pi', True]}
_WORKER_DECLARED = _WORKER_BEFORE | {'/method-cards': ['/home/lin/wiki/concepts/company-analysis', False]}


def test_declared_mounts_match_describe_shape_on_the_captured_compose_config(monkeypatch):
    monkeypatch.setattr(deploy, 'run', lambda *args: json.dumps(_COMPOSE_CONFIG))
    assert deploy.declared_mounts('worker') == _WORKER_DECLARED
    assert deploy.declared_mounts('tools') == {'/data': ['/home/lin/a-share-research-data', True],
                                               '/run/ashare-tools': ['tmpfs', True]}


@pytest.mark.parametrize('allow_config, actual, accepted', [
    (True, _WORKER_DECLARED, True),    # authorized change: the declared read-only mount is present
    (False, _WORKER_DECLARED, False),  # routine release: any mount change is rejected
    (True, _WORKER_BEFORE, False),     # declared mount missing from the running container
    # Minimal tampered variant of the captured mount: writable instead of the declared read-only.
    (True, _WORKER_BEFORE | {'/method-cards': ['/home/lin/wiki/concepts/company-analysis', True]}, False),
])
def test_verify_release_checks_mounts_against_compose_only_for_authorized_config(
        monkeypatch, allow_config, actual, accepted):
    def run(*args):
        if 'config' in args:
            return json.dumps(_COMPOSE_CONFIG)
        return json.dumps({'ok': True, 'ready': True})

    before = {'worker': {'image': 'old', 'running': True, 'config_hash': 'h1', 'cpus': 600_000_000,
                         'ports': {}, 'mounts': _WORKER_BEFORE}}
    monkeypatch.setattr(deploy, 'run', run)
    monkeypatch.setattr(deploy, 'wait_ready', lambda: None)
    monkeypatch.setattr(deploy, 'describe', lambda service: before['worker'] | {
        'image': 'new', 'config_hash': 'h2' if allow_config else 'h1', 'mounts': actual})

    def verify():
        return deploy._verify_release({'worker': 'new'}, before, ('worker',), allow_config=allow_config,
                                      save=lambda name, value: None)
    if accepted:
        assert verify()['worker']['mounts'] == actual
    else:
        with pytest.raises(RuntimeError, match='worker 镜像、运行状态或部署配置不一致'):
            verify()
