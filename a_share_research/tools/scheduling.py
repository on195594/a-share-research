"""Principal-scoped scheduled captures and delivery acknowledgements."""
from datetime import date, datetime, timedelta
import fcntl
import json
import logging
import re

from requests import RequestException

from a_share_research import db, gate, scans, service
from a_share_research.market import redact_error
from a_share_research.policy import account_view, benchmark, daily_brief, monitor, registry, stop_alert
from a_share_research.tools.registry import Context, ToolError, domain_error

log = logging.getLogger(__name__)
_SLOT_FORMAT = {
    'stop_alert': 'YYYY-MM-DDTHH:MM（5 分钟整点）', 'daily_brief': 'YYYY-MM-DD',
    'advisor_scan': 'YYYY-MM-DD', 'benchmark': 'YYYY-MM', 'advisor_weekly': 'YYYY-Www',
}


def _slot_start(job, slot, now):
    try:
        if job == 'stop_alert':
            start = datetime.fromisoformat(slot)
            canonical = start.isoformat(timespec='minutes')
            if start.tzinfo is not None or start.minute % 5:
                raise ValueError
        else:
            day = date.fromisoformat(slot + '-01' if job == 'benchmark' else slot)
            start = datetime.combine(day, datetime.min.time())
            if job == 'advisor_weekly':
                year, week, _ = day.isocalendar()
                canonical = f'{year:04d}-W{week:02d}'
            else:
                canonical = day.isoformat()[:7] if job == 'benchmark' else day.isoformat()
        if canonical != slot:
            raise ValueError
    except ValueError:
        raise ToolError('invalid_arguments', f'{job} 的 slot 须为上海时间 {_SLOT_FORMAT[job]}', 422) from None
    start = start.replace(tzinfo=db.SHANGHAI)
    if start > now:
        raise ToolError('invalid_arguments', 'slot 不能是未来的上海时段', 422)
    return start


def _expired(job, start, now, status, scan_id=None):
    if job == 'stop_alert':
        return now - start > timedelta(minutes=15)
    if job == 'advisor_scan' and scan_id is not None:
        # A submitted scan is delivered late rather than dropped: the next scan treats its
        # notices as already sent.
        return False
    return job in ('daily_brief', 'advisor_scan') and status != 'ready' and start.date() != now.date()


def _expire(conn, row):
    text = '' if row['job'] == 'stop_alert' else f"{row['slot']} 的定时任务已跨日过期，本次未推送原时段正文。\n"
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


def _accept(ctx, job, slot, start, now):
    conn = ctx.connection
    with db.transaction(conn):
        registry._authorize(conn, ctx.principal)
        key = (ctx.principal.key_id, job, slot)
        row = conn.execute('SELECT * FROM scheduled_runs WHERE principal_key_id=? AND job=? AND slot=?', key).fetchone()
        if row is None:
            cur = conn.execute("""INSERT INTO scheduled_runs (principal_key_id,job,slot,status,created_at)
                                  VALUES (?,?,?,'running',?)""", (*key, now.isoformat()))
            row = _row(conn, cur.lastrowid)
        if row['status'] in ('acked', 'expired'):
            return row
        if _expired(job, start, now, row['status'], row['scan_id']):
            _expire(conn, row)
        elif row['status'] == 'failed':
            conn.execute("""UPDATE scheduled_runs SET status='running',attempts=attempts+1,
                            activation_id=NULL,evaluation_id=NULL,finished_at=NULL,error_code=NULL,error=NULL,
                            state=NULL,result=NULL,text=NULL WHERE id=?""", (row['id'],))
        return _row(conn, row['id'])


def _previous(conn, row):
    query = "SELECT state FROM scheduled_runs WHERE principal_key_id=? AND job=? AND status='acked'"
    args = [row['principal_key_id'], row['job']]
    if row['job'] == 'stop_alert':
        day = row['slot'][:10]
        query += ' AND slot BETWEEN ? AND ?'
        args.extend([day + 'T00:00', day + 'T23:55'])
    found = conn.execute(query + ' ORDER BY slot DESC LIMIT 1', args).fetchone()
    return json.loads(found[0]) if found else {}


def _evaluate(ctx, row):
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
    args = (conn, ctx.principal, ctx.database, request, activation_id)
    if job == 'stop_alert':
        return stop_alert.run(*args, previous)
    if job == 'daily_brief':
        return daily_brief.run(*args)
    if job == 'benchmark':
        return benchmark.run(*args, row['created_at'][:10])
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
        result.update(weekly=scans.weekly(conn), opportunity_costs=[])
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


def _complete(ctx, row, start):
    conn = ctx.connection
    job = row['job']
    state, result, text = {}, {}, ''
    error_code = error = evaluation_id = None
    try:
        advisor = job in ('advisor_scan', 'advisor_weekly')
        if advisor and not _advisor_inputs(ctx, row, result):
            return
        receipt = _evaluate(ctx, row)
        evaluation_id = receipt['id']
        evaluated = receipt['result']
        if advisor:
            kind, field = (('research_opportunity', 'opportunities') if job == 'advisor_scan'
                           else ('opportunity_cost', 'opportunity_costs'))
            seen = set(_previous(conn, row).get('seen', []))
            current = [(_opportunity_key(p), p) for p in evaluated['review_prompts'] if p['kind'] == kind]
            result[field] = [p for key, p in current if key not in seen]
            state = {'seen': sorted(key for key, _ in current)}
        elif evaluated['exit_code']:
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
        conn.execute("""UPDATE scheduled_runs SET status=?,evaluation_id=?,finished_at=?,error_code=?,error=?,
                        state=?,result=?,text=? WHERE id=?""",
                     ('failed' if error_code else 'ready', evaluation_id, db.now(), error_code, error,
                      gate.canonical(state), gate.canonical(result), text, row['id']))
        if not error_code and _expired(job, start, datetime.fromisoformat(db.now()), 'running', row['scan_id']):
            _expire(conn, row)


def _backlog(ctx, job, slot):
    """An earlier scan still awaiting its result comes back before today's slot."""
    if job != 'advisor_scan':
        return slot
    found = ctx.connection.execute(
        """SELECT slot FROM scheduled_runs WHERE principal_key_id=? AND job=? AND slot<? AND status='running'
           AND scan_id IS NOT NULL ORDER BY slot LIMIT 1""", (ctx.principal.key_id, job, slot)).fetchone()
    return found[0] if found else slot


def run(ctx: Context, job: str, slot: str) -> dict:
    _slot_start(job, slot, datetime.fromisoformat(db.now()))
    # No SQLite write lock across collection. flock also releases on process death.
    lock = ctx.database.parent / f'.schedule-{ctx.principal.key_id}-{job}.lock'
    with lock.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        slot = _backlog(ctx, job, slot)
        start = _slot_start(job, slot, datetime.fromisoformat(db.now()))
        row = _accept(ctx, job, slot, start, datetime.fromisoformat(db.now()))
        if row['status'] == 'running':
            _complete(ctx, row, start)
        registry._authorize(ctx.connection, ctx.principal)
        return _view(_row(ctx.connection, row['id']))


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
        conn.execute("UPDATE scheduled_runs SET status='acked',acked_at=? WHERE id=?", (db.now(), run_id))
        return _view(_row(conn, run_id))
