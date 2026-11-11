"""Read-only readiness checks for adopting a mirrored account opening."""
import hashlib
import sqlite3
from pathlib import Path

from a_share_research import gate
from a_share_research.account import analysis, analysis_rebase, cycle_analysis, mirror
from a_share_research.policy import account_input


class CutoverError(ValueError):
    pass


def validate(conn, directory: Path, account_id: str, account_scope: str,
             mirror_row: tuple[str, str, str, str]) -> dict:
    """Validate within the caller's transaction; never migrate or mutate sources."""
    if not conn.in_transaction:
        raise CutoverError('切换校验必须在调用方事务内执行')
    try:
        return _validate(conn, directory, account_id, account_scope, mirror_row)
    except CutoverError:
        raise
    except (ValueError, TypeError, KeyError, IndexError, ArithmeticError,
            sqlite3.Error, OSError, RecursionError):
        raise CutoverError('切换所需账本、分析或证据校验失败') from None


def _validate(conn, directory, account_id, account_scope, row):
    if len(row) != 4 or row[0] != 'mirror':
        raise CutoverError('切换需要当前镜像来源')
    content = mirror._read(conn, directory)
    if mirror.digest(content) != row[1]:
        raise CutoverError('镜像内容与来源哈希不一致')
    watermark = account_input._json(row[3])
    expected = dict(rows={t: len(rows) for t, rows in content['tables'].items()},
                    sequences=content['sequences'],
                    files={name: data and hashlib.sha256(data).hexdigest()
                           for name, data in content['files'].items()})
    if gate.canonical(watermark) != gate.canonical(expected):
        raise CutoverError('镜像水位与当前内容不一致')
    books = {}
    for table in ('account_books', 'account_position_books'):
        found = account_input._rows(conn, f'SELECT * FROM main.{table} WHERE account_id=?', (account_id,))
        if len(found) != 1 or found[0]['version'] != 0:
            raise CutoverError('切换仅允许完整的零版本账户与成本期初')
        books[table] = found[0]
    if conn.execute('SELECT 1 FROM main.account_journal WHERE account_id=? LIMIT 1', (account_id,)).fetchone():
        raise CutoverError('已存在金融入账或作废历史，不能作为正式期初切换')
    openings = account_input._rows(conn, 'SELECT * FROM main.policy_v0_account_openings WHERE account_id=?', (account_id,))
    snapshot = {table: [dict(zip(mirror.MANIFEST[table], values)) for values in content['tables'][table]]
                for table in ('holdings', 'holding_events')}
    if (len(openings) != 1 or openings[0]['source_hash'] != gate.digest(snapshot)
            or gate.canonical(account_input._json(openings[0]['snapshot'])) != gate.canonical(snapshot)):
        raise CutoverError('政策期初与当前镜像持仓及完整事件历史不一致')
    cost = account_input._json(books['account_position_books']['projection'])
    for cycle in cost['cycles']:
        if cycle['exit_date'] is not None:
            continue
        legacy_id = cycle['legacy_holding_id']
        hid = cycle_analysis.mapped_id(conn, account_id, cycle['cycle_id'])
        baseline = cycle_analysis._baseline(conn, account_id, hid)
        latest = cycle_analysis._row(conn, "SELECT id FROM main.account_analysis_history WHERE holding_id=? AND account_id='' ORDER BY version DESC LIMIT 1", (legacy_id,))
        if (legacy_id is None or baseline is None or latest is None
                or account_input._json(baseline['request'])['initialize']['legacy_revision_id'] != latest['id']):
            raise CutoverError('账户周期分析未绑定当前旧分析版本')
        if analysis_rebase.baseline(conn, legacy_id) != analysis._capture(conn, legacy_id):
            raise CutoverError('旧分析基线与当前镜像不一致，须先协调')
    binding = dict(schema_version=1, account_id=account_id, account_scope=account_scope,
                   mirror_watermark=watermark)
    return account_input.read(conn, directory, ('ledger', row[1], row[2], gate.canonical(binding)))
