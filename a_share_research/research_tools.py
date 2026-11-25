"""Research evidence tools, caches, traces and snapshots owned by the worker."""

import hashlib
import json
import logging
import re
import time
from copy import deepcopy
from pathlib import Path
from decimal import Decimal, ROUND_HALF_UP, localcontext

from a_share_research import report as report_body
from a_share_research import db, sources, evidence, market, stock_codes, valuation as valuations
from a_share_research.checking import quote_found
from a_share_research.material import financial_table
from a_share_research.reviewing import digest

log = logging.getLogger(__name__)

TOOLS = [
    {"name": "industry_trend", "description": "按公司当前申万成分合成行业趋势与估值。code 为六位公司代码，level 默认 l2，可用 l1/l2/l3。返回近20/60/120交易日复权收益中位数、起点流通市值加权收益及剔除本公司后的加权收益、上涨家数占比、中证800价格指数同期收益、本公司收益；C.* 给出加权口径相对中证800的差及本公司相对行业中位数、相对同业加权的差。收益中位数与市值加权的中证800风格不同，二者之差不是超额。正PE_TTM中位数及近五年60个月末分位走缓存。X.* 保存成分、交易日、API参数及可复算输入；当前成分回看有幸存者偏差，缺失不填零，指数不含分红再投资。冷缓存约76次查询，可能超过一分钟；同日复用缓存。",
     "parameters": {"type": "object", "properties": {"code": {"type": "string"}, "level": {"type": "string", "enum": ["l1", "l2", "l3"]}}, "required": ["code"], "additionalProperties": False}},
    {"name": "macro", "description": "查询最近已公布的宏观序列（缺失不填零）：pmi 制造业PMI，money M1/M2，financing 社融增量，lpr 贷款利率，shibor 拆借利率，cpi/ppi 价格同比环比。count 默认12，最多24期；月度键为YYYY-MM，日度键为YYYY-MM-DD；值与来源注册为可引用 X.* 行，也可交给 calculate。月度数据是当前可得版本，不代表当月已知。",
     "parameters": {"type": "object", "properties": {"series": {"type": "string", "enum": ["pmi", "money", "financing", "lpr", "shibor", "cpi", "ppi"]},
                    "count": {"type": "integer", "minimum": 1, "maximum": 24}}, "required": ["series"], "additionalProperties": False}},
    {"name": "commodities", "description": "查期货可用品种主力连续代码、名称及报价单位；exchange 可用 SHFE（如铝）、DCE（如LPG）、CZCE、INE（如原油）、GFEX（如碳酸锂）。按目录代码调用 commodity，不凭记忆猜代码。",
     "parameters": {"type": "object", "properties": {"exchange": {"type": "string", "enum": ["SHFE", "DCE", "CZCE", "INE", "GFEX"]}}, "required": [], "additionalProperties": False}},
    {"name": "commodity", "description": "查询 commodities 目录中的主力连续合约，返回可引用 X.* 价格序列、逐日主力合约映射及经 calculate 计算的近20/60/120个交易日变化 C.*。价格是未复权连续收盘，换月跳空会影响变化率，不是现货价格或投资收益率。",
     "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"], "additionalProperties": False}},
    {"name": "research_methods", "description": "按问题关键词检索方法线索，可跨公司；用空格分隔关键词。包括已通过核查的历史纠错方法（附原报告出处）和有公开来源的分析方法卡（附标题与来源链接）。返回最多五条。它们是待重新验证的方法线索，不能作为当前公司事实或投资政策。",
     "parameters": {"type": "object", "properties": {"query": {"type": "string", "minLength": 1, "maxLength": 80}},
                    "required": ["query"], "additionalProperties": False}},
    {"name": "announcements", "description": "检索公司官方临时公告，包括业绩预告/快报、中标、重大事项等；query 为标题关键词，留空看近期目录。默认近180天，最多550天；返回截断范围。period 为空时不要用公告日代替财务期间。随后用 read_disclosure 读取原文。",
     "parameters": {"type": "object", "properties": {
         "code": {"type": "string", "pattern": stock_codes.KEY_PATTERN}, "query": {"type": "string", "maxLength": 40},
         "days": {"type": "integer", "minimum": 1, "maximum": 550}},
         "required": ["code"], "additionalProperties": False}},
    {"name": "peers", "description": "按申万2021行业（与候选初筛相同的分类）列出与该公司同属一个行业的上市公司代码与名称。level 默认 l3（最细），可用 l2/l1 放宽。行业相同不等于业务可比：自主决定是否比较、比谁，正文说明可比性与业务差异；随后用 financials 或 disclosures 取数。",
     "parameters": {"type": "object", "properties": {
         "code": {"type": "string", "pattern": stock_codes.KEY_PATTERN}, "level": {"type": "string", "enum": ["l1", "l2", "l3"]}},
         "required": ["code"], "additionalProperties": False}},
    {"name": "disclosures", "description": "列出本公司或同业的官方定期报告，最新在前。code 为六位代码。",
     "parameters": {"type": "object", "properties": {"code": {"type": "string", "pattern": stock_codes.KEY_PATTERN}},
                    "required": ["code"], "additionalProperties": False}},
    {"name": "read_disclosure", "description": "从已列出的文档读取原文。query 支持空格分隔多关键词同页匹配，返回匹配页号及最多 count 页原文（结合 page 可跳过目录或向后翻页）；不传 query 则从 page 起连续读 count 页（最多5页）。",
     "parameters": {"type": "object", "properties": {
         "source": {"type": "string"}, "query": {"type": "string"},
         "page": {"type": "integer", "minimum": 1}, "count": {"type": "integer", "minimum": 1, "maximum": 5}},
         "required": ["source"], "additionalProperties": False}},
    {"name": "financials", "description": "查询本公司或同业的可比财务指标及行情。返回可用于计算和引用的行，缺失不填零。",
     "parameters": {"type": "object", "properties": {"code": {"type": "string", "pattern": stock_codes.KEY_PATTERN}},
                    "required": ["code"], "additionalProperties": False}},
    {"name": "valuation", "description": "按拟定三情景估值（与报告 valuation 同格式，rationale 可暂缺）生成可引用 C.n：期望回报门槛价、期望年化回报，以及保守/中性/乐观各自的年化和累计回报，后者不是概率加权结果，也不是最坏情景。与报告页 valuation-v2 同源。正文引用时最终 valuation 的 basis/years/scenarios/payout_pct 须与测算输入一致，改动后重算。期限固定并返回实际年数；其他持有期用 calculate annual_return。相同定价输入重复调用返回已有行。",
     "parameters": {"type": "object", "properties": {"valuation": {"type": "object"}}, "required": ["valuation"], "additionalProperties": False}},
    {"name": "calculate", "description": "精确计算并生成可引用的 C.n 行。事实输入用表格/前序计算 {ref, period}，或已读披露原数 {source, page, quote, value, unit, period(可选), label(可选)}；原数含正负号、小数位必须匹配引文，会计括号表示负数。假设参数单独用 {assumption: 非空说明, value: 数字, unit: 单位}，不得混入事实出处；假设及其链式结果均标为假设测算，不是披露事实。multiply 用于数量乘无量纲系数（单位倍），如利润乘0.8检验下降20%；ratio 可除以倍数，其余比值要求同单位或可换算货币单位。annual_return 的 left 为退出价值加持有期间累计现金分红，right 为买入价值，另传 holding_years（正数，不超过50年），计算总回报的复合年化百分比；分红不再投资，不是IRR。growth_pct 只用于同指标相邻年度同月日同比；跨期、环比或异源变动用 change_pct。label 只记录目的，不证明财务含义。",
     "parameters": {"type": "object", "properties": {
         "label": {"type": "string"},
         "operation": {"type": "string", "enum": ["difference", "sum", "multiply", "growth_pct", "change_pct", "ratio", "ratio_pct", "annual_return"]},
         "holding_years": {"type": "number", "exclusiveMinimum": 0, "maximum": 50},
         **{side: {"type": "object", "properties": {
             "ref": {"type": "string"}, "period": {"type": "string"},
             "source": {"type": "string"}, "page": {"type": "integer", "minimum": 1},
             "quote": {"type": "string"}, "value": {"type": ["string", "number"]},
             "unit": {"type": "string"}, "label": {"type": "string"}, "assumption": {"type": "string", "minLength": 1, "maxLength": 200}},
             "additionalProperties": False} for side in ("left", "right")}},
         "required": ["label", "operation", "left", "right"], "additionalProperties": False}},
]

CURRENCY_UNITS = {
    "元": Decimal(1), "千元": Decimal("1e3"), "万元": Decimal("1e4"),
    "百万元": Decimal("1e6"), "亿元": Decimal("1e8"),
}
DECLARED_UNIT = re.compile(r"单位\s*[：:]\s*(?:人民币)?(亿元|百万元|万元|千元|元)")
OPERATION_NAMES = {"difference": "差额", "sum": "合计", "growth_pct": "相对变动率",
                   "change_pct": "相对变动率", "ratio": "比值", "ratio_pct": "比值（百分比）",
                   "multiply": "乘积", "annual_return": "持有期复合年化回报"}


# Shortcut: scan existing report JSON; add an index only when measured history size needs it.
ACTIVE_METHODS = (
    "SELECT r.id, r.code, r.created_at, r.body, m.key, m.value FROM reports r"
    " JOIN runs u ON u.id = r.run_id JOIN json_each(r.body, '$.methods') m"
    " WHERE r.passed = 1 AND u.status = 'done' AND u.kind = 'research'"
    " AND json_type(u.request, '$.feedback') = 'object'"
    " AND json_type(r.body, '$.methods') = 'array'"
    " AND NOT EXISTS (SELECT 1 FROM reports newer JOIN runs n ON n.id = newer.run_id"
    " WHERE newer.passed = 1 AND n.status = 'done' AND n.kind = 'research'"
    " AND json_type(newer.body, '$.methods') = 'array'"
    " AND n.parent_report_id = r.id"
    " AND json_extract(n.request, '$.feedback.location') = 'methods[' || m.key || ']')"
)


def method_catalog(conn, *, before=None) -> list[dict]:
    """Freeze retrievable methods at a run/batch boundary, including later supersessions."""
    sql = ACTIVE_METHODS.replace(
        " AND n.parent_report_id = r.id",
        " AND (? IS NULL OR newer.created_at <= ?) AND n.parent_report_id = r.id")
    rows = conn.execute(sql + " AND (? IS NULL OR r.created_at <= ?) ORDER BY r.id DESC, m.key",
                        (before, before, before, before)).fetchall()
    found = []
    for row in rows:
        body, method = report_body.load(row["body"]), json.loads(row["value"])
        sources_by_id = {"D0": {"document": body.get("document", {})}, **body.get("sources", {})}
        found.append({"report_id": row["id"], "code": row["code"], "created_at": row["created_at"],
                      "location": f"methods[{row['key']}]", "method": method,
                      "sources": {c.get("source", "D0"): sources_by_id.get(c.get("source", "D0"))
                                  for c in method["cites"] if "ref" not in c},
                      "table": [r for r in body.get("table", [])
                                if r["id"] in {c.get("ref") for c in method["cites"]}]})
    return found


def method_cards(root) -> list[dict]:
    """Load reviewed wiki method cards as catalog entries: analysis guidance, never company evidence.

    Cards share the `method.text` shape of correction methods so retrieval, freezing and
    prompt projection stay unchanged; they carry no report or evidence identity.
    """
    cards = []
    for path in sorted(Path(root).glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        page = re.fullmatch(r"---\n(.*?)\n---\n(.*)", raw, re.S)
        meta = dict(line.split(": ", 1) for line in page[1].splitlines() if ": " in line) if page else {}
        if meta.get("status") != "stable" or not meta.get("title"):
            log.warning("方法卡未载入（需 frontmatter 含 title 且 status: stable）：%s", path.name)
            continue
        cards.append({"card": path.stem, "title": meta["title"], "verified_at": meta.get("verified_at"),
                      "source_urls": re.findall(r"https?://[^\s,\]]+", meta.get("sources", "")),
                      "sha256": hashlib.sha256(raw.encode()).hexdigest(), "method": {"text": page[2].strip()}})
    if not cards:
        log.warning("方法卡目录没有可用卡片：%s", root)
    return cards


def run_method_catalog(conn, *, before, batch: bool, cards_dir=None) -> list[dict]:
    """Catalog frozen for one run: correction methods, plus method cards for non-batch single-stock runs.

    Batch deep dives keep the correction-only catalog so cross-company comparison inputs stay comparable.
    """
    catalog = method_catalog(conn, before=before)
    return catalog if batch or not cards_dir else catalog + method_cards(cards_dir)


def active_methods_digest(conn, *, catalog=None) -> str:
    """Hash the exact frozen catalog, shared by exploration and method retrieval."""
    if catalog is None:
        catalog = method_catalog(conn)
    return hashlib.sha256(json.dumps(catalog, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def research_methods(conn, query: str, *, catalog=None) -> list[dict]:
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 80:
        raise ValueError("方法检索关键词须为 1–80 字")
    methods = method_catalog(conn) if catalog is None else catalog
    terms = query.split()
    found = [method for method in methods if any(term in method["method"]["text"] for term in terms)]
    if len(found) > 5 and any("card" in method for method in found):
        def explicit_card(method):
            if "card" not in method:
                return False
            line = re.search(r"^关键词：(.+)$", method["method"]["text"], re.M)
            keywords = {word.strip() for word in line[1].removesuffix("。").split("、")} if line else set()
            return query.strip() == method["title"] or any(term in keywords for term in terms)

        # Stable partition on a new list; Relations links remain ordinary substring hits.
        found = sorted(found, key=lambda method: not explicit_card(method))
    return found[:5]


def _operand(side: dict, rows: dict, material) -> dict:
    if not isinstance(side, dict):
        raise ValueError("计算输入须为对象")
    clean = {k: v for k, v in side.items() if v is not None and v != ""}
    if "assumption" in side:
        if (set(side) != {"assumption", "value", "unit"}
                or not isinstance(side["assumption"], str) or not 1 <= len(side["assumption"].strip()) <= 200
                or not isinstance(side["unit"], str) or not side["unit"].strip()
                or valuations.finite_number(side["value"]) is None):
            raise ValueError("假设输入仅用 {assumption: 非空说明, value: 有限数字, unit: 单位}，不得混入事实出处")
        return {"assumption": side["assumption"].strip(), "label": side["assumption"].strip(),
                "value": str(valuations.finite_number(side["value"])), "unit": side["unit"].strip(), "period": "假设"}
    if set(clean) <= {"ref", "period"} and "ref" in clean and not (set(side) - {"ref", "period", "source", "page", "quote", "value", "unit", "label"}):
        if clean["ref"] not in rows:
            raise ValueError(f"表中没有 {clean['ref']}；可引用 financials 返回的行或此前计算得到的 C.n")
        row = rows[clean["ref"]]
        value_field = "exact_values" if "exact_values" in row else "values"
        period = clean.get("period") or ("result" if set(row.get(value_field, {})) == {"result"} else None)
        if reason := row.get("unavailable", {}).get(period):
            raise ValueError(f"{period} 更正版本待核对：{reason}")
        if period not in row.get(value_field, {}):
            # Tell the model what exists so it can retry instead of guessing.
            problem = f"没有期间 {period}" if period else "须指定 period"
            raise ValueError(f"{clean['ref']} {problem}；可用期间：{'、'.join(sorted(row.get(value_field, {}))) or '无'}")
        result = {"ref": clean["ref"], "period": period, "label": row["label"],
                  "value": row[value_field][period], "unit": row["unit"], "value_field": value_field}
        if assumptions := row.get("calculation", {}).get("assumptions"):
            result["assumptions"] = deepcopy(assumptions)
        return result
    if {"page", "quote", "value", "unit"} <= set(clean) and not clean.get("ref"):
        if material is None or not quote_found(side, material):
            raise ValueError("计算输入的原文引文在已读取页面中找不到")
        raw = str(side["value"]).replace(",", "").replace("−", "-").strip()
        try:
            num = Decimal(raw)
        except ArithmeticError as exc:
            raise ValueError("计算输入必须是有限数字") from exc
        tokens = (evidence.signed_numbers(str(side["quote"]))
                  + evidence.rejoined_numbers(side, material.source_registry()))
        if not num.is_finite() or not any(num == t and num.as_tuple().exponent == t.as_tuple().exponent for t in tokens):
            raise ValueError("计算输入的数值（含正负号与小数位）须与所引原文中的原数完全一致")
        source = str(side.get("source") or "D0")
        page = side["page"]
        unit = str(side["unit"]).strip()
        if not unit:
            raise ValueError("计算输入须注明单位")
        doc = material.source_registry().get(source, {})
        pages = doc.get("pages", {})
        page_text = "\n".join(pages.get(p) or pages.get(str(p), "") for p in (page - 1, page, page + 1))
        quote_text = str(side["quote"])
        quote_no_comma = re.sub(r"(?<=\d),(?=\d{3}\b)", "", quote_text)
        inline = (re.search(re.escape(str(side["value"]).strip()) + r"\s*(亿元|百万元|万元|千元|元|%)", quote_text)
                  or re.search(re.escape(raw) + r"\s*(亿元|百万元|万元|千元|元|%)", quote_no_comma))
        if inline and inline[1] != unit:
            raise ValueError(f"计算输入单位与原文紧邻单位（{inline[1]}）不一致")
        if not inline:
            if unit not in quote_text:
                declared = DECLARED_UNIT.findall(page_text)
                if declared and unit in CURRENCY_UNITS and unit not in declared:
                    raise ValueError(f"页面声明单位为 {declared[0]}，不能按 {unit} 输入")
            if unit != "元" and unit not in page_text and unit not in quote_text:
                raise ValueError(f"所引页面中未见单位 {unit}")
        loc = f"{source}第{page}页"
        return {"source": source, "page": page, "quote": side["quote"],
                "period": str(side.get("period") or loc), "label": str(side.get("label") or loc),
                "value": str(num), "unit": unit}
    raise ValueError("计算输入须为 {ref, period} 或带原文出处的 {source, page, quote, value, unit}")


def calculate(table: list[dict], label: str, operation: str, left: dict, right: dict, *, material=None, holding_years=None) -> dict:
    if not isinstance(label, str) or not 1 <= len(label) <= 160:
        raise ValueError("计算名称须为 1–160 字")
    years = valuations.finite_number(holding_years)
    if operation == "annual_return":
        if years is None or not 0 < years <= 50:
            raise ValueError("annual_return 须传 holding_years：大于0、不超过50年的有限数字")
    elif holding_years is not None:
        raise ValueError("holding_years 仅用于 annual_return")
    rows = {r["id"]: r for r in table}
    inputs = [_operand(left, rows, material), _operand(right, rows, material)]
    u0, u1 = inputs[0]["unit"], inputs[1]["unit"]
    a, b = (Decimal(v["value"]) for v in inputs)
    if not a.is_finite() or not b.is_finite():
        raise ValueError("计算输入必须是有限数字")
    if operation == "annual_return" and not (u0 in CURRENCY_UNITS and u1 in CURRENCY_UNITS):
        raise ValueError("annual_return 的终值和买入价值须为金额，不能用增长率或百分比代替")
    scaling = (operation == "multiply" and "倍" in (u0, u1)) or (operation == "ratio" and u1 == "倍")
    if scaling:
        unit = u1 if operation == "multiply" and u0 == "倍" else u0
    elif operation == "multiply":
        raise ValueError("multiply 至少一项须为无量纲系数（单位倍），不能将两个金额相乘")
    elif u0 == u1:
        unit = u0
    elif u0 in CURRENCY_UNITS and u1 in CURRENCY_UNITS:
        # Convert both operands deterministically to the larger currency unit (e.g. 元 -> 亿元).
        unit = u0 if CURRENCY_UNITS[u0] >= CURRENCY_UNITS[u1] else u1
        a = a * CURRENCY_UNITS[u0] / CURRENCY_UNITS[unit]
        b = b * CURRENCY_UNITS[u1] / CURRENCY_UNITS[unit]
    else:
        raise ValueError("计算输入单位不同，不能直接比较")
    normalized_inputs = [{"value": str(value), "unit": original if scaling else unit}
                         for value, original in zip((a, b), (u0, u1))]
    if operation == "difference":
        result, formula = a - b, "left - right"
        unit = "个百分点" if unit == "%" else unit
    elif operation == "sum":
        result, formula = a + b, "left + right"
    elif operation == "multiply":
        result, formula = a * b, "left * right"
    elif operation == "annual_return":
        assert years is not None  # validated before resolving the operands
        with localcontext() as context:
            context.prec = 40
            result = valuations.annual_return(a, b, years) * 100
        formula, unit = f"((left / right)^(1 / {years}) - 1) * 100", "%"
    elif operation == "growth_pct":
        lp, rp = inputs[0]["period"], inputs[1]["period"]
        same_metric = (
            inputs[0]["ref"] == inputs[1]["ref"] if ("ref" in inputs[0] and "ref" in inputs[1])
            else (bool(left.get("label") or right.get("label")) and inputs[0]["label"].split("·")[-1] == inputs[1]["label"].split("·")[-1])
        )
        if not same_metric:
            raise ValueError("growth_pct 同比须为同一指标：表格两侧取同一 ref，PDF 原数用与披露科目一致的 label。"
                             "异源相对变动用 change_pct，计算两项占比用 ratio_pct")
        dated = re.fullmatch(r"\d{4}-\d{2}-\d{2}", lp)
        if (not dated or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", rp)
                or lp[4:] != rp[4:] or int(lp[:4]) != int(rp[:4]) + 1):
            hint = f"；核对基期是否有 {int(lp[:4]) - 1:04d}{lp[4:]} 的同指标原数" if dated else ""
            raise ValueError(f"growth_pct 收到 left.period={lp}，right.period={rp}；同比取本期与上年同月日（YYYY-MM-DD）{hint}。"
                             "若要比较所选期间的相对变动且基期为正，改用 change_pct；若求差额，用 difference")
        if b <= 0:
            raise ValueError("right 基期非正数，增速不适用；改用 difference 计算差额并解释")
        result, formula, unit = (a / b - 1) * 100, "(left / right - 1) * 100", "%"
    elif operation == "change_pct":
        if b <= 0:
            raise ValueError("right 基期非正数，相对变动率不适用；改用 difference 计算差额并解释")
        result, formula, unit = (a / b - 1) * 100, "(left / right - 1) * 100", "%"
    elif operation in ("ratio", "ratio_pct"):
        if b == 0:
            raise ValueError("分母为零，不能计算比率")
        result, formula, unit = a / b, "left / right", u0 if scaling else "倍"
        if operation == "ratio_pct":
            result, formula, unit = result * 100, "left / right * 100", "%"
    else:
        raise ValueError("未知计算操作")
    precision = "基于所列输入计算，显示结果四舍五入到两位"
    if any(value.get("value_field") == "values" for value in inputs):
        precision += "；部分输入仅有已保存表值，精度受该表值限制"
    assumptions = []
    for item in inputs:
        for assumption in ([{k: item[k] for k in ("assumption", "value", "unit")}] if "assumption" in item
                           else item.get("assumptions", [])):
            if assumption not in assumptions:
                assumptions.append(deepcopy(assumption))
    if years is not None:
        assumptions.append({"assumption": "指定持有年数；累计现金分红不再投资，非IRR", "value": str(years), "unit": "年"})
    if assumptions:
        precision += "；包含显式假设，不是已披露事实"
    return {"id": f"C.{1 + sum(r['id'].startswith('C.') for r in table)}", "label": OPERATION_NAMES[operation],
            "unit": unit, "values": {"result": str(result.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))},
            "exact_values": {"result": str(result)},
            "calculation": {"operation": operation, "formula": formula, "inputs": inputs, "requested_label": label,
                            "normalized_inputs": normalized_inputs,
                            "precision": precision, **({"assumptions": assumptions} if assumptions else {})}}


def price_changes(table, row):
    """Compute observed-session changes through the same audited calculator as model calls."""
    days = sorted(row["values"])
    changes, gaps = {}, []
    for window in (20, 60, 120):
        if len(days) <= window:
            gaps.append(f"历史不足 {window + 1} 个交易日，无法计算 {window} 日变化")
            continue
        changes[str(window)] = calculate([*table, *changes.values()], label=f"{row['label']}近{window}个交易日变化",
                                          operation="change_pct", left={"ref": row["id"], "period": days[-1]},
                                          right={"ref": row["id"], "period": days[-window - 1]})
    return {"changes": changes, "gaps": gaps}


class ResearchTools:
    def __init__(self, material, conn):
        self.material = material
        self.conn = conn
        self.documents = {"D0": material.doc}
        self.cache = {}
        self.disclosure_cache = {stock_codes.normalize_key(material.code): dict(material.extra.get("catalog", {}))}
        self.financial_cache = {stock_codes.normalize_key(material.code): [r for r in material.table if not r["id"].startswith("C.")]}

    def _register_documents(self, docs: list[sources.Document]) -> dict:
        """Validate a whole search result before adding any document to the held evidence."""
        catalog = self.material.extra["catalog"]
        by_url = {}
        for key, info in catalog.items():
            by_url.setdefault(info["url"], key)
        next_id = max((int(k[1:]) for k in catalog), default=0) + 1
        found, additions = {}, {}
        for doc in docs:
            key = by_url.get(doc.url)
            if key is None:
                key = by_url[doc.url] = f"D{next_id}"
                next_id += 1
                additions[key] = doc
                found[key] = doc.info()
            else:
                previous = found[key] if key in found else catalog[key]
                if field := evidence.identity_conflict(previous, doc.info(), ("code", "period", "url", "sha256"), partial=True):
                    raise evidence.EvidenceError(f"来源 {key} 的 {field} 与既有身份冲突")
                found[key] = previous
        if additions:
            evidence.source_registry(self.material.doc.info(), self.material.pages, self.material.raw_pages,
                                     self.material.extra.get("sources"), catalog | found)
            catalog.update(found)
            self.documents.update(additions)
        return found

    def _register(self, doc: sources.Document) -> str:
        return next(iter(self._register_documents([doc])))

    def run(self, name: str, args: dict) -> dict | list:
        if not isinstance(args, dict):
            raise ValueError("工具参数须为对象")
        key = json.dumps([name, args], sort_keys=True, ensure_ascii=False)
        spec = next((tool for tool in TOOLS if tool["name"] == name), None)
        if spec is None or set(args) - spec["parameters"]["properties"].keys() or not set(spec["parameters"]["required"]) <= args.keys():
            raise ValueError("未知工具或参数不完整")
        if key in self.cache:
            return self.cache[key]
        result = getattr(self, name)(**args)
        self.cache[key] = result
        return result

    @staticmethod
    def code(value):
        if not isinstance(value, str) or not stock_codes.KEY_RE.fullmatch(value):
            raise ValueError("公司代码必须为六位数字")
        return value

    def _condition_rows(self, rows):
        from a_share_research.industry import validate_rows
        validate_rows(rows, [*self.material.table, *rows])
        existing = {r["id"]: r for r in self.material.table}
        for row in rows:
            if row["id"] in existing:
                old = existing[row["id"]]
                if any(p in old["values"] and old["values"][p] != v for p, v in row["values"].items()):
                    raise ValueError("同一外部序列返回了不同版本，未覆盖已引用数值；请记录版本缺口")
                for field in ("values", "exact_values"):
                    old[field].update(row[field])
            else:
                self.material.table.append(row)
        return rows

    def industry_trend(self, code: str, level: str = "l2"):
        from a_share_research.industry import industry_trend
        result = industry_trend(self.code(code), level)
        rows = self._condition_rows(result['rows'])
        changes = []
        by_metric = {r['id'].rsplit('.', 1)[-1]: r for r in rows}
        for window in (20, 60, 120):
            # The median is not compared with the cap-weighted index: their style gap can flip the sign.
            for left, right, label in (('weighted', 'benchmark', '行业加权相对中证800差'),
                                       ('weighted_ex', 'benchmark', '同业（剔除本公司）加权相对中证800差'),
                                       ('company', 'median', '本公司相对行业收益中位数差'),
                                       ('company', 'weighted_ex', '本公司相对同业（剔除本公司）加权差')):
                a, b = by_metric.get(f'{left}_{window}'), by_metric.get(f'{right}_{window}')
                if a is None or b is None:  # An industry with no other weighted member has no peer row.
                    continue
                period = next(iter(a['values']))
                calc = calculate(self.material.table, f'{window}交易日{label}', 'difference',
                                 {'ref': a['id'], 'period': period}, {'ref': b['id'], 'period': period})
                self.material.table.append(calc)
                changes.append(calc)
        # The full replay inputs stay in material; keep the model-facing response concise.
        result['rows'] = [{**r, 'source': {k: v for k, v in r['source'].items()
                                          if k not in ('dataset', 'eligible', 'sessions', 'members')}} for r in rows]
        return result | {'changes': changes}

    def macro(self, series: str, count: int = 12):
        from a_share_research import conditions
        return self._condition_rows(conditions.macro(series, count))

    def commodities(self, exchange: str = "SHFE"):
        from a_share_research import conditions
        return {"source": "TuShare fut_basic", "retrieved_at": db.now(), "items": conditions.commodities(exchange)}

    def commodity(self, code: str):
        from a_share_research import conditions
        rows = self._condition_rows(conditions.commodity(code))
        row = rows[0]
        result = price_changes(self.material.table, row)
        self.material.table.extend(result["changes"].values())
        return {"rows": rows, **result, "basis": "主力连续收盘（换月未复权），不是现货价格或可实现投资收益"}

    def peers(self, code: str, level: str = "l3"):
        result = sources.industry_peers(self.code(code), level)
        if len(result["members"]) > 80:
            result |= {"members": result["members"][:80], "truncated": True}
        return result

    def disclosures(self, code: str):
        code = self.code(code)
        if code not in self.disclosure_cache:
            self.disclosure_cache[code] = self._register_documents(sources.reports(code))
        return self.disclosure_cache[code]

    def announcements(self, code: str, query: str = "", days: int = 180):
        code = self.code(code)
        docs, coverage = sources.announcements(code, query, days)
        found = self._register_documents(docs)
        self.material.extra.setdefault("announcement_searches", []).append({"code": code, **coverage})
        if coverage["truncated"]:
            self.material.extra["gaps"].append(f"{code} 公告检索超过{sources.ANNOUNCEMENT_PAGE_SIZE * sources.ANNOUNCEMENT_MAX_PAGES}条，仅返回部分目录；请缩小关键词或时间范围。")
        return {"documents": found, "coverage": coverage}

    def research_methods(self, query: str):
        found = research_methods(self.conn, query, catalog=self.material.extra.get("method_catalog"))
        used = self.material.extra.setdefault("methods_used", [])
        for method in found:
            if method not in used:
                used.append(method)
        return found

    def read_disclosure(self, source: str, query: str = "", page: int = 1, count: int = 3):
        if not isinstance(query, str) or len(query) > 120 or type(page) is not int or page < 1 or type(count) is not int or not 1 <= count <= 5:
            raise ValueError("关键词最多 120 字；页码须为正整数，一次最多五页")
        catalog = self.material.extra["catalog"]
        if not isinstance(source, str) or source not in catalog:
            raise ValueError("文档未列出，请先调用 disclosures 或 announcements")
        # Fetch into a candidate: neither a new PDF nor conflicting pages may mutate old evidence.
        doc = deepcopy(self.documents.get(source) or sources.Document(**catalog[source]))
        existing = self.material.source_registry().get(source, {})
        expected = existing.get("document", {})
        if not doc.pages:
            sources.fetch(doc)
        if field := evidence.identity_conflict(expected, doc.info(),
                                               ("code", "period", "url", "sha256", "title", "announced")):
            raise evidence.EvidenceError(f"来源 {source} 的 {field} 与已存证据冲突，未合并新页")
        if expected.get("retrieved_at"):
            doc.retrieved_at = expected["retrieved_at"]
        if page > len(doc.pages):
            raise ValueError(f"文档只有 {len(doc.pages)} 页")
        terms = [re.sub(r"\s+", "", t) for t in query.split() if t]
        hits = [i + 1 for i, text in enumerate(doc.pages)
                if terms and (squashed := re.sub(r"\s+", "", text)) and all(t in squashed for t in terms)]
        selected = [p for p in hits if p >= page][:count] if terms else list(range(page, min(page + count, len(doc.pages) + 1)))
        pages = {n: doc.pages[n - 1] for n in selected}
        raw_pages = {n: doc.raw_pages[n - 1] for n in selected}
        for mode, addition in (("pages", pages), ("raw_pages", raw_pages)):
            old = existing.get(mode, {})
            if any(n in old and evidence.normalize_page(old[n]) != evidence.normalize_page(text)
                   for n, text in addition.items()):
                raise evidence.EvidenceError(f"来源 {source} 的已读页内容冲突，未合并新页")
            # Held pages keep their stored text (cited quotes stay put); only spacing may differ.
            addition.update({n: old[n] for n in addition if n in old})
        # Validate source identity before committing any page or document metadata.
        candidate = deepcopy(self.material.extra["sources"])
        if source != "D0":
            candidate[source] = {"document": doc.info(),
                                 "pages": existing.get("pages", {}) | pages,
                                 "raw_pages": existing.get("raw_pages", {}) | raw_pages}
        evidence.source_registry(doc.info() if source == "D0" else self.material.doc.info(),
                                 self.material.pages, self.material.raw_pages, candidate, catalog)
        self.documents[source] = doc
        if source == "D0":
            self.material.doc = doc
            self.material.pages.update(pages)
            self.material.raw_pages.update(raw_pages)
        else:
            self.material.extra["sources"][source] = candidate[source]
        return {"source": source, "document": doc.info(), "total_pages": len(doc.pages),
                "matched_pages": hits[:60], "pages": pages}

    def financials(self, code: str):
        code = self.code(code)
        if code in self.financial_cache:
            return self.financial_cache[code]
        fin = sources.financials(code)
        if str(fin["income"].sort_values("end_date")["comp_type"].iloc[-1]) != "1":
            raise ValueError("金融公司不适用当前财务口径")
        table = financial_table(fin, sources.valuation(code))
        name = sources.company(code)["zwjc"]
        for row in table:
            row["id"] = f"P{code}." + row["id"]
            row["label"] = name + "·" + row["label"]
            row["source"] = {"provider": "TuShare", "code": code, "retrieved_at": db.now()}
        self.material.table.extend(table)
        self.financial_cache[code] = table
        return table

    def calculate(self, **args):
        row = calculate(self.material.table, **args, material=self.material)
        self.material.table.append(row)
        return row

    def valuation(self, valuation: dict):
        # Rationale wording does not change prices: reuse rows already priced from the same inputs.
        priced = valuations.pricing_inputs(valuation)
        if existing := [r for r in self.material.table
                        if (r.get("calculation") or {}).get("operation") == valuations.PROGRAM_OPERATION
                        and r["calculation"].get("valuation") == priced]:
            return existing
        rows = valuations.program_rows(valuation, self.material.table, self.material.market_date)
        self.material.table.extend(rows)
        return rows


COMPUTE_TOOLS = {"calculate", "valuation"}  # program arithmetic on held evidence, not evidence fetches


def record_stop(material, reason, stage="explore"):
    stops = material.extra.setdefault("stop_reasons", [])
    entry = {"stage": stage, "category": "acquisition_stop", "reason": reason}
    if entry not in stops:
        stops.append(entry)


class EvidenceSession:
    """Execute research tools and persist their evidence and trace against a single run."""

    def __init__(self, conn, run_id: int, material):
        self.conn, self.run_id, self.material = conn, run_id, material
        material.extra.setdefault("catalog", {"D0": material.doc.info()})
        material.extra.setdefault("sources", {})
        material.extra.setdefault("gaps", [])
        self.traces = material.extra.setdefault("tools", [])
        self.toolkit = ResearchTools(material, conn)
        self.failures = {}
        self._evidence_key: str | None = None
        self._dirty = False

    def evidence_key(self):
        # Keep calculation rows/cache untouched; ignore only purpose names and generated IDs
        # when deciding whether another calculation added evidence.
        table = set()
        for row in self.material.table:
            if row["id"].startswith("C."):
                row = row | {"id": "C", "calculation": {k: v for k, v in row["calculation"].items()
                                                        if k != "requested_label"}}
            table.add(digest(row))
        return digest({"sources": self.material.source_registry(), "table": sorted(table),
                       "methods": self.material.extra.get("methods_used", [])})

    def _record(self, name, args, stage, result, failed, started):
        self.traces.append({"tool": name, "arguments": args, "result": result, "error": failed,
                            "stage": stage, "seconds": round(time.monotonic() - started, 3)})
        self._dirty = True

    def flush(self) -> None:
        """Persist accumulated evidence at a stage or service boundary."""
        if self._dirty:
            db.save_material(self.conn, self.run_id, self.material.snapshot())
            self._dirty = False

    def stop(self, reason, stage):
        record_stop(self.material, reason, stage)
        self._dirty = True
        self.flush()

    def _execute(self, name, args):
        try:
            return self.toolkit.run(name, args), False
        except evidence.EvidenceError:
            raise
        except Exception as exc:
            message = market.redact_error(exc)
            log.warning("run %s tool %s: %s", self.run_id, name, message)
            return {"error": message}, True

    def call(self, name: str, args: dict, stage: str) -> tuple[dict, bool]:
        started = time.monotonic()
        try:
            before = self._evidence_key if self._evidence_key is not None else self.evidence_key()
            # A same-value C.n is not progress, but its new ID may unlock a failed reference.
            key = (json.dumps([name, args], sort_keys=True, ensure_ascii=False), before,
                   tuple(row["id"] for row in self.material.table))
            if key in self.failures:
                return {"result": self.failures[key], "evidence_delta": 0, "charged": False}, True
            result, failed = self._execute(name, args)
            after = self.evidence_key()
            self._evidence_key = after
            delta = int(after != before)
        except evidence.EvidenceError as exc:
            self._evidence_key = None
            self._record(name, args, stage, {"error": str(exc)}, True, started)
            raise
        if failed:
            self.failures[key] = result
            if name not in COMPUTE_TOOLS:
                gap = f"{name}：{result['error']}"
                if gap not in self.material.extra["gaps"]:
                    self.material.extra["gaps"].append(gap)
        self._record(name, args, stage, result, failed, started)
        return {"result": result, "evidence_delta": delta, "charged": True}, failed
