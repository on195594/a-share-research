"""Host-supplied read harness executed with the rollback image's own Python modules.

No migrations, environment credentials, network, or production mounts are needed.
This is a read-compatibility gate, not a rehearsal of the old image's write paths.
"""
from contextlib import closing
import importlib
import json
from pathlib import Path
import sqlite3
import sys


def probe(directory: Path) -> dict:
    """Inspect an isolated release bundle; report categories without financial rows."""
    from a_share_research import integrity

    bundle = directory / 'bundle'
    problems, _ = integrity.check(bundle / 'app.db', bundle / 'pdf')
    if problems:
        return {'ok': False, 'reason': 'image_integrity_failed'}
    with closing(sqlite3.connect((bundle / 'app.db').as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        conn.execute('BEGIN')
        if (conn.execute('PRAGMA integrity_check').fetchall()[0][0] != 'ok'
                or conn.execute('PRAGMA foreign_key_check').fetchone()):
            return {'ok': False, 'reason': 'sqlite_integrity_failed'}
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'scan_requests' in tables:
            from a_share_research import db, scans
            # Readable additive tables are insufficient: old tools reject the new
            # schema, and old web cannot recover a request after a lost response.
            if (conn.execute('PRAGMA user_version').fetchone()[0] != len(db.MIGRATIONS)
                    or not callable(getattr(scans, 'submit_request', None))):
                return {'ok': False, 'reason': 'scan_requests_unsupported'}
        modern = any(conn.execute(f'SELECT 1 FROM {table} LIMIT 1').fetchone() for table in (
            'account_books', 'account_source', 'account_analysis_history', 'policy_v0_profiles',
            'policy_versions', 'policy_evaluations', 'policy_activations'
        ) if table in tables)
        if modern:
            # Older integrity commands can silently ignore tables added by a new application.
            for module in ('account.audit', 'account.position_store', 'account.projection_ids',
                           'account.analysis', 'policy.v0.account_store', 'policy.registry'):
                inspector = importlib.import_module('a_share_research.' + module).inspect
                if inspector(conn):
                    return {'ok': False, 'reason': 'account_policy_integrity_failed'}
        if 'policy_evaluations' in tables and 'capture_context' in {
                row[1] for row in conn.execute('PRAGMA table_info(policy_evaluations)')}:
            from a_share_research.policy import registry
            # A pre-capture inspector may exist but ignore new trailing columns entirely.
            for context, activation, bundle_json in conn.execute(
                    'SELECT capture_context,activation_id,input FROM policy_evaluations WHERE capture_context IS NOT NULL'):
                registry._capture_mode(json.loads(context), activation, json.loads(bundle_json))
        mode = None
        if 'account_source' in tables:
            sources = conn.execute('SELECT mode,source_sha256,updated_at,watermark FROM account_source').fetchall()
            if len(sources) > 1:
                return {'ok': False, 'reason': 'account_source_invalid'}
            if sources:
                row = sources[0]
                mode = row[0]
                if mode == 'ledger':
                    from a_share_research.policy import account_input
                    account_input.read(conn, bundle, row)
                elif mode == 'mirror':
                    from a_share_research.account import mirror
                    if mirror.digest(mirror._read(conn, directory / 'side')) != row[1]:
                        return {'ok': False, 'reason': 'mirror_digest_mismatch'}
                else:
                    return {'ok': False, 'reason': 'account_source_invalid'}
    return {'ok': True, 'reason': 'read_compatible', 'account_state': modern, 'source_mode': mode}


def main():
    try:
        result = probe(Path(sys.argv[1]))
    except Exception:
        # An old parser, missing module or unreadable evidence must fail closed without disclosure.
        result = {'ok': False, 'reason': 'image_reader_failed'}
    sys.stdout.write(json.dumps(result) + '\n')
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
