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
