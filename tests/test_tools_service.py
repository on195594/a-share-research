"""Credential isolation and physical database access modes at the tool boundary."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from requests import ConnectionError as UpstreamConnectionError

from a_share_research import api_keys, companies, db, service
from a_share_research.tools.registry import Arguments, Context, Payload, TOOLS, ToolError, invoke
from a_share_research.tools.server import create_app


@pytest.fixture
def account_enabled():
    return False


@pytest.fixture
def setup(tmp_path, account_enabled):
    path = tmp_path / 'app.db'
    conn = db.connect(path)
    tokens = {role: api_keys.create(conn, role, role=role) for role in ('research', 'assistant', 'owner')}
    conn.close()
    with TestClient(create_app(path, account_shadow_enabled=account_enabled)) as client:
        yield path, tokens, client


def header(token):
    return {'Authorization': 'Bearer ' + token}


def test_research_replays_web_admission_before_cninfo_and_scopes_request_ids(web, tmp_path, monkeypatch):
    company = dict(web.conn.execute("SELECT company_key AS code, name AS zwjc FROM reports WHERE id=39").fetchone())
    monkeypatch.setattr(companies, 'stock_list', lambda: {company['code']: company})
    first = header(api_keys.create(web.conn, 'web-to-tools'))
    second = header(api_keys.create(web.conn, 'second-caller', role='assistant'))
    args = {'code': company['code'], 'request_id': 'x' * 128, 'question': '现金流'}
    legacy = args | {'request_id': 'before-caller-scoping'}
    old_run = service.submit(web.conn, **legacy)
    before = web.conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0]

    with TestClient(web.app, base_url=web.settings.base_url) as browser, TestClient(create_app(tmp_path / 'app.db')) as tools:
        accepted = browser.post('/api/research', json=args, headers=first)
        assert accepted.status_code == 202
        run_id = accepted.json()['run_id']
        assert accepted.json() == {'version': service.VERSION, 'run_id': run_id}
        by_key = {'request_id': args['request_id']}
        assert tools.post('/tools/research_status', json=by_key, headers=second).status_code == 404
        other = tools.post('/tools/research_request', json=args, headers=second).json()['result']['data']
        assert other['run_id'] != run_id
        assert browser.get('/api/request', params=by_key, headers=second).json()['id'] == other['run_id']

        def unavailable():
            raise UpstreamConnectionError('offline directory')

        monkeypatch.setattr(companies, 'stock_list', unavailable)
        assert browser.post('/api/research', json=legacy, headers=first).json()['run_id'] == old_run
        assert browser.get('/api/request', params={'request_id': legacy['request_id']}, headers=first).json() == (
            service.run_view(web.conn, run_id=old_run))
        replay = tools.post('/tools/research_request', json=args, headers=first).json()['result']['data']
        assert (replay['run_id'], replay['status'], replay['path']) == (run_id, 'queued', f'/runs/{run_id}')
        assert replay['created_at'] and replay['gaps'] == []
        assert browser.post('/api/research', json=args, headers=first).json() == accepted.json()
        for changed in ({'question': '分红'}, {'code': '600519'}, {'parent_report_id': 39}):
            conflict = tools.post('/tools/research_request', json=args | changed, headers=first)
            assert conflict.status_code == 409 and conflict.json()['error']['code'] == 'conflict'
        fresh = tools.post('/tools/research_request', json=args | {'request_id': 'new'}, headers=first)
        assert fresh.status_code == 503 and fresh.json()['error']['code'] == 'unavailable'
        for selector in (by_key, {'run_id': run_id}):
            status = tools.post('/tools/research_status', json=selector, headers=first).json()['result']['data']
            assert status['id'] == run_id and status['path'] == replay['path']
    assert web.conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == before + 2


def test_feedback_reuses_web_run_and_rejects_changed_text(web, tmp_path):
    headers = header(api_keys.create(web.conn, 'feedback'))
    args = {'request_id': 'check-claim', 'location': 'summary', 'text': '请核对原文依据。'}
    with TestClient(web.app, base_url=web.settings.base_url) as browser, TestClient(create_app(tmp_path / 'app.db')) as tools:
        original = browser.post('/api/reports/39/feedback', json=args, headers=headers)
        assert original.status_code == 202
        args['report_id'] = 39
        replay = tools.post('/tools/research_feedback', json=args, headers=headers).json()['result']['data']
        assert replay['path'] == f"/runs/{original.json()['run_id']}"
        changed = tools.post('/tools/research_feedback', json=args | {'text': '请核对期间。'}, headers=headers)
        assert changed.status_code == 409 and changed.json()['error']['code'] == 'conflict'


def test_owner_form_replays_the_accepted_short_name_during_directory_outage(web, monkeypatch):
    company = dict(web.conn.execute("SELECT company_key AS code, name AS zwjc FROM reports WHERE id=39").fetchone())
    monkeypatch.setattr(companies, 'stock_list', lambda: {company['code']: company})
    origin = {'Origin': web.settings.base_url}
    with TestClient(web.app, base_url=web.settings.base_url, follow_redirects=False) as browser:
        assert browser.post('/auth/dev', headers=origin).status_code == 303
        form = browser.get('/').context
        args = {'csrf': form['csrf'], 'request_id': form['request_id'], 'q': company['zwjc'][:2], 'again': '1'}
        accepted = browser.post('/research', headers=origin, data=args)
        assert accepted.status_code == 303

        def unavailable():
            raise UpstreamConnectionError('offline directory')

        monkeypatch.setattr(companies, 'stock_list', unavailable)
        replay = browser.post('/research', headers=origin, data=args)
        assert replay.status_code == 303 and replay.headers['location'] == accepted.headers['location']
        assert browser.post('/research', headers=origin, data=args | {'question': '现金流'}).status_code == 409


@pytest.mark.parametrize('account_enabled', [False, True])
def test_discovery_and_call_enforce_same_scope(setup, account_enabled):
    path, tokens, client = setup
    assert client.get('/tools').status_code == 401
    for role, token in tokens.items():
        names = {t['name'] for t in client.get('/tools', headers=header(token)).json()['tools']}
        for name in ('research_cards', 'research_methods'):
            assert (name in names) == (role != 'owner')
        for name in ('account_research_coverage', 'account_portfolio_overview', 'research_company_overview'):
            assert (name in names) == (account_enabled and role == 'assistant')
        for name in ('research_scan_request', 'research_scan_read', 'research_scan_weekly',
                     'research_subscriptions', 'research_subscriptions_replace', 'schedule_run', 'schedule_ack'):
            assert (name in names) == (account_enabled and role == 'assistant')
            if name not in names:
                denied = client.post(f'/tools/{name}', json={}, headers=header(token))
                assert (denied.status_code, denied.json()['error']['code']) == (
                    (403, 'forbidden') if account_enabled else (404, 'not_found'))
        response = client.post('/tools/account_research_coverage', json={}, headers=header(token))
        expected = (409 if role == 'assistant' else 403) if account_enabled else 404
        assert response.status_code == expected
        if expected == 409:
            assert response.json()['error']['code'] == 'conflict'
    conn = db.connect(path)
    conn.execute('DELETE FROM api_keys WHERE role=?', ('research',))
    conn.close()
    assert client.get('/tools', headers=header(tokens['research'])).status_code == 401


def test_method_search_refuses_owner_and_blank_query(setup):
    _, tokens, client = setup
    endpoint = '/tools/research_methods'
    assert client.post(endpoint, json={'query': '低PE'}, headers=header(tokens['owner'])).status_code == 403
    invalid = client.post(endpoint, json={'query': '  '}, headers=header(tokens['research']))
    assert invalid.status_code == 422
    assert 'query' in invalid.json()['error']['message']


def test_validation_errors_identify_field_and_reason(setup):
    _, tokens, client = setup
    headers = header(tokens['assistant'])
    for payload, path, reason in (({'report_id': True}, 'report_id', 'integer'),
                                  ([], 'body', 'JSON 对象')):
        response = client.post('/tools/research_report', json=payload, headers=headers)
        assert response.status_code == 422
        error = response.json()['error']
        assert error['code'] == 'invalid_arguments'
        assert path in error['message'] and reason in error['message']
    assert client.post('/tools/missing', json={}, headers=headers).status_code == 404
    assert client.post('/tools/research_report', json={'report_id': 999}, headers=headers).status_code == 404


def test_service_connection_enforces_declared_access_with_account_disabled(setup, monkeypatch):
    _, tokens, client = setup
    class Submission(Arguments):
        question: str

    def submit(ctx, args):
        run_id = service.submit(ctx.connection, '600519', 'lost-http-response', question=args.question)
        return Payload(data={'run_id': run_id})

    tool = replace(TOOLS['research_cards'], name='test_submit', arguments=Submission, handler=submit)
    monkeypatch.setitem(TOOLS, tool.name, tool)
    headers = header(tokens['research'])
    args = {'question': '现金流'}
    response = client.post('/tools/test_submit', json=args, headers=headers)
    assert response.status_code == 503
    monkeypatch.setitem(TOOLS, tool.name, replace(tool, access='write'))
    saved = client.post('/tools/test_submit', json=args, headers=headers)
    assert saved.status_code == 200
    assert client.post('/tools/test_submit', json=args, headers=headers).json() == saved.json()
    conflict = client.post('/tools/test_submit', json={'question': '估值'}, headers=headers)
    assert conflict.status_code == 409 and conflict.json()['error']['code'] == 'conflict'


def test_in_process_dispatch_cannot_bypass_permissions(setup):
    path, tokens, _ = setup
    conn = db.connect(path)
    try:
        ctx = Context(conn, path, api_keys.authenticate(conn, tokens['research']), account_shadow_enabled=True)
        with pytest.raises(ToolError, match='凭据'):
            invoke(ctx, 'account_research_coverage', {})
        revoked = api_keys.authenticate(conn, tokens['assistant'])
        conn.execute('DELETE FROM api_keys WHERE id=?', (revoked.key_id,))
        with pytest.raises(ToolError, match='撤销') as error:
            invoke(Context(conn, path, revoked, account_shadow_enabled=True), 'account_research_coverage', {})
        assert error.value.status == 403
    finally:
        conn.close()


@pytest.mark.parametrize('error', [service.RequestError('暂时无法连接巨潮资讯，请稍后重试', 503),
                                   UpstreamConnectionError('connection unavailable')])
def test_upstream_failure_is_retryable(setup, monkeypatch, error):
    _, tokens, client = setup

    def unavailable(conn):
        raise error

    monkeypatch.setattr(service, 'company_cards', unavailable)
    response = client.post('/tools/research_cards', headers=header(tokens['research']), json={})
    assert response.status_code == 503
    assert response.json()['error']['code'] == 'unavailable'
    assert '重试' in response.json()['error']['message']


def test_invoke_rechecks_grant_revoked_after_authentication(setup):
    path, tokens, _ = setup
    conn = db.connect(path)
    try:
        principal = api_keys.authenticate(conn, tokens['research'])
        api_keys.revoke(conn, principal.key_id)
        with pytest.raises(ToolError, match='撤销') as error:
            invoke(Context(conn, path, principal), 'research_cards', {})
        assert error.value.status == 403
    finally:
        conn.close()


def test_tools_write_connection_keeps_full_synchronous_and_rechecks_revoke_in_tx(tmp_path, monkeypatch):
    from contextlib import closing

    path = tmp_path / 'app.db'
    with closing(db.connect(path)) as conn:
        token = api_keys.create(conn, 'assistant', role='assistant')
        principal = api_keys.authenticate(conn, token)
        conn.execute("INSERT INTO subscriptions(code, origin, added_at) VALUES ('600519', 'external', '2026-11-24T00:00:00+08:00')")

    sync_levels = []
    tool = TOOLS['research_cards']
    monkeypatch.setitem(TOOLS, 'test_sync', replace(
        tool, name='test_sync', access='write',
        handler=lambda ctx, args: sync_levels.append(ctx.connection.execute('PRAGMA synchronous').fetchone()[0]) or Payload(data={}),
    ))

    def revoke_during_catalog():
        with closing(db.connect(path)) as other:
            api_keys.revoke(other, principal.key_id)
        return {'000858': {'code': '000858', 'zwjc': '五粮液'}}

    monkeypatch.setattr(companies, 'stock_list', revoke_during_catalog)
    with TestClient(create_app(path, account_shadow_enabled=True)) as client:
        assert client.post('/tools/test_sync', json={}, headers=header(token)).status_code == 200
        assert sync_levels == [2]
        # Grant is revoked while fetching stock_list; the write transaction must still reject.
        rejected = client.post('/tools/research_subscriptions_replace', json={'codes': ['000858']}, headers=header(token))
        assert rejected.status_code == 403
    with closing(db.connect(path)) as check:
        assert [r[0] for r in check.execute('SELECT code FROM subscriptions').fetchall()] == ['600519']
