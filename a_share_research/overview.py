"""Compact read models shared by authenticated owner and assistant entry points."""
import json
import sqlite3

from a_share_research import db, reading, scans, service, stock_codes
from a_share_research.account import analysis, mirror, projection_ids
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


def _announcements(conn, code, path):
    reviews = {e['id']: e['review'] for e in scans.event_reviews(conn, code)}
    events = sorted(scans.events(conn, code=code), key=lambda e: (e['announced'], e['id']), reverse=True)
    announcements = []
    for index, event in enumerate(events):
        review = reviews.get(event['id'])
        # A no-card disclosure has no review that could close it.
        unresolved = event['action'] == 'no_card' or (review and review['state'] not in scans.RESOLVED)
        if index and not unresolved:
            continue
        event_path = path
        if review and review['report_id']:
            event_path = f"/reports/{review['report_id']}"
        elif event['run_id']:
            event_path = f"/runs/{event['run_id']}"
        announcements.append({k: event[k] for k in ('id', 'scan_id', 'title', 'kind', 'announced', 'action')} | {
            'review': {k: review[k] for k in ('state', 'label', 'report_id')} if review else None,
            'path': event_path})
    return announcements


def _company(conn, code, held, position, saved):
    card = scans.card_or_none(conn, code)
    gaps = []
    research = None
    path = f'/stock/{code}' if card else f'/stock/{code}/overview'
    if card:
        decision = reading.stance_decision(card['stance'], card['stance_calc'], card['valuation_calc'])
        research = {k: card[k] for k in ('report_id', 'period', 'completed_at', 'verdict', 'status')}
        research['current'] = {k: card['current'][k] for k in ('last_close_date', 'current_zone')}
        research.update(stance={k: decision[k] for k in ('action', 'text', 'hurdle', 'valid_until')} if decision else None,
                        path=f"/reports/{card['report_id']}")
        if card['check_failure']:
            gaps.append(card['check_failure']['reason'])
    else:
        gaps.append('暂无通过复核的研究报告')
    if failure := scans.announcement_failure(conn, code):
        gaps.append(failure['reason'])

    policy = _policy(saved, position) if position else None
    if position and not position['theses']:
        gaps.append('本次持仓周期缺少在仓论点')
    if held and policy is None:
        gaps.append('本次持仓周期暂无可引用的监控评估')
    return dict(code=code, name=card['name'] if card else (position or {}).get('name'), path=path,
                held=held, position=position, research=research, announcements=_announcements(conn, code, path),
                policy=policy, gaps=gaps)


def _overview(conn, code=None):
    gaps = []
    with db.transaction(conn, immediate=False):
        # An unverified account is unknown, including when public company research exists.
        try:
            current = account_view.verified_current(conn, mirror.target_directory(conn))
        except policies.PolicyError as exc:
            if exc.status != 409:
                raise
            current = None
            gaps.append(str(exc))
        source = current['source'] if current else None
        account = None
        positions = {}
        saved = None
        if source:
            account = dict(mode=source['mode'], source_updated_at=source['updated_at'], path='/policy#policy-source')
            if source['mode'] == 'ledger':
                account_id = source['watermark']['account_id']
                opening = conn.execute('SELECT opened_at FROM account_books WHERE account_id=?', (account_id,)).fetchone()
                account.update(account_id=account_id, version=source['account_version'], opened_at=opening['opened_at'])
                receipt = conn.execute("SELECT id, created_at FROM account_batches WHERE account_id=? AND status='posted' "
                                       "ORDER BY id DESC LIMIT 1", (account_id,)).fetchone()
                account['last_receipt'] = dict(receipt, path=f"/account/receipts/{receipt['id']}") if receipt else None
                positions = _positions(conn, source)
                try:
                    saved = _saved_monitor(conn)
                except policies.PolicyError as exc:
                    gaps.append(str(exc))
                else:
                    if saved is None and code is None:
                        gaps.append('暂无绑定账户的已保存监控评估')
            else:
                gaps.append('当前来源为已核实镜像，缺少绑定账本的持仓周期')
        if code is not None:
            held = code in current['codes'] if current else None
            result = _company(conn, code, held, positions.get(code), saved)
            result['gaps'].extend(gaps)
            return result | {'account': account}
        gaps.append('暂无可查询的 NAV 区间凭证')
        return dict(account=account, policy=_policy(saved), gaps=gaps,
                    holdings=[_company(conn, key, True, positions.get(key), saved)
                              for key in current['codes']] if current else None)


def company_overview(conn: sqlite3.Connection, code: str) -> dict:
    try:
        code = stock_codes.normalize_key(code)
    except ValueError as exc:
        raise service.RequestError(str(exc)) from exc
    return _overview(conn, code)


def portfolio_overview(conn: sqlite3.Connection) -> dict:
    return _overview(conn)
