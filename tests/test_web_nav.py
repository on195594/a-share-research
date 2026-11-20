"""Exercise the owner form against the NAV book and recorded H00906 response."""
import asyncio
import re

import httpx
import pytest

from a_share_research.account import evidence
from a_share_research.tools.registry import Context, ToolError, invoke
from a_share_research.tools_client import ToolCallError
from test_account_nav import book as book_fixture, collect_nav as collect_fixture, observe, post

book = book_fixture
collect_nav = collect_fixture


@pytest.mark.parametrize('with_flow', [False, True])
def test_freeze_and_evaluate_selected_interval(web, book, collect_nav, monkeypatch, with_flow):
    _, collections = collect_nav
    if with_flow:
        event_id = post(book)
        before = observe(book, '2026-01-06T10:00:00+08:00', '1000')
    ctx = Context(book['conn'], book['path'], book['principal'], account_shadow_enabled=True)
    attempts = []
    calls = []

    class BoundTools:
        async def call(self, name, arguments, *, request_id=None):
            calls.append(name)
            try:
                result = invoke(ctx, name, arguments)['data']
            except ToolError as exc:
                raise ToolCallError('business', str(exc), exc.code) from exc
            if name == 'account_nav_evaluate':
                attempts.append((request_id, arguments))
                if len(attempts) == 1:
                    return {}  # The committed result lost its artifact ID in transit.
            return result

    monkeypatch.setattr(web.ToolsClient, 'from_settings', lambda settings: BoundTools())
    token = web.start_session().headers['set-cookie'].split(';')[0].split('=', 1)[1]
    path = '/account/nav/new'
    evidence.record_upload(book['conn'], book['sha'], book['principal'].key_id, '区间流水.pdf', None)

    async def visit():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url=web.settings.base_url,
                                     headers={'Origin': web.settings.base_url}, cookies={'session': token}) as browser:
            entry = await browser.get(path)
            assert entry.status_code == 200 and '冻结当前账户' in entry.text
            auth = {'csrf': re.search(r'name="csrf" value="([0-9a-f]{64})"', entry.text)[1]}
            capture = await browser.post(path, data={**auth, 'action': 'capture'})
            assert capture.status_code == 200 and '区间流水.pdf' in capture.text
            state = re.search(r'name="state_sha256" value="([0-9a-f]{64})"', capture.text)[1]
            def point(ref):
                return re.search(rf'<option value="(observation:{ref["id"]}:[^"]+)"', capture.text)[1]
            form = {**auth, 'account_id': 'a', 'action': 'evaluate', 'state_sha256': state,
                    'start': point(book['start']), 'end': point(book['end']), 'evidence_hashes': book['sha'],
                    'explanation': '依据所选跨期凭证核对资金记录。', 'confirmed': 'yes', 'declaration': 'none'}
            if with_flow:
                missing = await browser.post(path, data=form)
                assert '须为区间内每笔有效出入金恰好指定一个流前估值' in missing.text
                assert not collections
                form.update(declaration='listed')
                form[f'before_{event_id}'] = point(before)
            incomplete = await browser.post(path, data=form)
            assert '用原区间与声明重试' in incomplete.text and attempts[0][0] in incomplete.text
            response = await browser.post(path, data=form)
            assert response.status_code == 303
            if not with_flow:
                post(book)
                calls.clear()
                stale = await browser.post(path, data=form | {'explanation': '新增流水后复核原区间。'})
                assert calls == ['account_nav_evaluate']
                assert stale.text.count('role="alert"') == 1 and '账户资料已变化' in stale.text
            return response.headers['location']

    location = asyncio.run(visit())
    ref, = book['conn'].execute('SELECT artifact_sha256 FROM account_nav_refs')
    assert location == '/account/nav/' + ref[0]
    assert attempts[0] == attempts[1]
    args = attempts[0][1]['request']
    assert 'benchmark_evidence_hash' not in args
    assert args['coverage']['evidence_hashes'] == [book['sha']]
    assert [flow['event_id'] for flow in args['flows']] == ([event_id] if with_flow else [])
    assert collections == [('H00906.CSI', '2026-01-05', '2026-01-08')]
