"""OAuth protocol boundaries for the web authorization server."""

import base64
import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from a_share_research import db, oauth_store


CODE_SECONDS = 300
ACCESS_SECONDS = 3600
REFRESH_SECONDS = 30 * 86400
CONSENT_SECONDS = 600


class OAuthError(ValueError):
    def __init__(self, error: str, description: str):
        super().__init__(description)
        self.error = error


def redirect_origin(uri: str) -> str:
    """Return a literal CSP origin for an exact registered web callback."""
    error = ValueError("回调须为完整 HTTPS 地址，不含用户信息、通配域名或片段。本机回调可使用 HTTP。")
    if not uri or not uri.isascii() or re.search(r"[\s\x00-\x1f\x7f\\#]", uri):
        raise error
    try:
        url = urlsplit(uri)
        port = url.port
    except ValueError:
        raise error from None
    host = url.hostname
    if not host or url.username is not None or not re.fullmatch(r"(?:[a-zA-Z0-9.-]+|\[[a-fA-F0-9:]+\])(?::[0-9]+)?", url.netloc):
        raise error
    if url.scheme != "https" and not (url.scheme == "http" and host in ("localhost", "127.0.0.1", "::1")):
        raise error
    if {"code", "state", "error"} & parse_qs(url.query, keep_blank_values=True).keys():
        raise ValueError("回调地址的查询参数请留出 code、state 和 error，供授权结果使用。")
    hostname = f"[{host}]" if ":" in host else host
    return f"{url.scheme}://{hostname}" + (f":{port}" if port is not None else "")


@dataclass(frozen=True)
class Authorization:
    client_id: str
    redirect_uri: str
    resource: str
    code_challenge: str
    state: str | None

    @classmethod
    def parse(cls, params: Mapping[str, str], resource: str):
        if not params.get("response_type"):
            raise OAuthError("invalid_request", "缺少 response_type。")
        if params["response_type"] != "code":
            raise OAuthError("unsupported_response_type", "本站仅支持授权码。")
        if params.get("resource") != resource:
            raise OAuthError("invalid_target", "resource 必须是本站的 MCP 地址。")
        if params.get("code_challenge_method") != "S256" or not re.fullmatch(r"[A-Za-z0-9_-]{43}", params.get("code_challenge", "")):
            raise OAuthError("invalid_request", "请使用 S256 并提供有效的 PKCE challenge。")
        return cls(params["client_id"], params["redirect_uri"], resource, params["code_challenge"], params.get("state"))

    def query(self) -> str:
        return urlencode({key: value for key, value in vars(self).items() if value is not None}
                         | {"response_type": "code", "code_challenge_method": "S256"})


def callback(uri: str, state: str | None, **result: str) -> str:
    url = urlsplit(uri)
    if state is not None:
        result["state"] = state
    return urlunsplit(url._replace(query=(url.query + "&" if url.query else "") + urlencode(result)))


def consent_form(auth: Authorization, session: str, secret: str) -> str:
    payload = f"{int(time.time()) + CONSENT_SECONDS}.{secrets.token_urlsafe(16)}.{auth.query()}"
    mac = hmac.digest(secret.encode(), f"consent:{session}:{payload}".encode(), "sha256").hex()
    return f"{mac}.{payload}"


def read_consent(ticket: str, session: str, secret: str) -> str | None:
    mac, _, payload = ticket.partition(".")
    expected = hmac.digest(secret.encode(), f"consent:{session}:{payload}".encode(), "sha256").hex()
    if not hmac.compare_digest(mac.encode(), expected.encode()):
        return None
    expiry, _, query = payload.split(".", 2)
    return query if int(expiry) > time.time() else None


def approve(conn: sqlite3.Connection, auth: Authorization, ticket: str, secret: str) -> str:
    # A retry of the same signed consent returns the same code and cannot revive it.
    code = hmac.digest(secret.encode(), b"authorization-code:" + ticket.encode(), "sha256").hex()
    with db.transaction(conn):
        if not oauth_store.get_client(conn, auth.client_id):
            raise OAuthError("invalid_request", "客户端已撤销。")
        if not oauth_store.code_exists(conn, code):
            grant_id = oauth_store.client_grant(conn, auth.client_id) or oauth_store.create_grant(conn, auth.client_id)
            oauth_store.save_code(conn, code, grant_id=grant_id, client_id=auth.client_id, resource=auth.resource,
                                  redirect_uri=auth.redirect_uri, code_challenge=auth.code_challenge,
                                  expires_at=time.time() + CODE_SECONDS)
    return code


def exchange(conn: sqlite3.Connection, form: Mapping[str, str], resource: str) -> dict:
    kind = form.get("grant_type")
    if not kind:
        raise OAuthError("invalid_request", "grant_type is required")
    if kind not in ("authorization_code", "refresh_token"):
        raise OAuthError("unsupported_grant_type", "unsupported grant_type")
    required = ("code", "code_verifier", "redirect_uri", "resource") if kind == "authorization_code" else ("refresh_token",)
    if not all(form.get(key) for key in ("client_id", *required)):
        raise OAuthError("invalid_request", "missing required token request parameter")
    if form.get("resource", resource) != resource:
        raise OAuthError("invalid_target", "resource must be this server's MCP endpoint")

    invalid = OAuthError("invalid_grant", "grant is invalid, expired, revoked or already used")
    with db.transaction(conn):
        if kind == "authorization_code":
            verifier = form["code_verifier"]
            if not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier):
                raise invalid
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            grant_id = oauth_store.consume_code(conn, form["code"], client_id=form["client_id"],
                                                redirect_uri=form["redirect_uri"], resource=resource, code_challenge=challenge)
        else:
            grant_id = oauth_store.consume_refresh(conn, form["refresh_token"], client_id=form["client_id"], resource=resource)
        if grant_id is None:
            raise invalid
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = time.time()
        oauth_store.save_token(conn, access, grant_id=grant_id, kind="access", resource=resource, expires_at=now + ACCESS_SECONDS)
        oauth_store.save_token(conn, refresh, grant_id=grant_id, kind="refresh", resource=resource, expires_at=now + REFRESH_SECONDS)
    return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_SECONDS, "refresh_token": refresh}
