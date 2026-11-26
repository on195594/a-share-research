"""Holdings-aware research (contract 12): gates, frozen snapshots and isolation from blind research.

Reports are the captured continuation records; a holdings-aware report is a marked copy of a real one
(only its run request gains holdings_context and a snapshot), never invented company data.
"""
import json
from pathlib import Path

import pytest

from a_share_research import (agenda, api_keys, db, material as materials, overview, reading, research, screening,
                              service, validation, zones)
from a_share_research.research_tools import research_methods
from a_share_research.tools.registry import Context, ToolError, invoke

DATA = Path(__file__).with_name('data')
SNAPSHOT = {'code': '603606', 'frozen_at': '2026-11-26T10:00:00+08:00', 'status': 'unheld',
            'account': {'account_id': 'a', 'version': 3, 'source_updated_at': '2026-11-25T15:00:00+08:00'},
            'policy_gaps': []}


def _aware_copy(conn, report_id: int, created_at: str, snapshot=SNAPSHOT) -> int:
    """Marked variant: the same reviewed report, accepted later as holdings-aware research."""
    run_id = conn.execute('SELECT run_id FROM reports WHERE id=?', (report_id,)).fetchone()[0]

    def copy(table, row_id, changes):
        stored = [c[1] for c in conn.execute(f'PRAGMA table_xinfo({table})') if c[6] == 0 and c[1] != 'id']
        row = dict(conn.execute(f'SELECT {",".join(stored)} FROM {table} WHERE id=?', (row_id,)).fetchone()) | changes
        return conn.execute(f'INSERT INTO {table}({",".join(row)}) VALUES ({",".join("?" * len(row))})',
                            list(row.values())).lastrowid

    request = json.loads(conn.execute('SELECT request FROM runs WHERE id=?', (run_id,)).fetchone()[0] or '{}') or {}
    new_run = copy('runs', run_id, {'request_id': None, 'created_at': created_at, 'replay_of': None,
                                    'request': json.dumps(request | {'holdings_context': True,
                                                                     'holdings_snapshot': snapshot})})
    return copy('reports', report_id, {'run_id': new_run, 'created_at': created_at})


def _latest_blind(conn, code):
    return conn.execute(service.LATEST_FORMAL_REPORT_ID, (code,)).fetchone()[0]


# ---------- permission gates ----------

def test_only_an_account_enabled_assistant_reaches_holdings_research(conn, monkeypatch):
    blind = _latest_blind(conn, '603606')
    aware = _aware_copy(conn, blind, '2026-11-26T10:00:00+08:00')
    aware_run = conn.execute('SELECT run_id FROM reports WHERE id=?', (aware,)).fetchone()[0]
    monkeypatch.setattr(service.companies, 'company', lambda code: {'code': code, 'zwjc': '东方电缆'})
    contexts = {}
    for role in ('research', 'assistant'):
        api_keys.create(conn, role, role=role)
        key_id = conn.execute('SELECT id FROM api_keys WHERE name=?', (role,)).fetchone()[0]
        principal = api_keys.Principal(key_id, role)
        for enabled in (False, True):
            contexts[role, enabled] = Context(conn, Path('unused'), principal, account_shadow_enabled=enabled)
    calls = {
        'research_request': {'code': '603606', 'request_id': 'aware-1', 'holdings_context': True},
        'research_status': {'run_id': aware_run},
        'research_report': {'report_id': aware},
        'research_feedback': {'report_id': aware, 'location': 'summary', 'text': '核对结论', 'request_id': 'fb-1'},
    }
    for key in (('research', False), ('research', True), ('assistant', False)):
        for name, args in calls.items():
            with pytest.raises(ToolError) as error:
                invoke(contexts[key], name, args)
            assert (error.value.status, error.value.code) == (403, 'forbidden'), (key, name)
    # Blind research and blind reports stay open to every research credential.
    assert invoke(contexts['research', False], 'research_report', {'report_id': blind})['data']['input_mode'] == 'blind'
    allowed = contexts['assistant', True]
    assert invoke(allowed, 'research_report', {'report_id': aware})['data']['input_mode'] == 'holdings_context'
    assert invoke(allowed, 'research_status', {'run_id': aware_run})['data']['input_mode'] == 'holdings_context'
    queued = invoke(allowed, 'research_request', calls['research_request'])['data']
    request = json.loads(conn.execute('SELECT request FROM runs WHERE id=?', (queued['run_id'],)).fetchone()[0])
    # No ledger is bound in this database: the position is unknown, never presumed unheld.
    assert request['holdings_context'] is True and request['holdings_snapshot']['status'] == 'unverified'
    checked = invoke(allowed, 'research_feedback', calls['research_feedback'])['data']
    feedback = json.loads(conn.execute('SELECT request FROM runs WHERE id=?', (checked['run_id'],)).fetchone()[0])
    assert feedback['holdings_context'] is True and feedback['holdings_snapshot']['status'] == 'unverified'


def test_legacy_http_request_refuses_the_same_assistant_pat_before_reading_the_view(reading_server, monkeypatch):
    conn, client, *_ = reading_server
    token = api_keys.create(conn, 'holdings-http', role='assistant')
    principal = api_keys.authenticate(conn, token)
    ctx = Context(conn, Path('unused'), principal, account_shadow_enabled=True)
    monkeypatch.setattr(service.companies, 'company', lambda code: {'code': code, 'zwjc': '东方电缆'})
    headers = {'Authorization': 'Bearer ' + token}
    run = invoke(ctx, 'research_request', {'code': '603606', 'request_id': 'aware',
                                           'holdings_context': True})['data']['run_id']
    invoke(ctx, 'research_request', {'code': '603606', 'request_id': 'blind'})
    assert client.get('/api/request?request_id=blind', headers=headers).status_code == 200
    monkeypatch.setattr(service, 'run_view', lambda *a, **kw: pytest.fail('知仓门禁前不能构造视图'))
    for path in ('/api/request?request_id=aware', f'/api/runs/{run}'):
        assert client.get(path, headers=headers).status_code == 403
    assert client.get('/api/request?request_id=missing', headers=headers).status_code == 404


def test_web_holdings_actions_and_forms_follow_account_capability(web, owner_request, bound_book, monkeypatch):
    from dataclasses import replace

    _, conn, *_ = bound_book
    request = owner_request('/')
    csrf = web.csrf_token(web.current_session(request))
    blind = _latest_blind(web.conn, '603606')
    aware = _aware_copy(web.conn, blind, '2026-11-26T10:00:00+08:00')
    run = service.submit(web.conn, '603606', 'retry-aware', holdings_context=True)
    feedback = service.request_feedback(web.conn, 'web-owner', aware, 'feedback', 'summary', '核对结论')['run_id']
    for task in (run, feedback):
        db.update_run(web.conn, task, status='failed')
    token = api_keys.create(conn, 'web-owner-proxy', role='assistant')
    enabled = replace(web.settings, tools_url='http://localhost', tools_token=token)
    monkeypatch.setattr(web, 'settings', enabled)
    conn.execute("UPDATE account_source SET mode='mirror' WHERE id=1")
    assert not web._holdings_actions()  # Proxy configured without a bound ledger.
    conn.execute("UPDATE account_source SET mode='ledger' WHERE id=1")
    monkeypatch.setattr(web, 'settings', replace(enabled, tools_url='', tools_token=''))
    initial = web.conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0]
    for response in (web.retry_run(request, run, csrf), web.retry_feedback(request, feedback, 'again', csrf),
                     web.submit_feedback(request, aware, 'summary', '核对', 'new-feedback', csrf)):
        assert response.status_code == 403
    assert web.conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == initial
    html = web.report_page(request, aware).body.decode()
    assert 'name="parent_report_id"' not in html and f'action="/reports/{aware}/feedback"' not in html
    html = web.run_page(request, run).body.decode()
    assert '受理时冻结持仓快照（未持有）' in html
    assert '输入模式 · 盲研究' in web.report_page(request, blind).body.decode()
    assert '输入模式 · 知仓研究' in html and f'action="/runs/{run}/retry"' not in html
    assert not web._holdings_actions()  # A bound ledger alone cannot enable the proxy.
    monkeypatch.setattr(web, 'settings', enabled)
    assert web._holdings_actions()
    html = web.report_page(request, aware).body.decode()
    assert 'name="parent_report_id"' not in html and f'action="/reports/{aware}/feedback"' in html
    for response in (web.retry_run(request, run, csrf), web.retry_feedback(request, feedback, 'again', csrf),
                     web.submit_feedback(request, aware, 'summary', '核对', 'new-feedback', csrf)):
        assert response.status_code == 303
        assert service.run_input_mode(conn, int(response.headers['location'].split('/')[-1])) == 'holdings_context'

    assert api_keys.revoke(conn, api_keys.authenticate(conn, token).key_id)
    for denied in (token, api_keys.create(conn, 'web-research', role='research'), 'invalid-token'):
        monkeypatch.setattr(web, 'settings', replace(enabled, tools_token=denied))
        count = conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0]
        for response in (web.retry_run(request, run, csrf), web.retry_feedback(request, feedback, 'again', csrf),
                         web.submit_feedback(request, aware, 'summary', '核对', 'new-feedback', csrf)):
            assert response.status_code == 403
        assert conn.execute('SELECT COUNT(*) FROM runs').fetchone()[0] == count


def test_progress_shows_public_held_snapshot(web, owner_request, bound_book):
    _, conn, *_ = bound_book
    snapshot = overview.holdings_snapshot(conn, '600001', frozen_at=db.now())
    snapshot['account']['account_id'] = 'private-account-marker'
    snapshot['weight']['total_assets'] = '981234567.89'  # Privacy marker.
    run = service.submit(conn, '600001', 'held-progress', holdings_context=True, holdings_snapshot=snapshot)
    html = web.run_page(owner_request('/'), run).body.decode()
    assert '受理时冻结持仓快照（在仓' in html and f"{snapshot['position']['shares']} 股" in html
    assert f"持有 {snapshot['position']['holding_days']} 天" in html and snapshot['weight']['reason'] in html
    assert 'private-account-marker' not in html and '981234567.89' not in html


def test_stock_page_with_only_holdings_reports_keeps_history_without_card_or_vitals(web, owner_request):
    conn = web.conn
    report_id = _latest_blind(conn, '603606')
    aware = _aware_copy(conn, report_id, '2026-11-26T10:00:00+08:00')
    page = web.stock_page(owner_request('/stock/603606'), '603606')
    assert page.context['card']['id'] == report_id
    assert page.context['vitals_report'] is None or page.context['vitals_report']['input_mode'] == 'blind'
    conn.execute("UPDATE runs SET request=json_set(COALESCE(NULLIF(request,'null'),'{}'),"
                 "'$.holdings_context',json('true')) WHERE company_key='603606'")
    for selected in (None, aware):
        page = web.stock_page(owner_request('/stock/603606'), '603606', selected)
        assert page.context['card'] is None and page.context['vitals'] is None
        html = page.body.decode()
        assert '暂无盲研究论点卡' in html and f'href="/reports/{aware}"' in html


# ---------- snapshot freezing ----------

def test_snapshot_is_frozen_once_per_request_and_inherited_by_retries(conn):
    run_id = service.submit(conn, '603606', 'aware', holdings_context=True)
    saved = conn.execute('SELECT created_at, request FROM runs WHERE id=?', (run_id,)).fetchone()
    request = json.loads(saved['request'])
    snapshot = request['holdings_snapshot']
    assert snapshot['frozen_at'] == saved['created_at'] and snapshot['status'] == 'unverified' and snapshot['reason']
    # Same intent returns the original run and snapshot; a different question or mode conflicts.
    assert service.submit(conn, '603606', 'aware', holdings_context=True) == run_id
    for change in ({'question': '新问题', 'holdings_context': True}, {}):
        with pytest.raises(service.RequestError) as error:
            service.submit(conn, '603606', 'aware', **change)
        assert error.value.status == 409
    assert conn.execute('SELECT request FROM runs WHERE id=?', (run_id,)).fetchone()[0] == saved['request']
    # A blind request keeps its pre-contract payload byte for byte.
    blind = service.submit(conn, '603606', 'blind')
    assert json.loads(conn.execute('SELECT request FROM runs WHERE id=?', (blind,)).fetchone()[0]) == {
        'question': '', 'parent_report_id': None, 'feedback': None}
    with pytest.raises(service.RequestError):
        service.submit(conn, '603606', 'blind-with-snapshot', holdings_snapshot=snapshot)

    db.update_run(conn, run_id, status='failed')
    retried = service.retry_run(conn, run_id)
    assert json.loads(conn.execute('SELECT request FROM runs WHERE id=?', (retried,)).fetchone()[0]) == request


def test_held_unheld_and_unverified_come_from_stored_ledger_records(bound_book):
    _, conn, _, _, _ = bound_book
    frozen_at = db.now()
    held = overview.holdings_snapshot(conn, '600001', frozen_at=frozen_at)
    unheld = overview.holdings_snapshot(conn, '603606', frozen_at=frozen_at)
    assert held['status'] == 'held' and unheld['status'] == 'unheld'
    assert held['account'] == unheld['account'] and held['account']['account_id'] == 'a'
    position = held['position']
    assert {'shares', 'cost_price', 'buy_date', 'holding_days', 'theses'} <= position.keys()
    assert all({'version', 'l1', 'l2'} <= thesis.keys() for thesis in position['theses'])
    # No stored close or verified total-asset record: the weight is a recorded gap, not an estimate.
    assert held['weight']['weight_pct'] is None and held['weight']['reason']
    assert any(gap.startswith('仓位权重缺失') for gap in held['gaps'])
    conn.execute("UPDATE account_source SET mode='mirror' WHERE id=1")
    assert overview.holdings_snapshot(conn, '600001', frozen_at=frozen_at)['status'] == 'unverified'


def test_weight_needs_a_stored_close_and_a_verified_total():
    position = {'shares': 100}
    close = {'last_close': 41.0, 'last_close_date': '2026-11-25'}
    asset = {'portfolio_value': '10250', 'portfolio_value_as_of': '2026-11-25', 'source': 'observation',
             'record_id': 7, 'recorded_at': '2026-11-25T15:00:00+08:00'}
    weight = overview._weight(position, close, asset)
    assert (weight['weight_pct'], weight['close_date'], weight['total_assets_as_of']) == (40.0, '2026-11-25', '2026-11-25')
    undated = overview._weight(position, close, asset | {'portfolio_value_as_of': None})
    assert undated['weight_pct'] is None and '缺少估值时点' in undated['reason']
    assert overview._weight(position, close, None)['reason'] == '没有已核实的账户总资产记录'
    missing = overview._weight(position, {'last_close': None, 'last_close_date': None}, asset)
    assert missing['weight_pct'] is None and missing['reason'] == '没有已保存的收盘价'
    assert overview._weight(position, close, asset | {'portfolio_value': '0'})['reason'] == '已核实总资产不为正'


def test_snapshot_renders_for_research_without_account_identity_and_survives_replay(conn):
    from support.reports import MATERIAL
    from copy import deepcopy

    blind = deepcopy(MATERIAL)
    aware = deepcopy(MATERIAL)
    held = {'code': '603606', 'frozen_at': '2026-11-26T10:00:00+08:00', 'status': 'held',
            'account': {'account_id': 'secret-account', 'version': 9, 'source_updated_at': 'x'},
            'position': {'cycle_id': 'c1', 'name': '东方电缆', 'shares': 300, 'cost_price': '38.5',
                         'buy_date': '2026-06-02', 'holding_days': 177, 'revision_id': 4,
                         'theses': [{'version': 2, 'l1': '海缆订单', 'l2': '毛利率', 'created_at': '2026-06-02'}]},
            'monitor': None, 'weight': {'weight_pct': None, 'reason': '没有已核实的账户总资产记录',
                                        'total_assets': None, 'close': 41.0, 'close_date': '2026-11-25'},
            'gaps': ['本次持仓周期暂无可引用的监控评估']}
    aware.extra |= {'holdings_context': True, 'holdings_snapshot': held}
    block = aware.prompt_block()
    assert block.startswith(blind.prompt_block().split('\n# 程序计算依据')[0])
    assert '知仓研究' not in blind.prompt_block() and '知仓研究' in block
    assert '海缆订单' in block and '受理时在仓' in block and '不能推定企业价值' in block
    assert 'frozen_at' not in block
    assert 'secret-account' not in block and 'revision_id' not in block
    assert aware.snapshot()['sha256'] != blind.snapshot()['sha256']

    run_id = db.create_run(conn, aware.code, status='failed')
    db.save_material(conn, run_id, aware.snapshot())
    replayed = materials.replay_material(conn, run_id)
    assert replayed.extra['holdings_snapshot'] == held and replayed.prompt_block() == block
    code, source_id, request = research._replay_target(conn, run_id)
    assert (code, source_id, request) == (aware.code, run_id, {'holdings_context': True, 'holdings_snapshot': held})
    blind_target = db.create_run(conn, code, status='running')
    research.execute(conn, blind_target, replay_of=run_id)
    failure = conn.execute('SELECT status,error,material FROM runs WHERE id=?', (blind_target,)).fetchone()
    assert failure['status'] == 'failed' and '输入模式' in failure['error'] and failure['material'] is None


def test_replay_cli_records_mode_at_insert_and_releases_batch_pages(conn, monkeypatch):
    import sys
    import weakref
    from copy import deepcopy
    from support.reports import MATERIAL

    sources = []
    for aware in (True, False):
        material = deepcopy(MATERIAL)
        if aware:
            material.extra.update(holdings_context=True, holdings_snapshot=SNAPSHOT)
        source = db.create_run(conn, material.code, status='failed')
        db.save_material(conn, source, material.snapshot())
        sources.append(source)
    conn.execute('CREATE TEMP TABLE accepted(request TEXT)')
    conn.execute('CREATE TEMP TRIGGER record_insert AFTER INSERT ON runs '
                 'BEGIN INSERT INTO accepted VALUES (NEW.request); END')
    refs = []
    read = research.replay_material

    def read_one(connection, run):
        assert all(ref() is None for ref in refs)
        material = read(connection, run)
        refs.append(weakref.ref(material))
        return material

    def check_target(connection, run_id, *, replay_of):
        assert all(ref() is None for ref in refs)
        assert service.run_input_mode(connection, run_id) == ('holdings_context' if replay_of == sources[0] else 'blind')

    monkeypatch.setattr(research, 'replay_material', read_one)
    monkeypatch.setattr(research, 'execute', check_target)
    monkeypatch.setattr(db, 'connect', lambda: conn)
    monkeypatch.setattr(research.eval_report, 'report_runs', lambda ids: None)
    monkeypatch.setattr(sys, 'argv', ['research', '--replay', *map(str, sources)])
    research.main()
    aware, blind = [row[0] for row in conn.execute('SELECT request FROM accepted ORDER BY rowid')]
    assert json.loads(aware) == {'holdings_context': True, 'holdings_snapshot': SNAPSHOT}
    assert blind is None


# ---------- isolation of blind research ----------

def test_blind_inputs_and_readers_never_use_a_holdings_report(conn):
    blind = _latest_blind(conn, '603606')
    reusable = screening.reusable_report(conn, '603606.SH', '2026-10-20')['id']
    text = reading.report(conn, blind)['body']
    aware = _aware_copy(conn, blind, '2026-11-26T10:00:00+08:00')

    assert _latest_blind(conn, '603606') == blind
    assert service.company_card(conn, '603606')['report_id'] == blind
    assert screening.reusable_report(conn, '603606.SH', '2026-10-20')['id'] == reusable
    assert research._prior_report(conn, '603606', 10 ** 6)['report_id'] == blind
    assert research._prior_report(conn, '603606', 10 ** 6, holdings=True)['report_id'] == aware
    assert reading.latest_reports(conn, codes=['603606'])[0]['id'] == blind
    assert aware not in {row['id'] for row in reading.api_reports(conn, '603606')}
    phrase = json.loads(text)['risks'][0]['text'][:12]
    hits, _ = reading.search_items(conn, phrase, code='603606', history=True)
    assert hits and aware not in {hit['report_id'] for hit in hits}
    # The company page lists both, labelled; the blind card and default head stay blind.
    modes = {row['id']: row['input_mode'] for row in reading.stock_reports(conn, '603606')}
    assert modes[aware] == 'holdings_context' and modes[blind] == 'blind'
    view = service.report_view(conn, aware)
    assert view['input_mode'] == 'holdings_context' and view['holdings_snapshot']['status'] == 'unheld'
    assert service.report_view(conn, blind)['holdings_snapshot'] is None


def test_feedback_follows_the_parent_mode_and_blind_never_descends_from_holdings(conn):
    blind = _latest_blind(conn, '603606')
    aware = _aware_copy(conn, blind, '2026-11-26T10:00:00+08:00')
    with pytest.raises(service.RequestError) as error:
        service.submit(conn, '603606', 'blind-child', parent_report_id=aware, question='追问')
    assert error.value.status == 409
    with pytest.raises(service.RequestError) as error:
        service.submit(conn, '603606', 'switch', parent_report_id=blind, holdings_context=True,
                       feedback={'location': 'summary', 'text': '核对'})
    assert error.value.status == 409
    # A holdings-aware follow-up may build on blind research; it projects only public request fields.
    child = service.submit(conn, '603606', 'aware-child', parent_report_id=blind, holdings_context=True, question='追问')
    assert json.loads(conn.execute('SELECT request FROM runs WHERE id=?', (child,)).fetchone()[0])['holdings_context']
    context = research._research_context(conn, '603606', aware, holdings=True)
    assert context[0]['report_id'] == aware
    assert not {'holdings_context', 'holdings_snapshot'} & context[0]['request'].keys()


def test_holdings_corrections_neither_join_nor_retire_blind_methods(tmp_path):
    captured = json.loads((DATA / 'research-methods.json').read_text())['reports']
    conn = db.connect(tmp_path / 'app.db')
    for row in captured:
        conn.execute('INSERT INTO runs(id,code,status,created_at,request) VALUES (?,?,?,?,?)',
                     (row['run_id'], row['code'], 'done', row['created_at'], json.dumps(row['request'])))
        conn.execute('INSERT INTO reports(id,run_id,code,period,passed,body,created_at) VALUES (?,?,?,?,?,?,?)',
                     (row['id'], row['run_id'], row['code'], row['period'], row['passed'],
                      json.dumps(row['body']), row['created_at']))
    keys = lambda: {(r['report_id'], r['location']) for r in research_methods(conn, '现金流 口径')}
    assert (12, 'methods[0]') in keys() and (11, 'methods[0]') not in keys()
    # Marked variant: the reviewed correction (run 18) was accepted as holdings-aware research.
    conn.execute("UPDATE runs SET request=json_set(request, '$.holdings_context', json('true')) WHERE id=18")
    assert (12, 'methods[0]') not in keys() and (11, 'methods[0]') in keys()
    blind_context = research._research_context(conn, '600519.SH', None)
    assert 12 not in {entry['report_id'] for entry in blind_context}
    assert 12 in {entry['report_id'] for entry in research._research_context(conn, '600519.SH', None, holdings=True)}
    conn.close()


# ---------- scoring ----------

def test_blind_and_holdings_first_samples_do_not_displace_each_other(tmp_path, monkeypatch):
    row = json.loads((DATA / 'valuation-warning-reports-20261105.json').read_text())['reports'][0]
    forecast = json.loads((DATA / 'k7a-forecast-report.json').read_text())['report']
    body = row['body'] | {'prior': {'report_id': 1, 'period': '2026-06-30'}, 'prior_status': [],
                          'table': forecast['body']['table'], 'checkpoints': forecast['body']['checkpoints']}
    body['judgment'] = (body.get('judgment') or {}) | {'contract': 12}
    blind = {'question': '现金流', 'feedback': None}
    conn = db.connect(tmp_path / 'app.db')
    # Marked variant: holdings-aware 700 lands first, the blind rerun 701 later the same day.
    later = row['created_at'][:11] + '23:59:00+08:00'
    for report_id, created, request in ((700, row['created_at'], blind | {'holdings_context': True}),
                                        (701, later, blind)):
        conn.execute('INSERT INTO runs(id,kind,code,status,created_at,request) VALUES (?,?,?,?,?,?)',
                     (report_id, 'research', row['code'], 'done', created, json.dumps(request)))
        conn.execute('INSERT INTO reports(id,run_id,code,period,passed,body,created_at) VALUES (?,?,?,?,?,?,?)',
                     (report_id, report_id, row['code'], row['period'], 1, json.dumps(body), created))
    conn.commit()
    conn.close()
    path = str(tmp_path / 'app.db')

    assert {(r['id'], r['source']) for r in validation.load_reports(path, 12)} == {
        (700, 'holdings_context'), (701, 'active')}
    firsts = {(f['report'], validation.row_mode(f)) for f in validation.load_forecasts(path, 12) if f['first']}
    assert {700, 701} == {report for report, _ in firsts} and (700, 'holdings_context') in firsts
    review = validation.forecast_review([dict(f, result={'status': 'pending'}) for f in validation.load_forecasts(path)])
    assert {validation.row_mode(group) for group in review['model_groups']} == {'blind', 'holdings_context'}
    assert {r['id'] for r in agenda.sample(validation.load_agenda(path, 12))['picked']} == {700, 701}
    assert {validation.row_mode(r) for r in zones.load_reports(path, 12)} == {'blind', 'holdings_context'}
    assert zones.load_reports(path, 11) == []
    import sys
    output = tmp_path / 'scores.json'

    def cli(main, *args):
        monkeypatch.setattr(sys, 'argv', ['scores', '--database', path, '--contract', '12',
                                          '--json', str(output), *args])
        main()
        return json.loads(output.read_text())

    result = cli(validation.main, '--forecasts', '--frozen')
    assert {(g['contract'], validation.row_mode(g), g['reports']) for g in result['model_groups']} == {
        (12, 'blind', 1), (12, 'holdings_context', 1)}
    assert result['summary'] == result['holdings_context']['summary']
    assert result['summary']['total'] == 2
    assert result['confidence_coverage'] == result['holdings_context']['confidence_coverage']
    # The captured benchmark ends before these reports. Use its through-date for the offline calendar.
    capture = json.loads((DATA / 'validation_v0.json').read_text())
    calendar = [day for day in capture['trade_cal'] if day <= max(capture['benchmark'])]
    monkeypatch.setattr(validation.market, 'client', lambda: object())
    monkeypatch.setattr(validation.market, 'trade_days', lambda pro, lo, hi: [d for d in calendar if lo <= d <= hi])
    assert {r['id'] for r in cli(validation.main)['rows']} == {700, 701}
    stances = cli(validation.main, '--stances')
    assert stances['summary'] == stances['holdings_context']['summary']
    assert sum(g['n'] for g in stances['summary'].values()) == 1
    agenda_scores = cli(validation.main, '--agenda')
    assert agenda_scores['groups'] == agenda_scores['holdings_context']['groups']
    assert sum(g['pending_entry'] for g in agenda_scores['groups'].values()) == 1
    monkeypatch.setattr(zones, 'load_market', lambda *args: (calendar, {row['code']: {
        'pe': {}, 'close': {}, 'adjusted': {}}}, {}))
    zone_scores = cli(zones.main)
    assert zone_scores['reports'] == zone_scores['holdings_context_reports'] == 1


def test_size_cut_points_stay_with_the_contract_and_input_mode():
    capture = json.loads((DATA / 'screen-small-rows.json').read_text())
    values = sorted({float(row['total_mv']) / 10000 for row in capture['small_market_rows']})[:4]
    blind = [{'contract': 12, 'source': 'active', 'mv': v} for v in values]
    aware = blind[0] | {'source': 'holdings_context'}
    earlier = blind[0] | {'contract': 11}
    validation.assign_size_buckets([*blind, aware, earlier])
    assert [r['size'] for r in blind] == ['小', '小', '中', '大']


def test_policy_cli_rejects_aware_report_and_status_by_id(conn, tmp_path, monkeypatch, capsys):
    from a_share_research.policy.v0 import research_client

    blind = _latest_blind(conn, '603606')
    aware = _aware_copy(conn, blind, '2026-11-26T10:00:00+08:00')
    conn.commit()
    monkeypatch.setattr(research_client.paths, 'cache_db_path', lambda: tmp_path / 'scans.db')
    for report_id, expected in ((aware, 1), (blind, 0)):
        run_id = conn.execute('SELECT run_id FROM reports WHERE id=?', (report_id,)).fetchone()[0]
        for args in (['report', str(report_id)], ['report', str(report_id), '--compact'], ['status', str(run_id)]):
            assert research_client.cmd_research(args) == expected
            captured = capsys.readouterr()
            if expected:
                assert '403' in captured.err and not captured.out
            else:
                assert json.loads(captured.out)['version'] == 'research-v1'
