"""Offline tool/protocol integration over captured disclosures, never model-quality claims.

Replies are explicit protocol inputs like test_review_evidence.Replies. The actual reader,
calculator, snapshot store, checks and ledger run unchanged; no network/model is invoked.
"""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from a_share_research import db, evidence, research, sources
from a_share_research.material import mdna_pages
from a_share_research.research_tools import Acquisition, CALL_LIMIT, FETCH_LIMIT, _fetches
from a_share_research.reviewing import ReviewLedger, ReviewFormatError, digest
from support.reports import MATERIAL, CASH, SALES_ROW, report
from test_review_evidence import BODY, REVIEW, captured_registry, issue, payload, accept, pending_ledger


class ProtocolInputs(research.Runner):
    """Feed explicit offline writer/reviewer messages; preserve calls and prompt audit in SQLite."""
    def __init__(self, conn, run_id):
        super().__init__(conn, run_id)
        self.responses = []
        self.offered = []

    def call(self, step, prompt, *, messages=None, tools=None):
        if self.calls >= self.max_calls:
            raise research.ResearchError('模型调用次数超过上限')
        self.stage = step
        self.calls += 1
        self.offered.append([t['name'] for t in tools or []])
        value = self.responses.pop(0)
        value = value() if callable(value) else value
        if isinstance(value, Exception):
            raise value
        if isinstance(value, list):
            message, text = {'role': 'assistant', 'content': value}, ''
        else:
            text = json.dumps(value, ensure_ascii=False)
            message = {'role': 'assistant', 'content': [{'type': 'text', 'text': text}]}
        db.log_call(self.conn, self.run_id, step, prompt, response=json.dumps(message))
        return SimpleNamespace(message=message, text=text)


def read(page, **kwargs):
    return {'type': 'toolCall', 'id': f'read-{page}', 'name': 'read_disclosure',
            'arguments': {'source': 'D0', 'page': page, 'count': 1, **kwargs}}


@pytest.fixture
def session(tmp_path):
    material = research.Material.from_snapshot(deepcopy(MATERIAL.snapshot()))
    # Only these genuine captured page fragments are requested; empty entries are unread pages.
    material.doc.pages = [MATERIAL.pages.get(p, '') for p in range(1, 11)]
    material.doc.raw_pages = [MATERIAL.raw_pages.get(p, MATERIAL.pages.get(p, '')) for p in range(1, 11)]
    conn = db.connect(tmp_path / 'isolated.db')
    runner = ProtocolInputs(conn, db.create_run(conn, material.code, status='running'))
    acquisition = Acquisition(runner, material)
    db.save_material(conn, runner.run_id, material.snapshot())
    yield runner, material, acquisition
    conn.close()


def bound(material, draft, **fields):
    return {'version': 1, 'draft_sha256': digest(draft), 'material_sha256': material.snapshot()['sha256'],
            'issues': [], 'resolutions': [], 'risk_resolutions': []} | fields


def test_review_missing_previous_page_can_acquire_repair_and_close_with_new_binding(session):
    runner, material, acquisition = session
    material.pages.pop(9)
    material.raw_pages.pop(9)
    original = report()
    original['business'] = deepcopy(original['risks'])
    original['financial'] = deepcopy(original['risks'])
    fixed = report()
    ledger = ReviewLedger()
    missing = {'type': 'question', 'where': 'financial[0]', 'problem': '需补读销售费用原文',
               'importance': '财务解释须有正式披露支持', 'evidence': []}
    runner.responses = [lambda: bound(material, original, issues=[missing]), [read(9)], {'ops': [{'op': 'replace', 'where': f'{section}[0]', 'value': fixed[section][0]}
                                          for section in ('business', 'financial')]},
                        lambda: bound(material, fixed, resolutions=[{
                            'id': ledger.pending()[0]['id'], 'status': 'resolved', 'reason': '补读并引用销售费用表',
                            'targets': ['financial[0]'], 'evidence': [SALES_ROW]}])]
    old_hash = material.snapshot()['sha256']
    result, problems, reviewed = research.revise(runner, material, original, ledger, acquisition=acquisition)
    assert result == fixed and problems == [] and reviewed
    assert runner.calls == 4 and len(material.extra['tools']) == 1
    assert material.pages[9] == MATERIAL.pages[9]
    latest = material.snapshot()['sha256']
    assert old_hash != latest
    saved = runner.conn.execute('SELECT material, identity FROM runs WHERE id=?', (runner.run_id,)).fetchone()
    assert research.Material.from_snapshot(json.loads(saved['material'])).snapshot()['sha256'] == latest
    assert json.loads(saved['identity'])['material_sha256'] == latest
    resolved = ledger.snapshot()['issues'][0]
    assert resolved['material_sha256'] == old_hash and resolved['reviewed_material_sha256'] == latest
    assert ledger.rounds[-1]['material_sha256'] == latest
    assert set(runner.offered[1]) == {'disclosures', 'announcements', 'read_disclosure', 'calculate'}


def test_already_read_evidence_only_needs_citation_repair_without_fetch(session):
    runner, material, acquisition = session
    original = report()
    original['financial'][0]['cites'] = []
    runner.responses = [{'ops': [{'op': 'replace', 'where': 'financial[0]', 'value': report()['financial'][0]}]},
                        lambda: bound(material, report())]
    result, problems, reviewed = research.revise(runner, material, original, ReviewLedger(), acquisition=acquisition)
    assert result == report() and not problems and reviewed
    assert material.extra['tools'] == [] and runner.calls == 2


def test_mechanical_failure_never_claims_fresh_review_or_closes_old_issue(session):
    runner, material, acquisition = session
    ledger = ReviewLedger()
    original = report()
    question = {'type': 'question', 'where': 'financial[0]', 'problem': '需核对费用原文',
                'importance': '影响财务解释', 'evidence': []}
    ledger.accept(bound(material, original, issues=[question]), original, material.source_registry(),
                  material.table, material.snapshot()['sha256'])
    pending = ledger.pending()
    bad = report()
    bad['financial'][0]['cites'] = []
    runner.responses = [{}, {}]
    _, problems, reviewed = research.revise(runner, material, bad, ledger, acquisition=acquisition)
    assert not reviewed and ledger.pending() == pending and len(ledger.rounds) == 1
    assert any(p.get('id') == pending[0]['id'] for p in problems)
    assert research._verified_facts(bad, problems, reviewed=reviewed) == {}


def test_frozen_repair_never_offers_tools_or_changes_material(session):
    runner, material, _ = session
    before = deepcopy(material.snapshot())
    runner.responses = [{}]
    assert research.repair(runner, material, report(), []) == report()
    assert runner.offered == [[]] and material.snapshot() == before


@pytest.mark.parametrize('kind', ['query_variants', 'failed_read', 'same_read'])
def test_no_new_evidence_stops_across_stages_and_does_not_reset_deadline(session, kind):
    runner, material, acquisition = session
    deadline = acquisition.deadline
    if kind == 'query_variants':
        calls = [read(9, query='销售费用'), read(9, query='费用')]
    elif kind == 'failed_read':
        calls = [read(99), read(99)]
    else:
        calls = [read(9), read(9)]
    # All requested evidence is already saved; failed calls also cannot manufacture progress.
    runner.responses = [[calls[0]], report()]
    acquisition.write('已有披露', 'explore')
    runner.responses = [[calls[1]], report()]
    acquisition.write('修正引用', 'repair')
    assert acquisition.stale == 2 and acquisition.deadline == deadline and acquisition.rounds == 2
    assert runner.offered[-1] == []
    assert any(s['reason'] == 'no_new_evidence' and s['stage'] == 'repair' for s in material.extra['stop_reasons'])
    if kind == 'failed_read':
        assert len(acquisition.traces) == 1 and acquisition.traces[0]['error']
        assert _fetches(acquisition.traces) == 1  # one real failed attempt; repeat served from error cache


@pytest.mark.parametrize('limit', ['fetch', 'tools', 'rounds', 'time', 'model'])
def test_repair_uses_remaining_budget_and_never_expands_it(session, limit):
    runner, material, acquisition = session
    if limit == 'fetch':
        # Exercise actual cached reads, not made-up trace rows.
        for _ in range(FETCH_LIMIT):
            acquisition.tool_result(read(9), 'explore')
        count = len(acquisition.traces)
        result, failed = acquisition.tool_result(read(10), 'repair')
        assert failed and '额度' in result['error'] and len(acquisition.traces) == count
        return
    if limit == 'tools':
        call = {'name': 'calculate', 'arguments': {'label': '现金流差额', 'operation': 'difference',
                'left': {'ref': 'T.op_cash', 'period': '2025-12-31'},
                'right': {'ref': 'T.op_cash', 'period': '2024-12-31'}}}
        for _ in range(CALL_LIMIT):
            acquisition.tool_result(call, 'explore')
    elif limit == 'rounds':
        acquisition.rounds = 12
    elif limit == 'time':
        acquisition.deadline = 0
    else:
        runner.max_calls = 1
    runner.responses = [report()]
    assert acquisition.write('说明尚未取得的证据', 'repair') == report()
    assert runner.offered == [[]] and runner.calls == 1
    if limit == 'model':
        with pytest.raises(research.ResearchError):
            acquisition.write('不能自动追加调用', 'repair')
        assert runner.calls == 1


def test_snapshot_survives_interruption_after_tool_result(session):
    runner, material, acquisition = session
    material.pages.pop(9)
    material.raw_pages.pop(9)
    runner.responses = [[read(9)], research.ResearchError('中断')]
    with pytest.raises(research.ResearchError, match='中断'):
        acquisition.write('补读费用', 'repair')
    restored = research.replay_material(runner.conn, runner.run_id)
    assert restored.pages[9] == MATERIAL.pages[9] and restored.extra['tools'][0]['stage'] == 'repair'


@pytest.mark.parametrize('mutation', ['hash', 'page'])
def test_same_source_conflict_rejects_before_mutating_material(session, mutation):
    runner, material, acquisition = session
    before = deepcopy(material.snapshot())
    changed = deepcopy(material.doc)
    if mutation == 'hash':
        # Use real different captured PDF identities; do not synthesize financial content.
        captured = json.loads((Path(__file__).parent / 'data/calculation-precision.json').read_text())['603606']
        material.doc.sha256 = captured['document']['sha256']
        material.extra['catalog']['D0']['sha256'] = material.doc.sha256
        before = deepcopy(material.snapshot())
        changed.sha256 = '0' * 64
    else:
        changed.pages[8] = MATERIAL.pages[10]
    acquisition.toolkit.documents['D0'] = changed
    with pytest.raises(evidence.EvidenceError, match='冲突'):
        acquisition.tool_result(read(9), 'repair')
    after = deepcopy(material.snapshot())
    # Only the failed-attempt audit changes; source identity, old pages and table remain untouched.
    after['data']['extra']['tools'] = []
    assert after['data'] == before['data']
    assert len(acquisition.traces) == 1 and acquisition.traces[0]['error']


def test_cross_company_and_cross_period_source_pages_keep_identity(session):
    _, material, acquisition = session
    registry = captured_registry()
    for sid, captured in registry.items():
        target = 'D1' if sid == 'D0' else 'D2'
        doc = sources.Document(**captured['document'])
        pages = captured['pages']
        doc.pages = [pages.get(p, '') for p in range(1, max(pages) + 1)]
        doc.raw_pages = list(doc.pages)
        material.extra['catalog'][target] = doc.info()
        acquisition.toolkit.documents[target] = doc
        page = min(pages)
        result, failed = acquisition.tool_result(read(page, source=target), 'repair')
        assert not failed and result['document']['code'] == doc.code
        assert material.extra['sources'][target]['document'] == doc.info()
    assert material.source_registry()['D1']['document']['code'] != material.code
    assert material.source_registry()['D1']['document']['period'] != material.source_registry()['D2']['document']['period']


def test_ledger_rejects_old_material_binding_and_unknown_cannot_resolve_omission():
    ledger = pending_ledger()
    draft = REVIEW['report']
    response = payload(ledger, draft, resolutions=[{'id': ledger.pending()[0]['id'], 'status': 'resolved',
        'reason': '未知', 'targets': [], 'evidence': issue()['evidence']}])
    before = ledger.snapshot()
    with pytest.raises(ReviewFormatError):
        ledger.accept(response, draft, captured_registry(), BODY['table'], digest(MATERIAL.snapshot()))
    with pytest.raises(ReviewFormatError):
        accept(ledger, response)
    assert ledger.snapshot() == before


def test_repair_refuses_unoffered_tools_without_touching_sources(session):
    _, material, acquisition = session
    before = deepcopy(material.table)
    for tool in ('financials', 'peers'):
        result, failed = acquisition.tool_result({'name': tool, 'arguments': {'code': '600519'}}, 'repair')
        assert failed and '不刷新' in result['error']
    assert material.table == before


def test_unavailable_disclosure_and_unknown_gap_remain_unresolved_after_repairs(session):
    runner, material, acquisition = session
    ledger = ReviewLedger()
    original = report()
    unknown = report(gaps=['销售费用补充原文尚未取得。'])
    question = {'type': 'question', 'where': 'financial[0]', 'problem': '需补读销售费用原文',
                'importance': '仍需核对正式来源', 'evidence': []}
    def still_missing():
        return bound(material, unknown, resolutions=[{'id': ledger.pending()[0]['id'],
                     'status': 'unresolved', 'reason': '未能取得所需证据，缺口仍在'}])
    runner.responses = [lambda: bound(material, original, issues=[question]),
                        [read(99)], {'gaps': unknown['gaps']}, still_missing,
                        [read(99)], {'gaps': unknown['gaps']}, still_missing]
    result, problems, reviewed = research.revise(runner, material, original, ledger, acquisition=acquisition)
    assert result == unknown and reviewed and len(problems) == 1
    assert problems[0]['status'] == 'unresolved' and len(ledger.rounds) == 3
    assert runner.calls == 7 and len(acquisition.traces) == 1
    assert acquisition.stale == 2 and runner.offered[-2] == []


def test_run_call_guard_preserves_a_smaller_cap_and_never_exceeds_default(session):
    recorded, _, _ = session
    for requested, expected in ((1, 1), (99, research.MAX_CALLS)):
        runner = research.Runner(recorded.conn, recorded.run_id, max_calls=requested)
        assert runner.max_calls == expected
        runner.calls = expected
        with pytest.raises(research.ResearchError, match='上限'):
            runner.call('repair', '不可发出模型请求')
        assert runner.calls == expected


def test_renaming_a_calculation_does_not_manufacture_new_evidence(session):
    _, material, acquisition = session
    args = {'label': '现金流差额', 'operation': 'difference',
            'left': {'ref': 'T.op_cash', 'period': '2025-12-31'},
            'right': {'ref': 'T.op_cash', 'period': '2024-12-31'}}
    first, failed = acquisition.tool_result({'name': 'calculate', 'arguments': args}, 'explore')
    assert not failed
    before = acquisition.evidence_key()
    second, failed = acquisition.tool_result({'name': 'calculate', 'arguments': args | {'label': '经营现金流差额'}}, 'repair')
    assert not failed and first['id'] != second['id'] and acquisition.evidence_key() == before
    assert first['calculation']['requested_label'] == args['label']
    assert second['calculation']['requested_label'] == '经营现金流差额'
    assert acquisition.toolkit.run('calculate', args) == first  # original full-argument cache semantics
    assert len([r for r in material.table if r['id'].startswith('C.')]) == 2


def test_duplicate_disclosure_identity_and_conflicting_identity_are_distinct(session):
    _, material, acquisition = session
    original = deepcopy(material.snapshot())
    assert acquisition.toolkit._register(material.doc) == 'D0'
    assert material.snapshot() == original
    conflicting = deepcopy(material.doc)
    conflicting.period = BODY['sources']['D1']['document']['period']
    assert conflicting.period != material.doc.period
    with pytest.raises(evidence.EvidenceError, match='冲突'):
        acquisition.toolkit._register(conflicting)
    assert material.snapshot() == original


def test_diagnostics_separates_current_mechanical_failure_from_pending_history():
    from a_share_research import service
    binding = {'draft_sha256': digest(report()), 'material_sha256': MATERIAL.snapshot()['sha256'],
               'completed': False, 'pending_issue_ids': ['issue-1-existing'], 'prompt': 'not public'}
    body = {'failure': {'stage': 'check', 'category': 'citation_calculation'}, 'review_binding': binding,
            'problems': [{'where': 'financial[0]', 'problem': '引文缺失'}]}
    view = service.diagnostics('failed', body)
    assert view['failure']['stage'] == 'check' and view['review_binding']['completed'] is False
    assert 'prompt' not in view['review_binding']
    assert service.diagnostics('failed', {'problems': body['problems']})['review_binding'] is None


def test_newly_read_evidence_invalidates_failed_calculation_without_resetting_limits(session):
    _, material, acquisition = session
    material.pages.pop(9)
    material.raw_pages.pop(9)
    call = {'name': 'calculate', 'arguments': {
        'label': '销售费用占营收比', 'operation': 'ratio_pct',
        'left': {'source': 'D0', 'page': 9, 'quote': MATERIAL.raw_pages[9],
                 'value': '7,253,499,600.68', 'unit': '元', 'period': '2025-12-31', 'label': '销售费用'},
        'right': {'ref': 'T.revenue', 'period': '2025-12-31'}}}
    deadline = acquisition.deadline
    first, failed = acquisition.tool_result(call, 'explore')
    assert failed and len(acquisition.traces) == 1
    material.extra['gaps'].append('仍待核查销售费用原文')
    repeated, failed = acquisition.tool_result(call, 'repair')
    assert failed and repeated == first and len(acquisition.traces) == 1
    _, failed = acquisition.tool_result(read(9), 'repair')
    assert not failed
    result, failed = acquisition.tool_result(call, 'repair')
    assert not failed and result['values'] == {'result': '4.22'}
    assert len(acquisition.traces) == 3 and _fetches(acquisition.traces) == 1
    assert acquisition.deadline == deadline


def test_stop_state_is_saved_before_a_model_interruption(session):
    runner, material, acquisition = session
    acquisition.rounds = 12
    runner.responses = [research.ResearchError('中断')]
    with pytest.raises(research.ResearchError, match='中断'):
        acquisition.write('保留停止原因', 'repair')
    saved = research.replay_material(runner.conn, runner.run_id)
    assert saved.extra['stop_reasons'] == material.extra['stop_reasons']
    assert saved.extra['stop_reasons'][0]['reason'] == 'round_limit'
    assert saved.extra['gaps'] == material.extra['gaps']


def test_calculation_progress_ignores_only_purpose_not_operation_or_inputs(session):
    _, material, acquisition = session
    args = {'label': '经营现金流变化', 'operation': 'difference',
            'left': {'ref': 'T.op_cash', 'period': '2025-12-31'},
            'right': {'ref': 'T.op_cash', 'period': '2024-12-31'}}
    acquisition.tool_result({'name': 'calculate', 'arguments': args}, 'repair')
    first = acquisition.evidence_key()
    acquisition.tool_result({'name': 'calculate', 'arguments': args | {'operation': 'ratio'}}, 'repair')
    second = acquisition.evidence_key()
    assert first != second
    acquisition.tool_result({'name': 'calculate', 'arguments': args | {'left': args['right'], 'right': args['left']}}, 'repair')
    assert acquisition.evidence_key() != second
    assert len(material.extra['tools']) == 3


@pytest.mark.parametrize('field', ['code', 'period', 'url', 'sha256', 'title', 'announced'])
def test_partial_and_fetched_source_identity_have_distinct_missing_field_rules(field):
    original = BODY['document']
    assert original[field]
    missing = dict(original, **{field: ''})
    assert evidence.identity_conflict(original, missing, partial=True) is None
    assert evidence.identity_conflict(original, missing) == field
    conflicting = dict(original, **{field: original[field] + ' altered'})
    assert evidence.identity_conflict(original, conflicting, partial=True) == field
    assert evidence.identity_conflict(original, conflicting) == field


def test_new_same_value_calculation_id_unlocks_dependent_failure_without_counting_progress(session):
    _, material, acquisition = session
    args = {'label': '现金流差额', 'operation': 'difference',
            'left': {'ref': 'T.op_cash', 'period': '2025-12-31'},
            'right': {'ref': 'T.op_cash', 'period': '2024-12-31'}}
    acquisition.tool_result({'name': 'calculate', 'arguments': args}, 'repair')
    dependent = {'name': 'calculate', 'arguments': {'label': '现金流差额占营收', 'operation': 'ratio_pct',
                 'left': {'ref': 'C.2'}, 'right': {'ref': 'T.revenue', 'period': '2025-12-31'}}}
    _, failed = acquisition.tool_result(dependent, 'repair')
    assert failed
    before = acquisition.evidence_key()
    row, failed = acquisition.tool_result({'name': 'calculate', 'arguments': args | {'label': '经营现金流差额'}}, 'repair')
    assert not failed and row['id'] == 'C.2' and acquisition.evidence_key() == before
    result, failed = acquisition.tool_result(dependent, 'repair')
    assert not failed and result['values'] == {'result': '-17.98'}
    assert len(material.extra['tools']) == 4 and _fetches(material.extra['tools']) == 0


def test_patch_fallback_keeps_acquisition_budget_and_persisted_evidence(session):
    runner, material, acquisition = session
    material.pages.pop(9)
    material.raw_pages.pop(9)
    acquisition.rounds = 11
    deadline = acquisition.deadline
    runner.responses = [[read(9)], {'ops': 'invalid'}, report()]
    assert research.repair(runner, material, report(), [], acquisition=acquisition) == report()
    assert acquisition.rounds == 12 and acquisition.deadline == deadline
    assert runner.calls == 3 and runner.offered[-2:] == [[], []]
    assert len(acquisition.traces) == 1
    restored = research.replay_material(runner.conn, runner.run_id)
    assert restored.pages[9] == MATERIAL.pages[9]
    assert restored.snapshot()['sha256'] == material.snapshot()['sha256']


@pytest.mark.parametrize('frozen', [False, True])
def test_patch_fallback_cannot_expand_small_model_cap(session, frozen):
    runner, material, acquisition = session
    runner.max_calls = 1
    runner.responses = [{'ops': 'invalid'}, report()]
    with pytest.raises(research.ResearchError, match='上限'):
        research.repair(runner, material, report(), [], acquisition=None if frozen else acquisition)
    assert runner.calls == 1 and runner.offered == [[]]
    assert acquisition.traces == []


def test_frozen_invalid_patch_fallback_stays_tool_free(session):
    runner, material, acquisition = session
    before = material.snapshot()
    runner.responses = [{'ops': 'invalid'}, report()]
    assert research.repair(runner, material, report(), []) == report()
    assert runner.calls == 2 and runner.offered == [[], []]
    assert material.snapshot() == before and acquisition.traces == []


def test_rereading_gathered_mdna_page_is_not_a_conflict_but_other_text_is(tmp_path):
    # 恒力石化 2025 real layout pages: gather stores MD&A pages whitespace-normalized, tools read layout text.
    layout = json.loads((Path(__file__).parent / 'data/annual-report-sections.json').read_text())['600346']['pages']
    doc = sources.Document('600346.SH', '恒力石化', '2025年年度报告', '2025-12-31', '2026-04-15', '',
                           pages=list(layout), raw_pages=list(layout))
    pages = mdna_pages(doc)
    assert pages[15] != layout[14]
    material = research.Material(code=doc.code, name=doc.name, doc=doc, pages=pages,
                                 raw_pages={n: layout[n - 1] for n in pages}, table=[], market_date='2026-10-30', extra={})
    conn = db.connect(tmp_path / 'isolated.db')
    acquisition = Acquisition(research.Runner(conn, db.create_run(conn, doc.code, status='running')), material)
    result, failed = acquisition.tool_result(read(15), 'repair')
    assert not failed and result['pages'] == {15: pages[15]} and material.pages[15] == pages[15]
    # Mutation: a different real page under the same number still fails closed without merging.
    acquisition.toolkit.documents['D0'].pages[16] = layout[30]
    with pytest.raises(evidence.EvidenceError, match='已读页内容冲突'):
        acquisition.tool_result(read(17), 'repair')
    assert material.pages[17] == pages[17]
    conn.close()
