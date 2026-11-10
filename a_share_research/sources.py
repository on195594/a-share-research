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
from a_stock_lib.providers.tushare_financials import TushareFinancialProvider
from a_stock_lib.providers.tushare_quotes import to_tushare_stock_code
from a_stock_lib.providers.tushare_valuation import TushareValuationProvider

from a_share_research.db import DATA, SHANGHAI
from a_share_research import market, stock_codes
from a_share_research.companies import CNINFO, SESSION, SourceError, company

log = logging.getLogger(__name__)
KINDS = {"年度报告": "12-31", "半年度报告": "06-30", "一季度报告": "03-31", "三季度报告": "09-30"}
ANNOUNCEMENT_PAGE_SIZE = 30
# 150 rows hold the busiest 180-day catalog seen (300750: 107); 300 would cost ~71k prompt chars per search.
ANNOUNCEMENT_MAX_PAGES = 5
PDF_PATH = re.compile(r"finalpage/\d{4}-\d{2}-\d{2}/\d+\.PDF", re.IGNORECASE)
TITLE_RE = re.compile(r"([0-9]{4})\s*年?\s*(半年度报告|年度报告|第?[一三]季度报告)")

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
            # Live membership includes nonstandard identifiers such as T00018.SH; do not parse them.
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
        pro = market.client()
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
            "stock": f"{stock_codes.normalize_key(code)},{org['orgId']}", "tabName": "fulltext", "pageSize": str(ANNOUNCEMENT_PAGE_SIZE),
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
    if raw.get("secCode") != stock_codes.normalize_key(code) or not PDF_PATH.fullmatch(raw.get("adjunctUrl", "")):
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
    rows, coverage = _query_announcements(code, days, query, "", ANNOUNCEMENT_MAX_PAGES)
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
    """Bind page caches to both the extractor and its CJK mapping package."""
    out = subprocess.run(["pdftotext", "-v"], capture_output=True, text=True, check=True, timeout=10)
    try:
        package = subprocess.run(["dpkg-query", "-W", "-f=${Version}", "poppler-data"],
                                 capture_output=True, text=True, check=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        raise SourceError("无法确认 poppler-data 版本；请安装 poppler-data，缺少该包会导致 PDF 中文静默丢失") from exc
    if not package.stdout.strip():
        raise SourceError("缺少 poppler-data 版本；请安装 poppler-data，避免 PDF 中文静默丢失")
    return f"{(out.stderr or out.stdout).splitlines()[0].strip()}; poppler-data {package.stdout.strip()}"


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
        deducted = market.client().fina_indicator(
            ts_code=to_tushare_stock_code(code), start_date=start.replace("-", ""),
            fields="ts_code,ann_date,end_date,update_flag,profit_dedt")
        if not isinstance(deducted, pd.DataFrame) or not {"ann_date", "end_date", "profit_dedt"} <= set(deducted.columns):
            raise ValueError("返回数据缺少必要列")
        for column in ("ann_date", "end_date"):
            deducted[column] = pd.to_datetime(deducted[column], format="%Y%m%d").dt.strftime("%Y-%m-%d")
    except Exception as exc:
        raise SourceError(f"TuShare fina_indicator: {exc}") from exc
    provider = TushareFinancialProvider()
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


def pe_quantiles(frame: pd.DataFrame, code: str, market_date: str) -> dict:
    """Five-year positive daily PE observations, with linear sample quantiles."""
    required = {"ts_code", "trade_date", "pe_ttm"}
    if not isinstance(frame, pd.DataFrame) or not required <= set(frame.columns):
        raise SourceError("TuShare daily_basic 估值历史缺少 ts_code、trade_date 或 pe_ttm")
    if not frame["ts_code"].eq(to_tushare_stock_code(code)).all():
        raise SourceError("TuShare daily_basic 估值历史含其他公司，无法作为本公司 PE 参照")
    end = date.fromisoformat(market_date)
    start = end.replace(year=end.year - 5, day=min(end.day, 28)) if end.month == 2 and end.day == 29 else end.replace(year=end.year - 5)
    dates = pd.to_datetime(frame["trade_date"], errors="raise").dt.date
    selected = frame.loc[(dates >= start) & (dates <= end)].copy()
    # One observation per trading day; duplicated rows must not inflate coverage.
    selected = selected.drop_duplicates("trade_date")
    values = pd.to_numeric(selected["pe_ttm"], errors="raise")
    values = values[values.notna() & (values > 0) & (values < float("inf"))]
    if len(values) < 250:
        raise SourceError(f"近 5 年正值 PE TTM 仅 {len(values)} 个交易日，少于 250 个，未提供历史 PE 分位参照")
    return {**{f"p{p}": float(values.quantile(p / 100, interpolation="linear")) for p in (30, 50, 70)},
            "n": len(values), "source": {"provider": "TuShare daily_basic.pe_ttm", "code": code,
            "start_date": str(start), "end_date": market_date, "samples": len(values),
            "method": "positive daily observations; linear quantile"}}


def pe_reference(code: str, market_date: str) -> dict:
    try:
        end = date.fromisoformat(market_date)
        start = end.replace(year=end.year - 5, day=28) if end.month == 2 and end.day == 29 else end.replace(year=end.year - 5)
        frame = _frame(TushareValuationProvider().fetch_valuation_history(
            code, start_date=str(start), end_date=market_date))
        return pe_quantiles(frame, code, market_date)
    except SourceError:
        raise
    except Exception as exc:
        raise SourceError(f"TuShare daily_basic 历史 PE 分位不可用：{exc}") from exc


def report_rc_window(as_of: str, *, days: int = 90, code: str | None = None,
                     cache_only: bool = False, max_calls: int | None = None) -> dict:
    """Read daily market-wide reports; optionally backfill within the shared daily budget.

    Batch freeze uses cache_only: missing report days remain missing, never trigger upstream calls.
    """
    cutoff = date.fromisoformat(as_of)
    end = min(cutoff, datetime.now(SHANGHAI).date() - timedelta(days=1))
    start = cutoff - timedelta(days=days)
    result = {"rows": [], "trading_days": 0, "covered_days": 0, "missing_days": [], "empty_days": [], "truncated_days": []}
    used_before = market.report_rc_status()["used"] if not cache_only else 0

    def failure(exc):
        reason = market.redact_error(exc)
        result["reason"] = f"TuShare report_rc: {reason}"
        log.warning("report_rc window %s: %s", as_of, reason)

    try:
        calendar_params = {"exchange": "SSE", "is_open": "1", "start_date": start.strftime("%Y%m%d"),
                           "end_date": end.strftime("%Y%m%d")}
        calendar = market.cached_response("trade_cal", calendar_params) if cache_only else None
        pro = None
        if start > end:
            trading_days = []
        elif calendar is not None:
            trading_days = sorted(set(calendar["cal_date"]), reverse=True)
        else:
            pro = market.client()
            trading_days = sorted(set(market.trade_days(
                pro, calendar_params["start_date"], calendar_params["end_date"])), reverse=True)
    except Exception as exc:
        failure(exc)
        return result
    result["trading_days"] = len(trading_days)
    stopped = False
    calls = 0
    for day in trading_days:
        params = {"start_date": day, "end_date": day}
        try:
            frame = market.cached_response("report_rc", params)
            if frame is None:
                if stopped or cache_only or (max_calls is not None and calls >= max_calls):
                    result["missing_days"].append(day)
                    continue
                calls += 1
                frame = market.query(pro, "report_rc", **params)
            rows = market.report_rc_rows(frame, code)
            # A daily query must never silently lend coverage to a different date.
            if not frame.empty and not pd.to_datetime(frame["report_date"], errors="raise").dt.date.eq(date.fromisoformat(day)).all():
                raise SourceError(f"report_rc {day} 返回了其他日期的预测")
            result["rows"].extend(rows)
            result["covered_days"] += 1
            if frame.empty:
                result["empty_days"].append(day)
            if len(frame) >= market.REPORT_RC_ROW_CAP:
                result["truncated_days"].append(day)
        except Exception as exc:
            if isinstance(exc, (market.ReportRCBudgetExhausted, market.ReportRCCoolingDown)):
                result["reason"] = str(exc)
            else:
                failure(exc)
            result["missing_days"].append(day)
            # Never retry the next missing day after an upstream failure.
            stopped = True
    if not cache_only:
        pacing = market.report_rc_status()
        result["upstream_attempts"] = max(0, pacing["used"] - used_before)
        result["retry_at"] = pacing["retry_at"]
    return result


def coverage_counts(window: dict, codes: list[str], as_of: str) -> dict:
    """Count distinct organizations, retaining partial daily coverage without extrapolating."""
    end = date.fromisoformat(as_of)
    metadata = {key: value for key, value in window.items() if key != "rows"}
    organizations = {code: set() for code in codes}
    for row in window["rows"]:
        code, org = row["ts_code"], row.get("org_name")
        if code in organizations and isinstance(org, str) and org.strip():
            organizations[code].add(org.strip())
    return {code: metadata | {"organizations": len(orgs), "start": str(end - timedelta(days=90)), "end": str(end)}
            for code, orgs in organizations.items()}


def batch_coverage(batch: dict) -> dict:
    as_of = date.fromisoformat(batch["as_of"]).isoformat()
    window = report_rc_window(as_of, days=90, cache_only=True)
    return coverage_counts(window, [r["code"] for r in batch["q20"] + (batch.get("s10") or [])], as_of)
