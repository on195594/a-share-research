"""Read-only policy presentation for the authenticated owner web session.

The route must authenticate its session first. This module neither accepts API
credentials nor executes policy/account calculations or loads market providers.
"""
from datetime import datetime

from a_share_research import db
from a_share_research.policy import account_input, registry


STATES = {
    'inactive': ('尚未启用政策', '尚无生效记录。由所有者完成登记与启用后，这里会显示对应政策。'),
    'unregistered': ('尚未登记完整政策', '已有旧版本指针，但缺少完整政策登记。请由所有者核对并登记当前版本。'),
    'invalid': ('政策记录需要核对', '当前政策记录未通过完整性检查，暂不展示参数。请核对登记记录。'),
    'implementation_changed': ('运行版本需要核对', '已登记政策与当前运行版本不一致。新的政策采集需先由所有者重新登记并启用。'),
    'not_effective': ('政策参数尚未生效', '参数尚未生效或账户范围尚未确认，暂不能用于新的政策采集。'),
    'expired': ('政策参数已到期', '有效期已结束。请由所有者确认新的参数与适用期间。'),
    'active': ('政策已启用', '当前运行版本与生效登记一致。'),
}


def _period(parameters, now):
    if parameters.get('source')=='not_applied_account_scope_required':
        return 'not_effective'
    if 'effective_from' not in parameters:
        if parameters.get('source')=='policy_file':raise ValueError('missing policy period')
        return None
    begin=account_input._timestamp(parameters['effective_from'])
    confirmed=account_input._timestamp(parameters['confirmed_at'])
    end=account_input._timestamp(parameters['effective_to']) if parameters.get('effective_to') is not None else None
    if end is not None and end<begin:raise ValueError('invalid policy period')
    if now<begin or now<confirmed:return 'not_effective'
    if end is not None and now>end:return 'expired'
    return None


def _source(conn, parameters):
    result=dict(mode=None,status='missing',title='账户来源尚未登记',
                message='尚无账户来源记录。',updated_at=None,account_id=None,account_scope=None,scope_matches=None)
    row=conn.execute('SELECT mode,updated_at,watermark FROM account_source WHERE id=1').fetchone()
    if row is None:return result
    try:
        account_input._timestamp(row[1])
        watermark=account_input._json(row[2])
        if row[0]=='mirror':
            if not isinstance(watermark,dict):raise ValueError('invalid mirror watermark')
            return result|dict(mode='mirror',status='mirror',title='旧账户镜像',
                message='账户数据来自切换前的旧账户镜像。',updated_at=row[1])
        if row[0]!='ledger':raise ValueError('unknown source')
        account_input._binding(watermark)
        if not conn.execute('SELECT 1 FROM account_books WHERE account_id=?',(watermark['account_id'],)).fetchone():
            raise ValueError('missing bound ledger')
        matches=parameters.get('account_scope')==watermark['account_scope'] if parameters is not None else None
        return result|dict(mode='ledger',status='scope_mismatch' if matches is False else 'ledger',
            title='账户范围不一致' if matches is False else '新账户账本',
            message='账本绑定的账户范围与政策不同，请核对后再采集。' if matches is False else '账户数据按已登记账本读取。',
            updated_at=row[1],account_id=watermark['account_id'],account_scope=watermark['account_scope'],scope_matches=matches)
    except (ValueError,TypeError,KeyError,RecursionError):
        return result|dict(status='invalid',title='账户来源需要核对',
                           message='账户来源或账本绑定不完整，暂不能确认读取范围。')


def owner_view(conn, *, now=None):
    """Return one consistent owner-only read; authorization belongs to the web route."""
    now=now or datetime.now(db.SHANGHAI)
    result=dict(status='inactive',activation=None,manifest=None,installed=False)
    with db.transaction(conn,immediate=False):
        try:
            state=registry._read_state(conn)
            result.update(state)
            if state['manifest'] is not None:
                result['manifest']={k:v for k,v in state['manifest'].items() if k not in ('files','guides')}
                if state['activation']['policy']!=result['manifest']['policy']:
                    raise ValueError('activation policy differs from manifest')
                account_input._timestamp(state['activation']['activated_at'])
                period=_period(result['manifest']['parameters'],now)
                result['status']=period or ('active' if state['installed'] else 'implementation_changed')
        except registry.PolicyError as exc:
            result.update(status='unregistered' if exc.status==404 else 'invalid',activation=None,manifest=None,installed=False)
        except (ValueError,TypeError,KeyError,RecursionError):
            result.update(status='invalid',activation=None,manifest=None,installed=False)
        result['source']=_source(conn,result['manifest']['parameters'] if result['manifest'] else None)
    result['title'],result['message']=STATES[result['status']]
    return result
