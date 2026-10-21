"""Server-rendered web app: owner login, start research, follow progress, read reports.

Holds no model or market credentials; the worker does all research.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, urlparse

import requests
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from a_share_research import db, sources

HERE = Path(__file__).parent
SESSION_COOKIE = "session"
STATE_COOKIE = "oauth_state"
SESSION_SECONDS = 8 * 3600
STEPS = {"gather": "取资料", "compose": "撰写报告", "review": "独立复核", "repair": "按复核意见修改"}
CSP = ("default-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self' https://github.com; "
       "object-src 'none'; img-src 'self'")
SECURITY_HEADERS = {
    "content-security-policy": CSP,
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

    @classmethod
    def from_env(cls) -> Settings:
        settings = cls(
            base_url=os.environ.get("PUBLIC_BASE_URL", "").rstrip("/"),
            session_secret=os.environ.get("SESSION_SECRET", ""),
            dev_login=os.environ.get("DEV_LOGIN") == "1",
            github_client_id=os.environ.get("GITHUB_CLIENT_ID", ""),
            github_client_secret=os.environ.get("GITHUB_CLIENT_SECRET", ""),
            github_user_id=os.environ.get("GITHUB_USER_ID", ""),
        )
        url = urlparse(settings.base_url)
        if url.scheme not in ("http", "https") or not url.hostname:
            raise SystemExit("PUBLIC_BASE_URL must be an absolute http(s) URL")
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


settings = Settings.from_env()
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
conn = db.connect()
OAUTH_STATES: dict[str, float] = {}


# ---------- security ----------

@app.middleware("http")
async def guard(request: Request, call_next):
    if request.headers.get("host", "").lower() != urlparse(settings.base_url).netloc.lower():
        return Response("Invalid Host header", status_code=403, media_type="text/plain")
    if request.method not in ("GET", "HEAD") and not same_origin(request):
        return Response("Cross-origin request refused", status_code=403, media_type="text/plain")
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    if not request.url.path.startswith("/static/"):
        response.headers["cache-control"] = "no-store, no-transform"
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
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    row = conn.execute("SELECT expires_at FROM sessions WHERE token_hash = ?", (_hash(token),)).fetchone()
    return _hash(token) if row and row["expires_at"] > time.time() else None


def csrf_token(session: str) -> str:
    return hmac.new(settings.session_secret.encode(), b"csrf:" + session.encode(), hashlib.sha256).hexdigest()


def csrf_ok(session: str, supplied: str) -> bool:
    return hmac.compare_digest(supplied, csrf_token(session))


def set_cookie(response: Response, name: str, value: str, age: int) -> None:
    response.set_cookie(name, value, max_age=age, path="/", httponly=True, samesite="lax", secure=settings.secure)


def start_session() -> RedirectResponse:
    token = secrets.token_urlsafe(32)
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))
    conn.execute("INSERT INTO sessions VALUES (?, ?)", (_hash(token), time.time() + SESSION_SECONDS))
    response = RedirectResponse("/", status_code=303)
    set_cookie(response, SESSION_COOKIE, token, SESSION_SECONDS)
    return response


def render(request: Request, name: str, session: str | None, status: int = 200, **context) -> HTMLResponse:
    context |= {"csrf": csrf_token(session) if session else "", "steps": STEPS}
    return templates.TemplateResponse(request, name, context, status_code=status)


def login_required() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


# ---------- login ----------

@app.get("/login")
def login(request: Request):
    return render(request, "login.html", None, dev_login=settings.dev_login)


@app.get("/auth/github")
def github_start():
    now = time.time()
    for state in [s for s, expiry in OAUTH_STATES.items() if expiry < now]:
        OAUTH_STATES.pop(state)
    if len(OAUTH_STATES) > 64:
        return Response("登录请求过多，请稍后再试", status_code=429, media_type="text/plain")
    state = secrets.token_urlsafe(24)
    OAUTH_STATES[state] = now + 600
    response = RedirectResponse("https://github.com/login/oauth/authorize?" + urlencode({
        "client_id": settings.github_client_id, "redirect_uri": settings.base_url + "/oauth_callback",
        "state": state, "allow_signup": "false",
    }), status_code=303)
    set_cookie(response, STATE_COOKIE, state, 600)  # binds the state to this browser
    return response


@app.get("/oauth_callback")
def github_callback(request: Request, code: str = "", state: str = ""):
    bound = request.cookies.get(STATE_COOKIE, "")
    expiry = OAUTH_STATES.pop(state, 0) if state else 0
    if not (code and bound and expiry > time.time() and hmac.compare_digest(state, bound)):
        return Response("登录已过期，请重新登录", status_code=400, media_type="text/plain")
    try:
        token = requests.post("https://github.com/login/oauth/access_token", timeout=10, headers={
            "Accept": "application/json"}, data={
            "client_id": settings.github_client_id, "client_secret": settings.github_client_secret,
            "code": code, "redirect_uri": settings.base_url + "/oauth_callback",
        }).json()["access_token"]
        user = requests.get("https://api.github.com/user", timeout=10, headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}).json()
    except (requests.RequestException, KeyError, ValueError):
        return Response("GitHub 登录失败，请重试", status_code=502, media_type="text/plain")
    if str(user.get("id")) != settings.github_user_id:
        return Response("该账号无权访问", status_code=403, media_type="text/plain")
    response = start_session()
    response.delete_cookie(STATE_COOKIE, path="/")
    return response


@app.post("/auth/dev")
def dev_login():
    if not settings.dev_login:
        return Response("Not Found", status_code=404, media_type="text/plain")
    return start_session()


@app.post("/logout")
def logout(request: Request, csrf: str = Form("")):
    session = current_session(request)
    if session and csrf_ok(session, csrf):
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (session,))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# ---------- pages ----------

def latest_reports() -> list:
    return conn.execute(
        "SELECT r.* FROM reports r JOIN (SELECT code, MAX(id) AS id FROM reports GROUP BY code) l ON r.id = l.id"
        " ORDER BY r.id DESC"
    ).fetchall()


def active_runs() -> list:
    return conn.execute("SELECT * FROM runs WHERE status IN ('queued', 'running') ORDER BY id").fetchall()


@app.get("/")
def home(request: Request):
    if not (session := current_session(request)):
        return login_required()
    return render(request, "home.html", session, companies=latest_reports(), active=active_runs(),
                  request_id=uuid.uuid4().hex)


def resolve(query: str) -> tuple[str | None, list[dict]]:
    """A 6-digit code or a (partial) short name -> code, or candidate companies when ambiguous."""
    query = re.sub(r"\s+", "", query).upper().removesuffix(".SH").removesuffix(".SZ").removesuffix(".BJ")
    stocks = sources.stock_list()
    if query in stocks:
        return query, []
    matches = [s for s in stocks.values() if query and query in s["zwjc"].replace(" ", "")]
    exact = [s for s in matches if s["zwjc"].replace(" ", "") == query]
    if len(exact) == 1 or len(matches) == 1:
        return (exact or matches)[0]["code"], []
    return None, matches[:10]


@app.post("/research")
def start_research(request: Request, q: str = Form(""), request_id: str = Form(""), csrf: str = Form(""),
                   again: str = Form("")):
    if not (session := current_session(request)):
        return login_required()
    if not csrf_ok(session, csrf):
        return Response("表单已过期，请返回首页重试", status_code=403, media_type="text/plain")
    try:
        code, choices = resolve(q)
    except requests.RequestException:
        return render(request, "home.html", session, 503, companies=latest_reports(), active=active_runs(),
                      request_id=request_id, q=q, error="暂时无法连接巨潮资讯核对公司，请稍后再试")
    if code is None:
        error = "找到多家公司，请选择一家" if choices else "没有找到这家公司，请输入 6 位代码或简称"
        return render(request, "home.html", session, companies=latest_reports(), active=active_runs(),
                      request_id=request_id, q=q, error=error, choices=choices)
    today = datetime.now(db.SHANGHAI).date().isoformat()
    recent = conn.execute(
        "SELECT * FROM reports WHERE code LIKE ? AND passed = 1 AND created_at >= ? ORDER BY id DESC LIMIT 1",
        (code + ".%", today),
    ).fetchone()
    if recent and not again:
        return render(request, "home.html", session, companies=latest_reports(), active=active_runs(),
                      request_id=request_id, q=code, recent=recent)
    run_id = db.create_run(conn, code, request_id or None)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


@app.get("/runs/{run_id}")
def run_page(request: Request, run_id: int):
    if not (session := current_session(request)):
        return login_required()
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        return Response("Not Found", status_code=404, media_type="text/plain")
    report = conn.execute("SELECT id FROM reports WHERE run_id = ?", (run_id,)).fetchone()
    if report and run["status"] in ("done", "failed"):
        return RedirectResponse(f"/reports/{report['id']}", status_code=303)
    ahead = conn.execute("SELECT COUNT(*) FROM runs WHERE status IN ('queued', 'running') AND id < ?",
                         (run_id,)).fetchone()[0]
    return render(request, "run.html", session, run=run, ahead=ahead, elapsed=_elapsed(run),
                  request_id=uuid.uuid4().hex)


def _elapsed(run) -> int | None:
    if not run["started_at"]:
        return None
    end = datetime.fromisoformat(run["finished_at"]) if run["finished_at"] else datetime.now(db.SHANGHAI)
    return int((end - datetime.fromisoformat(run["started_at"])).total_seconds())


@app.get("/reports/{report_id}")
def report_page(request: Request, report_id: int):
    if not (session := current_session(request)):
        return login_required()
    row = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    if row is None:
        return Response("Not Found", status_code=404, media_type="text/plain")
    body = json.loads(row["body"])
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (row["run_id"],)).fetchone()
    return render(request, "report.html", session, report=row, body=body, run=run,
                  rows={r["id"]: r for r in body["table"]}, request_id=uuid.uuid4().hex,
                  market=[r for r in body["table"] if r["id"].startswith("M.")],
                  fin=[r for r in body["table"] if r["id"].startswith("T.")])


@app.get("/stock/{code}")
def stock_page(request: Request, code: str):
    if not (session := current_session(request)):
        return login_required()
    reports = conn.execute("SELECT * FROM reports WHERE code = ? ORDER BY id DESC", (code,)).fetchall()
    if not reports:
        return Response("Not Found", status_code=404, media_type="text/plain")
    return render(request, "stock.html", session, reports=reports, code=code, request_id=uuid.uuid4().hex)
