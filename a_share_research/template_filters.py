"""Display formats for owner-facing HTML."""
import re
from decimal import Decimal

from a_share_research.scans import instant


def shanghai_minute(value: str) -> str:
    # Legacy thesis records may carry only a date.
    return value if len(value) == 10 else instant(value).strftime('%Y-%m-%d %H:%M')


def decimal_format(value: str | float, spec: str) -> str:
    return format(Decimal(str(value)), spec)


_GAP_FIELDS = {
    'shares': '持仓股数', 'framework': '持仓分析框架',
    'stop_loss_15': '第一档止损参考价', 'stop_loss_20': '第二档止损参考价',
    'quote': '个股行情', 'previous_close': '前收盘价', 'industry_change_pct': '行业涨跌幅',
    'stop_risk': '止损参考风险', 'p3_liquidity_gate': 'P3 流动性复核',
    'tier': '分档退出条件', 'alert.review_due': '预警复核日期', 'l3': 'L3 持仓论点条件',
    'industry_context': '行业对照资料', 'portfolio_value': '组合总资产',
    'account_scope': '账户范围', 'active_holding': '当前持仓周期',
    'account_valuation': '账户估值', 'database': '状态数据库',
}

_GAP_ACTIONS = {
    'supply a same-day industry comparison': '补充同一交易日的行业涨跌幅',
    'resolve quote source conflict': '核对行情来源之间的差异',
    'refresh the current quote': '更新当前行情',
    'retrieve a fresh timestamped quote': '补充带时间戳的最新行情',
    'retrieve a valid previous close': '补充有效的前收盘价',
    'restore valid risk inputs': '核对计算止损参考风险所需的数据',
    'reconcile structured Tier status and entry-time path configuration': '核对各档状态和建仓时设置的退出路径',
    'verify quarterly/interim L3 evidence and three-star timing for path A': '核对路径 A 的季报或中报 L3 证据及三星时点',
    'restore the entry-time path B target': '补充建仓时设置的路径 B 目标',
    'restore reference_cost for the configured Tier target': '补充分档目标使用的参考成本',
    'repair the alert review date': '核对并更正预警复核日期',
    'active thesis version missing': '补充当前生效的持仓论点',
    'active L3 contract incomplete': '补全当前论点的 L3 条件',
    'provide --portfolio-value': '补充组合总资产',
    'confirm and provide --account-scope': '确认并补充账户范围',
    'reconcile the active lifecycle': '核对当前持仓周期',
    'restore finite account valuation inputs': '核对账户估值所需的数据',
    'restore the required state database': '恢复监控所需的状态数据库',
}


def monitor_gap(gap: dict) -> str:
    field = gap['field']
    label = _GAP_FIELDS.get(field, field)
    action = gap['minimum_action']
    if action == f'reconcile holding {field}' and field in _GAP_FIELDS:
        hint = f'核对{label}'
    elif action.startswith('complete P3 review: '):
        hint = '完成 P3 流动性复核，原因代码：' + action.removeprefix('complete P3 review: ')
    elif action.startswith('restore same-industry context (') and action.endswith(')'):
        reason = action[len('restore same-industry context ('):-1]
        hint = f'补充同行业对照资料（{reason}）'
    elif match := re.fullmatch(r'L3 condition (\d+) (contract invalid|review date invalid)', action):
        detail = '复核日期' if match[2] == 'review date invalid' else '约定'
        hint = f'核对 L3 条件 #{match[1]} 的{detail}'
    else:
        hint = _GAP_ACTIONS.get(action, action)
    return f'{label} · {hint}'


_ESCALATIONS = {
    'quote sources conflict': '行情来源之间有冲突',
    'multiple active holding rows for one code': '同一代码有多条在仓记录',
    'quote is stale': '行情已过期',
    'quote or timestamp unavailable': '缺少行情或时间戳',
    'current price reached the stored second stop line': '现价已触及第二档止损参考价',
    'current price reached the stored first stop line': '现价已触及第一档止损参考价',
    'explicit portfolio value is required for weights and risk budgets': '缺少组合总资产，无法计算仓位占比和风险预算',
    'account scope is unconfirmed; policy not applied; weights and risk budgets unavailable':
        '账户范围未确认，未执行政策，仓位占比和风险预算不可用',
}

_L3_CONTRACTS = {'versioned': '有条件尚待核实', 'legacy': '缺少当前生效的论点版本',
                 'invalid': '论点条件不完整', 'none': '没有持仓论点'}

_ESCALATION_PATTERNS = (
    (r'daily change (\S+)% is at or below -3%', lambda m: f'当日涨跌 {m[1]}%，已到 -3% 提醒线'),
    (r'relative change (\S+)pts is at or below -2pts', lambda m: f'相对行业 {m[1]} 个百分点，已到 -2 个百分点提醒线'),
    (r'(?:path (\S+)|Tier1) price target reached; review Step 4',
     lambda m: f'{"路径 " + m[1] if m[1] else "第一档"}目标价已到，复核第 4 步'),
    (r'alert (\S+) is due', lambda m: f'预警 {m[1]} 已到复核日'),
    (r'L3 state requires verification \((.*)\)', lambda m: 'L3 持仓论点待核实：' + _L3_CONTRACTS.get(m[1], m[1])),
    (r'L3 condition (\d+) is due', lambda m: f'L3 条件 #{m[1]} 已到复核日'),
    (r'L3 condition (\d+) is triggered', lambda m: f'L3 条件 #{m[1]} 已触发'),
    (r'L3 condition (\d+) is watch \(as_of (\S+), next_review (\S+)\); no new risk',
     lambda m: f'L3 条件 #{m[1]} 观察中（资料截至 {m[2]}，下次复核 {m[3]}），无新增风险'),
)


def monitor_escalation(item: dict) -> str:
    # Saved evaluations keep the policy's English detail; the owner reads the Chinese rendering.
    detail = item['detail']
    if item['reason_code'] == 'risk_budget_exceeded':
        fields = dict(part.split('=', 1) for part in detail.split('; ') if '=' in part)
        scope = '单只' if fields.get('scope') == 'position' else '组合'
        return (f'{scope}止损参考风险 {fields.get("amount", "?").removesuffix(" CNY")} 元，占 {fields.get("ratio", "?")}，'
                f'超过上限 {fields.get("limit", "?")}')
    if item['reason_code'] == 'denominator_missing':
        for english, chinese in _ESCALATIONS.items():
            detail = detail.replace(english, chinese)
        return detail.replace('; ', '；')
    for pattern, render in _ESCALATION_PATTERNS:
        if match := re.fullmatch(pattern, detail):
            return render(match)
    return _ESCALATIONS.get(detail) or _GAP_ACTIONS.get(detail, detail)


def monitor_lines(policy: dict) -> list[tuple[str, str]]:
    """Escalations, then data gaps; the same line for several holdings collapses into one."""
    names = {h['code']: h.get('name') or h['code'] for h in policy.get('holdings') or []}
    lines: dict[str, list[str]] = {}
    for text, code in ([(monitor_escalation(e), e['code']) for e in policy.get('escalations') or []]
                       + [(monitor_gap(g), g['code']) for g in policy.get('data_gaps') or []]):
        codes = lines.setdefault(text, [])
        if code and code not in codes:
            codes.append(code)
    return [('、'.join(names.get(code, code) for code in codes), text) for text, codes in lines.items()]


FILTERS = dict(shanghai_minute=shanghai_minute, decimal_format=decimal_format, monitor_gap=monitor_gap,
               monitor_lines=monitor_lines)
