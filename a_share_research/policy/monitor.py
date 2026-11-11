"""Acquire and retain a v0 evaluation from a verified mirror or explicitly bound ledger."""
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile

from a_share_research import db, gate
from a_share_research.account import mirror
from a_share_research.policy import identity, registry


def _mirror(conn: sqlite3.Connection, directory: Path) -> dict:
    row = conn.execute('SELECT mode,source_sha256,updated_at,watermark FROM account_source WHERE id=1').fetchone()
    if not row or row[0] != 'mirror':
        raise registry.PolicyError('此采集入口需要已核对的切换前账户镜像；不能读取未登记来源或替代新账本',409)
    try:
        content = mirror._read(conn,directory)
        if mirror.digest(content) != row[1]:
            raise registry.PolicyError('账户镜像或资产原件与来源哈希不一致，请先完成镜像核对',409)
        watermark = json.loads(row[3])
    except (OSError,ValueError) as exc:
        if isinstance(exc,registry.PolicyError):
            raise
        raise registry.PolicyError('无法核对账户镜像与资产原件',409) from None
    return dict(mode='mirror',source_sha256=row[1],updated_at=row[2],watermark=watermark)


def _source(conn: sqlite3.Connection, directory: Path) -> dict:
    row=conn.execute('SELECT mode,source_sha256,updated_at,watermark FROM account_source WHERE id=1').fetchone()
    if row is None or row[0]=='mirror':
        return _mirror(conn,directory)
    from a_share_research.policy import account_input
    try:
        return account_input.read(conn,directory,row)
    except (ValueError,OSError,sqlite3.Error):
        raise registry.PolicyError('账户账本绑定、投影或证据无法核对；不能保存评估，请检查账户来源',409) from None


def _request(expected_activation_id):
    if type(expected_activation_id) is not int or expected_activation_id < 1:
        raise registry.PolicyError('expected_activation_id 须为当前生效指针编号',422)
    return dict(action='policy.monitor',expected_activation_id=expected_activation_id)


def _existing(conn, principal, request_id, request):
    row = conn.execute('SELECT * FROM policy_evaluations WHERE created_by=? AND request_id=?',
                       (principal.key_id,request_id)).fetchone()
    if row is None:
        return None
    saved = registry._evaluation(row)
    if saved.get('capture_context',{}).get('request') != request:
        raise registry.PolicyError('request_id 已用于不同评估请求或输入')
    return saved


def capture(database: Path, parameters: dict, *, source: dict | None = None) -> dict:
    payload = parameters if source is None else dict(parameters=parameters, source=source)
    return _capture(database, 'a_share_research.policy.capture', payload)


def _capture(database: Path, module: str, payload: dict, *, market_token: bool = False) -> dict:
    """Run the fixed collector with a private empty config and no inherited model credentials."""
    if market_token and module not in ('a_share_research.policy.daily_brief', 'a_share_research.policy.benchmark'):
        raise ValueError('market credentials are only allowed for the configured market collectors')
    with tempfile.TemporaryDirectory(prefix='ashare-policy-capture-') as directory:
        config = Path(directory)/'runtime.env'
        config.touch(mode=0o600)
        env = {k:v for k,v in os.environ.items() if k in ('PATH','HOME','LANG','LC_ALL')}
        env.update(POLICY_V0_DB=str(database.resolve()),A_STOCK_CONFIG_FILE=str(config))
        if market_token and os.environ.get('TUSHARE_TOKEN'):
            env['TUSHARE_TOKEN'] = os.environ['TUSHARE_TOKEN']
        process = subprocess.Popen([sys.executable,'-m',module],
            cwd=Path(__file__).resolve().parents[2],env=env,stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
        try:
            output,_ = process.communicate(gate.canonical(payload).encode(),timeout=25)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:
                pass  # The child may finish between the deadline and group termination.
            process.communicate()
            raise registry.PolicyError('行情与政策输入采集超时；未保存成功回执，可查询原请求后重试',504) from None
        if process.returncode:
            raise registry.PolicyError('政策输入采集失败；请核对资产快照、参数范围与行情来源，未保存成功回执',503)
        if len(output)>8*1024*1024:
            raise registry.PolicyError('政策输入包过大，未保存评估',503)
        try:
            def unique(pairs):
                value={}
                for k,v in pairs:
                    if k in value: raise ValueError('duplicate key')
                    value[k]=v
                return value
            bundle=json.loads(output,object_pairs_hook=unique)
            from a_share_research.policy.evaluation import _json
            _json(bundle)
            if not isinstance(bundle,dict): raise ValueError('object required')
        except (ValueError,UnicodeError,RecursionError):
            raise registry.PolicyError('采集器返回无效输入包，未保存评估',503) from None
        return bundle


def run(conn: sqlite3.Connection, principal, database: Path, request_id: str, expected_activation_id: int) -> dict:
    from a_share_research.policy.evaluation import evaluate
    def collect(database, parameters, source):
        return (capture(database, parameters, source=source) if source['mode'] == 'ledger'
                else capture(database, parameters))
    return _run(conn, principal, database, request_id, _request(expected_activation_id), collect, evaluate)


def _run(conn, principal, database, request_id, request, collect, evaluate, *, context_version=None):
    """Capture live quotes, recheck source/policy, then append one immutable evaluation receipt.

    No write lock spans network acquisition. Concurrent identical requests may both
    acquire quotes, but return the first committed receipt and cannot duplicate it.
    """
    request_id=registry._text(request_id,'request_id')
    with db.transaction(conn,immediate=False):
        registry._authorize(conn,principal)
        saved=_existing(conn,principal,request_id,request)
        if saved is not None: return saved
        activation=registry._current(conn)
        if activation is None or activation['id']!=request['expected_activation_id']:
            raise registry.PolicyError('生效政策已变化或尚未启用，请先读取当前指针')
        manifest=registry._version(conn,activation['identity'])
        try: identity.check_installed(manifest)
        except identity.IdentityError:
            raise registry.PolicyError('当前代码与生效政策身份不一致，须由 owner 登记并切换') from None
        source=_source(conn,database.resolve().parent)
        if source['mode']=='ledger' and manifest['parameters'].get('account_scope')!=source['watermark']['account_scope']:
            raise registry.PolicyError('生效政策的账户范围与账本绑定不一致',409)
    started=db.now()
    bundle=collect(database,manifest['parameters'],source)
    try:
        result=evaluate(bundle,manifest['parameters'])
        identity.check_installed(manifest)
    except ValueError as exc:
        raise registry.PolicyError(f'采集后的政策输入或实现校验失败：{exc}',422) from None
    finished=db.now()
    context=dict(schema_version=context_version or (2 if source['mode']=='ledger' else 1),request=request,source=source,
                 capture_started_at=started,capture_finished_at=finished)
    with db.transaction(conn):
        registry._authorize(conn,principal)
        saved=_existing(conn,principal,request_id,request)
        if saved is not None: return saved
        if registry._current(conn)!=activation:
            raise registry.PolicyError('采集期间生效政策改变，未保存评估，请重新发现当前指针')
        if _source(conn,database.resolve().parent)!=source:
            raise registry.PolicyError('采集期间账户来源改变，未保存评估，请重新采集')
        cursor=conn.execute('INSERT INTO policy_evaluations '
            '(identity,activation_id,created_at,created_by,request_id,input_hash,result_hash,input,result,capture_context,capture_context_hash) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (manifest['identity'],activation['id'],finished,principal.key_id,request_id,gate.digest(bundle),gate.digest(result),
             gate.canonical(bundle),gate.canonical(result),gate.canonical(context),gate.digest(context)))
        return registry._evaluation(conn.execute('SELECT * FROM policy_evaluations WHERE id=?',(cursor.lastrowid,)).fetchone())
