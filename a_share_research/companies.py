"""Public company identities and disclosure deadlines; no market-provider dependencies."""

import re
from datetime import date
from functools import cache

import requests
from requests.adapters import HTTPAdapter
from urllib3.util import Retry

CNINFO = "https://www.cninfo.com.cn/new"
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Mozilla/5.0", "Referer": "https://www.cninfo.com.cn/"})
# cninfo occasionally drops connections; retry reads (GET and the idempotent announcement query).
SESSION.mount("https://", HTTPAdapter(max_retries=Retry(
    total=3, backoff_factor=1, status_forcelist=(500, 502, 503, 504), allowed_methods=("GET", "POST"),
)))


class SourceError(Exception):
    pass


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


def due_report(today: date) -> tuple[str, str]:
    """The latest periodic report whose statutory deadline has passed: (period end, name)."""
    return max((f"{year}-{end}", f"{year} 年{name}") for year in (today.year - 1, today.year)
               for end, name, later, deadline in PERIODIC
               if today > date.fromisoformat(f"{year + later}-{deadline}"))


# shortcut: name-only, misses e.g. 中国平安/东方财富; the worker's TuShare comp_type check still catches those.
FINANCIAL_NAME = re.compile(r"银行|证券|保险|人寿|期货|信托")


# Periodic reports: period end, name, deadline year offset, statutory deadline.
PERIODIC = [("03-31", "一季报", 0, "04-30"), ("06-30", "半年报", 0, "08-31"),
            ("09-30", "三季报", 0, "10-31"), ("12-31", "年报", 1, "04-30")]
