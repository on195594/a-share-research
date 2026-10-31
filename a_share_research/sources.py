"""Official disclosures from cninfo and structured financials from TuShare."""

import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import cache, partial
from pathlib import Path

import pandas as pd
import requests
import tushare as ts
from a_stock_lib.providers.tushare_financials import TushareFinancialProvider
from a_stock_lib.providers.tushare_quotes import to_tushare_stock_code
from a_stock_lib.providers.tushare_valuation import TushareValuationProvider

from a_share_research.db import DATA, SHANGHAI
from a_share_research import db
from a_share_research.companies import CNINFO, SESSION, SourceError, company

log = logging.getLogger(__name__)
KINDS = {"年度报告": "12-31", "半年度报告": "06-30", "一季度报告": "03-31", "三季度报告": "09-30"}
PDF_PATH = re.compile(r"finalpage/\d{4}-\d{2}-\d{2}/\d+\.PDF", re.IGNORECASE)
TITLE_RE = re.compile(r"([0-9]{4})\s*年?\s*(半年度报告|年度报告|第?[一三]季度报告)")

FINANCIAL_APIS = {"fina_indicator", "income", "cashflow", "fina_audit", "balancesheet"}
HISTORICAL_APIS = {"daily", "daily_basic", "adj_factor", "index_daily", "stk_limit", "suspend_d", "trade_cal"}


def _tushare_cache_key(api: str, params: dict) -> str | None:
    if os.environ.get("TUSHARE_CACHE") == "0":
        return None
    today = datetime.now(SHANGHAI).date()
    bound = params.get("trade_date") or params.get("end_date")
    try:
        historical = bool(bound) and date.fromisoformat(str(bound)) < today
    except ValueError:
        return None
    if not historical or api not in FINANCIAL_APIS | HISTORICAL_APIS:
        return None
    key = {"api": api, "params": params}
    return hashlib.sha256(json.dumps(key, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def tushare_query(pro, api: str, **params) -> pd.DataFrame:
    """Cache historical raw responses, before a-stock-lib normalizes dates and numbers."""
    key = _tushare_cache_key(api, params)
    if key is None:
        return pro.query(api, **params)
    path = db.DATA / "tushare_cache.db"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path, timeout=30, isolation_level=None)) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("CREATE TABLE IF NOT EXISTS tushare_cache (key TEXT PRIMARY KEY, data TEXT, created_at TEXT)")
            row = conn.execute("SELECT data FROM tushare_cache WHERE key=?", (key,)).fetchone()
            if row:
                saved = json.loads(row[0])
                if not isinstance(saved, dict) or set(saved) != {"data", "index", "columns"}:
                    raise ValueError("TuShare cache is not a split DataFrame")
                return pd.DataFrame(**saved)
    except (OSError, sqlite3.Error, ValueError, TypeError):
        log.warning("TuShare %s cache read failed; fetching upstream", api, exc_info=True)
    frame = pro.query(api, **params)
    # Empty results may mean the provider has not published the data yet.
    if isinstance(frame, pd.DataFrame) and not frame.empty:
        try:
            # Python's JSON float encoding preserves precision and leaves date/code strings intact.
            data = json.dumps(frame.to_dict(orient="split"), ensure_ascii=False)
            with closing(sqlite3.connect(path, timeout=30, isolation_level=None)) as conn:
                conn.execute("INSERT OR REPLACE INTO tushare_cache VALUES (?, ?, ?)", (key, data, db.now()))
        except Exception:
            log.warning("TuShare %s cache write failed", api, exc_info=True)
    return frame


class TushareCacheClient:
    """a-stock-lib client factory; retain its validation, throttling and retry policy."""

    def __init__(self, token: str):
        self.pro = ts.pro_api(token)

    def __getattr__(self, api: str):
        return partial(tushare_query, self.pro, api)


@dataclass
class Document:
    code: str
    name: str
    title: str
    period: str  # e.g. 2025-12-31
    announced: str
    url: str
    sha256: str = ""
    retrieved_at: str = ""
    pages: list[str] = field(default_factory=list, repr=False)  # pdftotext -layout, shown to the model
    raw_pages: list[str] = field(default_factory=list, repr=False)  # -raw keeps table cells contiguous

    def info(self) -> dict:
        """Identity fields stored with evidence; the page texts are kept separately."""
        return {key: getattr(self, key) for key in
                ("code", "name", "title", "period", "announced", "url", "sha256", "retrieved_at")}


SW_LEVELS = ("l1", "l2", "l3")


def peer_members(subject: pd.DataFrame, members: pd.DataFrame, level: str) -> dict:
    """Current SW2021 membership of one company and its industry at `level`, validated at the boundary."""
    row = _membership(subject, level)
    _membership(members, level, single=False)
    if (members[f"{level}_code"] != row[f"{level}_code"]).any():
        raise SourceError("申万行业成分返回了其他行业的公司")
    return {
        "classification": "SW2021", "level": level, "source": "TuShare index_member_all (is_new=Y)",
        "retrieved_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
        "industry": {lv: row[f"{lv}_name"] for lv in SW_LEVELS},
        "members": sorted(
            ({"code": str(m.ts_code)[:6], "name": str(m.name), "l3": str(m.l3_name)}
             for m in members.drop_duplicates("ts_code").itertuples() if m.ts_code != row.ts_code),
            key=lambda m: m["code"]),
    }


def _membership(frame, level: str, *, single: bool = True):
    """Check an index_member_all response; with single, return the one company row."""
    if level not in SW_LEVELS:
        raise SourceError(f"申万行业层级须为 {'/'.join(SW_LEVELS)}")
    need = {"ts_code", "name", *(f"{lv}_{k}" for lv in SW_LEVELS for k in ("code", "name"))}
    if not isinstance(frame, pd.DataFrame) or not need <= set(frame.columns):
        raise SourceError("TuShare index_member_all 返回数据缺少必要列")
    if single and len(frame) != 1:
        raise SourceError(f"申万行业成分中该公司记录数为 {len(frame)}，无法确定所属行业")
    return frame.iloc[0] if single else None


def industry_peers(code: str, level: str = "l3") -> dict:
    """Companies in the same current SW2021 industry (TuShare), the screen's own classification."""
    try:
        pro = ts.pro_api(os.environ["TUSHARE_TOKEN"])
        subject = pro.query("index_member_all", ts_code=to_tushare_stock_code(code), is_new="Y")
        row = _membership(subject, level)
        members = pro.query("index_member_all", **{f"{level}_code": row[f"{level}_code"]}, is_new="Y")
    except SourceError:
        raise
    except Exception as exc:
        raise SourceError(f"TuShare index_member_all: {exc}") from exc
    return peer_members(subject, members, level)


def _query_announcements(code: str, days: int, query: str, category: str, max_pages: int) -> tuple[list[dict], dict]:
    org = company(code)
    end = datetime.now(SHANGHAI).date()
    rows = []
    for page in range(1, max_pages + 1):
        response = SESSION.post(f"{CNINFO}/hisAnnouncement/query", timeout=30, data={
            "stock": f"{code[:6]},{org['orgId']}", "tabName": "fulltext", "pageSize": "30",
            "pageNum": str(page), "column": "", "plate": "", "searchkey": query, "secid": "",
            "category": category,
            "seDate": f"{end - timedelta(days=days)}~{end}", "sortName": "", "sortType": "",
            "isHLtitle": "false",
        })
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or "announcements" not in data:
            raise SourceError("巨潮公告目录返回格式异常")
        rows += data.get("announcements") or []
        if not data.get("hasMore"):
            break
    return rows, {"since": str(end - timedelta(days=days)), "until": str(end), "query": query,
                  "truncated": bool(data.get("hasMore")), "scanned": len(rows)}


def reports(code: str, days: int = 550) -> list[Document]:
    """Periodic reports announced in the last `days`, newest period first, one version per period."""
    rows, _ = _query_announcements(code, days, "",
        "category_ndbg_szsh;category_bndbg_szsh;category_sjdbg_szsh;category_yjdbg_szsh;", 5)
    by_period: dict[str, list[Document]] = {}
    for raw in rows:
        doc = announcement_document(raw, code)
        if doc is None or not doc.period or any(
            w in doc.title for w in ("摘要", "英文", "English", "取消", "更正公告")
        ):
            continue
        by_period.setdefault(doc.period, []).append(doc)
    # A later or explicitly revised version supersedes the original.
    chosen = [
        max(docs, key=lambda d: (d.announced, any(w in d.title for w in ("修订", "更新", "更正后"))))
        for docs in by_period.values()
    ]
    return sorted(chosen, key=lambda d: d.period, reverse=True)


def announcement_document(raw: dict, code: str) -> Document | None:
    """Keep official company PDFs; an event date is never a financial period."""
    if raw.get("secCode") != code[:6] or not PDF_PATH.fullmatch(raw.get("adjunctUrl", "")):
        return None
    title = re.sub(r"<[^>]+>", "", raw.get("announcementTitle", ""))
    match = TITLE_RE.search(title)
    return Document(
        code=to_tushare_stock_code(code), name=raw.get("secName", ""), title=title,
        period=f"{match[1]}-{KINDS[match[2].removeprefix('第')]}" if match else "",
        announced=datetime.fromtimestamp(raw["announcementTime"] / 1000, SHANGHAI).date().isoformat(),
        url="https://static.cninfo.com.cn/" + raw["adjunctUrl"],
    )


def announcements(code: str, query: str = "", days: int = 180) -> tuple[list[Document], dict]:
    """Search recent official announcements, including forecasts, flash reports and events."""
    if not isinstance(query, str) or len(query) > 40 or type(days) is not int or not 1 <= days <= 550:
        raise ValueError("公告关键词最多 40 字；回看范围为 1–550 天")
    rows, coverage = _query_announcements(code, days, query, "", 3)
    docs = [doc for raw in rows if (doc := announcement_document(raw, code)) is not None]
    return sorted(docs, key=lambda d: d.announced, reverse=True), coverage


def fetch(doc: Document) -> Document:
    """Download the PDF (stored by content hash) and extract per-page text."""
    response = SESSION.get(doc.url, timeout=(10, 120))
    response.raise_for_status()
    payload = response.content
    if not payload.startswith(b"%PDF"):
        raise SourceError(f"{doc.url} is not a PDF")
    doc.sha256 = hashlib.sha256(payload).hexdigest()
    doc.retrieved_at = datetime.now(SHANGHAI).isoformat(timespec="seconds")
    pdf = store_pdf(DATA / "pdf", payload, doc.sha256)
    doc.pages, doc.raw_pages = _page_pair(pdf, doc.sha256)
    if len(doc.raw_pages) != len(doc.pages):
        raise SourceError(f"{doc.url}: page count differs between text extractions")
    return doc


def _page_pair(pdf: Path, sha: str) -> tuple[list[str], list[str]]:
    if not pdf.with_suffix(".layout.json").exists() and not pdf.with_suffix(".raw.json").exists():
        with ThreadPoolExecutor(max_workers=2) as pool:
            layout = pool.submit(_pages, pdf, sha, "-layout")
            raw = pool.submit(_pages, pdf, sha, "-raw")
            return layout.result(), raw.result()
    return _pages(pdf, sha, "-layout"), _pages(pdf, sha, "-raw")


def store_pdf(directory: Path, payload: bytes, sha: str) -> Path:
    """Keep the downloaded bytes at <sha>.pdf; a stored copy that no longer matches is replaced."""
    pdf = directory / f"{sha}.pdf"
    if pdf.exists():
        if hashlib.sha256(pdf.read_bytes()).hexdigest() == sha:
            return pdf
        log.warning("stored %s no longer matches its hash; replacing it with the fresh download", pdf.name)
    directory.mkdir(parents=True, exist_ok=True)
    _write_atomic(pdf, payload)
    return pdf


@cache
def extractor() -> str:
    """The pdftotext version line; cached page text is only reused from the same extractor."""
    out = subprocess.run(["pdftotext", "-v"], capture_output=True, text=True, check=True, timeout=10)
    return (out.stderr or out.stdout).splitlines()[0].strip()


def _pages(pdf: Path, sha: str, mode: str) -> list[str]:
    """Per-page text of a PDF the caller has verified to hash to sha."""
    cache = pdf.with_suffix(f".{mode.lstrip('-')}.json")
    identity = {"pdf_sha256": sha, "extractor": extractor(), "mode": mode}
    if cache.exists():
        try:
            saved = json.loads(cache.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            saved = None
        if isinstance(saved, dict) and isinstance(saved.get("pages"), list) and \
                {key: saved.get(key) for key in identity} == identity and \
                saved.get("pages_sha256") == _pages_hash(saved["pages"]):
            return saved["pages"]
        log.warning("re-extracting %s: cached text is damaged or from another PDF/extractor", cache.name)
    text = subprocess.run(
        ["pdftotext", mode, "-enc", "UTF-8", str(pdf), "-"],
        capture_output=True, text=True, check=True, timeout=120,
    ).stdout
    pages = text.split("\f")[:-1] if text.endswith("\f") else text.split("\f")
    saved = identity | {"pages_sha256": _pages_hash(pages), "pages": pages}
    _write_atomic(cache, json.dumps(saved, ensure_ascii=False).encode())
    return pages


def _pages_hash(pages: list) -> str:
    return hashlib.sha256(json.dumps(pages, ensure_ascii=False).encode()).hexdigest()


def _write_atomic(path: Path, payload: bytes) -> None:
    # A crash mid-write must not leave a truncated PDF or cache under the final name.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_bytes(payload)
    os.replace(tmp, path)


def _frame(result) -> pd.DataFrame:
    if result.value is None:
        raise SourceError(f"{result.source}: {result.error_code} {result.error_message}")
    return result.value


def financials(code: str, years: int = 7) -> dict[str, pd.DataFrame]:
    """Three statements and indicators for recent periods (TuShare)."""
    start = date(datetime.now(SHANGHAI).year - years, 1, 1).isoformat()
    try:
        deducted = TushareCacheClient(os.environ["TUSHARE_TOKEN"]).fina_indicator(
            ts_code=to_tushare_stock_code(code), start_date=start.replace("-", ""),
            fields="ts_code,ann_date,end_date,update_flag,profit_dedt")
        if not isinstance(deducted, pd.DataFrame) or not {"ann_date", "end_date", "profit_dedt"} <= set(deducted.columns):
            raise ValueError("返回数据缺少必要列")
        for column in ("ann_date", "end_date"):
            deducted[column] = pd.to_datetime(deducted[column], format="%Y%m%d").dt.strftime("%Y-%m-%d")
    except Exception as exc:
        raise SourceError(f"TuShare fina_indicator: {exc}") from exc
    provider = TushareFinancialProvider(client_factory=TushareCacheClient)
    return {
        "income": _frame(provider.fetch_income_history(code, start_date=start)),
        "cashflow": _frame(provider.fetch_cashflow_history(code, start_date=start)),
        "balance": _frame(provider.fetch_balance_history(code, start_date=start)),
        "deducted": deducted,
        "indicator": _frame(provider.fetch_indicator_history(code, start_date=start)),
    }


def valuation(code: str) -> dict:
    """Latest daily_basic row: close, PE(TTM), PB, dividend yield, market cap."""
    end = datetime.now(SHANGHAI).date()
    frame = _frame(TushareValuationProvider().fetch_valuation_history(
        code, start_date=(end - timedelta(days=14)).isoformat(), end_date=end.isoformat()
    ))
    if frame.empty:
        raise SourceError(f"no valuation rows for {code}")
    return frame.sort_values("trade_date").iloc[-1].to_dict()
