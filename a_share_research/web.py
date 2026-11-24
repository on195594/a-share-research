"""Server-rendered web app: owner login, start research, follow progress, read reports.

Holds no model or market credentials; the worker does all research.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from functools import partial
from pathlib import Path
from typing import Self
from urllib.parse import urlencode, urlparse

import requests
from fastapi import FastAPI, Form, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import QueryParams
from starlette.exceptions import HTTPException as StarletteHTTPException

from a_share_research import report as report_body
from a_share_research import charts, db, batches, companies as company_data, service, reading
from a_share_research import stock_codes, api_keys, oauth, oauth_store, skill_bundle, template_filters
from a_share_research import web_evidence, web_ledger, web_nav
from a_share_research.tools_client import ToolsClient
from a_share_research.contract import JUDGMENT_CONTRACT
from a_share_research.evidence import report_source_view, citation_link
from a_share_research.service import FeedbackRequest, ResearchRequest

HERE = Path(__file__).parent
SESSION_COOKIE = "session"
STATE_COOKIE = "oauth_state"
PENDING_COOKIE = "mcp_authorization"
LEDGER_RETURN_COOKIE = "resume_ledger"
SESSION_SECONDS = 8 * 3600
DEVICE_COOKIE = "remember_device"
DEVICE_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}")
DEVICE_SECONDS = 30 * 24 * 3600
STEPS = {"gather": "取资料", "explore": "补读披露与计算", "compose": "撰写报告", "review": "独立复核", "repair": "按复核意见修改",
         "price": "计算价格参考"}
SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
    "x-frame-options": "DENY",
}


@dataclass(frozen=True)
class Settings:
    base_url: str
    session_secret: str
    dev_login: bool
    github_client_id: str
    github_client_secret: str
    github_user_id: str
    tools_url: str = ""
    tools_token: str = field(default="", repr=False)

    @classmethod
    def from_env(cls) -> Self:
        tools_url = os.environ.get("ASHARE_TOOLS_URL", "").strip()
        settings = cls(
            base_url=os.environ.get("PUBLIC_BASE_URL", "").rstrip("/"),
            session_secret=os.environ.get("SESSION_SECRET", ""),
            dev_login=os.environ.get("DEV_LOGIN") == "1",
            github_client_id=os.environ.get("GITHUB_CLIENT_ID", ""),
            github_client_secret=os.environ.get("GITHUB_CLIENT_SECRET", ""),
            github_user_id=os.environ.get("GITHUB_USER_ID", ""),
            tools_url=tools_url.rstrip("/"),
            tools_token=os.environ.get("ASHARE_TOOLS_TOKEN", "").strip(),
        )
        url = urlparse(settings.base_url)
        if url.scheme not in ("http", "https") or not url.hostname:
            raise SystemExit("PUBLIC_BASE_URL must be an absolute http(s) URL")
        if bool(tools_url) != bool(settings.tools_token):
            raise SystemExit("ASHARE_TOOLS_URL and ASHARE_TOOLS_TOKEN must be configured together")
        if tools_url:
            try:
                endpoint = urlparse(tools_url)
            except ValueError:
                raise SystemExit("ASHARE_TOOLS_URL is malformed") from None
            if endpoint.scheme not in ("http", "https") or not endpoint.hostname or endpoint.username is not None:
                raise SystemExit("ASHARE_TOOLS_URL must be an absolute http(s) URL without userinfo")
        if len(settings.session_secret) < 32:
            raise SystemExit("SESSION_SECRET must be at least 32 characters")
        if settings.dev_login and url.hostname not in ("127.0.0.1", "localhost"):
            raise SystemExit("DEV_LOGIN is only allowed on a loopback PUBLIC_BASE_URL")
        if not settings.dev_login and not (
            settings.github_client_id and settings.github_client_secret and settings.github_user_id.isdigit()
        ):
            raise SystemExit("GitHub OAuth settings are required")
        return settings

    @property
    def secure(self) -> bool:
        return self.base_url.startswith("https://")


class ThreadLocalConnectionProxy:
    """Forward connection access to one SQLite connection per calling thread."""

    def __init__(self):
        self._path = db.DATA / "app.db"
        self._local = threading.local()
        # Keep startup schema initialization and migration failures eager.
        self._local.conn = db.connect(self._path)

    def __getattr__(self, name):
        connection = getattr(self._local, "conn", None)
        if connection is None:
            connection = self._local.conn = db.connect(self._path)
        return getattr(connection, name)


settings = Settings.from_env()
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["record_json"] = lambda value: json.dumps(value, ensure_ascii=False, indent=2)
templates.env.filters.update(template_filters.FILTERS)
# Busts browser caches of the stylesheet and script on each deploy that changes them.
ASSET_VERSION = hashlib.sha256(b"".join(path.read_bytes() for path in sorted((HERE / "static").iterdir())
                                        if path.suffix in (".js", ".css"))).hexdigest()[:12]
templates.env.globals["asset_version"] = ASSET_VERSION
templates.env.globals["funnel_chart"] = charts.funnel
templates.env.globals["citation_link"] = citation_link
templates.env.globals["report_source_view"] = report_source_view
templates.env.globals["stance_decision"] = reading.stance_decision
conn = ThreadLocalConnectionProxy()


# ---------- security ----------

def _csp(form_action_extra: str | None = None) -> str:
    form_action = "form-action 'self' https://github.com"
    if form_action_extra:
        form_action += " " + form_action_extra
    return (f"default-src 'self'; {form_action}; base-uri 'none'; frame-ancestors 'none'; "
            "object-src 'none'; img-src 'self'")


@app.middleware("http")
async def guard(request: Request, call_next):
    if request.headers.get("host", "").lower() != urlparse(settings.base_url).netloc.lower():
        return Response("Invalid Host header", status_code=403, media_type="text/plain")
    path = request.url.path
    machine = path.startswith(("/api/", "/.well-known/")) or path == "/oauth/token"
    if request.method not in ("GET", "HEAD") and not machine and not same_origin(request):
        return Response("Cross-origin request refused", status_code=403, media_type="text/plain")
    # Restore only safe page reads, never use a device cookie as API/write authorization.
    restored = None
    if (not machine and request.method in ("GET", "HEAD") and not path.startswith(("/static/", "/auth/"))
            and path not in ("/oauth_callback", "/mcp")
            and DEVICE_TOKEN.fullmatch(request.cookies.get(DEVICE_COOKIE, ""))):
        restored = await run_in_threadpool(restore_device, request)
    response = await call_next(request)
    if restored:
        token, age = restored
        set_cookie(response, SESSION_COOKIE, token, age)
    response.headers.update(SECURITY_HEADERS)
    response.headers["content-security-policy"] = _csp(form_action_extra=getattr(request.state, "consent_origin", None))
    if path == "/oauth/token":
        response.headers["cache-control"] = "no-store"
        response.headers["pragma"] = "no-cache"
    elif not path.startswith("/static/"):
        response.headers["cache-control"] = "no-store, no-transform"
    elif request.query_params.get("v") == ASSET_VERSION:
        response.headers["cache-control"] = "public, max-age=31536000, immutable"
    return response


def same_origin(request: Request) -> bool:
    site = request.headers.get("sec-fetch-site")
    if site:
        return site == "same-origin"
    origin = request.headers.get("origin") or ""
    return origin == settings.base_url


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def current_session(request: Request) -> str | None:
    token = getattr(request.state, "restored_session", None) or request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    row = conn.execute("SELECT expires_at FROM sessions WHERE token_hash = ?", (_hash(token),)).fetchone()
    if not row or row["expires_at"] <= time.time():
        return None
    linked = conn.execute("SELECT d.owner_id, d.expires_at FROM device_sessions s "
                          "LEFT JOIN remembered_devices d ON d.token_hash=s.device_hash "
                          "WHERE s.session_hash=?", (_hash(token),)).fetchone()
    if linked and (linked["owner_id"] != settings.github_user_id or not linked["expires_at"]
                   or linked["expires_at"] <= time.time()):
        return None
    return _hash(token)


def active_device(request: Request):
    token = request.cookies.get(DEVICE_COOKIE, "")
    if not DEVICE_TOKEN.fullmatch(token):
        return None
    return conn.execute("SELECT token_hash, expires_at FROM remembered_devices "
                        "WHERE token_hash=? AND owner_id=? AND expires_at>?",
                        (_hash(token), settings.github_user_id, time.time())).fetchone()


def restore_device(request: Request):
    if current_session(request):
        return None
    with db.transaction(conn):
        device = active_device(request)
        if not device:
            return None
        token = secrets.token_urlsafe(32)
        expires = min(time.time() + SESSION_SECONDS, device["expires_at"])
        conn.execute("DELETE FROM device_sessions WHERE session_hash IN "
                     "(SELECT token_hash FROM sessions WHERE expires_at<=?)", (time.time(),))
        conn.execute("DELETE FROM sessions WHERE expires_at<=?", (time.time(),))
        conn.execute("INSERT INTO sessions VALUES (?, ?)", (_hash(token), expires))
        conn.execute("INSERT INTO device_sessions VALUES (?, ?)", (_hash(token), device["token_hash"]))
    request.state.restored_session = token
    return token, max(1, int(expires - time.time()))


def revoke_device(device_hash: str):
    conn.execute("DELETE FROM sessions WHERE token_hash IN "
                 "(SELECT session_hash FROM device_sessions WHERE device_hash=?)", (device_hash,))
    conn.execute("DELETE FROM device_sessions WHERE device_hash=?", (device_hash,))
    conn.execute("DELETE FROM remembered_devices WHERE token_hash=?", (device_hash,))


def csrf_token(session: str) -> str:
    return hmac.new(settings.session_secret.encode(), b"csrf:" + session.encode(), hashlib.sha256).hexdigest()


def csrf_ok(session: str, supplied: str) -> bool:
    return bool(re.fullmatch(r"[a-f0-9]{64}", supplied)) and hmac.compare_digest(supplied, csrf_token(session))


def set_cookie(response: Response, name: str, value: str, age: int) -> None:
    response.set_cookie(name, value, max_age=age, path="/", httponly=True, samesite="lax", secure=settings.secure)


def pending_query(request: Request, state: str) -> str | None:
    ticket = request.cookies.get(PENDING_COOKIE)
    if state and ticket:
        return oauth.read_consent(ticket, "login:" + state, settings.session_secret)
    return None


def after_login(request: Request | None, state: str) -> RedirectResponse:
    target = "/account/ledger/new" if request and request.cookies.get(LEDGER_RETURN_COOKIE) == "1" else "/"
    if request and (query := pending_query(request, state)):
        try:
            auth = registered_authorization(query)
        except oauth.OAuthError as exc:
            logging.getLogger(__name__).info("Discarding pending authorization: %s", exc)
        else:
            target = "/oauth/authorize?" + auth.query()
    response = RedirectResponse(target, status_code=303)
    if request and LEDGER_RETURN_COOKIE in request.cookies:
        response.delete_cookie(LEDGER_RETURN_COOKIE, path="/")
    if request and PENDING_COOKIE in request.cookies:
        response.delete_cookie(PENDING_COOKIE, path="/")
    return response


def start_session(request: Request | None = None, state: str = "") -> RedirectResponse:
    token = secrets.token_urlsafe(32)
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
    conn.execute("INSERT INTO sessions VALUES (?, ?)", (_hash(token), time.time() + SESSION_SECONDS))
    response = after_login(request, state)
    set_cookie(response, SESSION_COOKIE, token, SESSION_SECONDS)
    return response


def render(request: Request, name: str, session: str | None, status: int = 200, **context) -> HTMLResponse:
    context |= {"csrf": csrf_token(session) if session else "", "steps": STEPS}
    return templates.TemplateResponse(request, name, context, status_code=status)


def message(request: Request, session: str | None, title: str, text: str, status: int) -> HTMLResponse:
    return render(request, "message.html", session, status, title=title, text=text)


def not_found(request: Request, session: str | None, text: str = "这个页面不存在，可能链接有误。") -> HTMLResponse:
    return message(request, session, "没有找到", text, 404)


def login_required() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


# ---------- login ----------

@app.get("/login")
def login(request: Request, resume: str = ""):
    if current_session(request):
        return after_login(request, resume)
    return render(request, "login.html", None, dev_login=settings.dev_login, resume=resume)


def _sign(message: str) -> str:
    return hmac.new(settings.session_secret.encode(), b"oauth:" + message.encode(), hashlib.sha256).hexdigest()


def new_state() -> str:
    # Self-verifying (nonce.expiry.mac): no server-side table an anonymous client could fill.
    message = f"{secrets.token_urlsafe(16)}.{int(time.time()) + 600}"
    return f"{message}.{_sign(message)}"


def state_ok(state: str, bound: str) -> bool:
    message, _, mac = state.rpartition(".")
    expiry = message.rpartition(".")[2]
    return (bool(bound) and hmac.compare_digest(state, bound) and hmac.compare_digest(mac, _sign(message))
            and expiry.isdigit() and int(expiry) > time.time())


@app.get("/auth/github")
def github_start(request: Request, resume: str = ""):
    state = resume if pending_query(request, resume) else new_state()
    response = RedirectResponse("https://github.com/login/oauth/authorize?" + urlencode({
        "client_id": settings.github_client_id, "redirect_uri": settings.base_url + "/oauth_callback",
        "state": state, "allow_signup": "false",
    }), status_code=303)
    set_cookie(response, STATE_COOKIE, state, 600)  # binds the state to this browser
    return response


@app.get("/oauth_callback")
def github_callback(request: Request, code: str = "", state: str = ""):
    if not (code and state_ok(state, request.cookies.get(STATE_COOKIE, ""))):
        return message(request, None, "登录已过期", "请回到首页重新登录。", 400)
    try:
        token = requests.post("https://github.com/login/oauth/access_token", timeout=10, headers={
            "Accept": "application/json"}, data={
            "client_id": settings.github_client_id, "client_secret": settings.github_client_secret,
            "code": code, "redirect_uri": settings.base_url + "/oauth_callback",
        }).json()["access_token"]
        user = requests.get("https://api.github.com/user", timeout=10, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}).json()
    except (requests.RequestException, KeyError, ValueError):
        return message(request, None, "GitHub 登录失败", "暂时无法连接 GitHub，请稍后回到首页重试。", 502)
    if str(user.get("id")) != settings.github_user_id:
        return message(request, None, "该账号无权访问", "这是个人使用的站点，只允许所有者的 GitHub 账号登录。", 403)
    response = start_session(request, state)
    response.delete_cookie(STATE_COOKIE, path="/")
    return response


@app.post("/auth/dev")
def dev_login(request: Request, resume: str = Form("")):
    if not settings.dev_login:
        return Response("Not Found", status_code=404, media_type="text/plain")
    return start_session(request, resume)


@app.post("/auth/remember-device")
def remember_device(request: Request, csrf: str = Form(""), return_path: str = Form("/")):
    session = current_session(request)
    if not session:
        return login_required()
    if not csrf_ok(session, csrf):
        return message(request, session, "页面已过期", "请刷新页面后重试。", 403)
    # Only a local ASCII path, never a URL, query or backslash-based network path.
    destination = return_path if re.fullmatch(r"/[A-Za-z0-9/_.*-]*", return_path) and not return_path.startswith("//") else "/"
    # Production persistent credentials require HTTPS; local development remains testable.
    if not settings.secure and not settings.dev_login:
        return message(request, session, "需要安全连接", "请通过 HTTPS 登记此设备。", 403)
    with db.transaction(conn):
        if not current_session(request):
            return login_required()
        if active_device(request):
            return RedirectResponse(destination, status_code=303)
        # A retried registration without its response cookie replaces, rather than orphans,
        # the previous credential belonging to this session.
        linked = conn.execute("SELECT device_hash FROM device_sessions WHERE session_hash=?", (session,)).fetchone()
        if linked:
            expiry = conn.execute("SELECT expires_at FROM sessions WHERE token_hash=?", (session,)).fetchone()[0]
            revoke_device(linked["device_hash"])
            conn.execute("INSERT INTO sessions VALUES (?, ?)", (session, expiry))
        token = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO remembered_devices VALUES (?, ?, ?)",
                     (_hash(token), settings.github_user_id, time.time() + DEVICE_SECONDS))
        conn.execute("INSERT OR REPLACE INTO device_sessions VALUES (?, ?)", (session, _hash(token)))
    response = RedirectResponse(destination, status_code=303)
    set_cookie(response, DEVICE_COOKIE, token, DEVICE_SECONDS)
    return response


@app.post("/logout")
def logout(request: Request, csrf: str = Form("")):
    session = current_session(request)
    if not session or not csrf_ok(session, csrf):
        return message(request, session, "页面已过期", "请刷新页面后再退出。", 403)
    with db.transaction(conn):
        linked = conn.execute("SELECT device_hash FROM device_sessions WHERE session_hash=?", (session,)).fetchone()
        if linked:
            revoke_device(linked["device_hash"])
        device = active_device(request)
        if device:
            revoke_device(device["token_hash"])
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (session,))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(DEVICE_COOKIE, path="/")
    response.delete_cookie(LEDGER_RETURN_COOKIE, path="/")
    response.delete_cookie(PENDING_COOKIE, path="/")
    response.delete_cookie(STATE_COOKIE, path="/")
    return response


# ---------- MCP OAuth ----------

@app.get("/.well-known/oauth-protected-resource/mcp")
def protected_resource():
    return {"resource": settings.base_url + "/mcp", "authorization_servers": [settings.base_url]}


@app.get("/.well-known/oauth-authorization-server")
def authorization_server():
    return {"issuer": settings.base_url, "authorization_endpoint": settings.base_url + "/oauth/authorize",
            "token_endpoint": settings.base_url + "/oauth/token", "grant_types_supported": ["authorization_code", "refresh_token"],
            "response_types_supported": ["code"], "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"]}


def owner_form_error(request: Request, session: str | None, csrf: str):
    if not session:
        return login_required()
    if not csrf_ok(session, csrf):
        return message(request, session, "页面已过期", "请刷新页面后重试。", 403)
    return None


@app.get("/oauth/authorize")
def authorize(request: Request):
    session = current_session(request)
    params = request.query_params
    client = oauth_store.get_client(conn, params.get("client_id", ""))
    uri = params.get("redirect_uri", "")
    if (len(params.getlist("client_id")) != 1 or len(params.getlist("redirect_uri")) != 1
            or not client or uri not in client["redirect_uris"]):
        return message(request, session, "无法授权", "客户端或回调地址未登记，或客户端已撤销。", 400)
    try:
        origin = oauth.redirect_origin(uri)
    except ValueError as exc:
        return message(request, session, "无法授权", str(exc), 400)
    try:
        if len(params) != len(params.multi_items()):
            raise oauth.OAuthError("invalid_request", "授权参数重复。")
        auth = oauth.Authorization.parse(params, settings.base_url + "/mcp")
    except oauth.OAuthError as exc:
        state = params.get("state") if len(params.getlist("state")) == 1 else None
        return RedirectResponse(oauth.callback(uri, state, error=exc.error), status_code=302)

    if not session:
        state = new_state()
        ticket = oauth.consent_form(auth, "login:" + state, settings.session_secret)
        if len(ticket) > 3800:  # Leave room for cookie attributes in the browser's 4 KiB limit.
            return message(request, None, "无法保存待授权请求", "请先登录，再从客户端重新连接。", 400)
        response = RedirectResponse("/login?" + urlencode({"resume": state}), status_code=303)
        set_cookie(response, PENDING_COOKIE, ticket, oauth.CONSENT_SECONDS)
        return response
    request.state.consent_origin = origin
    return render(request, "oauth_consent.html", session, client=client, redirect_uri=uri,
                  consent_id=oauth.consent_form(auth, session, settings.session_secret))


def registered_authorization(query: str) -> oauth.Authorization:
    auth = oauth.Authorization.parse(QueryParams(query), settings.base_url + "/mcp")
    client = oauth_store.get_client(conn, auth.client_id)
    if not client or auth.redirect_uri not in client["redirect_uris"]:
        raise oauth.OAuthError("invalid_request", "客户端或回调地址已失效，请从客户端重新连接。")
    return auth


@app.post("/oauth/authorize")
def consent(request: Request, csrf: str = Form(""), consent_id: str = Form(""), decision: str = Form("")):
    session = current_session(request)
    if error := owner_form_error(request, session, csrf):
        return error
    query = oauth.read_consent(consent_id, session, settings.session_secret)
    if not query or decision not in ("approve", "deny"):
        return message(request, session, "无法授权", "同意页已失效，请从客户端重新连接。", 400)
    try:
        auth = registered_authorization(query)
    except oauth.OAuthError as exc:
        return message(request, session, "无法授权", str(exc), 400)
    if decision == "deny":
        target = oauth.callback(auth.redirect_uri, auth.state, error="access_denied")
    else:
        try:
            code = oauth.approve(conn, auth, consent_id, settings.session_secret)
        except oauth.OAuthError as exc:
            return message(request, session, "无法授权", str(exc), 400)
        target = oauth.callback(auth.redirect_uri, auth.state, code=code)
    return RedirectResponse(target, status_code=302)


@app.post("/oauth/token")
async def oauth_token(request: Request):
    try:
        if "authorization" in request.headers:
            raise oauth.OAuthError("invalid_client", "public clients use token_endpoint_auth_method none")
        if request.headers.get("content-type", "").partition(";")[0].strip().lower() != "application/x-www-form-urlencoded":
            raise oauth.OAuthError("invalid_request", "use application/x-www-form-urlencoded")
        try:
            form = await request.form()
        except StarletteHTTPException as exc:
            raise oauth.OAuthError("invalid_request", "invalid form body") from exc
        if len(form) != len(form.multi_items()):
            raise oauth.OAuthError("invalid_request", "duplicate token request parameter")
        if form.get("client_secret") or form.get("client_assertion"):
            raise oauth.OAuthError("invalid_client", "public clients use token_endpoint_auth_method none")
        return await run_in_threadpool(oauth.exchange, conn, form, settings.base_url + "/mcp")
    except oauth.OAuthError as exc:
        return JSONResponse({"error": exc.error, "error_description": str(exc)}, status_code=400)


def connections_page(request: Request, session: str, status: int = 200, **fields):
    device = active_device(request)
    device_until = datetime.fromtimestamp(device["expires_at"], db.SHANGHAI).strftime("%Y-%m-%d %H:%M") if device else ""
    return render(request, "connections.html", session, status, clients=oauth_store.clients(conn),
                  grants=oauth_store.mcp_grants(conn), resource=settings.base_url + "/mcp",
                  device_until=device_until, **fields)


@app.get("/connections")
def connections(request: Request):
    session = current_session(request)
    return connections_page(request, session) if session else login_required()


@app.get("/connections/skill.zip")
def download_skill(request: Request):
    if not current_session(request):
        return login_required()
    return Response(skill_bundle.archive(), media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="a-share-{skill_bundle.version()}.zip"'})


@app.post("/connections/clients")
def register_client(request: Request, csrf: str = Form(""), name: str = Form(""), redirect_uris: str = Form("")):
    session = current_session(request)
    if error := owner_form_error(request, session, csrf):
        return error
    name = name.strip()
    uris = list(dict.fromkeys(line.strip() for line in redirect_uris.splitlines() if line.strip()))
    try:
        if not 1 <= len(name) <= 80 or not name.isprintable():
            raise ValueError("客户端名称须为 1–80 个可打印字符。")
        if not uris:
            raise ValueError("请填写至少一个回调地址。")
        for uri in uris:
            oauth.redirect_origin(uri)
    except ValueError as exc:
        return connections_page(request, session, 400, error=str(exc), client_name=name, redirect_uris=redirect_uris)
    with db.transaction(conn):
        if not any(client["name"] == name and set(client["redirect_uris"]) == set(uris) for client in oauth_store.clients(conn)):
            oauth_store.register_client(conn, secrets.token_urlsafe(18), name, uris)
    return RedirectResponse("/connections", status_code=303)


@app.post("/connections/clients/revoke")
def disconnect_client(request: Request, client_id: str = Form(...), csrf: str = Form("")):
    if error := owner_form_error(request, current_session(request), csrf):
        return error
    oauth_store.revoke_client(conn, client_id)
    return RedirectResponse("/connections", status_code=303)


@app.post("/connections/grants/{grant_id}/revoke")
def disconnect_grant(request: Request, grant_id: int, csrf: str = Form("")):
    if error := owner_form_error(request, current_session(request), csrf):
        return error
    with db.transaction(conn):
        if api_keys.is_active(conn, grant_id, "assistant"):
            api_keys.revoke(conn, grant_id)
    return RedirectResponse("/connections", status_code=303)


# ---------- pages ----------

def _prefers_json(request: Request) -> bool:
    # These owner pages default to HTML. Clients may explicitly prefer the original JSON view.
    weights = {}
    for item in request.headers.get("accept", "").lower().split(","):
        media_type, *params = (part.strip() for part in item.split(";"))
        if media_type not in ("text/html", "application/json"):
            continue
        quality = next((p[2:] for p in params if p.startswith("q=")), "1")
        if re.fullmatch(r"(?:0(?:\.\d{0,3})?|1(?:\.0{0,3})?)", quality):
            weights[media_type] = float(quality)
    return weights.get("application/json", 0) > weights.get("text/html", 0)


def _owner_read(request, template, read, *args):
    if not (session := current_session(request)):
        return login_required()
    as_json = template is None or _prefers_json(request)
    try:
        view = read(conn, *args)
    except service.RequestError as exc:
        response = (JSONResponse({"detail": str(exc)}, status_code=exc.status) if as_json else
                    message(request, session, "无法读取记录", str(exc), exc.status))
    else:
        response = JSONResponse(view) if as_json else render(request, template, session, view=view)
    if template is not None:
        response.headers["Vary"] = "Accept"
    return response


def _overview():
    # Policy-backed views load lazily so the web process stays free of market dependencies.
    from a_share_research import overview
    return overview


@app.get("/portfolio/overview")
def portfolio_overview(request: Request):
    return _owner_read(request, "overview.html", _overview().portfolio_overview)


@app.get("/stock/{code}/overview")
def company_overview(request: Request, code: str):
    return _owner_read(request, "overview.html", _overview().company_overview, code)


@app.get("/policy/evaluations/{evaluation_id}")
def monitor_detail(request: Request, evaluation_id: int):
    return _owner_read(request, "monitor_detail.html", _overview().monitor_detail, evaluation_id)


@app.get("/account/receipts/{receipt_id}")
def receipt_detail(request: Request, receipt_id: int):
    return _owner_read(request, "receipt_detail.html", _overview().receipt_detail, receipt_id)


async def _account_form(request: Request, page):
    session = current_session(request)
    if not session:
        response = login_required()
        if request.url.path == "/account/ledger/new":
            set_cookie(response, LEDGER_RETURN_COOKIE, "1", 600)
        return response
    client = ToolsClient.from_settings(settings)
    if request.method == "POST":
        async with request.form() as form:
            if error := owner_form_error(request, session, str(form.get("csrf", ""))):
                return error
            return await page(request, session, client, render, form)
    return await page(request, session, client, render)


@app.api_route("/account/evidence", methods=["GET", "POST"])
async def account_evidence(request: Request):
    return await _account_form(request, web_evidence.page)


@app.api_route("/account/ledger/new", methods=["GET", "POST"])
async def ledger_new(request: Request):
    return await _account_form(request, partial(web_ledger.page, connection=conn))


@app.api_route("/account/nav/new", methods=["GET", "POST"])
async def nav_new(request: Request):
    return await _account_form(request, web_nav.page)


@app.get("/account/nav/{artifact_sha256}")
def nav_detail(request: Request, artifact_sha256: str):
    return _owner_read(request, "nav_detail.html", _overview().nav_detail, artifact_sha256)


@app.get("/policy")
def policy_page(request: Request):
    if not (session := current_session(request)):
        return login_required()
    # This read uses the existing owner session. No API credential or policy runtime is created.
    from a_share_research.policy.presentation import owner_view
    try:
        view=owner_view(conn)
    except sqlite3.Error:
        logging.getLogger(__name__).exception('Policy page storage read failed')
        return message(request,session,"暂时无法读取政策","政策记录暂时无法读取，请稍后重试。",503)
    parameters=(view['manifest'] or {}).get('parameters',{})
    return render(request,"policy.html",session,policy=view,parameters=parameters,
                  parameters_json=json.dumps(parameters,ensure_ascii=False,indent=2))


@app.get("/research-library")
def research_library(request: Request, q: str = "", content: str = "", history: bool = False, scope: str = "",
                     page: int = Query(1, ge=1, le=10000)):
    if not (session := current_session(request)):
        return login_required()
    q = q.strip() or content.strip()  # `content` is the former second search box; old links keep working.
    whole_library = bool(q) and scope == "all"
    history = bool(q) and history
    results = reading.library_results(conn, q, history=history, whole_library=whole_library, page=page)
    base = {"q": q} if q else {}
    current = base | ({"history": "true"} if q and history else {}) | ({"scope": "all"} if q and whole_library else {})
    toggled = {k: v for k, v in current.items() if k != "history"} | ({} if history else {"history": "true"})
    return render(request, "research_library.html", session, q=q, history=history, whole_library=whole_library, page=page, **results,
                  history_url="/research-library?" + urlencode(toggled),
                  all_url="/research-library?" + urlencode(current | {"scope": "all"}),
                  previous_url="/research-library?" + urlencode(current | {"page": page - 1}),
                  next_url="/research-library?" + urlencode(current | {"page": page + 1}))


def home_page(request: Request, session: str, status: int = 200, **context) -> HTMLResponse:
    batch = reading.latest_batch(conn)
    performance = None
    if batch and batch["status"] in batches.ENTERED:
        loaded = batches.load(batch)
        performance = charts.batch_returns(loaded, batches.returns(loaded),
                                           {r["code"]: r["name"] for r in (loaded["q20"] or []) + (loaded.get("s10") or [])})
    latest = reading.latest_reports(conn, include_checkpoint=False)
    companies = [r for r in latest if r["own"]]
    pending, pending_more = reading.pending_reviews(conn)
    if companies:
        passed_before = {key: r for r in reading.latest_reports(
            conn, codes=[c["code"] for c in companies], passed=True, include_checkpoint=False)
            for key in (r["code"], stock_codes.normalize_key(r["code"]))}
        cards = reading.report_cards(conn, companies, passed_before)
    else:
        cards = []
    listed = {r['id'] for r in pending}
    return render(request, "home.html", session, status, companies=cards,
                  verdict_summary=charts.verdict_summary(companies),
                  pending=pending, pending_more=pending_more,
                  due=company_data.due_report(datetime.now(db.SHANGHAI).date()),
                  active=[r for r in reading.active_runs(conn) if r['id'] not in listed], batch=batch,
                  proposed=reading.proposed_batches(conn), performance=performance, **context)


@app.get("/")
def home(request: Request):
    if not (session := current_session(request)):
        return login_required()
    return home_page(request, session, request_id=uuid.uuid4().hex)


@app.post("/research")
def start_research(request: Request, q: str = Form(""), request_id: str = Form(""), csrf: str = Form(""),
                   again: str = Form(""), question: str = Form(""), parent_report_id: int | None = Form(None)):
    if not (session := current_session(request)):
        return login_required()
    if not csrf_ok(session, csrf):
        return message(request, session, "页面已过期", "请返回首页重新提交。", 403)
    try:
        result = service.request_research(conn, "web-owner", request_id, q, question=question,
                                          parent_report_id=parent_report_id, form=True, again=bool(again))
    except service.RequestError as exc:
        if exc.status == 503:
            return home_page(request, session, 503, request_id=request_id, q=q, question=question, error=str(exc))
        return message(request, session, "无法提交研究", str(exc), exc.status)
    if "run_id" in result:
        return RedirectResponse(result["path"], status_code=303)
    return home_page(request, session, request_id=request_id, question=question, **result)


@app.get("/runs/{run_id}")
def run_page(request: Request, run_id: int):
    if not (session := current_session(request)):
        return login_required()
    run = reading.run(conn, run_id)
    if run is None:
        return not_found(request, session)
    if run["kind"] == "price" and run["status"] == "done":
        return RedirectResponse(f"/stock/{run['code']}#price", status_code=303)
    report = reading.report_for_run(conn, run_id)
    if report and run["status"] in ("done", "failed"):
        return RedirectResponse(f"/reports/{report['id']}", status_code=303)
    # Only web runs share the worker queue; command-line runs execute in their own process.
    # Same order as the worker: running first, then owner submissions, then batch research.
    ahead = reading.queue_ahead(conn, run_id)[0]
    return render(request, "run.html", session, run=run, ahead=ahead, elapsed=_elapsed(run),
                  diagnostics=service.run_view(conn, run_id=run_id)["diagnostics"]
                  if run["kind"] in ("research", "diagnostic") and run["status"] == "failed" else None,
                  feedback_retry=service.feedback_retry_payload(run) is not None,
                  retry=service.retry_view(run),
                  request_id=uuid.uuid4().hex)


@app.post('/runs/{run_id}/retry')
def retry_run(request: Request, run_id: int, csrf: str = Form('')):
    if not (session := current_session(request)):
        return login_required()
    if not csrf_ok(session, csrf):
        return message(request, session, '页面已过期', '请返回任务页面重新提交。', 403)
    try:
        next_run = service.retry_run(conn, run_id)
    except service.RequestError as exc:
        return message(request, session, '无法重试', str(exc), exc.status)
    return RedirectResponse(f'/runs/{next_run}', status_code=303)


@app.post('/runs/{run_id}/retry-feedback')
def retry_feedback(request: Request, run_id: int, request_id: str = Form(''), csrf: str = Form('')):
    if not (session := current_session(request)):
        return login_required()
    if not csrf_ok(session, csrf):
        return message(request, session, '页面已过期', '请返回核查任务重新提交。', 403)
    try:
        next_run = service.retry_feedback(conn, run_id, request_id, requester='web-owner')
    except service.RequestError as exc:
        return message(request, session, '无法重试核查', str(exc), exc.status)
    return RedirectResponse(f'/runs/{next_run}', status_code=303)


def _elapsed(run) -> int | None:
    if not run["started_at"]:
        return None
    end = datetime.fromisoformat(run["finished_at"]) if run["finished_at"] else datetime.now(db.SHANGHAI)
    return int((end - datetime.fromisoformat(run["started_at"])).total_seconds())


@app.get("/reports/{report_id}")
def report_page(request: Request, report_id: int):
    if not (session := current_session(request)):
        return login_required()
    row = reading.report(conn, report_id)
    if row is None:
        return not_found(request, session)
    run = reading.report_run(conn, row["run_id"])
    if not service.ready(run["status"], row["passed"]):
        return message(request, session, "报告尚未就绪", "报告尚未就绪或状态不一致", 409)
    latest = reading.latest_reports(conn, codes=[row['code']], include_checkpoint=False) if run['kind'] == 'research' else []
    newer_report = latest[0] if latest and latest[0]['id'] > report_id else None
    body = report_body.load(row["body"])
    feedback = (json.loads(run["request"] or "{}") or {}).get("feedback")
    feedback_label = ""
    if feedback:
        section, _, index = feedback["location"].partition("[")
        feedback_label = {"summary": "一句话结论", "answer": "问题回答", "risks": "风险与反证",
                          "business": "业务", "financial": "财务", "methods": "研究方法",
                          "checkpoints": "下期检查点", "prior_status": "旧议程状态"}[section]
        if index:
            feedback_label += f"第 {int(index[:-1]) + 1} 条"
    evidence = reading.evidence_context(body, row['coverage'])
    rows = {r["id"]: r for r in body["table"]}
    fin = [r for r in body["table"] if r["id"].startswith("T.")]
    return render(request, "report.html", session, report=row, body=body, evidence=evidence, purpose=run["kind"], newer_report=newer_report,
                  source_view=report_source_view(body, (json.loads(run["material"] or "{}") or {}).get("data")),
                  figures=charts.financial_charts(body, fin), vitals=charts.vitals(body, fin),
                  peer_snapshots=reading.peer_snapshots(body) if row['passed'] else [],
                  feedback_retry=service.feedback_retry_payload(run) is not None,
                  retry=service.retry_view(run),
                  feedback=feedback, feedback_label=feedback_label, comparison=reading.comparison(conn, row),
                  replay_of=body.get("replay_of") or run["replay_of"],
                  rows=rows, request_id=uuid.uuid4().hex,
                  market=[r for r in body["table"] if r["id"].startswith("M.")],
                  fin=fin, price=reading.latest_price(conn, row["code"]),
                  prior_agenda=reading.prior_agenda(conn, body) if row['passed'] else [],
                  followups=reading.followups(conn, row['code'], report_id))


@app.post("/reports/{report_id}/feedback")
def submit_feedback(request: Request, report_id: int, location: str = Form(""), text: str = Form(""),
                    request_id: str = Form(""), csrf: str = Form("")):
    if not (session := current_session(request)):
        return login_required()
    if not csrf_ok(session, csrf):
        return message(request, session, "页面已过期", "请返回报告重新提交。", 403)
    try:
        result = service.request_feedback(conn, "web-owner", report_id, request_id, location, text)
    except service.RequestError as exc:
        return message(request, session, "无法提交核查请求", str(exc), exc.status)
    return RedirectResponse(result["path"], status_code=303)


# JSON clients accept persistent API keys and legacy agent sessions, never cookies.
def _api_credential(request: Request) -> tuple[str, api_keys.Principal | None]:
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(401, "需要研究 API bearer token")
    if principal := api_keys.authenticate(conn, header[7:]):
        if principal.role == "owner":
            raise HTTPException(403, "所有者管理凭据不能调用研究 API")
        key = service.requester(conn, principal)
    else:
        key = "agent:" + _hash(header[7:])
        row = conn.execute("SELECT expires_at FROM sessions WHERE token_hash = ?", (key,)).fetchone()
        if row is None or row["expires_at"] <= time.time():
            raise HTTPException(401, "研究凭据无效、已撤销或临时令牌已过期")
    if request.headers.get("x-research-version", service.VERSION) != service.VERSION:
        raise HTTPException(400, "不支持的研究合同版本")
    return key, principal


def api_session(request: Request) -> str:
    requester, _ = _api_credential(request)
    return requester


@app.exception_handler(service.RequestError)
async def api_error(request: Request, exc: service.RequestError):
    return JSONResponse({"version": service.VERSION, "error": str(exc)}, status_code=exc.status)


@app.post("/api/research", status_code=202)
def api_research(request: Request, body: ResearchRequest):
    result = service.request_research(conn, api_session(request), **body.model_dump())
    return {"version": service.VERSION, "run_id": result["run_id"]}


@app.get("/api/request")
def api_request(request: Request, request_id: str):
    return service.research_status(conn, requester=api_session(request), request_id=request_id, compact=False)


@app.get("/api/research/budget")
def api_research_budget(request: Request):
    api_session(request)
    return {"version": service.VERSION, "budget": service.run_budget(conn)}


@app.get("/api/runs/{run_id}")
def api_run(request: Request, run_id: int):
    api_session(request)
    return service.run_summary(conn, run_id)


@app.get("/api/schema")
def api_schema(request: Request):
    api_session(request)
    return {"version": service.VERSION, "schema_version": report_body.SCHEMA_VERSION,
            "judgment_contract": JUDGMENT_CONTRACT, "report": report_body.json_schema(),
            "views": {"compact": report_body.COMPACT_FIELDS, "card": report_body.CARD_FIELDS,
                      "failed": report_body.FAILED_FIELDS}}


@app.get("/api/reports")
def api_reports(request: Request, code: str):
    api_session(request)
    if not stock_codes.KEY_RE.fullmatch(code):
        raise service.RequestError("公司代码必须为六位数字")
    rows = reading.api_reports(conn, code)
    return {"version": service.VERSION, "reports": [dict(row) for row in rows]}


@app.get("/api/search")
def api_search(request: Request, q: str, code: str | None = None, section: str | None = None, history: bool = False,
               page: int = Query(1, ge=1, le=10000)):
    api_session(request)
    return service.research_search(conn, q, code=code, section=section, history=history, page=page)


class MonthlyBatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    month: str


@app.post('/api/batches/monthly')
def api_monthly_batch(request: Request, body: MonthlyBatchRequest):
    api_session(request)
    with batch_writes:
        return service.monthly_batch(conn, body.month, submit=True)


@app.get('/api/batches/monthly/{month}')
def api_monthly_status(request: Request, month: str):
    api_session(request)
    return service.monthly_batch(conn, month)


@app.get("/api/companies/{code}/card")
def api_company_card(request: Request, code: str):
    api_session(request)
    return service.company_card(conn, code)


@app.get('/api/cards')
def api_cards(request: Request):
    api_session(request)
    return service.company_cards(conn)


class SubscriptionsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    codes: list[str]


@app.put('/api/subscriptions')
def api_replace_subscriptions(request: Request, body: SubscriptionsRequest):
    api_session(request)
    from a_share_research import scans
    return {'version': service.VERSION, 'subscriptions': scans.replace_subscriptions(conn, body.codes)}


@app.get('/api/subscriptions')
def api_subscriptions(request: Request):
    api_session(request)
    from a_share_research import scans
    return {'version': service.VERSION, 'subscriptions': scans.subscriptions(conn)}


@app.post('/api/scans', status_code=202)
def api_submit_scan(request: Request):
    api_session(request)
    from a_share_research import scans
    return {'version': service.VERSION, 'scan_id': scans.submit(conn)}


@app.put('/api/scan-requests/{request_id}', status_code=202)
def api_submit_scan_request(request: Request, request_id: str):
    requester = api_session(request)
    from a_share_research import scans
    return {'version': service.VERSION, 'request_id': request_id,
            'scan_id': scans.submit_request(conn, requester, request_id)}


@app.get('/api/scans/weekly')
def api_weekly_scan(request: Request):
    api_session(request)
    from a_share_research import scans
    return scans.weekly(conn, account_sources=False)


@app.get('/api/scans/{scan_id}')
def api_scan(request: Request, scan_id: int):
    api_session(request)
    from a_share_research import scans
    return scans.view(conn, scan_id)


@app.get('/subscriptions')
def subscription_detail(request: Request):
    from a_share_research import scans
    return _owner_read(request, None, scans.subscriptions)


@app.get('/scans/weekly')
def weekly_scan_detail(request: Request):
    from a_share_research import scans
    return _owner_read(request, None, scans.weekly)


@app.get('/scans/{scan_id}')
def scan_detail(request: Request, scan_id: int):
    from a_share_research import scans
    return _owner_read(request, None, scans.view, scan_id)


@app.get("/api/reports/{report_id}")
def api_report(request: Request, report_id: int, compact: bool = False):
    api_session(request)
    return service.report_view(conn, report_id, compact=compact)


@app.post("/api/reports/{report_id}/feedback", status_code=202)
def api_feedback(request: Request, report_id: int, body: FeedbackRequest):
    result = service.request_feedback(conn, api_session(request), report_id, **body.model_dump())
    return {"version": service.VERSION, "run_id": result["run_id"]}


@app.post("/api/session/revoke")
def api_revoke(request: Request):
    key, principal = _api_credential(request)
    if principal:
        api_keys.revoke(conn, principal.key_id)
    else:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (key,))
    return {"version": service.VERSION, "revoked": True}


@app.get("/stock/{code}")
def stock_page(request: Request, code: str, report: int | None = None):
    if not (session := current_session(request)):
        return login_required()
    try:
        code = stock_codes.normalize_key(code)
    except ValueError:
        return not_found(request, session, "公司代码不存在。")
    from a_share_research import scans
    selected = None
    with db.transaction(conn, immediate=False):
        reports = reading.stock_reports(conn, code, summaries=True)
        scan_history = scans.ScanHistory(conn)
        overview = _overview().company_overview(conn, code, history=scan_history)
        if reports:
            # Formal research heads the page; a feedback check or replay leads only when nothing else exists.
            ordered = sorted(reports, key=lambda r: (bool(r["replay_of"]), bool(r["feedback_text"])))
            head = ordered[0]
            selected = next((r for r in reports if r['id'] == report), None) if report is not None else head
            if selected is not None:
                # The newest non-replay report with an operating series lends its vitals to the company page,
                # falling back to the newest non-replay report's saved T.* financial table when no series exists.
                loaded = {}

                def full(row):
                    if row['id'] not in loaded:
                        record = reading.report(conn, row['id'])
                        loaded[row['id']] = (record, report_body.load(record['body']))
                    return loaded[row['id']]

                latest = next(((r, v) for r in ordered if r['operating_series']
                               and (v := charts.vitals({'series': json.loads(r['operating_series'])}))), None)
                if latest is None:
                    for r in ordered:
                        b = full(r)[1]
                        t = [x for x in (b.get("table") or []) if isinstance(x, dict) and str(x.get("id", "")).startswith("T.")]
                        if v := charts.vitals(b, t):
                            latest = (r, v)
                            break
                latest = latest or (None, None)
                before = next((r for r in ordered if r["passed"] and not r["replay_of"]), None) if not head["passed"] else None
                card = reading.card(head, full(head)[1], before, checks=service.card_checks(conn, head["id"])) | {
                    "check_failure": scan_history.checks.get((code, head["id"])),
                    "coverage": head["coverage"],
                    "created_at": head["created_at"]}
                price = reading.latest_price(conn, code)
                comp = reading.comparison(conn, full(selected)[0])
    if not reports:
        return render(request, "message.html", session, 404, title="还没有研究过这家公司", text=code,
                      company_overview=overview)
    if selected is None:
        return not_found(request, session, "这份报告不属于当前公司。")
    return render(request, "stock.html", session, reports=reports, code=code, request_id=uuid.uuid4().hex, card=card,
                  company_overview=overview,
                  due=company_data.due_report(datetime.now(db.SHANGHAI).date()),
                  vitals=latest[1], vitals_report=latest[0],
                  price=price, comparison=comp,
                  selected=selected, track=charts.verdict_track(reports))


@app.post("/price")
def start_price(request: Request, code: str = Form(""), request_id: str = Form(""), csrf: str = Form("")):
    if not (session := current_session(request)):
        return login_required()
    if not csrf_ok(session, csrf):
        return message(request, session, "页面已过期", "请返回首页重新提交。", 403)
    try:
        run_id = service.submit_price(conn, code, request_id)
    except service.RequestError as exc:
        return not_found(request, session, str(exc))
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


# ---------- candidate batches ----------

    # The thread-local web connection serializes writes per thread; batches serialize updates explicitly.
batch_writes = threading.Lock()

@app.get("/batches")
def batches_page(request: Request):
    if not (session := current_session(request)):
        return login_required()
    rows = reading.batch_list(conn)
    return render(request, "batches.html", session, batches=[batches.load(r) for r in rows],
                  request_id=uuid.uuid4().hex)


@app.post("/batches")
def start_batch(request: Request, request_id: str = Form(""), csrf: str = Form("")):
    if not (session := current_session(request)):
        return login_required()
    if not csrf_ok(session, csrf):
        return message(request, session, "页面已过期", "请返回候选初筛页面重新提交。", 403)
    if not 1 <= len(request_id) <= 128:
        return message(request, session, "无法开始初筛", "请求编号无效，请返回重试。", 422)
    try:
        with batch_writes:
            batch_id = service.create_batch(conn, request_id)
    except service.RequestError as exc:
        return message(request, session, "无法开始初筛", str(exc), exc.status)
    return RedirectResponse(f"/batches/{batch_id}", status_code=303)


@app.get("/batches/{batch_id}")
def batch_page(request: Request, batch_id: int):
    if not (session := current_session(request)):
        return login_required()
    row = reading.batch(conn, batch_id)
    if row is None:
        return not_found(request, session)
    batch = batches.load(row)
    dual = batch["rule"]["version"] in {"screen-v1.2", "screen-v1.3"}
    if batch["status"] == "researching":
        batch["q20"] = reading.research_state(conn, batch["q20"])[1]
        if dual:
            batch["s10"] = reading.research_state(conn, batch.get("s10") or [])[1]
    # Frozen rows keep the ids of failed runs that were later deleted; show their error without a dead link.
    linked = [r for r in (batch["q20"] or []) + (batch.get("s10") or []) if r.get("run_id")]
    existing = reading.existing_run_ids(conn, [r["run_id"] for r in linked])
    for r in linked:
        if r["run_id"] not in existing:
            r["run_id"] = None
    draft = ((batch["selection"] or {}).get("draft") or {})
    results = batches.returns(batch)
    names = {r["code"]: r["name"] for r in (batch["q20"] or []) + (batch.get("s10") or [])}
    pool_batches = [(batch, "", "codes", "大市值组 · Q20 / Q5 / L5", "总市值 ≥100 亿元，近 20 日日均成交额 ≥1 亿元。", "机械前五", "前五")]
    if dual:
        small = dict(batch, q20=batch.get("s10") or [], q5=batch.get("sq3") or [],
                     l5=batch.get("sl3"), selection=batch.get("small_selection"),
                     universe=(batch.get("universe") or {}).get("small"),
                     rule=dict(batch["rule"], **batch["rule"]["small"]))
        pool_batches.append((small, "small-", "small_codes", "小市值组 · S10 / SQ3 / SL3",
                             "30 亿元 ≤ 总市值 <100 亿元，近 20 日日均成交额 ≥3000 万元；独立排名与比较，先只作模拟观察。", "机械前三", "前三"))
    pools = []
    for item, prefix, codes_field, label, description, mechanical_label, mechanical_short in pool_batches:
        pool_draft = ((item.get("selection") or {}).get("draft") or {})
        pools.append({"batch": item, "prefix": prefix, "codes_field": codes_field,
                      "label": label, "description": description,
                      "mechanical_label": mechanical_label, "mechanical_short": mechanical_short,
                      "draft": pool_draft, "draft_codes": [p["code"] for p in pool_draft.get("picks", [])],
                      "overview": charts.batch_overview(item), "sparks": charts.candidate_sparks(item.get("q20") or []),
                      "market_strip": charts.market_strip(item)})
    return render(request, "batch_v12.html" if dual else "batch.html", session, batch=batch, draft=draft, pools=pools,
                  draft_codes=[p["code"] for p in draft.get("picks", [])],
                  overview=pools[0]["overview"], sparks=pools[0]["sparks"], market_strip=pools[0]["market_strip"],
                  results=results, names=names, performance=charts.batch_returns(batch, results, names),
                  frozen_by=batches.FROZEN_BY)


@app.post("/batches/{batch_id}/freeze")
async def freeze_batch(request: Request, batch_id: int):
    if not (session := current_session(request)):
        return login_required()
    form = await request.form()
    if not csrf_ok(session, str(form.get("csrf", ""))):
        return message(request, session, "页面已过期", "请返回批次页面重新提交。", 403)
    try:
        with batch_writes:
            service.freeze_batch(conn, batch_id, [str(c) for c in form.getlist("codes")], str(form.get("note", "")),
                                 small_codes=[str(c) for c in form.getlist("small_codes")])
    except service.RequestError as exc:
        return message(request, session, "无法冻结", str(exc), exc.status)
    return RedirectResponse(f"/batches/{batch_id}", status_code=303)
