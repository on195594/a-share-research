import hashlib
import json
import sys
import time

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from a_share_research import api_keys, db, login_token
from a_share_research.login_token import mint


def test_anonymous_login_skips_write_transaction(web, monkeypatch):
    statements = []
    connect = db.connect

    def traced(path):
        conn = connect(path)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(db, "connect", traced)
    with TestClient(web.app, base_url=web.settings.base_url) as browser:
        for headers in ({}, {"Cookie": "remember_device="}, {"Cookie": "remember_device=" + "a" * 42 + "."}):
            response = browser.get("/login", headers=headers)
            assert response.status_code == 200 and "set-cookie" not in response.headers
    assert not any(sql.startswith("BEGIN") for sql in statements)


def test_minted_session_is_accepted_until_it_expires(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(db, "DATA", tmp_path)
    monkeypatch.setenv("DEV_LOGIN", "1")
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://127.0.0.1:8617")
    monkeypatch.setenv("SESSION_SECRET", "local-login-token-" * 3)
    monkeypatch.setattr(sys, "argv", ["login_token", "--minutes", "1"])
    login_token.main()
    token = capsys.readouterr().out.strip()

    monkeypatch.delitem(sys.modules, "a_share_research.web", raising=False)
    from a_share_research import web

    try:
        cookie = lambda value: Request({"type": "http", "headers": [(b"cookie", f"session={value}".encode())]})
        assert web.current_session(cookie(token)) == web._hash(token)
        assert web.conn.execute("SELECT token_hash FROM sessions").fetchone()[0] == hashlib.sha256(token.encode()).hexdigest()
        minted = mint(web.conn, 1)
        assert web.current_session(cookie(minted)) == web._hash(minted)
        web.conn.execute("INSERT INTO sessions VALUES (?, ?)",
                         ("agent:" + web._hash("legacy-agent"), time.time() + 60))
        assert web.current_session(cookie("legacy-agent")) is None

        web.conn.execute("UPDATE sessions SET expires_at = ?", (time.time() - 1,))
        assert web.current_session(cookie(token)) is None

        persistent = api_keys.create(web.conn, "skills-runtime")
        replacement = api_keys.create(web.conn, "skills-runtime")
        assert persistent != replacement
        for value in (persistent, replacement):
            assert web.current_session(cookie(value)) is None
        # Browser login clears expired sessions but must not clear persistent credentials.
        web.start_session()
        web.conn.close()
        web.conn = db.connect()
        assert web.conn.execute("SELECT COUNT(*) FROM sessions WHERE expires_at < ?", (time.time(),)).fetchone()[0] == 0
        key_id = api_keys.authenticate(web.conn, persistent).key_id
        assert api_keys.revoke(web.conn, key_id)
        assert web.conn.execute("SELECT revoked_at FROM api_keys WHERE id=?", (key_id,)).fetchone()[0]
        assert api_keys.authenticate(web.conn, persistent) is None
        assert api_keys.authenticate(web.conn, replacement) is not None
    finally:
        web.conn.close()


@pytest.mark.parametrize("origin,secure", [("http://127.0.0.1:8617", False), ("https://stock.keyi.win", True)])
def test_storage_state_is_private_and_does_not_overwrite_or_print_credentials(tmp_path, monkeypatch, capsys, origin, secure):
    monkeypatch.setattr(db, "DATA", tmp_path)
    monkeypatch.setenv("PUBLIC_BASE_URL", origin)
    destination = tmp_path / "browser.json"
    monkeypatch.setattr(sys, "argv", ["login_token", "--minutes", "1", "--storage-state", str(destination)])
    login_token.main()
    assert capsys.readouterr().out == ""
    state = json.loads(destination.read_text())
    cookie, = state["cookies"]
    assert state["origins"] == [] and cookie["name"] == "session"
    assert cookie["domain"] == ("stock.keyi.win" if secure else "127.0.0.1")
    assert cookie["secure"] is secure and cookie["httpOnly"] and cookie["sameSite"] == "Lax"
    assert destination.stat().st_mode & 0o777 == 0o600
    conn = db.connect()
    try:
        hashed, expires = conn.execute("SELECT token_hash, expires_at FROM sessions").fetchone()
        assert hashed == hashlib.sha256(cookie["value"].encode()).hexdigest()
        assert abs(expires - cookie["expires"]) < 1
        original = destination.read_bytes()
        with pytest.raises(FileExistsError):
            login_token.main()
        assert destination.read_bytes() == original
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    finally:
        conn.close()


@pytest.mark.parametrize("origin,extra", [("", []), ("https://stock.keyi.win", ["--agent"]),
                                        ("https://user:secret@stock.keyi.win", [])])
def test_invalid_storage_state_request_creates_no_session_or_file(tmp_path, monkeypatch, origin, extra):
    monkeypatch.setattr(db, "DATA", tmp_path)
    monkeypatch.setenv("PUBLIC_BASE_URL", origin)
    destination = tmp_path / "browser.json"
    monkeypatch.setattr(sys, "argv", ["login_token", "--storage-state", str(destination), *extra])
    with pytest.raises(SystemExit) as error:
        login_token.main()
    assert error.value.code == 2
    assert not destination.exists() and not (tmp_path / "app.db").exists()


def test_agent_flag_is_retired_without_creating_database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA", tmp_path)
    monkeypatch.setattr(sys, "argv", ["login_token", "--agent"])
    with pytest.raises(SystemExit) as error:
        login_token.main()
    assert error.value.code == 2
    assert not (tmp_path / "app.db").exists()
