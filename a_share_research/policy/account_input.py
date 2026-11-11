"""Verified ledger inputs for monitoring; no cutover writer or legacy side-file fallback."""
from contextlib import nullcontext
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import re
import sqlite3

from a_share_research import db, gate


class AccountInputError(ValueError):
    """The selected ledger source cannot support a reproducible monitoring read."""


def _object(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise AccountInputError('账本采集来源字段不完整或包含未知字段')


def _text(value, limit=None):
    if not isinstance(value, str) or not value.strip() or value != value.strip() or limit and len(value)>limit:
        raise AccountInputError('账本采集来源文本无效')


def _sha(value):
    if not isinstance(value, str) or re.fullmatch('[0-9a-f]{64}', value) is None:
        raise AccountInputError('账本采集来源哈希无效')


def _timestamp(value):
    if not isinstance(value, str):
        raise AccountInputError('账本采集记录时间无效')
    try:
        stamp=datetime.fromisoformat(value)
        if stamp.tzinfo is None: raise ValueError
        return stamp
    except ValueError:
        raise AccountInputError('账本采集记录时间须含时区') from None


def _json(value):
    def pairs(items):
        result={}
        for key,item in items:
            if key in result: raise AccountInputError('账本输入 JSON 键重复')
            result[key]=item
        return result
    def constant(_):
        raise AccountInputError('账本输入含非有限数字')
    parsed=json.loads(value,object_pairs_hook=pairs,parse_constant=constant)
    gate.canonical(parsed)  # Also rejects a floating-point overflow during parsing.
    return parsed


def _binding(value):
    _object(value,('schema_version','account_id','account_scope','mirror_watermark'))
    if type(value['schema_version']) is not int or value['schema_version']!=1:
        raise AccountInputError('未知账本来源绑定版本')
    _text(value['account_id'],80)
    _text(value['account_scope'])
    if not isinstance(value['mirror_watermark'],dict):
        raise AccountInputError('旧镜像水位须为对象')
    gate.canonical(value)


def _asset(value, scope):
    if value is None: return
    _object(value,('portfolio_value','account_scope','recorded_at','portfolio_value_as_of','source','record_id','evidence_hashes'))
    if value['account_scope']!=scope:
        raise AccountInputError('账本资产范围不符')
    if value['source'] not in ('account_observation','account_batch') or type(value['record_id']) is not int or value['record_id']<1:
        raise AccountInputError('账本资产记录身份无效')
    if not isinstance(value['portfolio_value'],str): raise AccountInputError('账本资产金额须为字符串')
    amount=Decimal(value['portfolio_value'])
    if not amount.is_finite() or amount<0: raise AccountInputError('账本资产金额无效')
    recorded=_timestamp(value['recorded_at'])
    if value['portfolio_value_as_of'] is not None and _timestamp(value['portfolio_value_as_of'])>recorded:
        raise AccountInputError('账本估值时点晚于记录时间')
    hashes=value['evidence_hashes']
    if not isinstance(hashes,list) or not hashes: raise AccountInputError('账本资产缺少证据')
    for sha in hashes: _sha(sha)


def validate_source(source):
    """Validate a saved source without consulting mutable current database state."""
    try:
        _object(source,('mode','source_sha256','updated_at','watermark','state_sha256','account_version','asset'))
        if source['mode']!='ledger': raise AccountInputError('不是账本采集来源')
        _sha(source['source_sha256']);_sha(source['state_sha256'])
        _timestamp(source['updated_at'])
        _binding(source['watermark'])
        if type(source['account_version']) is not int or source['account_version']<0:
            raise AccountInputError('账本版本无效')
        _asset(source['asset'],source['watermark']['account_scope'])
        gate.canonical(source)
    except (ValueError, TypeError, KeyError, ArithmeticError, RecursionError) as exc:
        if isinstance(exc,AccountInputError): raise
        raise AccountInputError('账本采集来源结构无效') from None


def _rows(conn, sql, args=()):
    cursor=conn.execute(sql,args)
    fields=[c[0] for c in cursor.description]
    return [dict(zip(fields,row)) for row in cursor]


def _read(conn, directory, row):
    from a_share_research.account import cycle_analysis, evidence, ledger, projection_ids
    from a_share_research.account.reconciliation import Event, Opening, identity, project, reconcile
    from a_share_research.policy.v0 import account_store

    if len(row)!=4 or row[0]!='ledger': raise AccountInputError('需要显式绑定的账本来源')
    binding=_json(row[3]);_binding(binding)
    _sha(row[1]);_timestamp(row[2])
    account=binding['account_id']
    state={}
    for table in ('account_books','account_position_books','policy_v0_account_openings','account_projection_ids',
                  'account_analysis_history','account_observations','account_journal'):
        # Every table has a stable primary key; JSON serialization sorts object keys.
        rows=_rows(conn,f'SELECT * FROM main.{table} WHERE account_id=?',(account,))
        state[table]=sorted(rows,key=gate.canonical)
    for table in ('account_books','account_position_books','policy_v0_account_openings'):
        if len(state[table])!=1: raise AccountInputError('账本、成本或政策期初尚未完整建立')
    book=state['account_books'][0]
    state['policy_v0_profiles']=_rows(conn,'SELECT * FROM main.policy_v0_profiles ORDER BY identity')
    state['account_batches']=_rows(conn,"SELECT * FROM main.account_batches WHERE account_id=? AND status='posted' ORDER BY id",(account,))
    state['account_voids']=_rows(conn,'SELECT v.* FROM main.account_voids v JOIN main.account_journal j ON j.id=v.event_id WHERE j.account_id=? ORDER BY v.event_id',(account,))
    voids={r['event_id'] for r in state['account_voids']}
    journals=state['account_journal']
    batches={r['id']:r for r in state['account_batches']}
    for item in journals:
        event=Event.model_validate(_json(item['event']))
        if (item['batch_id'] not in batches or item['business_id']!=identity(event,account)
                or item['company_key']!=event.code or item['occurred_at']!=event.occurred_at.isoformat()
                or event.occurred_at<_timestamp(book['opened_at'])):
            raise AccountInputError('账本事件归属或身份不一致')
    for item in state['account_voids']:
        original=next(r for r in journals if r['id']==item['event_id'])
        if item['batch_id'] not in batches or item['batch_id']<=original['batch_id']:
            raise AccountInputError('账本作废批次归属不一致')
    active=sorted((r for r in journals if r['id'] not in voids),key=lambda r:r['id'])
    if len({r['business_id'] for r in active})!=len(active): raise AccountInputError('账本有效事件身份重复')
    projection=project(Opening.model_validate(_json(book['opening'])),[Event.model_validate(_json(r['event'])) for r in active])
    if projection!=Opening.model_validate(_json(book['projection'])):
        raise AccountInputError('账本资金投影与事件重放不一致')
    versions={r['batch_id'] for r in journals}|{r['batch_id'] for r in state['account_voids']}
    if book['version']!=len(versions): raise AccountInputError('账本版本与入账批次不一致')
    projected=account_store.project(conn,account)
    state['v0_projection']=projected
    cost=_json(state['account_position_books'][0]['projection'])
    ids=projection_ids.lookup(conn,account,'cycle')
    active_ids={r['id'] for r in projected['holdings'] if r['exit_date'] is None}
    cycles={ids[c['cycle_id']]:c for c in cost['cycles']}
    for hid in active_ids:
        cycle=cycles.get(hid)
        if cycle is None: raise AccountInputError('活动持仓没有账本周期')
        cycle_analysis.projected_snapshot(conn,account,cycle['cycle_id'],hid,cycle['code'])
    hashes={book['opening_evidence']}
    for table in ('account_position_books','policy_v0_account_openings'):
        hashes.update(_json(state[table][0]['evidence_hashes']))
    legacy_ids=set()
    for history in state['account_analysis_history']:
        request=_json(history['request']);snapshot=_json(history['snapshot'])
        if gate.digest(request)!=history['request_hash'] or gate.digest(snapshot)!=history['snapshot_hash']:
            raise AccountInputError('账户分析历史哈希不一致')
        hashes.update(_json(history['evidence_hashes']))
        if history['source']=='journal_baseline':
            revision=request['initialize']['legacy_revision_id']
            if revision is not None: legacy_ids.add(revision)
    state['legacy_analysis_sources']=[]
    for revision in sorted(legacy_ids):
        original=_rows(conn,"SELECT * FROM main.account_analysis_history WHERE id=? AND account_id=''",(revision,))
        if len(original)!=1: raise AccountInputError('账户分析的原始基线不存在')
        original=original[0]
        if gate.digest(_json(original['request']))!=original['request_hash'] or gate.digest(_json(original['snapshot']))!=original['snapshot_hash']:
            raise AccountInputError('账户分析原始基线哈希不一致')
        hashes.update(_json(original['evidence_hashes']))
        state['legacy_analysis_sources'].append(original)
    codes=sorted({r['code'] for r in projected['holdings']})
    state['stock_fundamentals']=[]
    for code in codes:
        state['stock_fundamentals']+=_rows(conn,'SELECT * FROM main.stock_fundamentals WHERE code=? ORDER BY code',(code,))
    candidates=[]
    def asset(kind,item,total,refs,as_of):
        if total is not None:
            if as_of is not None and as_of>_timestamp(item['created_at']):
                raise AccountInputError('保存的资产估值时点晚于记录时间')
            candidates.append(dict(portfolio_value=str(total),account_scope=binding['account_scope'],
                recorded_at=item['created_at'],portfolio_value_as_of=as_of.isoformat() if as_of is not None else None,
                source=kind,record_id=item['id'],
                evidence_hashes=sorted(set(refs))))
    for item in state['account_observations']:
        raw=_json(item['observation']);observation=ledger.Observation.model_validate(raw)
        if gate.digest(dict(observation=raw,evidence_hash=item['evidence']))!=item['request_hash']:
            raise AccountInputError('账户资产观察哈希不一致')
        hashes.add(item['evidence'])
        asset('account_observation',item,observation.total_assets,[item['evidence']],observation.portfolio_value_as_of)
    for item in state['account_batches']:
        raw=_json(item['request']);batch=ledger.Batch.model_validate(raw);receipt=_json(item['receipt'])
        expected=dict(receipt_id=item['id'],account_id=account,request_hash=item['request_hash'],status='posted',mode='shadow',evidence_hashes=batch.evidence_hashes)
        if (gate.digest(raw)!=item['request_hash'] or any(receipt.get(k)!=v for k,v in expected.items())
                or receipt.get('added')!=sum(r['batch_id']==item['id'] for r in journals)
                or receipt.get('voided')!=sum(r['batch_id']==item['id'] for r in state['account_voids'])
                or batch.snapshot is None or not batch.evidence_hashes):
            raise AccountInputError('账户已入账快照与回执不一致')
        if reconcile(Opening.model_validate(receipt['after']),batch.snapshot,Decimal(book['tolerance'])):
            raise AccountInputError('账户已入账快照未与回执资金股数对平')
        hashes.update(batch.evidence_hashes)
        asset('account_batch',item,batch.snapshot.total_assets,batch.evidence_hashes,batch.snapshot.portfolio_value_as_of)
    for sha in sorted(hashes):
        _sha(sha);evidence.verify(directory/'account-evidence',sha)
    # Record time selects the latest saved evidence; it never becomes a valuation time.
    latest={}
    for candidate in candidates:
        kind=candidate['source']
        previous=latest.get(kind)
        if previous is None or (_timestamp(candidate['recorded_at']),candidate['record_id']) > (_timestamp(previous['recorded_at']),previous['record_id']):
            latest[kind]=candidate
    selected=None
    if latest:
        stamp=max(_timestamp(a['recorded_at']) for a in latest.values())
        tied=[a for a in latest.values() if _timestamp(a['recorded_at'])==stamp]
        if len({Decimal(a['portfolio_value']) for a in tied})>1:
            raise AccountInputError('同一记录时点的资产观察与入账快照金额冲突，不能猜测先后')
        # Equal amounts at the same instant: prefer the reconciled batch; preserve its reference.
        selected=next((a for a in tied if a['source']=='account_batch'),tied[0])
    result=dict(mode='ledger',source_sha256=row[1],updated_at=row[2],watermark=binding,
                state_sha256=gate.digest(state),account_version=book['version'],asset=selected)
    validate_source(result)
    return result


def read(conn: sqlite3.Connection, directory: Path, row) -> dict:
    """Read one verified account snapshot in the caller's transaction, or own a read transaction."""
    try:
        with nullcontext() if conn.in_transaction else db.transaction(conn,immediate=False):
            return _read(conn,Path(directory),row)
    except (ValueError, TypeError, KeyError, IndexError, ArithmeticError, sqlite3.Error, OSError, RecursionError) as exc:
        if isinstance(exc,AccountInputError): raise
        raise AccountInputError('无法核对账本、资产证据或账户周期分析') from None
