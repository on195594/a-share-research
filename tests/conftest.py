import os

import pytest


@pytest.fixture
def bind_account():
    from a_share_research import db, gate

    def bind(conn, account_id):
        binding = dict(schema_version=1, account_id=account_id, account_scope='structural-scope', mirror_watermark={})
        conn.execute('INSERT INTO account_source VALUES(1,?,?,?,?)',
                     ('ledger', 'a' * 64, db.now(), gate.canonical(binding)))
    return bind


def pytest_configure(config):
    config.addinivalue_line("markers", "wide_scan_scope: scan beyond subscriptions (holdings, tracking L5 and card companies)")


@pytest.fixture(autouse=True)
def narrow_scan_scope(request, monkeypatch):
    # Captured reports give several fixture companies a card; scan replays written against
    # subscriptions keep that scope unless a test opts into the wide production scope.
    if request.node.get_closest_marker("wide_scan_scope") is None:
        from a_share_research import scans
        monkeypatch.setattr(scans, "scan_scope", lambda conn, **_: scans.subscriptions(conn))


@pytest.fixture
def configured():
    """Installed Pi environment shared by offline runtime and opt-in live tests."""
    if not all(os.environ.get(key) for key in ("MODEL_PROVIDER", "MODEL_ID", "PI_AGENT_DIR")):
        pytest.skip("Run with the existing worker environment for installed Pi checks")
    return dict(os.environ)


@pytest.fixture(autouse=True)
def isolate_tushare_cache(monkeypatch, tmp_path):
    # Offline market replays may change between calls; never read/write the owner's cache.
    from a_share_research import db
    monkeypatch.setattr(db, "DATA", tmp_path / "data")
    monkeypatch.setenv("TUSHARE_CACHE", "0")
    # Releases sign with the owner's standing credential; tests never read the real one.
    from a_share_research import deploy
    monkeypatch.setattr(deploy, "OWNER_CREDENTIAL", tmp_path / "absent-owner.json")


@pytest.fixture
def server(tmp_path):
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
