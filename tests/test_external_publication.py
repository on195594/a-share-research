"""Publication boundaries over captured disclosure excerpts; no model-quality claim."""
import json

import pytest

from a_share_research import db, external_publication, research, report as report_body
from a_share_research.reviewing import ReviewLedger, digest
from support.reports import MATERIAL, report


@pytest.fixture
def context(tmp_path):
    conn = db.connect(tmp_path / 'app.db')
    run = db.create_run(conn, MATERIAL.code, kind='external', status='running')
    db.save_material(conn, run, MATERIAL.snapshot())
    yield conn, run
    conn.close()


def reviewed(draft, issues=()):
    ledger = ReviewLedger()
    payload = dict(version=1, draft_sha256=digest(draft), material_sha256=MATERIAL.snapshot()['sha256'],
                   issues=list(issues), resolutions=[], risk_resolutions=[])
    ledger.accept(payload, draft, MATERIAL.source_registry(), MATERIAL.table, MATERIAL.snapshot()['sha256'])
    return ledger


def publish(conn, run, draft, ledger, problems=()):
    with db.transaction(conn):
        rid = external_publication.publish(conn, run, MATERIAL, draft, ledger, list(problems),
                                           submission_id=4, owner_key_id=9, account_aware=True, calls=1)
    row = conn.execute('SELECT * FROM reports WHERE id=?', (rid,)).fetchone()
    return row, json.loads(row['body'])


def test_external_author_and_review_binding_are_preserved(context):
    conn, run = context
    draft = report()
    row, body = publish(conn, run, draft, reviewed(draft))
    assert row['passed'] == 1
    assert body['producer'] == dict(kind='external', key_id=9, submission_id=4, account_aware=True)
    assert body['judgment']['author']['provider'] == 'external'
    assert body['judgment']['author']['model'] is None
    assert body['review_binding']['draft_sha256'] == digest(draft)
    assert body['summary'] == draft['summary'] and 'producer' in report_body.COMPACT_FIELDS
    assert conn.execute('SELECT status FROM runs WHERE id=?', (run,)).fetchone()[0] == 'done'
    assert conn.execute(f'SELECT count(*) FROM reports r JOIN runs u ON u.id=r.run_id WHERE {db.FORMAL_REPORT}').fetchone()[0] == 0


def test_missing_or_stale_review_cannot_publish(context):
    conn, run = context
    draft = report()
    ledger = reviewed(draft)
    changed = report(summary='需要谨慎：已改变草稿。')
    for value, audit in ((draft, ReviewLedger()), (changed, ledger)):
        with pytest.raises(ValueError, match='bind'):
            publish(conn, run, value, audit)
    assert conn.execute('SELECT count(*) FROM reports').fetchone()[0] == 0


def test_omitted_pending_issue_still_blocks_and_hides_summary(context):
    conn, run = context
    draft = report()
    ledger = reviewed(draft, [dict(type='claim', where='summary', problem='测试绑定后的实质问题',
                                  importance='结论受影响', evidence=[{'ref': 'T.op_cash'}])])
    row, body = publish(conn, run, draft, ledger)
    assert row['passed'] == 0 and 'summary' not in body
    assert body['problems'] and body['review_binding']['pending_issue_ids']
    assert conn.execute('SELECT status FROM runs WHERE id=?', (run,)).fetchone()[0] == 'failed'


def test_valid_question_only_issue_keeps_named_gap(context):
    conn, run = context
    draft = report()
    ledger = reviewed(draft, [dict(type='question', where='missing', problem='尚缺更新材料',
                                  importance='继续跟踪', evidence=[])])
    row, body = publish(conn, run, draft, ledger)
    assert row['passed'] == 1 and body['open_questions'][0]['problem'] == '尚缺更新材料'
    assert body['review_binding']['pending_issue_ids']


def test_report_and_status_are_rolled_back_together(context):
    conn, run = context
    conn.execute("CREATE TRIGGER reject_status BEFORE UPDATE OF status ON runs BEGIN SELECT RAISE(ABORT,'reject'); END")
    import sqlite3
    draft = report()
    with pytest.raises(sqlite3.IntegrityError):
        publish(conn, run, draft, reviewed(draft))
    assert conn.execute('SELECT count(*) FROM reports').fetchone()[0] == 0


def test_external_prior_is_preserved_for_agenda_scoring(context):
    from copy import deepcopy
    conn, run = context
    material = deepcopy(MATERIAL)
    # Explicit metadata-only variant of the captured disclosure material.
    prior = {'report_id': 143, 'period': '2024-12-31', 'risks': [], 'checkpoints': []}
    material.extra.update(prior=prior, question=research.prior_question(prior))
    draft = report(prior_status=[], answer=report()["business"])
    ledger = ReviewLedger()
    ledger.accept(dict(version=1, draft_sha256=digest(draft), material_sha256=material.snapshot()['sha256'],
                       issues=[], resolutions=[], risk_resolutions=[]), draft, material.source_registry(),
                  material.table, material.snapshot()['sha256'])
    with db.transaction(conn):
        rid = external_publication.publish(conn, run, material, draft, ledger, [],
                                           submission_id=4, owner_key_id=9, account_aware=False, calls=1)
    body = json.loads(conn.execute('SELECT body FROM reports WHERE id=?', (rid,)).fetchone()[0])
    assert body['prior'] == {'report_id': 143, 'period': '2024-12-31', 'question_added': True}
    assert body['prior_status'] == []


def test_failed_projection_declares_producer():
    assert 'producer' in report_body.FAILED_FIELDS
