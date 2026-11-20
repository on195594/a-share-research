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


FILTERS = dict(shanghai_minute=shanghai_minute, decimal_format=decimal_format, monitor_gap=monitor_gap)
