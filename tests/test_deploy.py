"""Release guards on captured runs in an isolated database; never stops services."""

import json
import os
from pathlib import Path

import pytest

from a_share_research import db, deploy
from support.continuation import load


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
