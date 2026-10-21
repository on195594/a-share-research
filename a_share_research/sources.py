"""Official periodic reports from cninfo and structured financials from TuShare."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import cache
from pathlib import Path

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry
from a_stock_lib.providers.tushare_financials import TushareFinancialProvider
from a_stock_lib.providers.tushare_quotes import to_tushare_stock_code
from a_stock_lib.providers.tushare_valuation import TushareValuationProvider

from a_share_research.db import DATA, SHANGHAI
CNINFO = "https://www.cninfo.com.cn/new"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://www.cninfo.com.cn/"})
# cninfo occasionally drops connections; retry reads (GET and the idempotent announcement query).
SESSION.mount("https://", HTTPAdapter(max_retries=Retry(
    total=3, backoff_factor=1, status_forcelist=(500, 502, 503, 504), allowed_methods=("GET", "POST"),
)))
KINDS = {"年度报告": "12-31", "半年度报告": "06-30", "第一季度报告": "03-31", "第三季度报告": "09-30"}
PDF_PATH = re.compile(r"finalpage/\d{4}-\d{2}-\d{2}/\d+\.PDF", re.I)
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


@cache
def stock_list() -> dict[str, dict]:
    rows = SESSION.get(f"{CNINFO}/data/szse_stock.json", timeout=30).json()["stockList"]
    return {r["code"]: r for r in rows if r.get("category") == "A股"}


def company(code: str) -> dict:
    """cninfo identity for a 6-digit code: {code, zwjc (short name), orgId, ...}."""
    found = stock_list().get(code[:6])
    if not found:
        raise SourceError(f"{code} is not a listed A share on cninfo")
    return found


def reports(code: str, days: int = 550) -> list[Document]:
    """Periodic reports announced in the last `days`, newest period first, one version per period."""
    org = company(code)
    end = datetime.now(SHANGHAI).date()
    rows = []
    for page in range(1, 6):
        data = SESSION.post(f"{CNINFO}/hisAnnouncement/query", timeout=30, data={
            "stock": f"{code[:6]},{org['orgId']}", "tabName": "fulltext", "pageSize": "30",
            "pageNum": str(page), "column": "", "plate": "", "searchkey": "", "secid": "",
            "category": "category_ndbg_szsh;category_bndbg_szsh;category_sjdbg_szsh;category_yjdbg_szsh;",
            "seDate": f"{end - timedelta(days=days)}~{end}", "sortName": "", "sortType": "",
            "isHLtitle": "false",
        }).json()
        rows += data.get("announcements") or []
        if not data.get("hasMore"):
            break
    by_period: dict[str, list[Document]] = {}
    for raw in rows:
        title = re.sub(r"<[^>]+>", "", raw.get("announcementTitle", ""))
        if not PDF_PATH.fullmatch(raw.get("adjunctUrl", "")):
            continue
        match = TITLE_RE.search(title)
        if raw.get("secCode") != code[:6] or not match or any(
            w in title for w in ("摘要", "英文", "English", "取消", "更正公告")
        ):
            continue
        doc = Document(
            code=to_tushare_stock_code(code), name=raw.get("secName", ""), title=title,
            period=f"{match[1]}-{KINDS[match[2]]}",
            announced=datetime.fromtimestamp(raw["announcementTime"] / 1000, SHANGHAI).date().isoformat(),
            url="https://static.cninfo.com.cn/" + raw["adjunctUrl"],
        )
        by_period.setdefault(doc.period, []).append(doc)
    # A later or explicitly revised version supersedes the original.
    chosen = [
        max(docs, key=lambda d: (d.announced, any(w in d.title for w in ("修订", "更新", "更正后"))))
        for docs in by_period.values()
    ]
    return sorted(chosen, key=lambda d: d.period, reverse=True)


def fetch(doc: Document) -> Document:
    """Download the PDF once (stored by content hash) and extract per-page text."""
    response = SESSION.get(doc.url, timeout=(10, 120))
    response.raise_for_status()
    payload = response.content
    if not payload.startswith(b"%PDF"):
        raise SourceError(f"{doc.url} is not a PDF")
    doc.sha256 = hashlib.sha256(payload).hexdigest()
    doc.retrieved_at = datetime.now(SHANGHAI).isoformat(timespec="seconds")
    pdf = DATA / "pdf" / f"{doc.sha256}.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    if not pdf.exists():
        pdf.write_bytes(payload)
    doc.pages = _pages(pdf, "-layout")
    doc.raw_pages = _pages(pdf, "-raw")
    if len(doc.raw_pages) != len(doc.pages):
        raise SourceError(f"{doc.url}: page count differs between text extractions")
    return doc


def _pages(pdf: Path, mode: str) -> list[str]:
    cache = pdf.with_suffix(f".{mode.lstrip('-')}.json")
    if cache.exists():
        return json.loads(cache.read_text(encoding="utf-8"))
    text = subprocess.run(
        ["pdftotext", mode, "-enc", "UTF-8", str(pdf), "-"],
        capture_output=True, text=True, check=True, timeout=120,
    ).stdout
    pages = text.split("\f")[:-1] if text.endswith("\f") else text.split("\f")
    cache.write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")
    return pages


def _frame(result) -> pd.DataFrame:
    if result.value is None:
        raise SourceError(f"{result.source}: {result.error_code} {result.error_message}")
    return result.value


def financials(code: str, years: int = 4) -> dict[str, pd.DataFrame]:
    """Income, balance sheet, cash flow and indicators for recent periods (TuShare)."""
    provider = TushareFinancialProvider()
    start = date(datetime.now(SHANGHAI).year - years, 1, 1).isoformat()
    return {
        "income": _frame(provider.fetch_income_history(code, start_date=start)),
        "balance": _frame(provider.fetch_balance_history(code, start_date=start)),
        "cashflow": _frame(provider.fetch_cashflow_history(code, start_date=start)),
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
