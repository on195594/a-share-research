"""Single-trade HTML form. The original form snapshot is sufficient to replay a submit."""
import json
import re
import uuid
from urllib.parse import urlencode

from starlette.datastructures import FormData
from starlette.responses import RedirectResponse

from a_share_research.tools_client import ToolCallError


def _integer(value):
    # HTML submits strings. Let the tool report its own field path for invalid values.
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def _arguments(form):
    code = form.get('code', '')
    event = {key: form.get(key, '') for key in ('kind', 'code', 'occurred_at', 'price', 'fees')}
    event['shares'] = _integer(form.get('shares'))
    positions = {key.removeprefix('ending.'): _integer(form[key]) for key in form if key.startswith('ending.')}
    market_values = {stock: form.get('value.' + stock, '') for stock in positions}
    positions[code] = _integer(form.get('trade_ending'))
    market_values[code] = form.get('trade_value', '')
    snapshot = dict(positions=positions, market_values=market_values,
                    cash=form.get('cash', ''), total_assets=form.get('total_assets', ''))
    if stamp := form.get('as_of'):
        snapshot['portfolio_value_as_of'] = stamp + '+08:00'
    batch = dict(expected_version=_integer(form.get('expected_version')), events=[event], snapshot=snapshot,
                 evidence_hashes=sorted(form.getlist('evidence_hashes')))
    if form.get('kind') == 'buy' and form.get('policy_identity') and 'opening.' + code not in form:
        entry = {'policy_identity': form['policy_identity']}
        if form.get('framework'):
            entry['framework'] = form['framework']
        batch['position_entries'] = {'0': entry}
    return {'account_id': form.get('account_id', ''), 'batch': batch}


def _initial(state):
    fields = {'account_id': state['account_id'], 'expected_version': str(state['version']),
              'opening_cash': state['projection']['cash'], 'cost_basis': str(int(state['cost_basis_configured'])),
              'request_id': uuid.uuid4().hex, 'kind': 'buy', 'fees': '0'}
    for code, shares in state['projection']['positions'].items():
        fields['opening.' + code] = fields['ending.' + code] = str(shares)
    return FormData(fields)


async def page(request, session, client, render, form=None, *, connection):
    fields = form if form is not None else FormData()
    restored = fields.get('intent') == 'restore'
    errors, recent = [], []
    receipt = None
    fresh_version = None
    retry_only = restored and fields.get('awaiting_reply') == '1'
    enabled = client is not None
    if enabled and (form is None or restored):
        try:
            state = await client.call('account_state', {'account_id': fields.get('account_id', '')} if restored else {},
                                      read_only=True)
            fresh_version = str(state['version'])
            if form is None:
                fields = _initial(state)
        except ToolCallError as exc:
            errors.append(str(exc))
            enabled = False
    elif enabled:
        request_id = fields.get('request_id', '')
        if not re.fullmatch(r'[a-f0-9]{32}', request_id):
            errors.append('请求编号无效，请重新打开记账表单。')
        else:
            try:
                result = await client.call('account_submit', _arguments(fields), request_id=request_id)
                receipt_id = result.get('receipt_id')
                if (type(receipt_id) is not int or receipt_id <= 0 or
                        result.get('status') not in ('posted', 'conflict', 'missing_material', 'service_failure')):
                    raise ToolCallError('unknown', '未收到完整对账回执，结果未知。请用原编号和原内容重试。')
                receipt = result
            except ToolCallError as exc:
                errors.append(str(exc))
                retry_only = exc.kind == 'unknown' or (exc.kind == 'unavailable' and fields.get('awaiting_reply') == '1')
            else:
                if receipt['status'] == 'posted':
                    url = f"/account/receipts/{receipt['receipt_id']}?" + urlencode({'request': request_id})
                    return RedirectResponse(url, status_code=303)
    if enabled and not retry_only:
        try:
            recent = (await client.call('account_evidence_recent', {}, read_only=True))['evidence']
        except ToolCallError as exc:
            errors.append(str(exc))
    selected = fields.getlist('evidence_hashes')
    recent_shas = {item['sha256'] for item in recent}
    recent.extend({'sha256': sha} for sha in selected if sha not in recent_shas)
    holdings = {key.removeprefix('opening.'): fields[key] for key in fields if key.startswith('opening.')}
    profiles = []
    if enabled and fields.get('cost_basis') == '1':
        rows = connection.execute('SELECT identity,created_at,parameters FROM policy_v0_profiles ORDER BY created_at DESC')
        profiles = [{'identity': identity, 'created_at': stamp, 'frameworks': list(json.loads(parameters)['stops'])}
                    for identity, stamp, parameters in rows]
    return render(request, 'ledger_new.html', session, profiles=profiles, enabled=enabled, configured=client is not None,
                  fields=fields, holdings=holdings, selected=selected, recent=recent, errors=errors,
                  receipt=receipt, fresh_version=fresh_version, restored=restored, fresh=form is None,
                  retry_only=retry_only)
