"""Immutable policy identities, owner-local activation and evidenced snapshot replays."""
import argparse
import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from a_share_research import api_keys, db, gate
from a_share_research.policy import identity


class PolicyError(ValueError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


def _authorize(conn, principal, *, owner=False):
    row = conn.execute('SELECT role FROM api_keys WHERE id=?',(principal.key_id,)).fetchone()
    if not row or row[0] != principal.role or principal.role not in ({'owner'} if owner else {'assistant','owner'}):
        raise PolicyError('需要有效的 owner 凭据' if owner else '此凭据不能读取政策与账户评估',403)


def _text(value, name, limit=128):
    if not isinstance(value,str) or not value.strip() or len(value)>limit or not value.isprintable():
        raise PolicyError(f'{name} 须为非空文本且不超过 {limit} 字符',422)
    return value


def _version(conn, selected):
    row = conn.execute('SELECT manifest FROM policy_versions WHERE identity=?',(selected,)).fetchone()
    if row is None:
        raise PolicyError('政策身份未登记；不能将旧投影 profile 当作完整政策',404)
    try:
        manifest = json.loads(row[0])
        identity.verify(manifest)
        if manifest['identity'] != selected:
            raise identity.IdentityError('policy key differs from manifest')
    except (ValueError,KeyError,TypeError) as exc:
        raise PolicyError(f'政策身份记录损坏：{exc}') from None
    return manifest


def register(conn, principal, parameters):
    """Owner-only local preparation; registration never switches a policy pointer."""
    from a_share_research.policy.evaluation import validate_parameters
    _authorize(conn,principal,owner=True)
    validate_parameters(parameters)
    manifest = identity.build(parameters)
    with db.transaction(conn):
        _authorize(conn,principal,owner=True)
        conn.execute('INSERT OR IGNORE INTO policy_versions VALUES (?,?,?,?,?)',
                     (manifest['identity'],manifest['policy'],db.now(),principal.key_id,gate.canonical(manifest)))
        if _version(conn,manifest['identity']) != manifest:
            raise PolicyError('政策身份已存在但内容不同')
    return manifest


def _current(conn):
    cursor = conn.execute('SELECT * FROM policy_activations ORDER BY id DESC LIMIT 1')
    row = cursor.fetchone()
    return dict(zip([c[0] for c in cursor.description],row)) if row else None


def _read_state(conn):
    """Read in an already authorized caller's transaction; never infer a legacy fallback."""
    activation = _current(conn)
    if activation is None:
        return dict(activation=None,manifest=None,installed=False)
    manifest = _version(conn,activation['identity'])
    try:
        identity.check_installed(manifest)
        installed = True
    except identity.IdentityError:
        installed = False
    return dict(activation=activation,manifest=manifest,installed=installed)


def _state(conn, principal):
    """No implicit v0 fallback: a missing or legacy pointer stays visible as a gap."""
    with db.transaction(conn,immediate=False):
        _authorize(conn,principal)
        return _read_state(conn)


def current(conn, principal):
    state = _state(conn,principal)
    if state['manifest']:
        state['manifest'] = {k:v for k,v in state['manifest'].items() if k not in ('files','guides')}
    return state


def activate(conn, principal, selected, *, expected_id, request_id, reason):
    """Append a compare-and-swap pointer locally; this function has no HTTP tool."""
    request_id = _text(request_id,'request_id')
    reason = _text(reason,'reason',1000)
    if type(expected_id) is not int or expected_id < 0:
        raise PolicyError('expected_id 须为当前指针编号，首次为 0',422)
    with db.transaction(conn):
        _authorize(conn,principal,owner=True)
        actor = f'key:{principal.key_id}'
        cursor = conn.execute('SELECT * FROM policy_activations WHERE activated_by=? AND request_id=?',(actor,request_id))
        row = cursor.fetchone()
        if row:
            saved = dict(zip([c[0] for c in cursor.description],row))
            if (saved['identity'],saved['expected_id'],saved['reason']) != (selected,expected_id,reason):
                raise PolicyError('request_id 已用于不同政策切换')
            return saved
        manifest = _version(conn,selected)
        try:
            identity.check_installed(manifest)
        except identity.IdentityError as exc:
            raise PolicyError(f'当前安装与政策身份不一致，须先登记当前实现：{exc}') from None
        from a_share_research.policy.evaluation import validate_parameters
        parameters = manifest['parameters']
        validate_parameters(parameters)
        if parameters['source'] == 'not_applied_account_scope_required':
            raise PolicyError('尚未确认账户作用域的参数只能作历史重放，不能启用')
        stamp = db.now()
        if 'effective_from' in parameters:
            now = datetime.fromisoformat(stamp)
            if (datetime.fromisoformat(parameters['effective_from']) > now
                    or datetime.fromisoformat(parameters['confirmed_at']) > now
                    or parameters.get('effective_to') is not None and now > datetime.fromisoformat(parameters['effective_to'])):
                raise PolicyError('政策参数在切换时尚未生效、未确认或已经到期')
        old = _current(conn)
        if (old['id'] if old else 0) != expected_id:
            raise PolicyError('生效指针已变化，请重新读取后决定')
        conn.execute('INSERT INTO policy_activations (policy,activated_at,activated_by,identity,request_id,expected_id,reason)'
                     ' VALUES (?,?,?,?,?,?,?)',
                     (manifest['policy'],stamp,actor,selected,request_id,expected_id,reason))
        return _current(conn)


def guide(conn, principal, name):
    """Load the exact saved guide of the active identity; never read a caller-supplied path."""
    state = _state(conn,principal)
    if state['manifest'] is None:
        raise PolicyError('尚未启用政策；不能猜测指南版本',404)
    manifest = state['manifest']
    guides = manifest['guides']
    if name is None:
        return dict(identity=manifest['identity'],guide_version=manifest['guide_version'],paths=sorted(guides))
    if name not in guides:
        raise PolicyError('该政策没有此指南，请先发现可用路径',404)
    return dict(identity=manifest['identity'],guide_version=manifest['guide_version'],path=name,content=guides[name])


def _capture_mode(context, activation_id, bundle):
    if not isinstance(context, dict) or type(context.get('schema_version')) is not int:
        raise PolicyError('未知采集来源版本')
    version = context['schema_version']
    request = dict(action='policy.monitor', expected_activation_id=activation_id)
    mode = 'service_monitor_capture'
    if version == 3:
        from a_share_research.policy.stop_state import validate_state
        previous = validate_state(bundle.get('previous_state'))
        request = dict(action='policy.stop_alert', expected_activation_id=activation_id, previous_state=previous)
        mode = 'service_stop_alert_capture'
    elif version not in (1, 2):
        raise PolicyError('未知采集来源版本')
    if context.get('request') != request:
        raise PolicyError('采集来源或生效指针不一致')
    source = context.get('source', {})
    if not isinstance(source, dict):
        raise PolicyError('采集来源须为对象')
    if version == 1 or version == 3 and source.get('mode') == 'mirror':
        if source.get('mode') != 'mirror':
            raise PolicyError('旧采集来源须为镜像')
    else:
        from a_share_research.policy.account_input import validate_source
        validate_source(source)
    return mode


def _evaluation(row):
    result = dict(id=row[0],identity=row[1],activation_id=row[2],created_at=row[3],created_by=row[4],
                request_id=row[5],input_hash=row[6],result_hash=row[7],mode='supplied_input_replay',
                input=json.loads(row[8]),result=json.loads(row[9]))
    if len(row)>10 and row[10] is not None:
        context=json.loads(row[10])
        if gate.digest(context)!=row[11]:
            raise PolicyError('政策采集来源哈希不一致')
        try:
            mode = _capture_mode(context, row[2], result['input'])
        except (ValueError, TypeError, KeyError, AttributeError):
            raise PolicyError('政策采集来源或请求记录损坏') from None
        result.update(mode=mode,capture_context=context)
    return result


def replay(conn, principal, selected, request_id, bundle):
    """Save a program-generated replay with its actual identity and input; never label it a live account observation."""
    from a_share_research.policy.evaluation import evaluate, _json
    request_id = _text(request_id,'request_id')
    _authorize(conn,principal)
    manifest = _version(conn,selected)
    try:
        _json(bundle)
        input_hash = gate.digest(bundle)
    except (ValueError,TypeError):
        raise PolicyError('输入包须为可规范化的 JSON',422) from None
    with db.transaction(conn):
        _authorize(conn,principal)
        row = conn.execute('SELECT * FROM policy_evaluations WHERE created_by=? AND request_id=?',
                           (principal.key_id,request_id)).fetchone()
        if row:
            if row[1] != selected or row[6] != input_hash or len(row)>10 and row[10] is not None:
                raise PolicyError('request_id 已用于不同评估输入或政策')
            return _evaluation(row)
        try:
            identity.check_installed(manifest)
            result = evaluate(bundle,manifest['parameters'])
        except (identity.IdentityError,ValueError) as exc:
            raise PolicyError(f'政策评估拒绝：{exc}',422) from None
        activation = _current(conn)
        activation_id = activation['id'] if activation and activation['identity']==selected else None
        cursor = conn.execute('INSERT INTO policy_evaluations '
            '(identity,activation_id,created_at,created_by,request_id,input_hash,result_hash,input,result) '
            'VALUES (?,?,?,?,?,?,?,?,?)', (selected,activation_id,db.now(),principal.key_id,request_id,input_hash,
                gate.digest(result),gate.canonical(bundle),gate.canonical(result)))
        return _evaluation(conn.execute('SELECT * FROM policy_evaluations WHERE id=?',(cursor.lastrowid,)).fetchone())


def evaluation(conn, principal, evaluation_id):
    _authorize(conn,principal)
    row = conn.execute('SELECT * FROM policy_evaluations WHERE id=?',(evaluation_id,)).fetchone()
    if row is None:
        raise PolicyError('评估记录不存在',404)
    return _evaluation(row)


def inspect(conn):
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='policy_versions'").fetchone():
        return []
    problems = []
    for (selected,) in conn.execute('SELECT identity FROM policy_versions').fetchall():
        try:
            _version(conn,selected)
        except PolicyError as exc:
            problems.append(str(exc))
    for row in conn.execute('SELECT id,identity,policy,request_id,expected_id FROM policy_activations ORDER BY id'):
        # Pre-v19 raw pointers remain historical; new authenticated rows must bind a full identity.
        if row[3] is not None:
            try:
                manifest = _version(conn,row[1])
                previous = conn.execute('SELECT MAX(id) FROM policy_activations WHERE id<?',(row[0],)).fetchone()[0] or 0
                if manifest['policy'] != row[2] or previous != row[4]:
                    raise PolicyError('政策切换身份或前序不一致')
            except PolicyError as exc:
                problems.append(f'政策切换 {row[0]}：{exc}')
    for row in conn.execute('SELECT * FROM policy_evaluations'):
        try:
            if len(row)>10 and (row[10] is not None or row[11] is not None):
                context=json.loads(row[10])
                if gate.digest(context)!=row[11]:
                    raise PolicyError('采集来源或生效指针不一致')
                _capture_mode(context, row[2], json.loads(row[8]))
            if gate.digest(json.loads(row[8])) != row[6] or gate.digest(json.loads(row[9])) != row[7]:
                raise PolicyError('输入或结果哈希不一致')
            manifest = _version(conn,row[1])
            if json.loads(row[8]).get('risk_policy') != manifest['parameters']:
                raise PolicyError('评估参数与政策身份不同')
            if row[2] is not None:
                activation = conn.execute('SELECT identity FROM policy_activations WHERE id=?',(row[2],)).fetchone()
                if not activation or activation[0] != row[1]:
                    raise PolicyError('评估关联的生效身份不同')
        except (ValueError,TypeError,KeyError) as exc:
            problems.append(f'政策评估 {row[0]}：{exc}')
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest='action',required=True)
    actions.add_parser('current')
    prepare = actions.add_parser('register')
    prepare.add_argument('--parameters',type=Path,required=True,help='有效 risk_policy JSON（已解析的实际参数），私有文件')
    switch = actions.add_parser('activate')
    switch.add_argument('identity')
    switch.add_argument('--expected-id',type=int,required=True)
    switch.add_argument('--request-id',required=True)
    switch.add_argument('--reason',required=True)
    args = parser.parse_args()
    path = Path(os.environ.get('DATA_DIR',str(db.DATA))).absolute()/'app.db'
    # A local policy command does not own migrations and cannot silently create a database.
    conn = sqlite3.connect(path.as_uri()+'?mode='+('ro' if args.action=='current' else 'rw'),uri=True,
                           isolation_level=None,timeout=30)
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        principal = api_keys.authenticate(conn,os.environ.get('ASHARE_OWNER_TOKEN',''))
        if principal is None:
            raise PolicyError('请提供有效的 ASHARE_OWNER_TOKEN',403)
        _authorize(conn,principal,owner=True)
        if args.action == 'register':
            from a_share_research.policy.v0 import paths, risk_policy
            parameters = json.loads(paths.read_private_config(args.parameters,label='policy parameters'),
                object_pairs_hook=risk_policy._unique_object,parse_constant=risk_policy._reject_constant,
                parse_float=risk_policy._json_float)
            result = register(conn,principal,parameters)
            result = {k:result[k] for k in ('identity','policy','code_hash','rules_hash','guide_version')}
        elif args.action == 'activate':
            result = activate(conn,principal,args.identity,expected_id=args.expected_id,request_id=args.request_id,reason=args.reason)
        else:
            result = current(conn,principal)
            if result['manifest']:
                result['manifest'] = {k:result['manifest'][k] for k in ('identity','policy','code_hash','rules_hash','guide_version')}
        print(gate.canonical(result))
    except (ValueError,sqlite3.Error,OSError) as exc:
        parser.exit(1,f'{type(exc).__name__}: {exc}\n')
    finally:
        conn.close()


if __name__ == '__main__':
    main()
