"""Compact read models shared by authenticated owner and assistant entry points."""
import json
import sqlite3

from a_share_research import db, reading, scans, service, stock_codes
from a_share_research.account import analysis, mirror, nav_refs, projection_ids
from a_share_research.evidence import safe_url
from a_share_research.policy import account_view, registry as policies


def _positions(conn, source):
    account_id = source['watermark']['account_id']
    row = conn.execute('SELECT projection FROM account_position_books WHERE account_id=?', (account_id,)).fetchone()
    ids = projection_ids.lookup(conn, account_id, 'cycle')
    positions = {}
    for cycle in json.loads(row['projection'])['cycles']:
        if cycle['exit_date'] is not None:
            continue
        hid = ids[cycle['cycle_id']]
        # verified_current already checked this exact account/cycle revision in this transaction.
        revision = analysis.latest_revision(conn, hid, account_id=account_id)
        theses = json.loads(revision['snapshot'])['holding_thesis_versions']
        positions[cycle['code']] = {
            **{k: cycle[k] for k in ('cycle_id', 'name', 'shares', 'cost_price', 'buy_date')},
            'holding_id': hid, 'revision_id': revision['id'], 'revision_at': revision['created_at'],
            'theses': [{k: item[k] for k in ('version', 'l1', 'l2', 'created_at')}
                       for item in theses if item['status'] == 'active'],
        }
    return positions


def _saved_monitor(conn, evaluation_id=None):
    # A historical deep link keeps the same account and capture constraints as the latest summary.
    query = """
        SELECT e.* FROM policy_evaluations e JOIN account_source s
          ON json_extract(e.capture_context, '$.source.watermark.account_id') = json_extract(s.watermark, '$.account_id')
         AND json_extract(e.capture_context, '$.source.watermark.account_scope') = json_extract(s.watermark, '$.account_scope')
        WHERE s.id=1 AND s.mode='ledger' AND json_extract(e.capture_context, '$.source.mode')='ledger'
          AND json_extract(e.capture_context, '$.request.action')='policy.monitor'
    """
    if evaluation_id is None:
        row = conn.execute(query + ' ORDER BY e.created_at DESC, e.id DESC LIMIT 1').fetchone()
    else:
        row = conn.execute(query + ' AND e.id=?', (evaluation_id,)).fetchone()
    return policies._evaluation(row) if row else None


def monitor_detail(conn, evaluation_id):
    try:
        saved = _saved_monitor(conn, evaluation_id)
    except policies.PolicyError as exc:
        raise service.RequestError(str(exc), 409) from exc
    if saved is None:
        raise service.RequestError('绑定账户没有这份监控评估', 404)
    return saved


def receipt_detail(conn, receipt_id):
    row = conn.execute("""
        SELECT b.receipt FROM account_batches b JOIN account_source s
          ON b.account_id=json_extract(s.watermark, '$.account_id')
        WHERE s.id=1 AND s.mode='ledger' AND b.id=?
    """, (receipt_id,)).fetchone()
    if row is None:
        raise service.RequestError('绑定账户没有这份对账回执', 404)
    return json.loads(row[0])


def _receipt_refs(conn, account_id):
    query = 'SELECT id, created_at, status FROM account_batches WHERE account_id=?'
    order = ' ORDER BY id DESC LIMIT 1'
    latest = conn.execute(query + order, (account_id,)).fetchone()
    posted = latest
    if latest and latest['status'] != 'posted':
        posted = conn.execute(query + " AND status='posted'" + order, (account_id,)).fetchone()
    return {
        'latest_receipt': dict(latest, path=f"/account/receipts/{latest['id']}") if latest else None,
        'last_receipt': {'id': posted['id'], 'created_at': posted['created_at'],
                         'path': f"/account/receipts/{posted['id']}"} if posted else None,
    }


def _nav_summary(row):
    summary = {k: row[k] for k in ('artifact_sha256', 'start_date', 'end_date', 'benchmark_code', 'created_at')}
    return dict(summary, path=f"/account/nav/{row['artifact_sha256']}")


def nav_detail(conn, artifact_sha256):
    with db.transaction(conn, immediate=False):
        try:
            account_id = account_view.bound_account_id(conn)
        except policies.PolicyError as exc:
            raise service.RequestError(str(exc), 404) from exc
        row = conn.execute('SELECT * FROM account_nav_refs WHERE account_id=? AND artifact_sha256=?',
                           (account_id, artifact_sha256)).fetchone()
    if row is None:
        raise service.RequestError('绑定账户没有这份 NAV 区间凭证', 404)
    try:
        result = nav_refs.read_result(row, mirror.target_directory(conn) / 'account-evidence')
    except (ValueError, TypeError, KeyError) as exc:
        raise service.RequestError(f'NAV 区间凭证完整性核验失败：{exc}', 409) from exc
    return _nav_summary(row) | {'result': result}


def _policy(saved, holding=None):
    if saved is None:
        return None
    result = saved['result']
    reference = {k: saved[k] for k in ('id', 'identity', 'activation_id', 'created_at')}
    reference.update(as_of=result['as_of'], account_version=saved['capture_context']['source']['account_version'],
                     path=f"/policy/evaluations/{saved['id']}", policy_path='/policy')
    if holding is None:
        return reference | {k: result[k] for k in ('action_status', 'review_status', 'data_status')} | {
            'escalations': [e for e in result['escalations'] if e['code'] is None],
            'data_gaps': [g for g in result['data_gaps'] if g['code'] is None]}
    captured = next((h for h in result['holdings'] if h['id'] == holding['holding_id']), None)
    if captured is None:
        return None
    return reference | {'stop_risk_pct': captured['stop_risk_pct'],
                        'escalations': [e for e in result['escalations'] if e['code'] == captured['code']],
                        'data_gaps': [g for g in result['data_gaps'] if g['code'] == captured['code']]}


def _announcements(events, reviews, path):
    reviews = {e['id']: e['review'] for e in reviews}
    events = sorted(events, key=lambda e: (e['announced'], e['id']), reverse=True)
    announcements = []
    for index, event in enumerate(events):
        review = reviews.get(event['id'])
        unresolved = review['state'] not in scans.RESOLVED if review else event['action'] == 'no_card'
        if index and not unresolved:
            continue
        event_path = path
        if review and review['report_id']:
            event_path = f"/reports/{review['report_id']}"
        elif event['run_id']:
            event_path = f"/runs/{event['run_id']}"
        announcements.append({k: event[k] for k in ('id', 'scan_id', 'title', 'kind', 'announced', 'action')} | {
            'review': {k: review[k] for k in ('state', 'label', 'report_id')} if review else None,
            'unresolved': unresolved, 'path': event_path, 'source_url': safe_url(event['url'])})
    return announcements


def _company(conn, code, held, position, saved, history):
    events = scans.events(conn, code=code)
    reviews = scans.event_reviews(conn, code, company_events=events, include_no_card=True)
    card = scans.card_or_none(conn, code, history=history, reviews=reviews)
    gaps = []
    research = None
    path = f'/stock/{code}' if card else f'/stock/{code}/overview'
    if card:
        decision = reading.stance_decision(card['stance'], card['stance_calc'], card['valuation_calc'])
        research = {k: card[k] for k in ('report_id', 'period', 'completed_at', 'verdict', 'status', 'summary')}
        research['current'] = {k: card['current'][k] for k in ('last_close_date', 'current_zone')}
        research.update(stance={k: decision[k] for k in ('action', 'text', 'hurdle', 'valid_until')} if decision else None,
                        zone_note=card['current'].get('note'), path=f"/reports/{card['report_id']}")
        if card['check_failure']:
            gaps.append(card['check_failure']['reason'])
    elif held:
        gaps.append('暂无通过复核的研究报告')
    if failure := history.announcements.get(code):
        gaps.append(failure['reason'])

    policy = _policy(saved, position) if position else None
    if position and not position['theses']:
        gaps.append('本次持仓周期缺少在仓论点')
    if held and policy is None:
        gaps.append('本次持仓周期暂无可引用的监控评估')
    return dict(code=code, name=card['name'] if card else (position or {}).get('name'), path=path,
                evidence_path=f'/stock/{code}/overview#evidence-{code}',
                held=held, position=position, research=research, announcements=_announcements(events, reviews, path),
                policy=policy, gaps=gaps)


def _overview(conn, code=None):
    account_gaps = []
    with db.transaction(conn, immediate=False):
        # An unverified account is unknown, including when public company research exists.
        try:
            current = account_view.verified_current(conn, mirror.target_directory(conn), verify_evidence=False)
        except policies.PolicyError as exc:
            if exc.status != 409:
                raise
            current = None
            account_gaps.append(str(exc))
        source = current['source'] if current else None
        account = None
        account_id = None
        positions = {}
        saved = None
        if source:
            account = dict(mode=source['mode'], source_updated_at=source['updated_at'], path='/policy#policy-source')
            if source['mode'] == 'ledger':
                account_id = source['watermark']['account_id']
                opening = conn.execute('SELECT opened_at FROM account_books WHERE account_id=?', (account_id,)).fetchone()
                account.update(account_id=account_id, version=source['account_version'], opened_at=opening['opened_at'])
                account.update(_receipt_refs(conn, account_id))
                positions = _positions(conn, source)
                try:
                    saved = _saved_monitor(conn)
                except policies.PolicyError as exc:
                    account_gaps.append(str(exc))
                else:
                    if saved is None:
                        account_gaps.append('暂无绑定账户的已保存监控评估')
            else:
                account_gaps.append('当前来源为已核实镜像，缺少绑定账本的持仓周期')
        elif conn.execute("SELECT 1 FROM account_source WHERE id=1 AND mode='ledger'").fetchone():
            # A committed interval survives gaps in the current position projection.
            try:
                account_id = account_view.bound_account_id(conn)
            except policies.PolicyError as exc:
                account_gaps.append(str(exc))
        nav_interval = None
        if account_id is not None:
            row = conn.execute('SELECT artifact_sha256,start_date,end_date,benchmark_code,created_at '
                               'FROM account_nav_refs WHERE account_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1',
                               (account_id,)).fetchone()
            if row:
                nav_interval = _nav_summary(row)
        if nav_interval is None:
            account_gaps.append('暂无可查询的 NAV 区间凭证')
        history = scans.ScanHistory(conn) if code is not None or (current and current['codes']) else None
        if code is not None:
            held = code in current['codes'] if current else None
            result = _company(conn, code, held, positions.get(code), saved, history)
            return result | {'account': account, 'nav_interval': nav_interval, 'account_gaps': account_gaps}
        return dict(account=account, policy=_policy(saved), nav_interval=nav_interval, gaps=account_gaps,
                    holdings=[_company(conn, key, True, positions.get(key), saved, history)
                              for key in current['codes']] if current else None)


def company_overview(conn: sqlite3.Connection, code: str) -> dict:
    try:
        code = stock_codes.normalize_key(code)
    except ValueError as exc:
        raise service.RequestError(str(exc)) from exc
    return _overview(conn, code)


def portfolio_overview(conn: sqlite3.Connection) -> dict:
    return _overview(conn)
