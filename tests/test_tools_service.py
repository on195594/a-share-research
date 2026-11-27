"""Credential isolation and physical database access modes at the tool boundary."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from requests import ConnectionError as UpstreamConnectionError

from a_share_research import api_keys, companies, db, service
from a_share_research.tools.registry import Arguments, Context, Payload, TOOLS, ToolError, invoke
from a_share_research.tools.server import create_app
from support.mcp import ORIGIN, RESOURCE, list_tools, protocol_error, rpc, tool_data, tool_error


@pytest.fixture(autouse=True)
def public_mcp(monkeypatch):
    monkeypatch.setenv('ASHARE_PUBLIC_MCP_URL', RESOURCE)


@pytest.fixture
def account_enabled():
    return False


@pytest.fixture
def setup(tmp_path, account_enabled):
    path = tmp_path / 'app.db'
    conn = db.connect(path)
    tokens = {role: api_keys.create(conn, role, role=role) for role in ('research', 'assistant', 'owner')}
    conn.close()
    with TestClient(create_app(path, account_shadow_enabled=account_enabled), base_url=ORIGIN) as client:
        yield path, tokens, client


def test_research_replays_admission_before_cninfo_and_scopes_request_ids(web, tmp_path, monkeypatch):
    company = dict(web.conn.execute("SELECT company_key AS code, name AS zwjc FROM reports WHERE id=39").fetchone())
    monkeypatch.setattr(companies, 'stock_list', lambda: {company['code']: company})
    first = api_keys.create(web.conn, 'first-caller')
    second = api_keys.create(web.conn, 'second-caller', role='assistant')
    args = {'code': company['code'], 'request_id': 'x' * 128, 'question': '现金流'}
    # Accepted before request IDs were scoped by caller; any caller may still recover it.
    legacy = args | {'request_id': 'before-caller-scoping'}
    old_run = service.submit(web.conn, **legacy)
    before = web.conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0]

    with TestClient(create_app(tmp_path / 'app.db'), base_url=ORIGIN) as tools:
        accepted = tool_data(tools, first, 'research_request', args)['data']
        run_id = accepted['run_id']
        assert (accepted['status'], accepted['path']) == ('queued', f'/runs/{run_id}')
        by_key = {'request_id': args['request_id']}
        assert tool_error(tools, second, 'research_status', by_key)['code'] == 'not_found'
        other = tool_data(tools, second, 'research_request', args)['data']
        assert other['run_id'] != run_id
        assert tool_data(tools, second, 'research_status', by_key)['data']['id'] == other['run_id']

        def unavailable():
            raise UpstreamConnectionError('offline directory')

        monkeypatch.setattr(companies, 'stock_list', unavailable)
        assert tool_data(tools, first, 'research_request', legacy)['data']['run_id'] == old_run
        recovered = tool_data(tools, first, 'research_status', {'request_id': legacy['request_id'], 'compact': False})
        assert recovered['data'] == service.run_view(web.conn, run_id=old_run) | service.run_link(old_run)
        replay = tool_data(tools, first, 'research_request', args)['data']
        assert (replay['run_id'], replay['status'], replay['path']) == (run_id, 'queued', f'/runs/{run_id}')
        assert replay['created_at'] and replay['gaps'] == []
        for changed in ({'question': '分红'}, {'code': '600519'}, {'parent_report_id': 39}):
            assert tool_error(tools, first, 'research_request', args | changed)['code'] == 'conflict'
        assert tool_error(tools, first, 'research_request', args | {'request_id': 'new'})['code'] == 'unavailable'
        for selector in (by_key, {'run_id': run_id}):
            status = tool_data(tools, first, 'research_status', selector)['data']
            assert status['id'] == run_id and status['path'] == replay['path']
    assert web.conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == before + 2


def test_feedback_replays_original_run_and_rejects_changed_text(web, tmp_path):
    token = api_keys.create(web.conn, 'feedback')
    args = {'report_id': 39, 'request_id': 'check-claim', 'location': 'summary', 'text': '请核对原文依据。'}
    before = web.conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0]
    with TestClient(create_app(tmp_path / 'app.db'), base_url=ORIGIN) as tools:
        original = tool_data(tools, token, 'research_feedback', args)['data']
        assert original['path'] == f"/runs/{original['run_id']}"
        replay = tool_data(tools, token, 'research_feedback', args)['data']
        assert (replay['run_id'], replay['path']) == (original['run_id'], original['path'])
        assert tool_error(tools, token, 'research_feedback', args | {'text': '请核对期间。'})['code'] == 'conflict'
    assert web.conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == before + 1


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
    assert rpc(client, None, 'tools/list').status_code == 401
    # Owner credentials stay on the host's policy commands and never reach /mcp.
    assert rpc(client, tokens['owner'], 'tools/list').status_code == 401
    assert rpc(client, tokens['owner'], 'tools/call', {'name': 'research_cards', 'arguments': {}}).status_code == 401
    for role in ('research', 'assistant'):
        token = tokens[role]
        names = set(list_tools(client, token))
        for name in ('research_cards', 'research_methods'):
            assert name in names
        for name in ('account_research_coverage', 'account_portfolio_overview', 'research_company_overview'):
            assert (name in names) == (account_enabled and role == 'assistant')
        for name in ('research_scan_request', 'research_scan_read', 'research_scan_weekly',
                     'research_subscriptions', 'research_subscriptions_replace', 'schedule_run', 'schedule_ack'):
            assert (name in names) == (account_enabled and role == 'assistant')
            if name not in names:
                if account_enabled:
                    assert tool_error(client, token, name)['code'] == 'forbidden'
                else:
                    unknown = protocol_error(client, token, name)
                    assert unknown['code'] == -32602 and '工具不存在' in unknown['message']
        if not account_enabled:
            unknown = protocol_error(client, token, 'account_research_coverage')
            assert unknown['code'] == -32602 and '工具不存在' in unknown['message']
        else:
            expected = 'conflict' if role == 'assistant' else 'forbidden'
            assert tool_error(client, token, 'account_research_coverage')['code'] == expected
    conn = db.connect(path)
    conn.execute('DELETE FROM api_keys WHERE role=?', ('research',))
    conn.close()
    assert rpc(client, tokens['research'], 'tools/list').status_code == 401


def test_method_search_refuses_owner_and_blank_query(setup):
    path, tokens, client = setup
    with db.connect(path) as conn:
        owner = Context(conn, path, api_keys.authenticate(conn, tokens['owner']))
        with pytest.raises(ToolError) as refused:
            invoke(owner, 'research_methods', {'query': '低PE'})
        assert (refused.value.code, refused.value.status) == ('forbidden', 403)
    invalid = tool_error(client, tokens['research'], 'research_methods', {'query': '  '})
    assert invalid['code'] == 'invalid_arguments' and 'query' in invalid['message']


def test_validation_errors_identify_field_and_reason(setup):
    _, tokens, client = setup
    token = tokens['assistant']
    error = tool_error(client, token, 'research_report', {'report_id': True})
    assert error['code'] == 'invalid_arguments'
    assert 'report_id' in error['message'] and 'integer' in error['message']
    shape = rpc(client, token, 'tools/call', {'name': 'research_report', 'arguments': []}).json()['error']
    assert shape['code'] == -32602 and 'arguments' in shape['message'] and 'dictionary' in shape['message']
    unknown = protocol_error(client, token, 'missing')
    assert unknown['code'] == -32602 and '工具不存在' in unknown['message']
    assert tool_error(client, token, 'research_report', {'report_id': 999})['code'] == 'not_found'


def test_service_connection_enforces_declared_access_with_account_disabled(setup, monkeypatch):
    _, tokens, client = setup
    class Submission(Arguments):
        question: str

    def submit(ctx, args):
        run_id = service.submit(ctx.connection, '600519', 'lost-http-response', question=args.question)
        return Payload(data={'run_id': run_id})

    tool = replace(TOOLS['research_cards'], name='test_submit', arguments=Submission, handler=submit)
    monkeypatch.setitem(TOOLS, tool.name, tool)
    token = tokens['research']
    args = {'question': '现金流'}
    assert tool_error(client, token, 'test_submit', args)['code'] == 'unavailable'
    monkeypatch.setitem(TOOLS, tool.name, replace(tool, access='write'))
    saved = tool_data(client, token, 'test_submit', args)
    assert tool_data(client, token, 'test_submit', args) == saved
    assert tool_error(client, token, 'test_submit', {'question': '估值'})['code'] == 'conflict'


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
    failure = tool_error(client, tokens['research'], 'research_cards')
    assert failure['code'] == 'unavailable' and '重试' in failure['message']


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
    with TestClient(create_app(path, account_shadow_enabled=True), base_url=ORIGIN) as client:
        tool_data(client, token, 'test_sync')
        assert sync_levels == [2]
        # Grant is revoked while fetching stock_list; the write transaction must still reject.
        assert tool_error(client, token, 'research_subscriptions_replace', {'codes': ['000858']})['code'] == 'forbidden'
    with closing(db.connect(path)) as check:
        assert [r[0] for r in check.execute('SELECT code FROM subscriptions').fetchall()] == ['600519']
