"""Real localhost HTTP, isolated database and disposable web process; no worker or provider calls."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest
import requests

from a_share_research import api_keys, db, service
from test_service_delivery import saved


@pytest.fixture
def server(tmp_path):
    conn = db.connect(tmp_path / "app.db")
    run, report = saved(conn)
    _, failed = saved(conn, passed=False)
    queued = service.submit(conn, "600519", "lost-http-response", question="现金流")
    token = api_keys.create(conn, "isolated-http-test")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    env = {**os.environ, "DATA_DIR": str(tmp_path), "PUBLIC_BASE_URL": url,
           "DEV_LOGIN": "1", "SESSION_SECRET": "isolated-test-only-not-a-real-secret"}
    log_path = tmp_path / "web.log"
    log = log_path.open("w")
    process = subprocess.Popen([sys.executable, "-m", "uvicorn", "a_share_research.web:app",
                                "--fd", str(listener.fileno()), "--log-level", "error"],
                               pass_fds=(listener.fileno(),), env=env,
                               cwd=Path(__file__).resolve().parents[1],
                               stdout=log, stderr=log)
    listener.close()
    client = requests.Session()
    client.trust_env = False
    client.headers["Authorization"] = f"Bearer {token}"
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            assert process.poll() is None, f"isolated web process exited; log: {log_path}"
            try:
                if client.get(url + "/api/research/budget", timeout=1).status_code == 200:
                    break
            except requests.ConnectionError:
                pass
            time.sleep(0.05)
        else:
            pytest.fail("isolated web did not become ready")
        yield conn, client, url, run, report, failed, queued
    finally:
        client.close()
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        conn.close()
        log.close()


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
