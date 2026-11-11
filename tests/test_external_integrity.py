"""Frozen external material and publication links are checked without requiring an active key."""
import json

import pytest

from a_share_research import api_keys, db, external_research, external_review, integrity
from support.reports import MATERIAL, report


@pytest.fixture
def state(tmp_path, monkeypatch):
    path = tmp_path / 'app.db'
    conn = db.connect(path)
    token = api_keys.create(conn, 'integrity')
    principal = api_keys.authenticate(conn, token)
    source = db.create_run(conn, MATERIAL.code, status='done')
    db.save_material(conn, source, MATERIAL.snapshot())
    item = external_research.start(conn, principal, 'integrity', source)
    receipt = external_research.submit(conn, principal, item['id'], 0, report())
    monkeypatch.setattr(external_review, 'protected', lambda: False)
    queued = external_review.enqueue(conn, principal, receipt['submission_id'], allow=True)
    yield conn, path, item['id'], receipt['submission_id'], queued['run_id']
    conn.close()


def check(state):
    return integrity.check(state[1], state[1].parent / 'pdf')[0]


def test_clean_and_revoked_producer_remain_valid(state):
    assert check(state) == []
    state[0].execute('DELETE FROM api_keys')
    assert check(state) == []


@pytest.mark.parametrize('sql,expected', [
    ("UPDATE external_research SET material='{}'", '材料快照哈希'),
    ("UPDATE external_submissions SET draft='{}'", '草稿哈希'),
    ("UPDATE external_submissions SET material_sha256='bad'", '材料与所属研究'),
    ("UPDATE runs SET kind='research' WHERE kind='external'", '任务类型'),
    ("UPDATE external_reviews SET status='done'", '状态关联'),
    ("UPDATE external_reviews SET report_id=999", '发布报告关联'),
])
def test_corruption_is_reported(state, sql, expected):
    conn = state[0]
    for row in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'external_%'").fetchall():
        conn.execute('DROP TRIGGER ' + row[0])  # Explicit corruption experiment bypasses normal mutation guards.
    conn.execute('PRAGMA foreign_keys=OFF')  # Simulate damage by an older/offline writer.
    conn.execute(sql)
    assert any(expected in p for p in check(state))


def test_published_producer_must_match_submission(state):
    conn, _, _, sid, run = state
    producer = dict(kind='external', key_id=1, account_aware=False, submission_id=sid)
    db.update_run(conn, run, status='done', identity=json.dumps({'producer': producer}))
    db.save_report(conn, run, MATERIAL.code, MATERIAL.name, '2025-12-31', True, '需要谨慎', {'producer': producer})
    rid = conn.execute('SELECT id FROM reports WHERE run_id=?', (run,)).fetchone()[0]
    conn.execute("UPDATE external_reviews SET status='done',report_id=?", (rid,))
    assert check(state) == []
    db.update_run(conn, run, identity=json.dumps({'producer': producer | {'account_aware': True}}))
    assert any('任务生产者' in p for p in check(state))
    conn.execute("UPDATE reports SET body=? WHERE id=?", (json.dumps({'producer': producer | {'key_id': 999}}), rid))
    assert any('报告生产者' in p for p in check(state))


def test_pre_external_database_is_supported(tmp_path):
    path = tmp_path / 'old.db'
    conn = db.connect(path)
    conn.execute('DROP TABLE external_reviews')
    conn.execute('DROP TABLE external_submissions')
    conn.execute('DROP TABLE external_research')
    conn.close()
    problems, counts = integrity.check(path, tmp_path / 'pdf')
    assert problems == [] and 'external_research' not in counts
