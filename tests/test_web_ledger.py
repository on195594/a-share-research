"""Exercise the form against the actual ledger using the existing structural account fixture."""
import asyncio

import pytest
from starlette.datastructures import FormData
from starlette.requests import Request

from a_share_research import web_ledger
from a_share_research.account import evidence, ledger
from a_share_research.tools import registry
from a_share_research.tools_client import ToolCallError
from test_account_ledger import batch, book, buy, submit  # noqa: F401
from test_policy_account_input import bound_book as policy_book_fixture
from test_position_store import batch as position_batch, event as position_event

policy_book = policy_book_fixture


class BoundTools:
    def __init__(self, ctx):
        self.ctx = ctx
        self.submissions = []
        self.reply_loss = None

    async def call(self, name, arguments, *, request_id=None):
        try:
            result = registry.invoke(self.ctx, name, arguments)['data']
        except registry.ToolError as exc:
            raise ToolCallError('business', str(exc)) from exc
        if name == 'account_submit':
            self.submissions.append((request_id, arguments))
            loss, self.reply_loss = self.reply_loss, None
            if loss == 'timeout':
                raise ToolCallError('unknown', '结果未知，请使用同一请求编号和原内容重试。')
            if loss == 'missing_id':
                result.pop('receipt_id')
        return result


@pytest.fixture
def account(book, bind_account):  # noqa: F811
    conn, principal, directory, sha = book
    directory.rename(directory.with_name('account-evidence'))
    directory = directory.with_name('account-evidence')
    book = (conn, principal, directory, sha)
    bind_account(conn, 'a')
    evidence.record_upload(conn, sha, principal.key_id, filename='成交凭证.pdf', label='资产')
    assert submit(book, batch(book))['status'] == 'posted'
    ctx = registry.Context(conn, directory.parent / 'app.db', principal, account_shadow_enabled=True)

    return BoundTools(ctx), book


def show(web, client, fields=None):
    request = Request({'type': 'http', 'method': 'POST' if fields is not None else 'GET',
                       'path': '/account/ledger/new', 'headers': [], 'query_string': b''})
    return asyncio.run(web_ledger.page(request, 'test-session', client, web.render, fields, connection=web.conn))


@pytest.mark.parametrize('loss', ['timeout', 'missing_id'])
def test_trade_prefill_and_lost_result_replay_after_ledger_version_changes(web, account, loss):
    client, (conn, principal, directory, sha) = account
    page = show(web, client)
    body = page.body.decode()
    assert '成交前现金 59950' in body and '600519 · 100 股' in body
    assert '成交凭证.pdf' in body and sha in body
    fields = dict(page.context['fields'])
    assert fields['expected_version'] == '1' and fields['ending.600519'] == '100'
    trade = buy(kind='sell', shares=50, price='700', fees='5', tax='17.5', occurred_at='2026-10-11T09:00:00')
    fields.update(kind=trade.kind, code=trade.code, shares=str(trade.shares), price=str(trade.price),
                  fees=str(trade.fees + trade.tax), occurred_at=trade.occurred_at.replace(tzinfo=None).isoformat(),
                  as_of='2026-10-11T15:00:00', trade_ending='50', trade_value='35000',
                  total_assets='129927.5', cash='94927.5', evidence_hashes=sha)
    client.reply_loss = loss
    submitted = FormData(fields)
    response = show(web, client, submitted)
    assert '用原编号和原内容重试' in response.body.decode() and fields['request_id'] in response.body.decode()
    assert '<fieldset disabled>' in response.body.decode() and '提交对账</button>' not in response.body.decode()
    assert ledger.state(conn, principal, 'a')['version'] == 2
    pending = fields | {'awaiting_reply': '1'}
    restored = show(web, client, FormData(pending | {'intent': 'restore'}))
    assert '基于旧版本 1，当前版本为 2' in restored.body.decode()
    assert '用原编号和原内容重试' in restored.body.decode()
    assert len(client.submissions) == 1

    retry = show(web, client, FormData(pending))
    repeated = show(web, client, FormData(pending))
    assert retry.status_code == 303
    assert retry.headers['location'] == repeated.headers['location'] == '/account/receipts/2?request=' + fields['request_id']
    assert [cid for cid, args in client.submissions] == [fields['request_id']] * 3
    assert all(args == client.submissions[0][1] for cid, args in client.submissions)
    assert client.submissions[0][1]['batch']['snapshot']['portfolio_value_as_of'] == '2026-10-11T15:00:00+08:00'
    assert conn.execute('SELECT count(*) FROM account_batches WHERE id > 1').fetchone()[0] == 1
    assert ledger.receipt(conn, principal, 'a', 2)['key_id'] == principal.key_id


def test_trade_field_error_and_disabled_form(web, account):
    client, (_, _, _, sha) = account
    fields = dict(show(web, client).context['fields'])
    event = buy()
    fields.update(kind=event.kind, code=event.code, shares=str(event.shares), price=str(event.price),
                  occurred_at=event.occurred_at.isoformat(), fees='0', trade_ending='200', trade_value='0',
                  cash='0', total_assets='0', evidence_hashes=sha)
    reply = show(web, client, FormData(fields | {'awaiting_reply': '1'}))
    assert 'batch.snapshot.market_values: 缺 600519 市值' in reply.body.decode()
    assert fields['request_id'] in reply.body.decode() and '提交对账</button>' in reply.body.decode()
    stale = show(web, client, FormData(fields | {'expected_version': '0', 'trade_value': '100'}))
    assert '对账冲突' in stale.body.decode() and '按当前版本重新填写' in stale.body.decode()
    disabled = show(web, None)
    assert 'Web 账户写入未启用' in disabled.body.decode() and '<fieldset disabled>' in disabled.body.decode()


def test_reopened_position_uses_selected_registered_profile(web, policy_book):
    path, conn, principal, directory, sha = policy_book
    client = BoundTools(registry.Context(conn, path, principal, account_shadow_enabled=True))
    closing_trade = position_batch((conn, principal, directory, sha), [position_event('sell')], shares=0, cash='11200')
    assert ledger.submit(conn, principal, 'a', closing_trade, directory)['status'] == 'posted'
    page = show(web, client)
    profile = conn.execute('SELECT profile_identity FROM policy_v0_account_openings').fetchone()[0]
    assert f'<option value="{profile}"' in page.body.decode()
    event = position_event(price='10', occurred_at='2026-11-03T10:00:00+08:00')
    fields = dict(page.context['fields']) | {
        'kind': event.kind, 'code': event.code, 'occurred_at': event.occurred_at.isoformat(),
        'price': str(event.price), 'shares': str(event.shares), 'trade_ending': '100', 'trade_value': '1000',
        'cash': '10200', 'total_assets': '11200', 'evidence_hashes': sha,
        'policy_identity': profile, 'framework': 'A通用',
    }
    assert show(web, client, FormData(fields)).status_code == 303
    assert client.submissions[0][1]['batch']['position_entries'] == {'0': {'policy_identity': profile, 'framework': 'A通用'}}
