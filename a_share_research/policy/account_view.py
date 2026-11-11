"""Read active company codes from the verified source selected for policy capture."""
import json
from pathlib import Path
import sqlite3

from a_share_research import db
from a_share_research.policy import monitor, registry
from a_share_research.stock_codes import normalize_key


def current(conn: sqlite3.Connection, principal, database: Path) -> dict:
    """Return codes and provenance in one read transaction; never guess an account."""
    with db.transaction(conn, immediate=False):
        registry._authorize(conn, principal)
        source = monitor._source(conn, database.resolve().parent)
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
        return dict(codes=codes, source=source)
