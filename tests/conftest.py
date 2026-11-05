import pytest


@pytest.fixture(autouse=True)
def isolate_tushare_cache(monkeypatch, tmp_path):
    # Offline market replays may change between calls; never read/write the owner's cache.
    from a_share_research import db
    monkeypatch.setattr(db, "DATA", tmp_path / "data")
    monkeypatch.setenv("TUSHARE_CACHE", "0")


@pytest.fixture
def server(tmp_path):
    import os
    from pathlib import Path
    import socket
    import subprocess
    import sys
    import time

    import requests

    from a_share_research import api_keys, db, service
    from test_service_delivery import saved

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
            except (requests.ConnectionError, requests.Timeout):
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
