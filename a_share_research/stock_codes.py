"""Parse stock codes and normalize company and TuShare identifiers."""

import re

KEY_PATTERN = r"^[0-9]{6}$"
CODE_RE = re.compile(r"^([0-9]{6})(?:\.(SH|SZ|BJ))?$")
KEY_RE = re.compile(KEY_PATTERN)
_INVALID_CODE = "股票代码必须为6位数字或带.SH/.SZ/.BJ后缀"


def parse_code(code: str) -> tuple[str, str | None]:
    """Return the six-digit key and optional explicit exchange suffix."""
    if not isinstance(code, str) or not code:
        raise ValueError(_INVALID_CODE)
    match = CODE_RE.fullmatch(code.strip())
    if match is None:
        raise ValueError(_INVALID_CODE)
    return match.group(1), match.group(2)


def normalize_key(code: str) -> str:
    """Return the six-digit company key."""
    return parse_code(code)[0]


def infer_ts_code(code: str) -> str:
    """Preserve an explicit suffix or infer one using a-stock-lib's exchange rules."""
    key, suffix = parse_code(code)
    if suffix is not None:
        return f"{key}.{suffix}"
    # Deferred: the provider module pulls pandas, which code parsing does not otherwise need.
    from a_stock_lib.providers.tushare_quotes import to_tushare_stock_code

    return to_tushare_stock_code(key)
