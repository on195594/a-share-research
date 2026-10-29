import hashlib
import json
import sys
import time

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from a_share_research import api_keys, db, login_token
from a_share_research.login_token import mint


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
        agent = mint(web.conn, 1, agent=True)
        cookie = lambda value: Request({"type": "http", "headers": [(b"cookie", f"session={value}".encode())]})
        bearer = lambda value: Request({"type": "http", "headers": [(b"authorization", f"Bearer {value}".encode())]})

        assert web.current_session(cookie(token)) == web._hash(token)
        assert web.current_session(cookie(agent)) is None
        assert web.api_session(bearer(agent)) == "agent:" + web._hash(agent)
        for denied in (cookie(token), bearer(token)):
            with pytest.raises(HTTPException) as exc:
                web.api_session(denied)
            assert exc.value.status_code == 401

        web.api_revoke(bearer(agent))
        with pytest.raises(HTTPException):
            web.api_session(bearer(agent))

        web.conn.execute("UPDATE sessions SET expires_at = ?", (time.time() - 1,))
        assert web.current_session(cookie(token)) is None

        persistent = api_keys.create(web.conn, "skills-runtime")
        replacement = api_keys.create(web.conn, "skills-runtime")
        assert persistent != replacement
        for value in (persistent, replacement):
            assert web.current_session(cookie(value)) is None
            assert web.api_session(bearer(value)) == "key:" + web._hash(value)
        # Browser login clears expired sessions but must not clear persistent credentials.
        web.start_session()
        web.conn.close()
        web.conn = db.connect()
        assert web.api_session(bearer(persistent)) == "key:" + web._hash(persistent)
        request = bearer(persistent)
        request.scope["headers"].append((b"x-research-version", b"unsupported"))
        with pytest.raises(HTTPException) as exc:
            web.api_session(request)
        assert exc.value.status_code == 400

        assert web.api_revoke(bearer(persistent))["revoked"]
        with pytest.raises(HTTPException) as exc:
            web.api_session(bearer(persistent))
        assert exc.value.status_code == 401
        assert web.api_session(bearer(replacement)) == "key:" + web._hash(replacement)
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
