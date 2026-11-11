"""Add account/cycle scope without rewriting legacy analysis history or its hashes."""
import sqlite3

_LEGACY_COLUMNS = ('id','holding_id','company_key','version','parent_id','request_hash','source','actor_key_id',
                   'created_at','snapshot_hash','evidence_hashes','request','snapshot')
_SCOPED_COLUMNS = (*_LEGACY_COLUMNS,'account_id','cycle_id')
_TEMP = 'account_analysis_history_scope_migration'


def migrate(conn: sqlite3.Connection) -> None:
    """Rebuild immutable history in the caller's migration transaction, retaining self-parent IDs.

    The replacement table references itself while copying. Deferral permits dropping the
    old self-referencing table; ALTER TABLE then renames the new self-reference atomically.
    Foreign-key deferral lasts until the caller commits or rolls back this transaction.
    """
    if not conn.in_transaction:
        raise ValueError('分析账户作用域迁移须在事务内执行')
    columns = tuple(row[1] for row in conn.execute('PRAGMA table_info(account_analysis_history)'))
    if columns == _SCOPED_COLUMNS:
        return
    if columns != _LEGACY_COLUMNS:
        raise ValueError('分析历史结构不符合迁移输入，不能丢弃或猜测额外列')
    schema = [row[0] for row in conn.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name='account_analysis_history'"
        " AND type IN ('index','trigger') AND sql IS NOT NULL ORDER BY type,name")]
    conn.execute('PRAGMA defer_foreign_keys=ON')
    conn.execute(f'''CREATE TABLE {_TEMP} (
        id INTEGER PRIMARY KEY, holding_id INTEGER NOT NULL, company_key TEXT,
        version INTEGER NOT NULL, parent_id INTEGER REFERENCES {_TEMP}(id),
        request_hash TEXT NOT NULL, source TEXT NOT NULL, actor_key_id INTEGER,
        created_at TEXT NOT NULL, snapshot_hash TEXT NOT NULL, evidence_hashes TEXT NOT NULL,
        request TEXT NOT NULL, snapshot TEXT NOT NULL,
        account_id TEXT NOT NULL DEFAULT '', cycle_id TEXT NOT NULL DEFAULT '',
        CHECK((account_id='' AND cycle_id='') OR (account_id<>'' AND cycle_id<>'')),
        UNIQUE(account_id,holding_id,version), UNIQUE(account_id,holding_id,request_hash))''')
    names = ','.join(_LEGACY_COLUMNS)
    conn.execute(f'INSERT INTO {_TEMP} ({names}) SELECT {names} FROM account_analysis_history')
    if conn.execute(f'PRAGMA foreign_key_check({_TEMP})').fetchone():
        raise ValueError('分析历史父链外键不完整，迁移已停止')
    conn.execute('DROP TABLE account_analysis_history')
    conn.execute(f'ALTER TABLE {_TEMP} RENAME TO account_analysis_history')
    for statement in schema:
        conn.execute(statement)
    conn.execute("CREATE UNIQUE INDEX account_analysis_cycle_version ON account_analysis_history(account_id,cycle_id,version) WHERE account_id<>''")
    conn.execute("CREATE UNIQUE INDEX account_analysis_cycle_request ON account_analysis_history(account_id,cycle_id,request_hash) WHERE account_id<>''")
