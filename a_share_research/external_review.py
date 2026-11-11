"""Version-bound external review queue; never gathers evidence or rewrites the draft."""
from __future__ import annotations

import json
import logging
import os
from datetime import date

from a_share_research import db, external_research, gate

log = logging.getLogger(__name__)


def protected(day: date | None = None) -> bool:
    day = day or date.fromisoformat(db.now()[:10])
    return date(2026, 11, 29) <= day <= date(2026, 12, 4)


def enabled() -> bool:
    return os.environ.get('EXTERNAL_REVIEW_ENABLED') == '1' and not protected()


def migrate(conn) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS external_reviews (
        submission_id INTEGER PRIMARY KEY REFERENCES external_submissions(id),
        run_id INTEGER NOT NULL UNIQUE REFERENCES runs(id),
        status TEXT NOT NULL CHECK(status IN ('submitted','reviewing','done','failed')),
        attempt INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        report_id INTEGER REFERENCES reports(id), error TEXT)""")
    conn.execute('CREATE INDEX IF NOT EXISTS external_reviews_by_status ON external_reviews(status, run_id)')
    conn.execute("""CREATE TRIGGER IF NOT EXISTS external_review_identity_immutable
        BEFORE UPDATE OF submission_id, run_id ON external_reviews
        BEGIN SELECT RAISE(ABORT, 'review identity is immutable'); END""")


def view(conn, submission_id: int) -> dict:
    row = conn.execute('SELECT run_id,status,attempt,report_id,error FROM external_reviews WHERE submission_id=?',
                       (submission_id,)).fetchone()
    return dict(row) if row else {'status': 'not_queued'}


def enqueue(conn, principal, submission_id: int, *, allow: bool = False) -> dict:
    """Idempotently queue one accepted revision, with a new isolated run for that revision."""
    with db.transaction(conn):
        source = conn.execute('SELECT s.*,e.owner_key_id,e.account_aware,e.company_key,m.material'
                              ' FROM external_submissions s JOIN external_research e ON e.id=s.external_id'
                              ' JOIN external_materials m ON m.external_id=s.external_id AND m.revision=s.material_revision'
                              ' WHERE s.id=?', (submission_id,)).fetchone()
        if source is None:
            raise external_research.SubmissionError('提交不存在', 404)
        external_research._owned(conn, principal, source['external_id'])
        existing = view(conn, submission_id)
        if existing['status'] != 'not_queued':
            return existing
        if not allow or protected():
            raise external_research.SubmissionError('外部复核队列尚未启用或处于月批保护期')
        if not source['accepted']:
            raise external_research.SubmissionError('草稿未通过机械检查，不能排队')
        run_id = db.create_run(conn, source['company_key'], request_id=f'external-submission-{submission_id}',
                               kind='external', status='queued')
        db.update_run(conn, run_id, material=source['material'], request=json.dumps({'submission_id': submission_id}))
        conn.execute("INSERT INTO external_reviews(submission_id,run_id,status,created_at,updated_at)"
                     " VALUES (?,?,'submitted',?,?)", (submission_id, run_id, db.now(), db.now()))
        return view(conn, submission_id)


def recover(conn) -> None:
    """Only interrupted external reviews return to submitted; frozen drafts and reports stay intact."""
    with db.transaction(conn):
        rows = conn.execute("SELECT run_id FROM external_reviews WHERE status='reviewing'").fetchall()
        for row in rows:
            conn.execute("UPDATE external_reviews SET status='submitted',updated_at=? WHERE run_id=?",
                         (db.now(), row['run_id']))
            db.update_run(conn, row['run_id'], status='queued', step='submitted', error=None, finished_at=None)


def execute(conn, run_id: int) -> None:
    from a_share_research import external_publication, model, research
    from a_share_research.material import Material
    from a_share_research.reviewing import ReviewLedger

    with db.transaction(conn):
        row = conn.execute("SELECT q.*,s.draft,s.draft_sha256,s.material_sha256,m.material,e.owner_key_id,e.account_aware"
                           " FROM external_reviews q JOIN external_submissions s ON s.id=q.submission_id"
                           " JOIN external_materials m ON m.external_id=s.external_id AND m.revision=s.material_revision"
                           " JOIN external_research e ON e.id=s.external_id JOIN runs u ON u.id=q.run_id"
                           " WHERE q.run_id=? AND u.kind='external' AND q.status='submitted'", (run_id,)).fetchone()
        if row is None:
            return
        attempt = row['attempt'] + 1
        conn.execute("UPDATE external_reviews SET status='reviewing',attempt=?,updated_at=? WHERE run_id=?",
                     (attempt, db.now(), run_id))
        db.update_run(conn, run_id, status='running', step='review', started_at=db.now(), error=None)
    try:
        material = Material.from_snapshot(json.loads(row['material']))
        draft = json.loads(row['draft'])
        if gate.digest(draft) != row['draft_sha256'] or material.snapshot()['sha256'] != row['material_sha256']:
            raise ValueError('external frozen content mismatch')
        problems = gate.check_draft(draft, json.loads(row['material']))
        if problems:
            raise ValueError('external draft no longer passes mechanical checks')
        runner = research.Runner(conn, run_id)
        # Recovery must not reset the existing run's paid-call ceiling.
        runner.calls = conn.execute('SELECT count(*) FROM calls WHERE run_id=?', (run_id,)).fetchone()[0]
        runner.material = material
        identity = model.application_identity() | {'material_sha256': row['material_sha256'],
                   'producer': {'kind': 'external', 'key_id': row['owner_key_id'],
                                'account_aware': bool(row['account_aware']), 'submission_id': row['submission_id']}}
        db.update_run(conn, run_id, identity=json.dumps(identity, ensure_ascii=False))
        ledger = ReviewLedger()
        problems = research.review(runner, material, draft, ledger)
        with db.transaction(conn):
            current = conn.execute('SELECT status,attempt FROM external_reviews WHERE run_id=?', (run_id,)).fetchone()
            if current['status'] != 'reviewing' or current['attempt'] != attempt:
                return  # An interrupted worker no longer owns this review attempt.
            report_id = external_publication.publish(conn, run_id, material, draft, ledger, problems,
                         submission_id=row['submission_id'], owner_key_id=row['owner_key_id'],
                         account_aware=bool(row['account_aware']), calls=runner.calls)
            outcome = conn.execute('SELECT status,error FROM runs WHERE id=?', (run_id,)).fetchone()
            conn.execute('UPDATE external_reviews SET status=?,report_id=?,error=?,updated_at=? WHERE run_id=?',
                         (outcome['status'], report_id, outcome['error'], db.now(), run_id))
    except Exception as exc:
        if isinstance(exc, research.ModelStageError):
            log.error('External review %s: model stage failed', run_id)
        else:
            log.exception('External review %s failed', run_id)
        with db.transaction(conn):
            changed = conn.execute("UPDATE external_reviews SET status='failed',error=?,updated_at=?"
                                   " WHERE run_id=? AND status='reviewing' AND attempt=?",
                                   ('外部复核未完成；请查看阶段记录，不自动重试', db.now(), run_id, attempt)).rowcount
            if changed:
                db.update_run(conn, run_id, status='failed', step='review', finished_at=db.now(),
                              error='外部复核未完成；请查看阶段记录，不自动重试')
