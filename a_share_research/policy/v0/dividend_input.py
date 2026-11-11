"""Pure projection of normalized provider rows into the existing v0 dividend input."""
from a_share_research.policy.v0 import risk_budget


def implemented_rows(rows: list[dict]) -> list[dict]:
    """Preserve the legacy implemented-dividend filter and per-share conversion."""
    implemented = []
    for row in rows:
        cash = row.get('cash_div_tax')
        if row.get('div_proc') != '实施' or not isinstance(row.get('record_date'), str):
            continue
        if not risk_budget.positive_number(cash) and not risk_budget.positive_number(row.get('stk_div')):
            continue
        implemented.append({
            'record_date': row['record_date'],
            'ex_date': row.get('ex_date') if isinstance(row.get('ex_date'), str) else None,
            'pay_date': row.get('pay_date') if isinstance(row.get('pay_date'), str) else None,
            'cash_div': float(cash) if risk_budget.positive_number(cash) else 0.0,
            'split_ratio': float(row['stk_div']) if risk_budget.positive_number(row.get('stk_div')) else 0.0,
        })
    return implemented
