"""Typed external-condition series from the account's verified TuShare endpoints."""

import os
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

import pandas as pd

from a_share_research import db, sources

MACRO = {
    "pmi": ("cn_pmi", {"pmi010000": ("制造业PMI", "指数"), "pmi010400": ("制造业生产指数", "指数"),
                       "pmi010500": ("制造业新订单指数", "指数")}),
    "money": ("cn_m", {"m1": ("M1余额", "亿元"), "m2": ("M2余额", "亿元"),
                       "m1_yoy": ("M1同比", "%"), "m2_yoy": ("M2同比", "%")}),
    "financing": ("sf_month", {"inc_month": ("社融当月增量", "亿元"), "inc_cumval": ("社融累计增量", "亿元")}),
    "lpr": ("shibor_lpr", {"1y": ("一年期LPR", "%"), "5y": ("五年期LPR", "%")}),
    "shibor": ("shibor", {"on": ("隔夜Shibor", "%"), "1w": ("一周Shibor", "%"), "3m": ("三个月Shibor", "%")}),
    "cpi": ("cn_cpi", {"nt_yoy": ("全国CPI同比", "%"), "nt_mom": ("全国CPI环比", "%")}),
    "ppi": ("cn_ppi", {"ppi_yoy": ("PPI同比", "%"), "ppi_mom": ("PPI环比", "%")}),
}
EXCHANGES = {"SHFE": "SHF", "DCE": "DCE", "CZCE": "ZCE", "INE": "INE", "GFEX": "GFE"}


def query(api, **params):
    try:
        return sources.tushare_query(sources.ts.pro_api(os.environ["TUSHARE_TOKEN"]), api, **params)
    except Exception as exc:
        message = str(exc).replace(os.environ.get("TUSHARE_TOKEN") or "\x00", "[REDACTED]")
        raise sources.SourceError(f"TuShare {api} 未取得数据：{message}；请检查品种/交易所参数；权限或频次问题请记录缺口并使用其他已取得证据") from None


def series_rows(frame, *, api, params, key, fields, prefix, retrieved_at):
    """Reject ambiguous keys and malformed values at the data boundary; missing is never zero."""
    if not isinstance(frame, pd.DataFrame) or not {key, *fields} <= set(frame.columns):
        raise sources.SourceError(f"{api} 缺少必要字段 {key}/{','.join(fields)}；请记录数据缺口")
    if frame.empty:
        raise sources.SourceError(f"{api} 该范围没有数据；请用 commodities 查品种代码，或换宏观系列")
    values = {field: {} for field in fields}
    for record in frame.to_dict("records"):
        raw_period = str(record[key])
        try:
            if key == "month":
                parsed = datetime.strptime(raw_period, "%Y%m")
                period = parsed.strftime("%Y-%m")
            else:
                period = date.fromisoformat(raw_period).isoformat()
        except ValueError:
            raise sources.SourceError(f"{api} 返回无效期间；请记录数据缺口") from None
        compact = period.replace("-", "")
        lower, upper = (params.get("start_m"), params.get("end_m")) if key == "month" else (params.get("start_date"), params.get("end_date"))
        if (lower and compact < lower.replace("-", "")) or (upper and compact > upper.replace("-", "")):
            raise sources.SourceError(f"{api} 返回范围外期间；请记录数据缺口")
        for field in fields:
            raw = record[field]
            if pd.isna(raw):
                continue
            try:
                number = Decimal(str(raw))
            except InvalidOperation:
                raise sources.SourceError(f"{api}.{field} 非数值；请记录数据缺口") from None
            if isinstance(raw, bool) or not number.is_finite():
                raise sources.SourceError(f"{api}.{field} 非有限数值；请记录数据缺口")
            if period in values[field]:
                raise sources.SourceError(f"{api}.{field} 期间重复，不能确定版本；请记录数据缺口")
            values[field][period] = str(number)
    return [{"id": f"{prefix}.{field}", "label": label, "unit": unit,
             "values": dict(sorted(values[field].items())), "exact_values": dict(sorted(values[field].items())),
             "source": {"provider": "TuShare", "api": api, "params": params, "field": field,
                        "retrieved_at": retrieved_at, "period_kind": "month" if key == "month" else "date"}}
            for field, (label, unit) in fields.items() if values[field]]


def macro(series, count=12):
    if not isinstance(series, str) or series not in MACRO or type(count) is not int or not 1 <= count <= 24:
        raise ValueError(f"series 可用 {', '.join(MACRO)}，count 须为 1–24")
    api, fields = MACRO[series]
    end = datetime.now(db.SHANGHAI).date() - timedelta(days=1)
    monthly = series not in ("lpr", "shibor")
    params = ({"start_m": (end - timedelta(days=800)).strftime("%Y%m"), "end_m": end.strftime("%Y%m")}
              if monthly else {"start_date": (end - timedelta(days=800 if series == "lpr" else 80)).strftime("%Y%m%d"),
                                   "end_date": end.strftime("%Y%m%d")})
    key = "month" if monthly else "date"
    frame = query(api, **params)
    rows = series_rows(frame, api=api, params=params, key=key, fields=fields,
                       prefix=f"X.{api}", retrieved_at=db.now())
    for row in rows:
        for field in ("values", "exact_values"):
            row[field] = dict(list(row[field].items())[-count:])
    return rows


def commodities(exchange="SHFE"):
    if not isinstance(exchange, str) or exchange not in EXCHANGES:
        raise ValueError(f"exchange 可用 {', '.join(EXCHANGES)}")
    frame = query("fut_basic", exchange=exchange, fut_type="1")
    needed = {"fut_code", "name", "quote_unit", "ts_code", "list_date"}
    if not needed <= set(frame.columns) or frame.empty:
        raise sources.SourceError("fut_basic 无有效品种目录；请换交易所 SHFE/DCE/CZCE/INE/GFEX")
    found = []
    for _, row in frame.sort_values("list_date").groupby("fut_code", sort=True).tail(1).iterrows():
        found.append({"code": f"{row.fut_code}.{EXCHANGES[exchange]}",
                      "name": re.sub(r"\d+", "", row['name']), "unit": row.quote_unit})
    return sorted(found, key=lambda item: item["code"])


def commodity(code):
    if not isinstance(code, str) or not re.fullmatch(r"[A-Z]{1,3}\.(SHF|DCE|ZCE|INE|GFE)", code):
        raise ValueError("code 须为主力连续代码；先用 commodities(exchange) 查目录，例如 AL.SHF")
    exchange = next(k for k, v in EXCHANGES.items() if v == code.split('.')[1])
    item = next((r for r in commodities(exchange) if r["code"] == code), None)
    if not item or not isinstance(item["unit"], str) or not item["unit"].strip():
        raise ValueError("目录中未找到该品种及报价单位；请用 commodities 查可用代码")
    end = datetime.now(db.SHANGHAI).date() - timedelta(days=1)
    params = {"ts_code": code, "start_date": (end - timedelta(days=400)).strftime("%Y%m%d"), "end_date": end.strftime("%Y%m%d")}
    frame = query("fut_daily", **params)
    mapping = query("fut_mapping", **params)
    if (not {"ts_code", "trade_date", "mapping_ts_code"} <= set(mapping.columns) or mapping.empty
            or "ts_code" not in frame or not mapping.ts_code.eq(code).all() or not frame.ts_code.eq(code).all()):
        raise sources.SourceError("主力行情/映射身份不一致或缺失；请换 commodities 目录中的品种并记录缺口")
    rows = series_rows(frame, api="fut_daily", params=params, key="trade_date",
                       fields={"close": (f"{item['name']}主力连续收盘价（换月未复权）", item["unit"])},
                       prefix=f"X.{code}", retrieved_at=db.now())
    mapped = {date.fromisoformat(str(r.trade_date)).isoformat(): r.mapping_ts_code for r in mapping.itertuples()}
    if not set(rows[0]["values"]) <= set(mapped):
        raise sources.SourceError("部分主力行情缺少对应合约映射；请记录缺口，不用单一合约冒充连续行情")
    rows[0]["source"]["mapping"] = {"api": "fut_mapping", "params": params, "contracts": mapped}
    return rows
