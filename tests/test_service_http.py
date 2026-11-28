"""Real localhost HTTP, isolated database and disposable web process; no worker or provider calls."""
import pytest
import requests

from a_share_research import db, service
from support.mcp import rpc, tool_data, tool_error


@pytest.fixture
def mcp(server, mcp_tools):
    """The tools /mcp endpoint on the same database, with the web server's research PAT."""
    return mcp_tools, server[1].headers['Authorization'].removeprefix('Bearer ')


def test_request_recovery_and_report_compaction_without_writes(server, mcp):
    conn, *_, run, report, failed, queued = server
    client, token = mcp
    counts = [conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("runs", "calls", "reports")]
    for name, args in (("research_status", {"request_id": "lost-http-response"}),
                       ("research_report", {"report_id": report})):
        assert rpc(client, "invalid", "tools/call", {"name": name, "arguments": args}).status_code == 401
    recovered = tool_data(client, token, "research_status", {"request_id": "lost-http-response", "compact": False})
    assert recovered["data"]["id"] == queued
    assert tool_error(client, token, "research_status", {"request_id": "unknown"})["code"] == "not_found"
    full = tool_data(client, token, "research_report", {"report_id": report, "compact": False})["data"]
    small = tool_data(client, token, "research_report", {"report_id": report, "compact": True})["data"]
    assert small["run_id"] == run and small["view"] == "compact"
    assert "pages" in full["evidence"]["sources"]["D0"]
    assert "pages" not in small["evidence"]["sources"]["D0"]
    rejected = tool_data(client, token, "research_report", {"report_id": failed, "compact": True})["data"]
    assert not rejected["passed"] and "summary" not in rejected["report"]
    assert rejected["diagnostics"]["problems"]
    assert counts == [conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("runs", "calls", "reports")]


def test_monthly_admission_and_polling_are_authenticated_and_idempotent(server, mcp):
    from datetime import datetime
    conn, *_ = server
    client, token = mcp
    month = datetime.now(db.SHANGHAI).strftime('%Y-%m')
    request = {'name': 'research_batch_monthly_request', 'arguments': {'month': month}}
    assert rpc(client, None, 'tools/call', request).status_code == 401
    result = tool_data(client, token, 'research_batch_monthly_request', {'month': month})['data']
    assert result['created'] and result['status'] == 'queued'
    replay = tool_data(client, token, 'research_batch_monthly_request', {'month': month})['data']
    assert not replay['created'] and replay['batch_id'] == result['batch_id']
    assert tool_error(client, token, 'research_batch_monthly_request', {'month': '2020-01'})['code'] == 'conflict'
    conn.execute("UPDATE batches SET status='frozen' WHERE id=?", (result['batch_id'],))
    status = tool_data(client, token, 'research_batch_monthly_status', {'month': month, 'compact': False})['data']
    assert status['batch']['status'] == 'frozen' and status['batch']['id'] == result['batch_id']
    assert conn.execute('SELECT COUNT(*) FROM batches').fetchone()[0] == 1


def test_historical_diagnostics_remain_read_only_and_labeled(server):
    import json
    from pathlib import Path
    conn, _, url, run, report, *_ = server
    # Captured diagnostic row; controller metadata is an explicit compatibility variant.
    captured = json.loads((Path(__file__).parent / 'data/evolution-note-plan-20261106.json').read_text())
    original = captured['diagnostic_runs'][0]
    keys = tuple(original)
    conn.execute(f"INSERT INTO runs({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})", tuple(original.values()))
    root = original['id']
    conn.execute("UPDATE runs SET request=?,identity=? WHERE id=?",
                 (json.dumps({'source': {'event': 'evolution'}}),
                  json.dumps({'evolution': {'phase': 'rejected'}}), root))
    # The captured successful report exercises the historical diagnostic case reading path.
    conn.execute("UPDATE runs SET kind='diagnostic',request=? WHERE id=?",
                 (json.dumps({'source': {'event': 'evolution_case', 'evolution_id': root}}), run))
    browser = requests.Session()
    browser.trust_env = False
    assert browser.post(url + '/auth/dev', headers={'Origin': url}, timeout=3).status_code == 200
    before = conn.total_changes
    for run_id in (root, run):
        assert service.run_summary(conn, run_id)['kind'] == 'diagnostic'
        page = browser.get(url + f'/runs/{run_id}', timeout=3)
        assert page.status_code == 200 and '研究诊断' in page.text
    assert service.report_view(conn, report)['purpose'] == 'diagnostic'
    page = browser.get(url + f'/reports/{report}', timeout=3)
    assert page.status_code == 200 and '研究诊断' in page.text
    assert conn.total_changes == before
    browser.close()


def test_report_schema_describes_public_views():
    from a_share_research import report
    from a_share_research.contract import JUDGMENT_CONTRACT
    assert report.SCHEMA_VERSION == 2 and JUDGMENT_CONTRACT == 15
    properties = set(report.json_schema()['properties'])
    assert {'summary', 'risks', 'checkpoints'} <= properties and set(report.FAILED_FIELDS) <= properties
    assert set(report.CARD_FIELDS) <= set(report.COMPACT_FIELDS)
    for view in (report.COMPACT_FIELDS, report.CARD_FIELDS, report.FAILED_FIELDS):
        assert view and len(set(view)) == len(view)


def test_persistent_roles_cannot_use_owner_token_for_research(server, mcp):
    from a_share_research import api_keys
    conn, *_ = server
    client, _ = mcp
    for role in api_keys.ROLES:
        token = api_keys.create(conn, 'role-' + role, role=role)
        assert rpc(client, token, 'tools/list').status_code == (401 if role == 'owner' else 200)
        key = api_keys.authenticate(conn, token)
        conn.execute('DELETE FROM api_keys WHERE id=?', (key.key_id,))
        assert rpc(client, token, 'tools/list').status_code == 401


def test_retired_json_entries_are_gone(server, mcp):
    _, client, url, *_ = server
    for path in ('/api/research/budget', '/api/request?request_id=lost-http-response', '/api/schema',
                 '/api/reports?code=600519', '/api/search?q=cash', '/api/batches/monthly/2026-11',
                 '/api/companies/600519/card', '/api/cards', '/api/subscriptions', '/api/scans/weekly',
                 '/api/scans/1', '/api/runs/1', '/api/reports/1'):
        assert client.get(url + path, timeout=3).status_code == 404
    same_origin = {'Origin': url}
    for path in ('/api/research', '/api/reports/1/feedback', '/api/batches/monthly', '/api/scans', '/api/session/revoke'):
        assert client.post(url + path, json={}, headers=same_origin, timeout=3).status_code == 404
    assert client.put(url + '/api/subscriptions', json={}, headers=same_origin, timeout=3).status_code == 404
    assert client.put(url + '/api/scan-requests/x', headers=same_origin, timeout=3).status_code == 404
    # Without the retired machine exemption, cross-origin writes stop at the same-origin guard.
    assert client.post(url + '/api/research', json={}, timeout=3).status_code == 403
    tools, token = mcp
    headers = {'Authorization': 'Bearer ' + token}
    assert tools.get('/tools', headers=headers).status_code == 404
    assert tools.post('/tools/research_cards', headers=headers, json={}).status_code == 404
    assert not any(getattr(r, 'path', '').startswith('/tools') for r in tools.app.routes)


def test_web_app_declares_no_legacy_api_routes(web):
    assert not any(getattr(r, 'path', '').startswith('/api/') for r in web.app.routes)
