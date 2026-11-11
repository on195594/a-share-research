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


def test_historical_diagnostics_remain_read_only_and_labeled(server):
    import json
    from pathlib import Path
    conn, client, url, run, report, *_ = server
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
        response = client.get(url + f'/api/runs/{run_id}', timeout=3)
        assert response.status_code == 200 and response.json()['kind'] == 'diagnostic'
        page = browser.get(url + f'/runs/{run_id}', timeout=3)
        assert page.status_code == 200 and '研究诊断' in page.text
    response = client.get(url + f'/api/reports/{report}', timeout=3)
    assert response.status_code == 200 and response.json()['purpose'] == 'diagnostic'
    page = browser.get(url + f'/reports/{report}', timeout=3)
    assert page.status_code == 200 and '研究诊断' in page.text
    assert conn.total_changes == before
    browser.close()


def test_report_schema_is_authenticated_and_describes_public_views(server):
    from a_share_research import report
    from a_share_research.contract import JUDGMENT_CONTRACT
    _, client, url, *_ = server
    assert client.get(url + "/api/schema", headers={"Authorization": "Bearer invalid"}, timeout=3).status_code == 401
    response = client.get(url + "/api/schema", timeout=3)
    assert response.status_code == 200
    schema = response.json()
    assert schema == {"version": "research-v1", "schema_version": 2,
                      "judgment_contract": JUDGMENT_CONTRACT, "report": report.json_schema(),
                      "views": {"compact": list(report.COMPACT_FIELDS), "card": list(report.CARD_FIELDS),
                                "failed": list(report.FAILED_FIELDS)}}


def test_persistent_roles_cannot_use_owner_token_for_research(server):
    from a_share_research import api_keys
    conn, client, url, *_ = server
    for role in api_keys.ROLES:
        token = api_keys.create(conn, 'role-' + role, role=role)
        response = client.get(url + '/api/research/budget',
                              headers={'Authorization': 'Bearer ' + token}, timeout=3)
        assert response.status_code == (403 if role == 'owner' else 200)
        key = api_keys.authenticate(conn, token)
        conn.execute('DELETE FROM api_keys WHERE id=?', (key.key_id,))
        assert client.get(url + '/api/research/budget',
                          headers={'Authorization': 'Bearer ' + token}, timeout=3).status_code == 401
