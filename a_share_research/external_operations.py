"""Durable, owner-bound evidence work; network and PDF work runs outside transactions."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from a_share_research import api_keys, db, gate, stock_codes
from a_share_research.external_research import SubmissionError, _authorize, _owned
from a_share_research.limits import ACQUISITION_SECONDS

log = logging.getLogger(__name__)


def migrate(conn):
    """Rebuild with enforcement off (db.connect migration convention), preserving old identities."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='external_materials'").fetchone():
        expected = {'external_materials': {'external_id','revision','parent_revision','operation_id','material'},
                    'external_operations': {'parameters_sha256','parameters','base_revision','deadline'},
                    'external_research': {'material_revision'}, 'external_submissions': {'material_revision'}}
        if all(columns <= {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
               for table, columns in expected.items()):
            return
        raise ValueError('incomplete external acquisition schema')
    conn.execute('DROP TRIGGER IF EXISTS external_material_immutable')
    for event in ('update', 'delete'):
        conn.execute(f'DROP TRIGGER IF EXISTS external_submissions_no_{event}')
    conn.execute("""CREATE TABLE external_research_new (
        id INTEGER PRIMARY KEY, owner_key_id INTEGER NOT NULL, request_id TEXT NOT NULL,
        company_key TEXT NOT NULL, source_run_id INTEGER REFERENCES runs(id),
        account_aware INTEGER NOT NULL CHECK(account_aware IN (0,1)),
        status TEXT NOT NULL CHECK(status IN ('collecting','submitted')),
        version INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
        material_revision INTEGER NOT NULL DEFAULT 0 CHECK(material_revision>=0),
        material_sha256 TEXT NOT NULL, material TEXT NOT NULL,
        UNIQUE(owner_key_id,request_id))""")
    columns = 'id,owner_key_id,request_id,company_key,source_run_id,account_aware,status,version,created_at,material_sha256,material'
    conn.execute(f'INSERT INTO external_research_new({columns}) SELECT {columns} FROM external_research')
    conn.execute('DROP TABLE external_research')
    conn.execute('ALTER TABLE external_research_new RENAME TO external_research')
    conn.execute('CREATE INDEX external_research_by_company ON external_research(company_key,id)')
    conn.execute("""CREATE TABLE external_operations (
        id INTEGER PRIMARY KEY, owner_key_id INTEGER NOT NULL, request_id TEXT NOT NULL,
        company_key TEXT NOT NULL, account_aware INTEGER NOT NULL CHECK(account_aware IN (0,1)),
        action TEXT NOT NULL CHECK(action IN ('open','acquire')),
        external_id INTEGER REFERENCES external_research(id), base_revision INTEGER,
        status TEXT NOT NULL CHECK(status IN ('queued','running','done','failed')),
        created_at TEXT NOT NULL, started_at TEXT, deadline TEXT, finished_at TEXT,
        material_revision INTEGER, parameters_sha256 TEXT NOT NULL,
        parameters TEXT NOT NULL, result TEXT, error TEXT,
        UNIQUE(owner_key_id,request_id))""")
    conn.execute('CREATE INDEX external_operations_by_status ON external_operations(status,id)')
    conn.execute("CREATE UNIQUE INDEX external_operations_serial ON external_operations(external_id)"
                 " WHERE status IN ('queued','running') AND external_id IS NOT NULL")
    conn.execute("""CREATE TABLE external_materials (
        external_id INTEGER NOT NULL REFERENCES external_research(id), revision INTEGER NOT NULL CHECK(revision>=0),
        parent_revision INTEGER, operation_id INTEGER UNIQUE REFERENCES external_operations(id),
        created_at TEXT NOT NULL, material_sha256 TEXT NOT NULL, material TEXT NOT NULL,
        PRIMARY KEY(external_id,revision),
        FOREIGN KEY(external_id,parent_revision) REFERENCES external_materials(external_id,revision))""")
    conn.execute('INSERT INTO external_materials(external_id,revision,created_at,material_sha256,material)'
                 ' SELECT id,0,created_at,material_sha256,material FROM external_research')
    conn.execute("""CREATE TABLE external_submissions_new (
        id INTEGER PRIMARY KEY, external_id INTEGER NOT NULL REFERENCES external_research(id),
        version INTEGER NOT NULL, created_at TEXT NOT NULL,
        accepted INTEGER NOT NULL CHECK(accepted IN (0,1)), material_revision INTEGER NOT NULL DEFAULT 0,
        draft_sha256 TEXT NOT NULL, material_sha256 TEXT NOT NULL, draft TEXT NOT NULL, problems TEXT NOT NULL,
        UNIQUE(external_id,version), UNIQUE(external_id,draft_sha256,material_sha256),
        FOREIGN KEY(external_id,material_revision) REFERENCES external_materials(external_id,revision))""")
    columns = 'id,external_id,version,created_at,accepted,draft_sha256,material_sha256,draft,problems'
    conn.execute(f'INSERT INTO external_submissions_new({columns}) SELECT {columns} FROM external_submissions')
    conn.execute('DROP TABLE external_submissions')
    conn.execute('ALTER TABLE external_submissions_new RENAME TO external_submissions')
    for table in ('external_submissions', 'external_materials'):
        for event in ('UPDATE', 'DELETE'):
            conn.execute(f'CREATE TRIGGER {table}_no_{event.lower()} BEFORE {event} ON {table}'
                         " BEGIN SELECT RAISE(ABORT, 'submission material is immutable'); END")
    conn.execute("""CREATE TRIGGER external_material_immutable
        BEFORE UPDATE OF owner_key_id,request_id,company_key,source_run_id,account_aware,material_sha256,material
        ON external_research BEGIN SELECT RAISE(ABORT, 'submission identity is immutable'); END""")
    conn.execute("""CREATE TRIGGER external_operation_identity_immutable
        BEFORE UPDATE OF owner_key_id,request_id,company_key,account_aware,action,base_revision,parameters_sha256,parameters
        ON external_operations BEGIN SELECT RAISE(ABORT, 'operation identity is immutable'); END""")
    conn.execute("""CREATE TRIGGER external_operation_terminal BEFORE UPDATE ON external_operations
        WHEN OLD.status IN ('done','failed') BEGIN SELECT RAISE(ABORT, 'operation is terminal'); END""")
    conn.execute("""CREATE TRIGGER external_operation_no_delete BEFORE DELETE ON external_operations
        BEGIN SELECT RAISE(ABORT, 'operation is durable'); END""")


def request_key(request_id):
    if not isinstance(request_id, str) or not 1 <= len(request_id.strip()) <= 128:
        raise SubmissionError('request_id 须为1–128字符', 422)


def busy(conn, external_id):
    return conn.execute("SELECT id FROM external_operations WHERE external_id=? AND status IN ('queued','running')",
                        (external_id,)).fetchone()


def revision(conn, external_id, number):
    row = conn.execute('SELECT * FROM external_materials WHERE external_id=? AND revision=?',
                       (external_id, number)).fetchone()
    if row is None:
        raise SubmissionError('材料版本不存在', 404)
    return row


def operation(conn, principal, operation_id):
    _authorize(conn, principal)
    row = conn.execute('SELECT * FROM external_operations WHERE id=? AND owner_key_id=?',
                       (operation_id, principal.key_id)).fetchone()
    if row is None:
        raise SubmissionError('取证操作不存在或不属于此凭据', 404)
    saved = json.loads(row['result']) if row['result'] else None
    return {key: row[key] for key in ('id','request_id','action','company_key','external_id','base_revision',
            'status','created_at','started_at','deadline','finished_at','material_revision','error')} | {
            'account_aware': bool(row['account_aware']), 'result': saved if row['status'] == 'done' else None,
            'error_detail': saved.get('error_detail') if row['status'] == 'failed' and saved else None}


def _enqueue(conn, principal, request_id, parameters, *, external_id=None, base_revision=None):
    request_key(request_id)
    with db.transaction(conn):
        _authorize(conn, principal)
        previous = conn.execute('SELECT id,parameters_sha256 FROM external_operations WHERE owner_key_id=? AND request_id=?',
                                (principal.key_id, request_id)).fetchone()
        sha = gate.digest(parameters)
        if previous:
            if previous['parameters_sha256'] != sha:
                raise SubmissionError('请求编号已绑定其他操作或参数')
            return operation(conn, principal, previous['id'])
        if conn.execute('SELECT 1 FROM external_research WHERE owner_key_id=? AND request_id=?',
                        (principal.key_id, request_id)).fetchone():
            raise SubmissionError('请求编号已绑定材料复用操作')
        if external_id is not None:
            source = _owned(conn, principal, external_id)
            if source['material_revision'] != base_revision or busy(conn, external_id):
                raise SubmissionError('材料版本冲突或存在未完成取证操作')
        operation_id = conn.execute(
            "INSERT INTO external_operations(owner_key_id,request_id,company_key,account_aware,action,external_id,"
            "base_revision,status,created_at,parameters_sha256,parameters) VALUES (?,?,?,?,?,?,?,'queued',?,?,?)",
            (principal.key_id, request_id, parameters['code'], int(principal.role == 'assistant'), parameters['action'],
             external_id, base_revision, db.now(), sha, gate.canonical(parameters))).lastrowid
        return operation(conn, principal, operation_id)


def open_research(conn, principal, request_id, code):
    if not isinstance(code, str) or stock_codes.KEY_RE.fullmatch(code) is None:
        raise SubmissionError('公司代码必须为六位数字', 422)
    return _enqueue(conn, principal, request_id, {'action': 'open', 'code': code})


def acquire(conn, principal, request_id, external_id, base_revision, tool, arguments):
    from a_share_research.external_acquisition import validate_call
    row = _owned(conn, principal, external_id)
    if type(base_revision) is not int or base_revision < 0:
        raise SubmissionError('材料版本须为非负整数', 422)
    try:
        validate_call(tool, arguments)
        if len(gate.canonical(arguments).encode()) > 65536:
            raise ValueError('工具参数超过64KiB')
    except (ValueError, TypeError) as exc:
        raise SubmissionError(str(exc), 422) from None
    except RecursionError:
        raise SubmissionError('工具参数嵌套过深', 422) from None
    return _enqueue(conn, principal, request_id, {'action': 'acquire', 'code': row['company_key'],
                    'external_id': external_id, 'base_revision': base_revision, 'tool': tool, 'arguments': arguments},
                    external_id=external_id, base_revision=base_revision)


def recover(conn):
    """An interrupted attempt is terminal; a new explicit request is required to spend again."""
    with db.transaction(conn):
        conn.execute("UPDATE external_operations SET status='failed',finished_at=?,error='interrupted' WHERE status='running'",
                     (db.now(),))


def execute(conn, operation_id):
    from a_share_research import acquisition_process
    from a_share_research.material import Material

    with db.transaction(conn):
        row = conn.execute("SELECT * FROM external_operations WHERE id=? AND status='queued'", (operation_id,)).fetchone()
        if row is None:
            return
        started = db.now()
        deadline = (datetime.fromisoformat(started) + timedelta(seconds=ACQUISITION_SECONDS)).isoformat()
        conn.execute("UPDATE external_operations SET status='running',started_at=?,deadline=? WHERE id=?",
                     (started, deadline, operation_id))
    try:
        principal = api_keys.Principal(row['owner_key_id'], 'assistant' if row['account_aware'] else 'research')
        _authorize(conn, principal)
        parameters = json.loads(row['parameters'])
        if gate.digest(parameters) != row['parameters_sha256']:
            raise ValueError('operation parameters hash mismatch')
        if row['action'] == 'open':
            request = {'action': 'open', 'code': row['company_key']}
        else:
            parent = revision(conn, row['external_id'], row['base_revision'])
            request = {'action': 'acquire', 'material': json.loads(parent['material']),
                       'tool': parameters['tool'], 'arguments': parameters['arguments']}
        remaining = (datetime.fromisoformat(deadline) - datetime.fromisoformat(db.now())).total_seconds()
        result = acquisition_process.run(request, timeout=remaining)
        snapshot = result['material']
        material = Material.from_snapshot(snapshot)
        if stock_codes.normalize_key(material.code) != row['company_key'] or material.doc.code != material.code:
            raise ValueError('material company mismatch')
        # Material's existing hash includes insertion order; canonical request hashing must not reorder it.
        text = json.dumps(snapshot, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        with db.transaction(conn):
            current = conn.execute('SELECT status FROM external_operations WHERE id=?', (operation_id,)).fetchone()
            if current['status'] != 'running':
                return
            _authorize(conn, principal)
            if datetime.fromisoformat(db.now()) >= datetime.fromisoformat(deadline):
                raise acquisition_process.AcquisitionProcessError('timeout')
            if row['action'] == 'open':
                external_id = conn.execute(
                    "INSERT INTO external_research(owner_key_id,request_id,company_key,account_aware,status,created_at,"
                    "material_sha256,material) VALUES (?,?,?,?,'collecting',?,?,?)",
                    (row['owner_key_id'], row['request_id'], row['company_key'], row['account_aware'],
                     db.now(), snapshot['sha256'], text)).lastrowid
                number, parent_number = 0, None
            else:
                source = _owned(conn, principal, row['external_id'])
                if source['material_revision'] != row['base_revision']:
                    raise SubmissionError('材料版本冲突')
                external_id, parent_number = row['external_id'], row['base_revision']
                number = parent_number + 1
            conn.execute('INSERT INTO external_materials(external_id,revision,parent_revision,operation_id,created_at,'
                         'material_sha256,material) VALUES (?,?,?,?,?,?,?)',
                         (external_id, number, parent_number, operation_id, db.now(), snapshot['sha256'], text))
            conn.execute("UPDATE external_research SET material_revision=?,status='collecting' WHERE id=?", (number, external_id))
            conn.execute("UPDATE external_operations SET status='done',external_id=?,material_revision=?,finished_at=?,result=? WHERE id=?",
                         (external_id, number, db.now(), gate.canonical(result['result']), operation_id))
    except Exception as exc:
        # Persist safe categories; tool/provider exceptions may contain credentials or full response bodies.
        error = (exc.code if isinstance(exc, acquisition_process.AcquisitionProcessError)
                 else 'revoked' if isinstance(exc, SubmissionError) and exc.status == 403 else 'acquisition_failed')
        detail = getattr(exc, 'detail', None) if isinstance(exc, acquisition_process.AcquisitionProcessError) else None
        with db.transaction(conn):
            conn.execute("UPDATE external_operations SET status='failed',finished_at=?,error=?,result=? WHERE id=? AND status='running'",
                         (db.now(), error, gate.canonical({'error_detail': detail}) if detail else None, operation_id))
        log.warning('external operation %s failed: %s', operation_id, error)


def inspect(conn):
    """Read all revision/operation links without requiring historical keys to remain active."""
    from a_share_research.external_acquisition import validate_extension
    from a_share_research.material import Material, ResearchError

    problems = []
    sessions = {r['id']: r for r in conn.execute('SELECT * FROM external_research')}
    operations = {r['id']: r for r in conn.execute('SELECT * FROM external_operations')}
    revisions = {(r['external_id'], r['revision']): r for r in conn.execute('SELECT * FROM external_materials')}
    parsed = {}
    for key, row in revisions.items():
        try:
            snapshot = json.loads(row['material'])
            material = Material.from_snapshot(snapshot)
            parent = sessions[row['external_id']]
            if (snapshot['sha256'] != row['material_sha256'] or stock_codes.normalize_key(material.code) != parent['company_key']
                    or material.doc.code != material.code):
                raise ValueError('material identity mismatch')
            parsed[key] = material
            if row['revision'] == 0:
                if (row['parent_revision'] is not None or row['material'] != parent['material']
                        or row['material_sha256'] != parent['material_sha256']):
                    raise ValueError('seed mismatch')
                if parent['source_run_id'] is None and row['operation_id'] is None:
                    raise ValueError('missing open operation')
            elif row['parent_revision'] != row['revision'] - 1 or row['operation_id'] is None:
                raise ValueError('revision ancestry mismatch')
            if row['operation_id'] is not None:
                op = operations[row['operation_id']]
                if (op['status'] != 'done' or op['external_id'] != row['external_id']
                        or op['material_revision'] != row['revision']):
                    raise ValueError('revision operation mismatch')
        except (ValueError, TypeError, KeyError, ResearchError):
            problems.append(f'外部材料 {key}：身份、哈希或版本关联不一致')
    for key, material in parsed.items():
        if key[1] > 0:
            try:
                validate_extension(parsed[(key[0], key[1] - 1)], material)
            except (ValueError, KeyError, TypeError):
                problems.append(f'外部材料 {key}：既有证据被改写或父版本缺失')
    for eid, row in sessions.items():
        numbers = [number for external_id, number in revisions if external_id == eid]
        if not numbers or row['material_revision'] != max(numbers):
            problems.append(f'外部研究 {eid}：最新材料版本关联不一致')
    for oid, row in operations.items():
        try:
            parameters = json.loads(row['parameters'])
            if (gate.digest(parameters) != row['parameters_sha256'] or parameters['action'] != row['action']
                    or parameters['code'] != row['company_key']):
                raise ValueError('parameters mismatch')
            if row['action'] == 'acquire':
                parent = sessions[row['external_id']]
                if (parameters['external_id'] != row['external_id'] or parameters['base_revision'] != row['base_revision']
                        or (row['external_id'], row['base_revision']) not in revisions
                        or parent['owner_key_id'] != row['owner_key_id'] or parent['account_aware'] != row['account_aware']
                        or parent['company_key'] != row['company_key']):
                    raise ValueError('acquire identity mismatch')
            if row['status'] == 'queued':
                if any(row[k] is not None for k in ('started_at','deadline','finished_at','result','error','material_revision')):
                    raise ValueError('queued state mismatch')
            else:
                start, deadline = datetime.fromisoformat(row['started_at']), datetime.fromisoformat(row['deadline'])
                if deadline <= start:
                    raise ValueError('deadline mismatch')
                if row['status'] == 'running' and any(row[k] is not None for k in ('finished_at','result','error','material_revision')):
                    raise ValueError('running state mismatch')
                if row['status'] in ('done','failed') and row['finished_at'] is None:
                    raise ValueError('terminal time missing')
                if row['status'] == 'failed':
                    if row['error'] is None or row['material_revision'] is not None:
                        raise ValueError('failed result mismatch')
                    if row['result'] is not None:
                        detail = json.loads(row['result'])
                        if set(detail) != {'error_detail'} or not isinstance(detail['error_detail'], str):
                            raise ValueError('failed error detail mismatch')
                if row['status'] == 'done':
                    linked = revisions[(row['external_id'], row['material_revision'])]
                    parent = sessions[row['external_id']]
                    json.loads(row['result'])
                    if (linked['operation_id'] != oid or row['error'] is not None
                            or parent['owner_key_id'] != row['owner_key_id'] or parent['account_aware'] != row['account_aware']
                            or parent['company_key'] != row['company_key']
                            or (row['action'] == 'open' and (row['material_revision'] != 0 or parent['source_run_id'] is not None))
                            or (row['action'] == 'acquire' and row['material_revision'] != row['base_revision'] + 1)):
                        raise ValueError('done identity mismatch')
        except (ValueError, TypeError, KeyError):
            problems.append(f'外部取证 {oid}：参数、状态或版本关联不一致')
    return problems
