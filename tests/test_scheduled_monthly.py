"""Monthly admission and acknowledged event delivery through the schedule tools."""
from contextlib import closing
import json
from pathlib import Path

import pytest

from a_share_research import api_keys, db, service
from a_share_research.tools.registry import Context, ToolError, invoke

CAPTURE = Path(__file__).parent / 'data/monthly-research-status.json'


@pytest.fixture
def monthly(tmp_path, monkeypatch):
    database = tmp_path / 'monthly.db'
    with closing(db.connect(database)) as conn:
        token = api_keys.create(conn, 'monthly-schedule', role='assistant')
        ctx = Context(conn, database, api_keys.authenticate(conn, token), True)
        clock = ['2026-11-01T10:00:00+08:00']
        monkeypatch.setattr(db, 'now', lambda: clock[0])
        yield ctx, clock


def run(ctx, job, slot):
    return invoke(ctx, 'schedule_run', {'job': job, 'slot': slot})['data']


def ack(ctx, row):
    return invoke(ctx, 'schedule_ack', {'run_id': row['run_id']})['data']


def test_scheduled_submit_reuses_admission_and_final_text(monthly):
    ctx, _ = monthly
    month = '2026-11'
    row = run(ctx, 'monthly_submit', month)
    batch_id = service.monthly_batch(ctx.connection, month)['batch']['id']
    assert row['status'] == 'ready'
    assert row['text'] == (f'【月批】已发起 {month} 月度候选批次 #{batch_id}（排队中）；初筛+研究通常需数小时，'
                           '完成或失败时另行通知。\n')
    assert run(ctx, 'monthly_submit', month) == row
    assert ack(ctx, row) == run(ctx, 'monthly_submit', month)
    ctx.connection.execute("UPDATE batches SET status='failed' WHERE id=?", (batch_id,))
    token = api_keys.create(ctx.connection, 'other-schedule', role='assistant')
    other = Context(ctx.connection, ctx.database, api_keys.authenticate(ctx.connection, token), True)
    existing = run(other, 'monthly_submit', month)
    assert existing['text'] == f'【月批】{month} 已有批次 #{batch_id}（失败），按规则不再新建（失败批次也不自动重开）。\n'
    assert ctx.connection.execute('SELECT COUNT(*) FROM batches').fetchone()[0] == 1


def test_hourly_status_repeats_until_ack_and_delivers_new_events(monthly):
    ctx, clock = monthly
    conn = ctx.connection
    captured = json.loads(CAPTURE.read_text())
    batch = captured['batches'][1]
    failure = next(r for r in captured['runs'] if r['id'] == batch['q20'][1]['run_id'])

    def hourly(stamp):
        clock[0] = stamp + ':30:00+08:00'
        return run(ctx, 'monthly_status', stamp)

    assert ack(ctx, hourly('2026-11-01T09'))['text'] == ''
    conn.execute('INSERT INTO batches(id,status,created_at,rule) VALUES (?,?,?,?)',
                 (batch['id'], 'proposed', batch['created_at'], json.dumps(batch['rule'])))
    first, second = (hourly(f'2026-11-01T{hour}') for hour in (11, 12))
    head = f"【月批】2026-11 批次 #{batch['id']}"
    tail = '详情见批次页；入选与账户动作仍由你决定。\n'
    needs_owner = head + '（待确认）自动冻结未完成，需要你在批次页确认入选。\n' + tail
    assert first['text'] == second['text'] == needs_owner
    assert ack(ctx, second)['status'] == 'acked'
    silent = ack(ctx, hourly('2026-11-01T13'))
    assert silent['text'] == '' and silent['state'] == second['state']

    # The captured tracking batch has one failed research run. Its new event remains deliverable.
    conn.execute('INSERT INTO runs(id,request_id,code,status,error,created_at) VALUES (?,?,?,?,?,?)',
                 tuple(failure[k] for k in ('id', 'request_id', 'code', 'status', 'error', 'created_at')))
    conn.execute('UPDATE batches SET status=?,q20=? WHERE id=?',
                 (batch['status'], json.dumps(batch['q20']), batch['id']))
    completed = hourly('2026-11-01T14')
    assert completed['text'] == (head + '（跟踪中）已完成，冻结于 ，基准日 —。\n'
                                 + f"研究失败 1 家：{failure['code']} {failure['error']}\n" + tail)
    # Two different results are ready at once. A later acknowledgement of the older row
    # must retain both delivered events, including the one absent when that row was built.
    conn.execute("UPDATE batches SET status='failed' WHERE id=?", (batch['id'],))
    failed = hourly('2026-11-01T15')
    assert '失败' in failed['text']
    ack(ctx, failed)
    clock[0] = '2026-11-01T15:31:00+08:00'
    ack(ctx, completed)
    assert ack(ctx, hourly('2026-11-01T16'))['text'] == ''
    conn.execute('UPDATE batches SET status=? WHERE id=?', (batch['status'], batch['id']))
    assert ack(ctx, hourly('2026-11-01T17'))['text'] == ''

    # A different assistant still gets its first notification, and the next month starts fresh.
    token = api_keys.create(conn, 'other-assistant', role='assistant')
    other = Context(conn, ctx.database, api_keys.authenticate(conn, token), True)
    assert run(other, 'monthly_status', '2026-11-01T17')['text'] == completed['text']
    next_month = hourly('2026-12-01T00')
    assert next_month['text'] == '' and not next_month['state'].get('seen')


def test_hourly_status_stuck_boundary_and_terminal_messages(monthly):
    ctx, clock = monthly
    conn = ctx.connection
    batch_id = service.monthly_batch(conn, '2026-11', submit=True)['batch']['id']
    tail = '详情见批次页；入选与账户动作仍由你决定。\n'
    head = f'【月批】2026-11 批次 #{batch_id}'
    for hour, status, body in [
        (10, 'queued', ''),
        (11, 'queued', '（排队中）创建超过 48 小时仍未完成，请查看 Research 服务。'),
        (12, 'failed', '（失败）失败：未记录原因。自动月批不自动重跑，需你决定是否在网页手动重开。'),
        (13, 'frozen', '（已冻结）已完成，冻结于 2026-11-03T13:00，基准日 2026-11-02。'),
    ]:
        clock[0] = f'2026-11-03T{hour}:00:00+08:00'
        conn.execute('UPDATE batches SET status=?,as_of=?,frozen_at=? WHERE id=?',
                     (status, '2026-11-02', clock[0] if status == 'frozen' else None, batch_id))
        row = run(ctx, 'monthly_status', clock[0][:13])
        assert row['status'] == 'ready' and row['text'] == (head + body + '\n' + tail if body else '')
        ack(ctx, row)


@pytest.mark.parametrize('job,slot', [('monthly_status', '2026-11-01T10:00'), ('monthly_status', '2026-11-01T11'),
                                      ('monthly_submit', '2026-11-01')])
def test_monthly_slots_must_be_canonical_and_started(monthly, job, slot):
    ctx, _ = monthly
    with pytest.raises(ToolError) as exc:
        run(ctx, job, slot)
    assert exc.value.code == 'invalid_arguments'
    assert not ctx.connection.execute('SELECT id FROM scheduled_runs').fetchone()
