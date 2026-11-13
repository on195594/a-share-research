"""Controlled acquisition against captured annual-report excerpts.

Network replacements replay those excerpts; malformed variants are rejection tests,
not synthetic research runs or live acquisition acceptance.
"""
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import subprocess
import sys

import pytest

from a_share_research import db, evidence, sources
from a_share_research import external_acquisition as adapter
from a_share_research.material import Material
from a_share_research.research_tools import TOOLS
from support.reports import MATERIAL


ARGS = {'label': '经营现金流同比', 'operation': 'growth_pct',
        'left': {'ref': 'T.op_cash', 'period': '2025-12-31'},
        'right': {'ref': 'T.op_cash', 'period': '2024-12-31'}}


def request(snapshot=None, name='calculate', args=None):
    return {'action': 'acquire', 'material': snapshot or MATERIAL.snapshot(),
            'tool': name, 'arguments': ARGS if args is None else args}


@pytest.mark.parametrize('name,args', [
    ('shell', {'command': 'true'}), ('calculate', []),
    ('calculate', ARGS | {'left': ARGS['left'] | {'shell': 'true'}}),
    ('calculate', ARGS | {'holding_years': True}),
    ('calculate', ARGS | {'holding_years': 0}),
    ('calculate', ARGS | {'holding_years': 51}),
    ('calculate', ARGS | {'left': {'value': math.inf}}),
    ('calculate', ARGS | {'left': {'value': math.nan}}),
    ('calculate', ARGS | {'left': {'value': False}}),
    ('calculate', ARGS | {'left': {'page': True}}),
    ('calculate', ARGS | {'left': {'assumption': ''}}),
    ('calculate', ARGS | {'operation': 'eval'}),
    ('announcements', {'code': '600519', 'days': 551}),
    ('announcements', {'code': '600519', 'days': 1.5}),
    ('announcements', {'code': '600519', 'days': True}),
    ('announcements', {'code': 'https://example.com/'}),
    ('announcements', {'code': '600519', 'query': 'a' * 41}),
    ('announcements', {'code': '600519', 'url': 'file:///etc/passwd'}),
    ('read_disclosure', {'source': 'D0', 'count': 0}),
    ('read_disclosure', {'source': 'D0', 'count': 6}),
    ('read_disclosure', {'source': 'D0', 'page': 0}),
    ('read_disclosure', {'source': 'D0', 'query': []}),
    ('read_disclosure', {'source': 'D0', 'path': '/tmp/file'}),
    ('macro', {'series': 'sql'}), ('macro', {'series': 'pmi', 'count': 25}),
    ('commodities', {'exchange': 'UNKNOWN'}), ('financials', {}),
    ('peers', {'code': '600519', 'level': 'l4'}),
    ('research_methods', {'query': ''}),
    ('research_methods', {'query': 'a' * 81}),
    ('valuation', {'valuation': {'nested': math.inf}}),
    ('valuation', {'valuation': {1: 'value'}}),
    ('valuation', {'valuation': {'large': 'x' * 65_536}}),
])
def test_strict_boundaries_before_any_network(name, args):
    with pytest.raises(adapter.InputError):
        adapter.validate_call(name, args)


def test_every_frozen_tool_schema_is_supported():
    examples = {
        'industry_trend': {'code': '600519'}, 'macro': {'series': 'pmi'},
        'commodities': {}, 'commodity': {'code': 'AL.SHF'},
        'research_methods': {'query': '现金'}, 'announcements': {'code': '600519'},
        'peers': {'code': '600519'}, 'disclosures': {'code': '600519'},
        'read_disclosure': {'source': 'D0'}, 'financials': {'code': '600519'},
        'valuation': {'valuation': {}}, 'calculate': ARGS,
    }
    assert set(examples) == {tool['name'] for tool in TOOLS}
    for name, args in examples.items():
        adapter.validate_call(name, args)


def test_calculation_is_real_program_output_and_chains_across_process_snapshots():
    original = deepcopy(MATERIAL.snapshot())
    first = adapter.execute(request(original))
    assert original == MATERIAL.snapshot()  # caller's held snapshot never mutates
    assert first['result']['id'] == 'C.1'
    assert first['result']['values'] == {'result': '-33.46'}
    second = adapter.execute(request(first['material'], args={
        'label': '同比变动率复用', 'operation': 'sum',
        'left': {'ref': 'C.1'}, 'right': {'ref': 'C.1'},
    }))
    assert second['result']['id'] == 'C.2'
    assert second['result']['calculation']['inputs'][0]['ref'] == 'C.1'
    first_material, second_material = map(Material.from_snapshot, [first['material'], second['material']])
    adapter.validate_extension(first_material, second_material)
    assert second_material.table[:-1] == first_material.table
    assert second_material.pages == MATERIAL.pages
    assert second_material.extra['tools'][-1]['stage'] == 'external'
    assert second_material.extra['tools'][-1]['result'] == second['result']


def test_unseen_company_open_uses_gather_without_research_run(monkeypatch, tmp_path):
    # Structural boundary replay of captured material, not a live gather claim.
    calls = []
    def gather(code):
        calls.append(code)
        return deepcopy(MATERIAL)
    monkeypatch.setattr(adapter.materials, 'gather', gather)
    monkeypatch.setattr(db, 'DATA', tmp_path)
    result = adapter.execute({'action': 'open', 'code': '600519'})
    assert result['material'] == MATERIAL.snapshot()
    assert result['result'] == {'company_key': '600519'} and calls == ['600519']
    assert not (tmp_path / 'app.db').exists()
    with pytest.raises(evidence.EvidenceError):
        adapter.execute({'action': 'open', 'code': '600938'})


@pytest.mark.parametrize('value', [
    {}, {'action': 'open', 'code': '600519', 'source_run_id': 1},
    {'action': 'open', 'code': '600519.SH'}, {'action': 'open', 'code': 600519},
    {'action': 'open', 'code': '600519\n'},
    {'action': 'acquire', 'material': {}, 'tool': 'calculate'},
    {'action': 'shell', 'command': 'true'}, [],
])
def test_request_shape_rejects_uncontrolled_arguments(value):
    with pytest.raises(adapter.InputError):
        adapter.execute(value)


def test_read_new_page_preserves_held_page_and_source_identity(monkeypatch):
    material = deepcopy(MATERIAL)
    material.pages = {9: material.pages[9]}
    def replay(doc):
        # Empty unselected pages are only positional padding of the real excerpt fixture.
        doc.pages = [''] * 10
        doc.raw_pages = [''] * 10
        for number, text in MATERIAL.pages.items():
            doc.pages[number - 1] = text
            doc.raw_pages[number - 1] = MATERIAL.raw_pages.get(number, text)
    monkeypatch.setattr(sources, 'fetch', replay)
    result = adapter.execute(request(material.snapshot(), 'read_disclosure', {'source': 'D0', 'page': 10, 'count': 1}))
    parsed = Material.from_snapshot(result['material'])
    assert parsed.pages == MATERIAL.pages
    assert parsed.pages[9] == material.pages[9]
    assert result['result']['pages'] == {10: MATERIAL.pages[10]}
    assert parsed.doc.info() == MATERIAL.doc.info()


def test_replayed_announcements_keep_d_ids_across_calls(monkeypatch):
    # Same captured document metadata replayed as discovery; no manufactured new facts.
    def announcements(code, query, days):
        return [deepcopy(MATERIAL.doc)], {'truncated': False}
    monkeypatch.setattr(sources, 'announcements', announcements)
    first = adapter.execute(request(name='announcements', args={'code': '600519'}))
    second = adapter.execute(request(first['material'], 'announcements', {'code': '600519'}))
    assert first['result']['documents'] == second['result']['documents']
    assert set(second['result']['documents']) == {'D0'}
    assert Material.from_snapshot(second['material']).pages == MATERIAL.pages


@pytest.mark.parametrize('tamper', ['page', 'raw', 'identity', 'table', 'duplicate', 'removed'])
def test_changed_evidence_is_rejected(tamper):
    before = deepcopy(MATERIAL)
    after = deepcopy(MATERIAL)
    if tamper == 'page': after.pages[9] = '篡改原文'
    if tamper == 'raw': after.raw_pages[9] = '篡改原文'
    if tamper == 'identity': after.doc.period = '2000-12-31'
    if tamper == 'table': after.table[0]['values']['2025-12-31'] = '0'
    if tamper == 'duplicate': after.table.append(deepcopy(after.table[0]))
    if tamper == 'removed': after.table.pop()
    with pytest.raises(evidence.EvidenceError):
        adapter.validate_extension(before, after)


def test_research_methods_uses_readonly_database(monkeypatch, tmp_path):
    conn = db.connect(tmp_path / 'app.db')
    conn.close()
    monkeypatch.setattr(db, 'DATA', tmp_path)
    original = adapter.ResearchTools.research_methods
    def verify_readonly(self, query):
        with pytest.raises(Exception, match='readonly'):
            self.conn.execute('CREATE TABLE should_not_exist(id INTEGER)')
        return original(self, query)
    monkeypatch.setattr(adapter.ResearchTools, 'research_methods', verify_readonly)
    result = adapter.execute(request(name='research_methods', args={'query': '现金'}))
    assert result['result'] == []


def test_cli_real_calculation_protocol_does_not_create_database(tmp_path):
    result = subprocess.run([sys.executable, '-m', 'a_share_research.external_acquisition'],
        input=json.dumps(request()), text=True, capture_output=True, timeout=30,
        env=os.environ | {'DATA_DIR': str(tmp_path)}, cwd=Path(__file__).parents[1])
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output['result']['values'] == {'result': '-33.46'}
    assert set(output) == {'material', 'result'}
    assert not (tmp_path / 'app.db').exists()


def test_cli_failure_has_no_success_envelope_or_traceback():
    result = subprocess.run([sys.executable, '-m', 'a_share_research.external_acquisition'],
        input=json.dumps({'action': 'open', 'code': 'file:///secret'}), text=True,
        capture_output=True, timeout=30, cwd=Path(__file__).parents[1])
    assert result.returncode == 1
    failure = json.loads(result.stdout)
    assert set(failure) == {'error'} and failure['error']['code'] == 'invalid_arguments'
    assert '公司代码' in failure['error']['message'] and 'Traceback' not in result.stderr
    assert 'file:///secret' not in result.stdout + result.stderr


def test_held_peer_financials_reused_without_duplicate_or_refresh(monkeypatch):
    # Same real Moutai rows held as a peer in a structural 600938 session.
    material = deepcopy(MATERIAL)
    rows = deepcopy(material.table)
    for row in rows:
        row['id'] = 'P600519.' + row['id']
        row['source'] = {'provider': 'TuShare', 'code': '600519', 'retrieved_at': '2026-10-20'}
    material.code = material.doc.code = '600938.SH'
    material.table = rows
    def no_refetch(*args):
        raise AssertionError('held financials must not be presented as a fresh collection')
    monkeypatch.setattr(sources, 'financials', no_refetch)
    first = adapter.execute(request(material.snapshot(), 'financials', {'code': '600519'}))
    second = adapter.execute(request(first['material'], 'financials', {'code': '600519'}))
    assert first['result'] == second['result'] == rows
    assert Material.from_snapshot(second['material']).table == rows
    assert first['result'][0]['source']['retrieved_at'] == '2026-10-20'


def test_deterministic_error_detail_is_bounded_and_redacts_credentials(monkeypatch):
    monkeypatch.setenv('TUSHARE_TOKEN', 'fixture-secret-market-token')
    monkeypatch.setenv('MODEL_CUSTOM_KEY', 'fixture-secret-model-key')
    message = adapter.safe_detail(
        '可用期间：2024-12-31、2025-12-31 fixture-secret-market-token fixture-secret-model-key '
        'https://provider.invalid/path?api_key=unknown token=unknown sk-secret asr_secret\n' + '字' * 2000)
    assert '可用期间：2024-12-31、2025-12-31' in message
    assert len(message) == 1024 and message.isprintable()
    for secret in ('fixture-secret', 'provider.invalid', 'unknown', 'sk-secret', 'asr_secret'):
        assert secret not in message


def test_provider_value_error_is_not_a_public_argument_error(monkeypatch):
    def fail(code):
        raise ValueError('provider credential=secret response body')
    monkeypatch.setattr(adapter.materials, 'gather', fail)
    with pytest.raises(ValueError) as raised:
        adapter.execute({'action': 'open', 'code': '600519'})
    assert not isinstance(raised.value, adapter.InputError)
