"""Real localhost HTTP, isolated database and disposable web process; no worker or provider calls."""
import requests

from a_share_research import db


def test_get_endpoints_auth_recovery_compaction_and_no_restart(server):
    conn, client, url, run, report, failed, queued = server
    counts = [conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("runs", "calls", "reports")]
    for path in ("/api/research/budget", "/api/request?request_id=lost-http-response",
                 f"/api/reports/{report}?compact=true"):
        assert client.get(url + path, headers={"Authorization": "Bearer invalid"}, timeout=3).status_code == 401
    recovered = client.get(url + "/api/request", params={"request_id": "lost-http-response"}, timeout=3)
    assert recovered.status_code == 200 and recovered.json()["id"] == queued
    assert client.get(url + "/api/request", params={"request_id": "unknown"}, timeout=3).status_code == 404
    full = client.get(url + f"/api/reports/{report}", timeout=3).json()
    small = client.get(url + f"/api/reports/{report}", params={"compact": "true"}, timeout=3).json()
    assert small["run_id"] == run and small["view"] == "compact"
    assert "pages" in full["evidence"]["sources"]["D0"]
    assert "pages" not in small["evidence"]["sources"]["D0"]
    rejected = client.get(url + f"/api/reports/{failed}", params={"compact": "true"}, timeout=3).json()
    assert not rejected["passed"] and "summary" not in rejected["report"]
    assert rejected["diagnostics"]["problems"]
    assert counts == [conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("runs", "calls", "reports")]


def test_monthly_admission_and_polling_are_authenticated_and_idempotent(server):
    from datetime import datetime
    conn, client, url, *_ = server
    month = datetime.now(db.SHANGHAI).strftime('%Y-%m')
    unauth = requests.Session()
    unauth.trust_env = False
    assert unauth.post(url + '/api/batches/monthly', json={'month': month}, timeout=2).status_code == 401
    result = client.post(url + '/api/batches/monthly', json={'month': month}, timeout=2).json()
    assert result['created'] and result['batch']['status'] == 'queued'
    assert not client.post(url + '/api/batches/monthly', json={'month': month}, timeout=2).json()['created']
    assert client.post(url + '/api/batches/monthly', json={'month': '2020-01'}, timeout=2).status_code == 409
    ident = result['batch']['id']
    conn.execute("UPDATE batches SET status='frozen' WHERE id=?", (ident,))
    status = client.get(f'{url}/api/batches/monthly/{month}', timeout=2).json()
    assert status['batch']['status'] == 'frozen' and status['batch']['id'] == ident
    assert conn.execute('SELECT COUNT(*) FROM batches').fetchone()[0] == 1


def test_improvements_are_authenticated_read_only_and_diagnostic(server):
    import json
    from pathlib import Path
    from a_share_research import evolution, service
    conn, client, url, *_ = server
    captured = json.loads((Path(__file__).parent / 'data/evolution-note-plan-20261106.json').read_text())
    original = captured['diagnostic_runs'][0]
    keys = tuple(original)
    conn.execute(f"INSERT INTO runs({','.join(keys)}) VALUES ({','.join('?' for _ in keys)})", tuple(original.values()))
    source_hash = evolution.digest(captured)
    root = evolution.record_archived_rejection(conn, title='半年报附注基线', method=captured['method'],
                 reason='E2、E4 未通过，保留原拒绝结论。', source_sha256=source_hash,
                 source_label='k7b-note-baseline-20261106', case_run_ids=[original['id']])
    before = conn.total_changes
    for path in ('/api/improvements', f'/api/improvements/{root}'):
        assert client.get(url + path, headers={'Authorization': 'Bearer invalid'}, timeout=3).status_code == 401
    listing = client.get(url + '/api/improvements', timeout=3).json()['items']
    detail = client.get(url + f'/api/improvements/{root}', timeout=3).json()['item']
    assert listing[0] == detail and detail['phase'] == 'rejected'
    assert client.get(url + '/api/improvements/999999', timeout=3).status_code == 404
    run = client.get(url + '/api/request', params={'request_id': conn.execute('SELECT request_id FROM runs WHERE id=?', (root,)).fetchone()[0]}, timeout=3).json()
    assert run['kind'] == 'diagnostic' and run['improvement_id'] == root
    assert run['budget']['model_call_limit'] is None
    assert service.run_budget(conn)['model_call_limit'] == 30
    assert conn.total_changes == before
    assert evolution.active_methods(conn) == []
    browser = requests.Session()
    browser.trust_env = False
    assert browser.get(url + '/improvements', allow_redirects=False, timeout=3).status_code == 303
    assert browser.post(url + '/auth/dev', headers={'Origin': url}, timeout=3).status_code == 200
    page = browser.get(url + f'/improvements/{root}', timeout=3)
    assert page.status_code == 200 and '半年报附注基线' in page.text and '已拒绝' in page.text
    assert browser.get(url + f'/runs/{root}', allow_redirects=False, timeout=3).headers['location'] == f'/improvements/{root}'
    browser.close()
