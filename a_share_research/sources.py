"""Official disclosures from cninfo and structured financials from TuShare."""

import hashlib
import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import cache
from pathlib import Path

import pandas as pd
import requests
import tushare as ts
from a_stock_lib.providers.tushare_financials import TushareFinancialProvider
from a_stock_lib.providers.tushare_quotes import to_tushare_stock_code
from a_stock_lib.providers.tushare_valuation import TushareValuationProvider
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

from a_share_research.db import DATA, SHANGHAI

log = logging.getLogger(__name__)
CNINFO = "https://www.cninfo.com.cn/new"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://www.cninfo.com.cn/"})
# cninfo occasionally drops connections; retry reads (GET and the idempotent announcement query).
SESSION.mount("https://", HTTPAdapter(max_retries=Retry(
    total=3, backoff_factor=1, status_forcelist=(500, 502, 503, 504), allowed_methods=("GET", "POST"),
)))
KINDS = {"年度报告": "12-31", "半年度报告": "06-30", "第一季度报告": "03-31", "第三季度报告": "09-30"}
PDF_PATH = re.compile(r"finalpage/\d{4}-\d{2}-\d{2}/\d+\.PDF", re.IGNORECASE)
TITLE_RE = re.compile(r"([0-9]{4})\s*年?\s*(半年度报告|年度报告|第一季度报告|第三季度报告)")


class SourceError(Exception):
    pass


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


@cache
def stock_list() -> dict[str, dict]:
    rows = SESSION.get(f"{CNINFO}/data/szse_stock.json", timeout=30).json()["stockList"]
    return {r["code"]: r for r in rows if r.get("category") == "A股"}


# shortcut: name-only, misses e.g. 中国平安/东方财富; the worker's TuShare comp_type check still catches those.
FINANCIAL_NAME = re.compile(r"银行|证券|保险|人寿|期货|信托")


def company(code: str) -> dict:
    """cninfo identity for a 6-digit code: {code, zwjc (short name), orgId, ...}."""
    found = stock_list().get(code[:6])
    if not found:
        raise SourceError(f"{code} is not a listed A share on cninfo")
    return found


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
        period=f"{match[1]}-{KINDS[match[2]]}" if match else "",
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
    doc.pages = _pages(pdf, doc.sha256, "-layout")
    doc.raw_pages = _pages(pdf, doc.sha256, "-raw")
    if len(doc.raw_pages) != len(doc.pages):
        raise SourceError(f"{doc.url}: page count differs between text extractions")
    return doc


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


def financials(code: str, years: int = 4) -> dict[str, pd.DataFrame]:
    """Three statements and indicators for recent periods (TuShare)."""
    provider = TushareFinancialProvider()
    start = date(datetime.now(SHANGHAI).year - years, 1, 1).isoformat()
    deducted = ts.pro_api(os.environ["TUSHARE_TOKEN"]).fina_indicator(
        ts_code=to_tushare_stock_code(code), start_date=start.replace("-", ""),
        fields="ts_code,ann_date,end_date,update_flag,profit_dedt")
    for column in ("ann_date", "end_date"):
        deducted[column] = pd.to_datetime(deducted[column], format="%Y%m%d").dt.strftime("%Y-%m-%d")
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
