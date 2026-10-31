"""Server-rendered web app: owner login, start research, follow progress, read reports.

Holds no model or market credentials; the worker does all research.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Self
from urllib.parse import urlencode, urlparse

import requests
from fastapi import FastAPI, Form, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, Response, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, ConfigDict

from a_share_research import charts, db, batches, companies as company_data, service, reading, progress
from a_share_research.evidence import report_source_view, citation_link

HERE = Path(__file__).parent
SESSION_COOKIE = "session"
STATE_COOKIE = "oauth_state"
SESSION_SECONDS = 8 * 3600
STEPS = {"gather": "取资料", "explore": "补读披露与计算", "compose": "撰写报告", "review": "独立复核", "repair": "按复核意见修改",
         "price": "计算价格参考"}
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
    def from_env(cls) -> Self:
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
# Busts browser caches of the stylesheet and script on each deploy that changes them.
ASSET_VERSION = hashlib.sha256(b"".join((HERE / "static" / name).read_bytes() for name in ("app.css", "app.js"))).hexdigest()[:12]
templates.env.globals["asset_version"] = ASSET_VERSION
templates.env.globals["funnel_chart"] = charts.funnel
templates.env.globals["citation_link"] = citation_link
templates.env.globals["report_source_view"] = report_source_view
conn = ThreadLocalConnectionProxy()


# ---------- security ----------

@app.middleware("http")
async def guard(request: Request, call_next):
    if request.headers.get("host", "").lower() != urlparse(settings.base_url).netloc.lower():
        return Response("Invalid Host header", status_code=403, media_type="text/plain")
    if request.method not in ("GET", "HEAD") and not request.url.path.startswith("/api/") and not same_origin(request):
        return Response("Cross-origin request refused", status_code=403, media_type="text/plain")
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    if not request.url.path.startswith("/static/"):
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


def message(request: Request, session: str | None, title: str, text: str, status: int) -> HTMLResponse:
    return render(request, "message.html", session, status, title=title, text=text)


def not_found(request: Request, session: str | None, text: str = "这个页面不存在，可能链接有误。") -> HTMLResponse:
    return message(request, session, "没有找到", text, 404)


def login_required() -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)


# ---------- login ----------

@app.get("/login")
def login(request: Request):
    return render(request, "login.html", None, dev_login=settings.dev_login)


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
def github_start():
    state = new_state()
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

@app.get("/research-library")
def research_library(request: Request, q: str = "", page: int = Query(1, ge=1, le=10000)):
    if not (session := current_session(request)):
        return login_required()
    q = q.strip()
    error = "请将查询缩短到 100 个字符以内。" if len(q) > 100 else ""
    rows, more = reading.search_reports(conn, q, page) if not error else ([], False)
    params = {"q": q} if q else {}
    return render(request, "research_library.html", session, companies=rows, q=q, page=page, more=more,
                  error=error, previous_url="/research-library?" + urlencode(params | {"page": page - 1}),
                  next_url="/research-library?" + urlencode(params | {"page": page + 1}))


def home_page(request: Request, session: str, status: int = 200, **context) -> HTMLResponse:
    batch = reading.latest_batch(conn)
    performance = None
    if batch and batch["status"] == "tracking":
        loaded = batches.load(batch)
        performance = charts.batch_returns(loaded, batches.returns(loaded),
                                           {r["code"]: r["name"] for r in loaded["q20"] or []})
    latest = reading.latest_reports(conn, include_checkpoint=False)
    companies = [r for r in latest if r["own"]]
    batch_only = sum(1 for r in latest if not r["own"])
    pending, pending_more = reading.pending_reviews(conn)
    if companies:
        passed_before = {r["code"]: r for r in reading.latest_reports(
            conn, codes=[c["code"] for c in companies], passed=True, include_checkpoint=False)}
        cards = reading.report_cards(conn, companies, passed_before)
    else:
        cards = []
    listed = {r['id'] for r in pending}
    return render(request, "home.html", session, status, companies=cards, batch_only=batch_only,
                  verdict_summary=charts.verdict_summary(companies),
                  pending=pending, pending_more=pending_more,
                  due=company_data.due_report(datetime.now(db.SHANGHAI).date()),
                  active=[r for r in reading.active_runs(conn) if r['id'] not in listed], batch=batch,
                  performance=performance, **context)


@app.get("/")
def home(request: Request):
    if not (session := current_session(request)):
        return login_required()
    return home_page(request, session, request_id=uuid.uuid4().hex)


def resolve(query: str) -> tuple[dict | None, list[dict]]:
    """A 6-digit code or a (partial) short name -> the company, or candidates when ambiguous."""
    query = re.sub(r"\s+", "", query).upper().removesuffix(".SH").removesuffix(".SZ").removesuffix(".BJ")
    stocks = company_data.stock_list()
    if query in stocks:
        return stocks[query], []
    matches = [s for s in stocks.values() if query and query in s["zwjc"].replace(" ", "")]
    exact = [s for s in matches if s["zwjc"].replace(" ", "") == query]
    if len(exact) == 1 or len(matches) == 1:
        return (exact or matches)[0], []
    return None, matches[:10]


@app.post("/research")
def start_research(request: Request, q: str = Form(""), request_id: str = Form(""), csrf: str = Form(""),
                   again: str = Form(""), question: str = Form(""), parent_report_id: int | None = Form(None)):
    if not (session := current_session(request)):
        return login_required()
    if not csrf_ok(session, csrf):
        return message(request, session, "页面已过期", "请返回首页重新提交。", 403)
    try:
        company, choices = resolve(q)
    except requests.RequestException:
        return home_page(request, session, 503, request_id=request_id, q=q, question=question,
                         error="暂时无法连接巨潮资讯核对公司，请稍后再试")
    if company is None:
        error = "找到多家公司，请选择一家" if choices else "没有找到这家公司，请输入 6 位代码或简称"
        return home_page(request, session, request_id=request_id, q=q, question=question, error=error, choices=choices)
    if company_data.FINANCIAL_NAME.search(company["zwjc"]):
        return home_page(request, session, request_id=request_id, q=q, question=question,
                         error=f"{company['zwjc'].replace(' ', '')}属于银行、保险、券商等金融类公司，暂不支持研究。")
    code = company["code"]
    recent = reading.recent_report(conn, code)
    if recent and not again and not question.strip() and not parent_report_id:
        return home_page(request, session, request_id=request_id, q=code, recent=recent)
    try:
        run_id = service.submit(conn, code, request_id, name=company["zwjc"].replace(" ", ""),
                                question=question, parent_report_id=parent_report_id)
    except service.RequestError as exc:
        return message(request, session, "无法提交研究", str(exc), exc.status)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


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
                  if run["kind"] == "research" and run["status"] == "failed" else None,
                  feedback_retry=service.feedback_retry_payload(run) is not None,
                  request_id=uuid.uuid4().hex)


@app.post('/runs/{run_id}/retry-feedback')
def retry_feedback(request: Request, run_id: int, request_id: str = Form(''), csrf: str = Form('')):
    if not (session := current_session(request)):
        return login_required()
    if not csrf_ok(session, csrf):
        return message(request, session, '页面已过期', '请返回核查任务重新提交。', 403)
    try:
        next_run = service.retry_feedback(conn, run_id, request_id)
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
    body = json.loads(row["body"])
    feedback = (json.loads(run["request"] or "{}") or {}).get("feedback")
    feedback_label = ""
    if feedback:
        section, _, index = feedback["location"].partition("[")
        feedback_label = {"summary": "一句话结论", "answer": "问题回答", "risks": "风险与反证",
                          "business": "业务", "financial": "财务", "methods": "研究方法",
                          "checkpoints": "下期检查点"}[section]
        if index:
            feedback_label += f"第 {int(index[:-1]) + 1} 条"
    evidence = reading.evidence_context(body, row['coverage'])
    rows = {r["id"]: r for r in body["table"]}
    fin = [r for r in body["table"] if r["id"].startswith("T.")]
    return render(request, "report.html", session, report=row, body=body, evidence=evidence,
                  source_view=report_source_view(body, (json.loads(run["material"] or "{}") or {}).get("data")),
                  ledger=reading.evidence_ledger(body if row['passed'] else body.get('facts') or {}, rows, evidence),
                  figures=charts.financial_charts(body, fin), vitals=charts.vitals(body, fin),
                  peer_snapshots=reading.peer_snapshots(body) if row['passed'] else [],
                  feedback_retry=service.feedback_retry_payload(run) is not None,
                  feedback=feedback, feedback_label=feedback_label, comparison=reading.comparison(conn, row),
                  replay_of=body.get("replay_of") or run["replay_of"],
                  rows=rows, request_id=uuid.uuid4().hex,
                  market=[r for r in body["table"] if r["id"].startswith("M.")],
                  fin=fin, price=reading.latest_price(conn, row["code"]),
                  followups=reading.followups(conn, row['code'], report_id))


@app.post("/reports/{report_id}/feedback")
def submit_feedback(request: Request, report_id: int, location: str = Form(""), text: str = Form(""),
                    request_id: str = Form(""), csrf: str = Form("")):
    if not (session := current_session(request)):
        return login_required()
    if not csrf_ok(session, csrf):
        return message(request, session, "页面已过期", "请返回报告重新提交。", 403)
    try:
        run_id = service.submit(conn, "", request_id, parent_report_id=report_id,
                                feedback={"location": location, "text": text})
    except service.RequestError as exc:
        return message(request, session, "无法提交核查请求", str(exc), exc.status)
    return RedirectResponse(f"/runs/{run_id}", status_code=303)


# JSON clients accept persistent API keys and legacy agent sessions, never cookies.
def api_session(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(401, "需要研究 API bearer token")
    digest = _hash(header[7:])
    if conn.execute("SELECT id FROM api_keys WHERE token_hash = ?", (digest,)).fetchone():
        key = "key:" + digest
    else:
        key = "agent:" + digest
        row = conn.execute("SELECT expires_at FROM sessions WHERE token_hash = ?", (key,)).fetchone()
        if row is None or row["expires_at"] <= time.time():
            raise HTTPException(401, "研究凭据无效、已撤销或临时令牌已过期")
    if request.headers.get("x-research-version", service.VERSION) != service.VERSION:
        raise HTTPException(400, "不支持的研究合同版本")
    return key


class ResearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    code: str = Field(pattern=r"^\d{6}$")
    request_id: str = Field(min_length=1, max_length=128)
    question: str = Field(default="", max_length=4000)
    parent_report_id: int | None = Field(default=None, gt=0)


class FeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    request_id: str = Field(min_length=1, max_length=128)
    location: str = Field(min_length=1, max_length=80)
    text: str = Field(min_length=1, max_length=4000)


@app.exception_handler(service.RequestError)
async def api_error(request: Request, exc: service.RequestError):
    return JSONResponse({"version": service.VERSION, "error": str(exc)}, status_code=exc.status)


@app.post("/api/research", status_code=202)
def api_research(request: Request, body: ResearchRequest):
    api_session(request)
    if reading.request_run(conn, body.request_id):
        return {"version": service.VERSION, "run_id": service.submit(conn, **body.model_dump())}
    try:
        company = company_data.company(body.code)
    except company_data.SourceError as exc:
        raise service.RequestError(str(exc)) from exc
    except requests.RequestException as exc:
        raise service.RequestError("暂时无法连接巨潮资讯，请稍后重试", 503) from exc
    if company_data.FINANCIAL_NAME.search(company["zwjc"]):
        raise service.RequestError("金融类公司暂不支持研究")
    run_id = service.submit(conn, **body.model_dump(), name=company["zwjc"].replace(" ", ""))
    return {"version": service.VERSION, "run_id": run_id}


@app.get("/api/request")
def api_request(request: Request, request_id: str):
    api_session(request)
    return service.run_view(conn, request_id=request_id)


@app.get("/api/research/budget")
def api_research_budget(request: Request):
    api_session(request)
    return {"version": service.VERSION, "budget": service.run_budget(conn)}


@app.get("/api/runs/{run_id}")
def api_run(request: Request, run_id: int):
    api_session(request)
    row = reading.api_run(conn, run_id)
    if row is None:
        raise service.RequestError("任务不存在", 404)
    return {"version": service.VERSION, **dict(row)}


@app.get("/api/reports")
def api_reports(request: Request, code: str):
    api_session(request)
    if not re.fullmatch(r"\d{6}", code):
        raise service.RequestError("公司代码必须为六位数字")
    rows = reading.api_reports(conn, code)
    return {"version": service.VERSION, "reports": [dict(row) for row in rows]}


@app.get("/api/reports/{report_id}")
def api_report(request: Request, report_id: int, compact: bool = False):
    api_session(request)
    return service.report_view(conn, report_id, compact=compact)


@app.post("/api/reports/{report_id}/feedback", status_code=202)
def api_feedback(request: Request, report_id: int, body: FeedbackRequest):
    api_session(request)
    run_id = service.submit(conn, "", body.request_id, parent_report_id=report_id,
                            feedback={"location": body.location, "text": body.text})
    return {"version": service.VERSION, "run_id": run_id}


@app.post("/api/session/revoke")
def api_revoke(request: Request):
    key = api_session(request)
    if key.startswith("key:"):
        conn.execute("DELETE FROM api_keys WHERE token_hash = ?", (key[4:],))
    else:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (key,))
    return {"version": service.VERSION, "revoked": True}


@app.get("/stock/{code}")
def stock_page(request: Request, code: str, report: int | None = None, progress_item: int | None = None):
    if not (session := current_session(request)):
        return login_required()
    reports = reading.stock_reports(conn, code)
    if not reports:
        return not_found(request, session, "还没有研究过这家公司。")
    head = next((r for r in reports if not r['replay_of']), reports[0])
    selected = next((r for r in reports if r['id'] == report), None) if report is not None else head
    if selected is None:
        return not_found(request, session, "这份报告不属于当前公司。")
    advancement = progress.view(conn, selected['id'])
    draft = None
    if advancement['status'] == 'passed' and progress_item is not None and 0 <= progress_item < len(advancement['body']['items']):
        draft = advancement['body']['items'][progress_item]['next_step']['text']
    # The newest non-replay report with an operating series lends its vitals to the company page,
    # falling back to the newest non-replay report's saved T.* financial table when no series exists.
    ordered = [r for r in reports if not r["replay_of"]] + [r for r in reports if r["replay_of"]]
    parsed = [(r, json.loads(r["body"])) for r in ordered]
    latest = next(((r, v) for r, b in parsed if (v := charts.vitals(b))), None)
    if latest is None:
        for r, b in parsed:
            t = [x for x in (b.get("table") or []) if isinstance(x, dict) and str(x.get("id", "")).startswith("T.")]
            if v := charts.vitals(b, t):
                latest = (r, v)
                break
    latest = latest or (None, None)
    before = next((r for r in reports if r["passed"] and not r["replay_of"]), None) if not head["passed"] else None
    card = reading.card(head, parsed[0][1], before) | {
        "coverage": head["coverage"],
        "created_at": head["created_at"]}
    return render(request, "stock.html", session, reports=reports, code=code, request_id=uuid.uuid4().hex, card=card,
                  due=company_data.due_report(datetime.now(db.SHANGHAI).date()),
                  vitals=latest[1], vitals_report=latest[0],
                  price=reading.latest_price(conn, code), comparison=reading.comparison(conn, selected),
                  selected=selected, track=charts.verdict_track(reports), progress=advancement, progress_kinds=progress.KINDS, progress_draft=draft)


@app.post('/reports/{report_id}/progress')
def submit_progress(request: Request, report_id: int, csrf: str = Form('')):
    if not (session := current_session(request)):
        return login_required()
    if not csrf_ok(session, csrf):
        return message(request, session, '页面已过期', '请返回公司档案重新提交。', 403)
    try:
        progress.submit(conn, report_id)
    except service.RequestError as exc:
        return message(request, session, '无法整理研究进展', str(exc), exc.status)
    row = reading.report(conn, report_id)
    return RedirectResponse(f'/stock/{row["code"]}?report={report_id}#research-progress', status_code=303)


@app.get('/api/reports/{report_id}/progress')
def api_progress(request: Request, report_id: int):
    api_session(request)
    return progress.view(conn, report_id)


@app.post('/api/reports/{report_id}/progress', status_code=202)
def api_submit_progress(request: Request, report_id: int):
    api_session(request)
    identifier = progress.submit(conn, report_id)
    return {'version': progress.VERSION, 'progress_id': identifier, **progress.view(conn, report_id)}


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
    if batch["status"] == "researching":
        batch["q20"] = reading.research_state(conn, batch["q20"])[1]
    draft = ((batch["selection"] or {}).get("draft") or {})
    results, names = batches.returns(batch), {r["code"]: r["name"] for r in batch["q20"] or []}
    return render(request, "batch.html", session, batch=batch, draft=draft,
                  draft_codes=[p["code"] for p in draft.get("picks", [])],
                  results=results, names=names, performance=charts.batch_returns(batch, results, names),
                  overview=charts.batch_overview(batch), sparks=charts.candidate_sparks(batch.get("q20") or []),
                  market_strip=charts.market_strip(batch),
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
            service.freeze_batch(conn, batch_id, [str(c) for c in form.getlist("codes")], str(form.get("note", "")))
    except service.RequestError as exc:
        return message(request, session, "无法冻结", str(exc), exc.status)
    return RedirectResponse(f"/batches/{batch_id}", status_code=303)
