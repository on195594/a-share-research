"""Read committed file references without migrating or changing the source database."""
import json
import re
import sqlite3


def _hash(value, label):
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{64}', value) is None:
        raise ValueError(f'{label}: invalid SHA256 reference')
    return value


def _non_json_constant(value):
    raise ValueError('non-JSON numeric constant')


def _json(value, label):
    try:
        return json.loads(value, parse_constant=_non_json_constant)
    except (ValueError, TypeError):
        raise ValueError(f'{label}: invalid committed JSON') from None


def _object(value, label):
    if not isinstance(value, dict):
        raise ValueError(f'{label}: expected JSON object')
    return value


def _hashes(value, label):
    if not isinstance(value, list):
        raise ValueError(f'{label}: expected evidence hash list')
    return {_hash(item, label) for item in value}


def _documents(body, label):
    """Only document identities are PDF references, never material/report hashes."""
    result = set()

    def document(value):
        if value is None:
            return  # source_registry accepts an explicitly unrecorded document identity.
        value = _object(value, label)
        sha = value.get('sha256', '')
        if sha != '':
            result.add(_hash(sha, label))

    if 'document' in body:
        document(body['document'])
    sources = body.get('sources')
    sources = {} if sources is None else _object(sources, label)
    for source in sources.values():
        source = _object(source, label)
        document(source.get('document'))
    return result


def _material(value, label):
    snapshot = _object(_json(value, label), label)
    data = _object(snapshot.get('data'), label)
    if 'doc' not in data:
        raise ValueError(f'{label}: material missing document')
    extra = _object(data.get('extra', {}), label)
    return _documents({'document': data['doc'], 'sources': extra.get('sources', {})}, label)


def required(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Return sorted, deduplicated hashes from all committed evidence and PDF references.

    Missing tables are valid for older schemas. Failed batch requests are deliberately
    excluded: they never committed the evidence they may have unsuccessfully referenced.
    Both sqlite3.Row and ordinary tuple row factories are supported.
    """
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    account, pdf = set(), set()
    for table, column, is_list in (
        ('account_books', 'opening_evidence', False),
        ('account_observations', 'evidence', False),
        ('account_position_books', 'evidence_hashes', True),
        ('account_analysis_history', 'evidence_hashes', True),
        ('policy_v0_account_openings', 'evidence_hashes', True),
    ):
        if table in tables:
            label = f'{table}.{column}'
            for (value,) in conn.execute(f'SELECT {column} FROM {table}'):
                account.update(_hashes(_json(value, label), label) if is_list else {_hash(value, label)})
    if 'account_batches' in tables:
        label = 'account_batches.request'
        for (value,) in conn.execute("SELECT request FROM account_batches WHERE status='posted'"):
            request = _object(_json(value, label), label)
            account.update(_hashes(request.get('evidence_hashes'), label))
    for table in ('runs', 'external_research'):
        if table in tables:
            for (value,) in conn.execute(f'SELECT material FROM {table} WHERE material IS NOT NULL'):
                pdf.update(_material(value, f'{table}.material'))
    if 'reports' in tables:
        for (value,) in conn.execute('SELECT body FROM reports'):
            label = 'reports.body'
            pdf.update(_documents(_object(_json(value, label), label), label))
    return {'account_evidence': sorted(account), 'pdf': sorted(pdf)}
