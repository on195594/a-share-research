"""Principal-scoped scheduled captures and delivery acknowledgements."""
from calendar import monthrange
from datetime import date, datetime, timedelta
import fcntl
import json
import logging
import os
import re
from urllib.parse import urlsplit

from requests import RequestException

from a_share_research import db, gate, scans, service
from a_share_research.market import redact_error
from a_share_research.policy import account_view, benchmark, daily_brief, monitor, registry, stop_alert
from a_share_research.tools import advisor_text, scheduled_monthly
from a_share_research.tools.registry import Context, ToolError, domain_error

log = logging.getLogger(__name__)
_SLOT_FORMAT = {
    'stop_alert': 'YYYY-MM-DDTHH:MM（5 分钟整点）', 'daily_brief': 'YYYY-MM-DD',
    'advisor_scan': 'YYYY-MM-DD', 'benchmark': 'YYYY-MM', 'advisor_weekly': 'YYYY-Www',
    'monthly_submit': 'YYYY-MM', 'monthly_status': 'YYYY-MM-DDTHH',
}


def _slot_start(job, slot, now):
    try:
        if job in ('stop_alert', 'monthly_status'):
            start = datetime.fromisoformat(slot)
            canonical = start.isoformat(timespec='hours' if job == 'monthly_status' else 'minutes')
            if start.tzinfo is not None or start.minute % 5:
                raise ValueError
        else:
            monthly = job in ('benchmark', 'monthly_submit')
            day = date.fromisoformat(slot + '-01' if monthly else slot)
            start = datetime.combine(day, datetime.min.time())
            if job == 'advisor_weekly':
                year, week, _ = day.isocalendar()
                canonical = f'{year:04d}-W{week:02d}'
            else:
                canonical = day.isoformat()[:7] if monthly else day.isoformat()
        if canonical != slot:
            raise ValueError
    except ValueError:
        raise ToolError('invalid_arguments', f'{job} 的 slot 须为上海时间 {_SLOT_FORMAT[job]}', 422) from None
    start = start.replace(tzinfo=db.SHANGHAI)
    if start > now:
        raise ToolError('invalid_arguments', 'slot 不能是未来的上海时段', 422)
    return start


def _expired(row, start, now):
    job = row['job']
    if job == 'stop_alert':
        return now - start > timedelta(minutes=15)
    if row['status'] == 'ready' or (job == 'advisor_scan' and row['scan_id'] is not None):
        return False
    if job == 'advisor_weekly':
        return now >= start + timedelta(weeks=1) and 'weekly' not in json.loads(row['result'] or '{}')
    if job == 'monthly_submit':
        # Admission is only for the current Shanghai month; a past month can never be admitted.
        return (start.year, start.month) != (now.year, now.month)
    return job in ('daily_brief', 'advisor_scan') and start.date() != now.date()


def _expire(conn, row):
    span = '跨月' if row['job'] == 'monthly_submit' else '跨日'
    text = '' if row['job'] == 'stop_alert' else f"{row['slot']} 的定时任务已{span}过期，本次未推送原时段正文。\n"
    conn.execute("""UPDATE scheduled_runs SET status='expired',finished_at=?,text=?,result=NULL,state=NULL,
                    error=NULL,error_code=NULL WHERE id=?""", (db.now(), text, row['id']))


def _row(conn, run_id):
    return conn.execute('SELECT * FROM scheduled_runs WHERE id=?', (run_id,)).fetchone()


def _view(row):
    data = dict(row)
    data['run_id'] = data.pop('id')
    del data['principal_key_id']
    for field in ('result', 'state'):
        if data[field] is not None:
            data[field] = json.loads(data[field])
    return data


def _accept(ctx, job, slot, now):
    conn = ctx.connection
    with db.transaction(conn):
        registry._authorize(conn, ctx.principal)
        slot = _backlog(ctx, job, slot)
        start = _slot_start(job, slot, now)
        key = (ctx.principal.key_id, job, slot)
        row = conn.execute('SELECT * FROM scheduled_runs WHERE principal_key_id=? AND job=? AND slot=?', key).fetchone()
        if row is None:
            cur = conn.execute("""INSERT INTO scheduled_runs (principal_key_id,job,slot,status,created_at)
                                  VALUES (?,?,?,'running',?)""", (*key, now.isoformat()))
            row = _row(conn, cur.lastrowid)
        if row['status'] in ('acked', 'expired'):
            return row, start
        if _expired(row, start, now):
            _expire(conn, row)
        elif row['status'] == 'failed':
            conn.execute("""UPDATE scheduled_runs SET status='running',attempts=attempts+1,
                            activation_id=NULL,evaluation_id=NULL,finished_at=NULL,error_code=NULL,error=NULL,
                            state=NULL,result=CASE WHEN job='advisor_weekly' THEN result END,text=NULL
                            WHERE id=?""", (row['id'],))
        return _row(conn, row['id']), start


def _previous(conn, row):
    query = "SELECT state FROM scheduled_runs WHERE principal_key_id=? AND job=? AND status='acked'"
    args = [row['principal_key_id'], row['job']]
    if row['job'] == 'monthly_status':
        # Acks share second-resolution timestamps. The merged set orders ties.
        found = conn.execute(query + """ AND slot GLOB ? ORDER BY acked_at DESC,
                             json_array_length(state,'$.seen') DESC LIMIT 1""",
                             (*args, row['slot'][:7] + '-*')).fetchone()
        return json.loads(found[0]) if found else {}
    if row['job'] == 'stop_alert':
        from a_share_research.policy.v0.commands_holdings import new_stop_alerts
        day = row['slot'][:10]
        query += ' AND slot BETWEEN ? AND ?'
        delivered = {}
        for (raw,) in conn.execute(query, (*args, day + 'T00:00', day + 'T23:55')):
            state = json.loads(raw)
            if state.get('date') == day:
                _, delivered = new_stop_alerts(state['levels'], delivered, day)
        return delivered
    found = conn.execute(query + ' ORDER BY slot DESC LIMIT 1', args).fetchone()
    return json.loads(found[0]) if found else {}


def _evaluate(ctx, row):
    try:
        return _evaluate_current(ctx, row)
    except registry.PolicyError as exc:
        if exc.code != 'policy_activation_changed':
            raise
    # This rejection guarantees no receipt was written under the old activation.
    return _evaluate_current(ctx, row)


def _evaluate_current(ctx, row):
    conn = ctx.connection
    job = row['job']
    with db.transaction(conn):
        activation = registry._current(conn)
        if activation is None:
            raise registry.PolicyError('生效政策尚未启用，请先读取当前指针')
        activation_id = activation['id']
        request = f"schedule:{row['id']}:{activation_id}:{row['attempts']}"
        previous = {}
        if job == 'stop_alert':
            # Only a committed evaluation freezes the prior input across an interruption.
            saved = row['activation_id'] == activation_id and conn.execute(
                'SELECT 1 FROM policy_evaluations WHERE created_by=? AND request_id=?',
                (ctx.principal.key_id, request)).fetchone()
            previous = json.loads(row['state']) if saved else _previous(conn, row)
        conn.execute('UPDATE scheduled_runs SET activation_id=?,state=? WHERE id=?',
                     (activation_id, gate.canonical(previous), row['id']))
    if job == 'benchmark':
        end = row['created_at'][:10]
        if end[:7] > row['slot']:
            year, month = map(int, row['slot'].split('-'))
            end = f"{row['slot']}-{monthrange(year, month)[1]}"
            # A pre-fix receipt under the original ID can have a different cutoff.
            request += f':{end}'
        return benchmark.run(conn, ctx.principal, ctx.database, request, activation_id, end)
    args = (conn, ctx.principal, ctx.database, request, activation_id)
    if job == 'stop_alert':
        return stop_alert.run(*args, previous)
    if job == 'daily_brief':
        return daily_brief.run(*args)
    return monitor.run(*args)


def _opportunity_key(prompt):
    evidence = prompt['evidence']
    if prompt['kind'] == 'research_opportunity':
        return f"{prompt['code']}|{evidence.get('report_id')}|{evidence.get('stance_action')}|{evidence.get('v2_zone')}"
    # Weekly comparisons group the candidate with every compared holding.
    pairs = sorted((item['candidate']['report_id'], item['holding']['code'], item['holding']['report_id'])
                   for item in evidence['comparisons'])
    return gate.canonical([prompt['code'], pairs])


def _safe_message(exc):
    message = redact_error(exc)
    message = re.sub(r'\basr_[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])', '[REDACTED]', message)
    message = re.sub(r'(https?://)[^/@\s]+@', r'\1[REDACTED]@', message)
    message = re.sub(r'(?i)(Bearer\s+)\S+', r'\1[REDACTED]', message)
    return re.sub(r"""(?ix)(\b(?:access_token|refresh_token|token|api[_-]?key|password)\b["']?\s*[:=]\s*)
                       (?:"[^"]*"|'[^']*'|[^&\s,;]+)""", r'\1[REDACTED]', message)


def _failure(exc):
    if isinstance(exc, registry.PolicyError) and exc.code:
        return exc.code, _safe_message(exc)
    if isinstance(exc, (registry.PolicyError, service.RequestError)):
        exc = domain_error(exc)
    if isinstance(exc, ToolError):
        return exc.code, _safe_message(exc)
    code = 'unavailable' if isinstance(exc, RequestException) else 'service_failure'
    return code, _safe_message(exc) or type(exc).__name__


def _advisor_inputs(ctx, row, result):
    conn = ctx.connection
    if row['job'] == 'advisor_weekly':
        result.update(json.loads(row['result'] or '{}'))
        if 'weekly' not in result:
            result.update(weekly=scans.weekly(conn), opportunity_costs=[])
            conn.execute('UPDATE scheduled_runs SET result=? WHERE id=?', (gate.canonical(result), row['id']))
        return True
    scan_id = row['scan_id']
    if scan_id is None:
        scan_id = scans.submit_request(conn, service.requester(conn, ctx.principal), f"schedule:advisor_scan:{row['slot']}")
        conn.execute('UPDATE scheduled_runs SET scan_id=? WHERE id=?', (scan_id, row['id']))
    status, = conn.execute('SELECT status FROM scans WHERE id=?', (scan_id,)).fetchone()
    if status in ('queued', 'running'):
        return False
    scan = scans.view(conn, scan_id)
    result.update(scan=scan, held_codes=None, holding_note='', opportunities=[])
    if scan['notify']:
        try:
            result['held_codes'] = account_view.current(conn, ctx.principal, ctx.database)['codes']
        except registry.PolicyError as exc:
            result['holding_note'] = _safe_message(exc)
            raise
    return True


def _advisor_opportunities(ctx, row, result):
    scan = row['job'] == 'advisor_scan'
    if scan and row['slot'] < db.now()[:10]:
        return None, _previous(ctx.connection, row)
    receipt = _evaluate(ctx, row)
    previous = _previous(ctx.connection, row)
    if scan and row['slot'] < db.now()[:10]:
        # Collection itself can cross midnight.
        return receipt['id'], previous
    kind, field = ('research_opportunity', 'opportunities') if scan else ('opportunity_cost', 'opportunity_costs')
    seen = set(previous.get('seen', []))
    current = [(_opportunity_key(p), p) for p in receipt['result']['review_prompts'] if p['kind'] == kind]
    result[field] = [p for key, p in current if key not in seen]
    return receipt['id'], {'seen': sorted(key for key, _ in current)}


def _advisor_body(job, result):
    if job == 'advisor_scan':
        url = urlsplit(os.environ.get('ASHARE_PUBLIC_MCP_URL', ''))
        origin = f'https://{url.netloc}' if url.scheme == 'https' and url.hostname and url.username is None else ''
        body = advisor_text.scan_text(result, origin, date.fromisoformat(db.now()[:10]))
    else:
        body = advisor_text.render_weekly(result['weekly'], result['opportunity_costs'])
    return body + '\n' if body else ''


def _complete(ctx, row, start):
    conn = ctx.connection
    job = row['job']
    state, result, text = {}, {}, ''
    error_code = error = evaluation_id = None
    try:
        if job in ('advisor_scan', 'advisor_weekly'):
            try:
                if not _advisor_inputs(ctx, row, result):
                    return
                evaluation_id, state = _advisor_opportunities(ctx, row, result)
            finally:
                # Even a failed account lookup or monitor can leave disclosures or a weekly digest.
                if result:
                    text = _advisor_body(job, result)
        elif job in ('monthly_submit', 'monthly_status'):
            previous = _previous(conn, row) if job == 'monthly_status' else {}
            text, state = scheduled_monthly.run(conn, job, row['slot'], previous)
        else:
            receipt = _evaluate(ctx, row)
            evaluation_id = receipt['id']
            evaluated = receipt['result']
            if evaluated['exit_code']:
                error_code, error = evaluated['status'], evaluated['stderr']
            else:
                text = evaluated['stdout']
                if job == 'stop_alert':
                    state = evaluated['state']
                    text = text.replace('按 a-stock-monitor 复核止损与 L3；', '按 a-share 的生效政策复核止损与 L3；')
                elif job == 'daily_brief':
                    text = text.replace('（运行 dividend-check 核对）', '（通过 a-share 核对券商流水）')
                else:
                    text = '📊 A股账本月度对比\n' + text
                if evaluated['stderr']:
                    result['stderr'] = evaluated['stderr']
    except Exception as exc:
        error_code, error = _failure(exc)
        log.error('Scheduled run %s (%s) failed: %s (%s)', row['id'], job, error_code, type(exc).__name__)
    with db.transaction(conn):
        finished = db.now()
        status = 'failed' if error_code else 'ready'
        if error_code and job == 'advisor_scan' and row['slot'] < finished[:10]:
            # A failed catch-up is terminal so future scans can proceed. Keep its partial disclosures and error.
            status = 'expired'
            if result:
                result['has_body'] = bool(text)
            text += _scan_failure_text(row['slot'], error_code, error)
        conn.execute("""UPDATE scheduled_runs SET status=?,evaluation_id=?,finished_at=?,error_code=?,error=?,
                        state=?,result=?,text=? WHERE id=?""",
                     (status, evaluation_id, finished, error_code, error,
                      gate.canonical(state), gate.canonical(result), text, row['id']))
        if not error_code and job in ('daily_brief', 'stop_alert') and _expired(row, start, datetime.fromisoformat(finished)):
            _expire(conn, row)


def _backlog(ctx, job, slot):
    """Recover interrupted links before selecting the oldest undelivered scan."""
    if job != 'advisor_scan':
        return slot
    conn = ctx.connection
    scope = (ctx.principal.key_id, job, slot)
    conn.execute("""UPDATE scheduled_runs AS r SET scan_id=q.scan_id FROM scan_requests AS q
                    WHERE r.principal_key_id=? AND r.job=? AND r.slot<=? AND r.scan_id IS NULL
                    AND r.status NOT IN ('acked','expired') AND q.requester=?
                    AND q.request_id='schedule:advisor_scan:' || r.slot""",
                 (*scope, service.requester(conn, ctx.principal)))
    found = conn.execute(
        """SELECT slot FROM scheduled_runs WHERE principal_key_id=? AND job=? AND slot<?
           AND status NOT IN ('acked','expired') AND scan_id IS NOT NULL ORDER BY slot LIMIT 1""", scope).fetchone()
    return found[0] if found else slot


def _replay(conn, row):
    data = _view(row)
    result = data['result']
    if data['job'] not in ('advisor_scan', 'advisor_weekly') or not result:
        return data
    if data['status'] == 'ready':
        # v26 rows may predate server rendering. A scan can also cross midnight before delivery.
        text = _advisor_body(data['job'], result)
    elif data['status'] == 'expired' and data['error_code'] and 'has_body' not in result:
        body = _advisor_body(data['job'], result)
        result['has_body'] = bool(body)
        text = body + _scan_failure_text(data['slot'], data['error_code'], data['error'])
    else:
        return data
    if text != data['text'] or data['status'] == 'expired':
        conn.execute('UPDATE scheduled_runs SET text=?,result=? WHERE id=?',
                     (text, gate.canonical(result), data['run_id']))
        data['text'] = text
    return data


def _scan_failure_text(slot, code, error):
    reason = ' '.join(error.split())[:160]
    return f"{slot} 的扫描补回失败：{code}，{reason}\n"


def run(ctx: Context, job: str, slot: str) -> dict:
    _slot_start(job, slot, datetime.fromisoformat(db.now()))
    # No SQLite write lock across collection. flock also releases on process death.
    lock = ctx.database.parent / f'.schedule-{ctx.principal.key_id}-{job}.lock'
    with lock.open('a') as stream:
        if ctx.nonblocking_schedule_lock:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ToolError('busy', '该定时任务正在执行，请稍后按原时段重试', 503) from None
        else:
            fcntl.flock(stream, fcntl.LOCK_EX)
        row, start = _accept(ctx, job, slot, datetime.fromisoformat(db.now()))
        replay = row['status'] != 'running'
        if not replay:
            _complete(ctx, row, start)
        conn = ctx.connection
        with db.transaction(conn):
            registry._authorize(conn, ctx.principal)
            row = _row(conn, row['id'])
            return _replay(conn, row) if replay else _view(row)


def ack(ctx: Context, run_id: int) -> dict:
    conn = ctx.connection
    with db.transaction(conn):
        registry._authorize(conn, ctx.principal)
        row = conn.execute('SELECT * FROM scheduled_runs WHERE id=? AND principal_key_id=?',
                           (run_id, ctx.principal.key_id)).fetchone()
        if row is None:
            raise ToolError('not_found', '此凭据没有对应的定时运行', 404)
        if row['status'] == 'acked':
            return _view(row)
        if row['status'] != 'ready':
            raise ToolError('conflict', '仅可确认 ready 状态的定时运行', 409)
        state = row['state']
        if row['job'] == 'monthly_status':
            # Two different events can be ready before either delivery is acknowledged.
            seen = set(_previous(conn, row).get('seen', [])) | set(json.loads(state).get('seen', []))
            state = gate.canonical({'seen': sorted(seen)})
        conn.execute("UPDATE scheduled_runs SET status='acked',acked_at=?,state=? WHERE id=?", (db.now(), state, run_id))
        return _view(_row(conn, run_id))
