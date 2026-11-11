"""Retain sizing derived from a verified monitor receipt; never acquire quotes or place orders."""
from datetime import datetime
import json

from a_share_research import db, gate
from a_share_research.policy import identity, monitor, registry
from a_share_research.policy.position_request import Reference, validate_proposal


def _hashes(receipt):
    if (gate.digest(receipt['input']) != receipt['input_hash']
            or gate.digest(receipt['result']) != receipt['result_hash']):
        raise registry.PolicyError('评估输入或结果哈希不一致')


def verify_parent(conn, receipt):
    """Check the stored derivation against its immutable parent, without consulting current state."""
    _hashes(receipt)
    ref = Reference.model_validate(receipt['input']['monitor_evaluation'])
    row = conn.execute('SELECT * FROM policy_evaluations WHERE id=?', (ref.id,)).fetchone()
    if row is None:
        raise registry.PolicyError('定仓关联的监控评估不存在')
    parent = registry._evaluation(row)
    _hashes(parent)
    if (parent['mode'] != 'service_monitor_capture' or parent['id'] >= receipt['id']
            or parent['identity'] != receipt['identity'] or parent['activation_id'] != receipt['activation_id']
            or ref.input_hash != parent['input_hash'] or ref.result_hash != parent['result_hash']
            or receipt['input']['snapshot'] != parent['result']
            or receipt['input']['risk_policy'] != parent['input']['risk_policy']
            or receipt['capture_context']['source'] != parent['capture_context']['source']):
        raise registry.PolicyError('定仓输入与关联监控评估不一致')


def run(conn, principal, database, request_id, expected_activation_id, monitor_evaluation_id, proposal):
    """Size against the selected saved snapshot while the policy and account source still match it."""
    from a_share_research.policy import capture, position_evaluation
    request_id = registry._text(request_id, 'request_id')
    if type(monitor_evaluation_id) is not int or monitor_evaluation_id < 1:
        raise registry.PolicyError('monitor_evaluation_id 须为监控评估编号', 422)
    try:
        proposal = validate_proposal(proposal)
    except ValueError:
        raise registry.PolicyError('拟买入代码、价格、框架或可用资金不符合定仓参数要求', 422) from None
    request = monitor._request(expected_activation_id) | dict(
        action='policy.position_size', monitor_evaluation_id=monitor_evaluation_id, proposal=proposal)
    # Calculation has no network I/O. Keep parent/source checks and the append in one transaction.
    with db.transaction(conn):
        registry._authorize(conn, principal)
        saved = monitor._existing(conn, principal, request_id, request)
        if saved is not None:
            return registry.evaluation(conn, principal, saved['id'])
        activation = registry._current(conn)
        if activation is None or activation['id'] != expected_activation_id:
            raise registry.PolicyError('生效政策已变化或尚未启用，请先读取当前指针')
        manifest = registry._version(conn, activation['identity'])
        try:
            identity.check_installed(manifest)
            capture._current_policy(manifest['parameters'], datetime.fromisoformat(db.now()))
        except ValueError:
            raise registry.PolicyError('当前实现或参数与生效政策不一致，须先核对政策') from None
        parent = registry.evaluation(conn, principal, monitor_evaluation_id)
        _hashes(parent)
        if (parent['mode'] != 'service_monitor_capture' or parent['activation_id'] != activation['id']
                or parent['identity'] != manifest['identity'] or parent['input']['risk_policy'] != manifest['parameters']):
            raise registry.PolicyError('须引用当前生效政策的服务端监控回执；请先重新监控')
        source = monitor._source(conn, database.resolve().parent)
        if source != parent['capture_context']['source']:
            raise registry.PolicyError('账户来源已不同于监控快照，请先重新监控')
        if source['mode'] == 'ledger' and manifest['parameters'].get('account_scope') != source['watermark']['account_scope']:
            raise registry.PolicyError('生效政策的账户范围与账本绑定不一致')
        bundle = json.loads(gate.canonical(dict(risk_policy=manifest['parameters'], snapshot=parent['result'],
            monitor_evaluation=dict(id=parent['id'], input_hash=parent['input_hash'], result_hash=parent['result_hash']),
            proposal=proposal)))
        try:
            result = position_evaluation.evaluate(bundle, manifest['parameters'])
            identity.check_installed(manifest)
            capture._current_policy(manifest['parameters'], datetime.fromisoformat(db.now()))
        except ValueError as exc:
            raise registry.PolicyError(f'定仓输入或政策校验失败：{exc}', 422) from None
        if monitor._source(conn, database.resolve().parent) != source:
            raise registry.PolicyError('计算期间账户来源改变，未保存定仓评估')
        stamp = db.now()
        context = dict(schema_version=6, request=request, source=source, derived_at=stamp)
        cursor = conn.execute('INSERT INTO policy_evaluations '
            '(identity,activation_id,created_at,created_by,request_id,input_hash,result_hash,input,result,capture_context,capture_context_hash) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (manifest['identity'], activation['id'], stamp, principal.key_id, request_id, gate.digest(bundle), gate.digest(result),
             gate.canonical(bundle), gate.canonical(result), gate.canonical(context), gate.digest(context)))
        return registry.evaluation(conn, principal, cursor.lastrowid)
