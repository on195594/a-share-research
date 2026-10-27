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
