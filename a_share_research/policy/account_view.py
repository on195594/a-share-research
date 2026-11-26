"""Read active company codes from the verified source selected for policy capture."""
import json
from pathlib import Path
import sqlite3

from a_share_research import db
from a_share_research.account import mirror
from a_share_research.policy import account_input, monitor, registry
from a_share_research.stock_codes import normalize_key


def bound_account_id(conn: sqlite3.Connection) -> str:
    """Resolve the explicit ledger binding without capturing portfolio or market data."""
    rows = conn.execute('SELECT mode,watermark FROM account_source LIMIT 2').fetchall()
    if len(rows) != 1 or rows[0][0] != 'ledger':
        raise registry.PolicyError('需要唯一绑定的账本，请所有者核对账户来源并绑定一个账户')
    try:
        binding = account_input._json(rows[0][1])
        account_input._binding(binding)
    except (ValueError, TypeError, RecursionError) as exc:
        raise registry.PolicyError('账本绑定格式无效，请所有者核对账户来源') from exc
    account_id = binding['account_id']
    if not conn.execute('SELECT 1 FROM account_books WHERE account_id=?', (account_id,)).fetchone():
        raise registry.PolicyError('绑定账户的账本期初不存在，请所有者核对账户来源与期初')
    return account_id


def verified_current(conn: sqlite3.Connection, directory: Path, *, verify_evidence: bool = True) -> dict:
    """Read codes and provenance within the caller's snapshot of the shared database.

    Economic positions stay readable while a new cycle awaits v0 context or its analysis
    branch; policy_gaps names what is pending.
    """
    source, policy_gaps = monitor._source(conn, directory, verify_evidence=verify_evidence,
                                          require_v0_projection=False)
    if source['mode'] == 'mirror':
        codes = [row[0] for row in conn.execute('SELECT code FROM holdings WHERE exit_date IS NULL')]
    else:
        row = conn.execute('SELECT projection FROM account_books WHERE account_id=?',
                           (source['watermark']['account_id'],)).fetchone()
        codes = [code for code, shares in json.loads(row[0])['positions'].items() if shares]
    try:
        codes = sorted({normalize_key(code) for code in codes})
    except (ValueError, TypeError):
        raise registry.PolicyError('在仓代码不能转换为研究公司代码；未返回空账户，请核对来源', 409) from None
    return dict(codes=codes, source=source, policy_gaps=policy_gaps)


def current_holding_codes(conn: sqlite3.Connection, *, allow_unconfigured: bool = False) -> list[str]:
    """Read within the caller's snapshot. Scans can start before an account source is configured."""
    if allow_unconfigured and not conn.execute('SELECT 1 FROM account_source WHERE id=1').fetchone():
        return []
    return verified_current(conn, mirror.target_directory(conn))['codes']


def current(conn: sqlite3.Connection, principal, database: Path) -> dict:
    """Return codes and provenance in one read transaction; never guess an account."""
    with db.transaction(conn, immediate=False):
        registry._authorize(conn, principal)
        return verified_current(conn, database.resolve().parent)
