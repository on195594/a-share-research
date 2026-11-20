"""Owner form for an evidence-backed NAV interval."""
import json
import re

from fastapi.responses import RedirectResponse
from starlette.datastructures import FormData

from a_share_research import gate
from a_share_research.tools_client import ToolCallError


def _capture_choices(capture, recent):
    source = capture['source']
    points = []
    evidence = {item['sha256']: ' · '.join(filter(None, [item['filename'], item['label']])) or '上传凭证'
                for item in recent}

    def add_point(kind, label, row_id, valuation, hashes):
        title = f'{label} #{row_id}'
        for sha in hashes:
            evidence.setdefault(sha, title)
        if valuation and (stamp := valuation.get('portfolio_value_as_of')):
            points.append(dict(value=f'{kind}:{row_id}:{stamp}', title=title, stamp=stamp,
                               assets=valuation['total_assets'], kind=kind, id=row_id))

    for row in source['account_observations']:
        add_point('observation', '资产观察', row['id'], json.loads(row['observation']), [row['evidence']])
    for row in source['account_batches']:
        batch = json.loads(row['request'])
        add_point('batch', '入账快照', row['id'], batch.get('snapshot'), batch['evidence_hashes'])
    points.sort(key=lambda point: point['stamp'])

    voids = {row['event_id'] for row in source['account_voids']}
    flows = []
    for row in source['account_journal']:
        event = json.loads(row['event'])
        if row['id'] in voids or event['kind'] not in ('withdrawal', 'deposit'):
            continue
        stamp = event['occurred_at']
        before = [point for point in points if point['stamp'] == stamp and (
            point['kind'] == 'observation' or point['id'] < row['batch_id'])]
        flows.append(dict(id=row['id'], label='转入' if event['kind'] == 'deposit' else '转出',
                          cash=event['cash'], stamp=stamp, before=before))
    return dict(account_id=source['account_id'], state_sha256=capture['state_sha256'],
                points=points, flows=flows, evidence=evidence)


def _valuation(value, reason):
    try:
        kind, row_id, stamp = value.split(':', 2)
        return {'kind': kind, 'id': int(row_id), 'reason': reason}, stamp
    except (ValueError, AttributeError):
        raise ValueError('请选择冻结资料中的估值记录。') from None


def _evaluate_arguments(form):
    explanation = form.get('explanation', '')
    start, start_time = _valuation(form.get('start', ''), explanation)
    end, end_time = _valuation(form.get('end', ''), explanation)
    declaration = form.get('declaration')
    if declaration not in ('none', 'listed'):
        raise ValueError('请声明区间内是否有外部出入金。')
    flows = []
    if declaration == 'listed':
        for name, value in form.multi_items():
            if name.startswith('before_') and value:
                try:
                    event_id = int(name.removeprefix('before_'))
                except ValueError:
                    raise ValueError('请选择冻结资料中的出入金事件。') from None
                before, _ = _valuation(value, explanation)
                flows.append({'event_id': event_id, 'before': before})
        if not flows:
            raise ValueError('已声明有出入金，请为资金事件选择同一时点的流前估值；缺少的资金流水或估值须先补录。')
    confirmed = form.get('confirmed') == 'yes'
    return {'account_id': form.get('account_id', ''), 'request': {
        'expected_state_sha256': form.get('state_sha256', ''),
        'start': start, 'end': end, 'flows': flows,
        'coverage': {'start': start_time, 'end': end_time, 'currency': 'CNY', 'scope': 'cash_equity_no_liabilities',
                     'complete_external_flows': confirmed, 'no_security_transfers': confirmed,
                     'evidence_hashes': form.getlist('evidence_hashes'), 'explanation': explanation}}}


async def page(request, session, client, render, form=None):
    values = form if form is not None else FormData()
    context = dict(enabled=client is not None, values=values, errors=[])

    def show():
        return render(request, 'nav_new.html', session, **context)

    if client is None or form is None:
        return show()
    action = values.get('action')
    if action not in ('capture', 'evaluate'):
        context['errors'].append('请选择冻结账户或评估区间。')
        return show()
    if action == 'evaluate':
        try:
            args = _evaluate_arguments(values)
        except ValueError as exc:
            context['errors'].append(str(exc))
        else:
            try:
                request_id = gate.digest(args)
                result = await client.call('account_nav_evaluate', args, request_id=request_id)
                sha = result.get('artifact_sha256')
                if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{64}', sha):
                    raise ToolCallError('unknown', '未取得完整的 NAV 凭证编号，请用原区间与声明重试。')
            except ToolCallError as exc:
                context['errors'].append(str(exc))
                if exc.kind != 'business':
                    context['retry'] = request_id
                    return show()
            else:
                return RedirectResponse('/account/nav/' + sha, status_code=303)

    try:
        capture = await client.call('account_nav_capture', {})
    except ToolCallError as exc:
        context['errors'].append(str(exc))
        return show()
    if action == 'evaluate' and values.get('state_sha256') != capture['state_sha256']:
        context['errors'].append('冻结资料已变化，请重新冻结并核对区间。')
        return show()
    try:
        recent = (await client.call('account_evidence_recent', {}))['evidence']
    except ToolCallError as exc:
        context['errors'].append(str(exc))
        recent = []
    context['frozen'] = _capture_choices(capture, recent)
    return show()
