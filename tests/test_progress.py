"""Progress contract against captured reports; hand-bound claims are boundary tests, not model acceptance."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
import re
import subprocess
import sys

import pytest

from a_share_research import db, progress, service
from support.continuation import BODY, load
from support.templates import render


@pytest.fixture
def captured(tmp_path):
    conn = db.connect(tmp_path / 'app.db')
    load(conn)
    yield conn
    conn.close()


def candidate():
    # Exact saved claims. The false novelty in current.answer[0] is intentionally retained as a negative control.
    def claim(text, *refs):
        return {'text': text, 'refs': list(refs)}
    return {'items': [{'kind': 'response',
                      'before': claim(BODY[37]['business'][2]['text'], 'before:business:2'),
                      'change': claim(BODY[39]['answer'][0]['text'], 'current:answer:0'),
                      'impact': claim(BODY[39]['answer'][0]['text'], 'before:business:2', 'current:answer:0'),
                      'unresolved': claim(BODY[39]['checkpoints'][0]['text'], 'current:checkpoints:0'),
                      'next_step': claim(BODY[39]['checkpoints'][0]['text'], 'current:checkpoints:0')}],
            'limits': ['结构/引用测试使用原段落，不代表这份候选已通过真实语义复核。']}


def test_full_old_knowledge_and_explicit_parent_are_available(captured):
    snap = progress.snapshot(captured, 39)
    assert snap['inputs']['before']['id'] == 37  # isolated 39, not production 39
    assert snap['inputs']['before']['period'] == snap['inputs']['current']['period']
    prompt = progress.prompt_input(snap)
    assert BODY[37]['business'][2]['text'] in prompt
    assert '28.42' in prompt
    assert progress.check(candidate(), snap) == []  # structural support does not establish novelty


def test_same_saved_announcement_cannot_be_classified_new_disclosure(captured):
    body = candidate()
    body['items'][0]['kind'] = 'new_disclosure'
    errors = progress.check(body, progress.snapshot(captured, 39))
    assert any('重复引用不得当新消息' in e for e in errors)


def test_semantic_reviewer_must_confirm_each_dimension():
    review = {'passed': True, 'items': [{k: True for k in progress.REVIEW_CHECKS}], 'issues': []}
    assert progress.review_errors(review, 1) == []
    review['items'][0]['novelty'] = False
    assert progress.review_errors(review, 1)
    review['items'][0]['novelty'] = True
    review['issues'] = ['上次已知，不能说新增']
    assert progress.review_errors(review, 1)


@pytest.mark.parametrize('field,refs', [('before', ['current:answer:0']), ('change', ['before:business:2']),
                                      ('impact', ['current:answer:0']), ('unresolved', ['current:checkpoints:99']),
                                      ('next_step', ['other:checkpoints:0'])])
def test_wrong_report_or_missing_reference_is_rejected(captured, field, refs):
    body = candidate()
    body['items'][0][field]['refs'] = refs
    assert progress.check(body, progress.snapshot(captured, 39))


@pytest.mark.parametrize('value', [None, [], 1, {'text': 'missing refs'}, {'text': 'x', 'refs': None}])
def test_malformed_model_output_is_diagnosed(captured, value):
    body = candidate()
    body['items'][0]['kind'] = 'new_disclosure'
    body['items'][0]['change'] = value
    assert progress.check(body, progress.snapshot(captured, 39))


def test_unknown_derived_number_is_not_accepted(captured):
    body = candidate()
    body['items'][0]['impact']['text'] += '据此增长98765.43%。'
    assert any('数字' in e for e in progress.check(body, progress.snapshot(captured, 39)))


def test_saved_cited_amounts_are_usable_without_borrowing_other_periods(captured):
    body, snap = candidate(), progress.snapshot(captured, 39)
    body['items'][0]['change'] = {'text': '2026-06-30 经营现金流 -1.57 亿元。', 'refs': ['current:financial:0']}
    assert progress.check(body, snap) == []  # captured claim says only "现金流转负"; its cited row has -1.57
    body['items'][0]['change']['text'] = '2026-06-30 经营现金流 19.65 亿元。'
    assert any('数字' in e for e in progress.check(body, snap))  # 19.65 is the uncited 2025 annual value
    body['items'][0]['change']['text'] = '归母净利润增加 0.92 亿元。'
    assert progress.check(body, snap) == []  # saved page-6 quote, not the paragraph text


def test_same_document_can_be_reinterpreted_and_changed_hash_is_not_deduplicated(captured):
    body, snap = candidate(), progress.snapshot(captured, 39)
    for kind in ('reinterpretation', 'basis_revision', 'response', 'comparison'):
        body['items'][0]['kind'] = kind
        assert progress.check(body, snap) == []
    body['items'][0]['kind'] = 'new_disclosure'
    assert any('重复引用' in e for e in progress.check(body, snap))
    # Metadata boundary mutation only: same URL with an actually different content identity
    # is not proof of repetition. Semantic review must still verify the claimed revision.
    doc = snap['inputs']['current']['body']['sources']['D6']['document']
    doc['sha256'] = '0' * 64
    assert progress.check(body, snap) == []
    snap['inputs']['before']['body']['sources']['D6']['document'].pop('sha256')
    assert any('新披露' in e for e in progress.check(body, snap))


def test_missing_unrelated_and_failed_reports_do_not_get_progress(captured):
    assert progress.view(captured, 999)['status'] == 'unavailable'
    assert progress.view(captured, 37)['status'] == 'unavailable'
    assert progress.view(captured, 8)['status'] == 'unavailable'  # old capture missing complete business/financial
    captured.execute('UPDATE reports SET passed=0 WHERE id=39')
    captured.execute("UPDATE runs SET status='failed' WHERE id=(SELECT run_id FROM reports WHERE id=39)")
    assert progress.view(captured, 39)['status'] == 'unavailable'
    with pytest.raises(service.RequestError):
        progress.submit(captured, 39)


def test_duplicate_submit_and_concurrent_claim_reserve_one_record(captured, tmp_path):
    def submit(_):
        conn = db.connect(tmp_path / 'app.db')
        try:
            return progress.submit(conn, 39)
        finally:
            conn.close()
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(submit, range(8)))
    assert len(set(ids)) == 1
    assert progress.claim(captured, ids[0])
    assert progress.claim(captured, ids[0]) is None
    assert progress.submit(captured, 39) == ids[0]
    assert captured.execute('SELECT count(*) FROM progress_calls').fetchone()[0] == 0


def test_failed_record_never_restarts_and_gets_never_queue(captured, tmp_path):
    for _ in range(3):
        assert progress.view(captured, 39)['status'] == 'missing'
    assert captured.execute('SELECT count(*) FROM research_progress').fetchone()[0] == 0
    identifier = progress.submit(captured, 39)
    captured.execute("UPDATE research_progress SET status='failed' WHERE id=?", (identifier,))
    assert progress.submit(captured, 39) == identifier
    assert progress.claim(captured, identifier) is None
    assert progress.view(captured, 39)['status'] == 'failed'
    result = subprocess.run([sys.executable, '-m', 'a_share_research.progress', '39', '--generate'],
                            env={**os.environ, 'DATA_DIR': str(tmp_path)}, capture_output=True, text=True)
    assert result.returncode == 1
    assert captured.execute('SELECT count(*) FROM progress_calls').fetchone()[0] == 0


def test_changed_input_invalidates_old_record_without_rewrite(captured):
    identifier = progress.submit(captured, 39)
    before = captured.execute('SELECT body FROM reports WHERE id=37').fetchone()[0]
    body = deepcopy(BODY[39]); body['gaps'].append('测试变更：采用本报告已存缺口验证版本绑定')
    captured.execute('UPDATE reports SET body=? WHERE id=39', (json.dumps(body, ensure_ascii=False),))
    assert progress.view(captured, 39)['status'] == 'stale'
    assert captured.execute('SELECT status FROM research_progress WHERE id=?', (identifier,)).fetchone()[0] == 'queued'
    assert captured.execute('SELECT body FROM reports WHERE id=37').fetchone()[0] == before


def test_pdf_link_requires_saved_page_mapping(captured):
    snap = progress.snapshot(captured, 39)
    refs = progress.references(snap)
    assert any(s.get('url') for r in refs.values() for s in r['sources'])
    for record in snap['inputs'].values():
        record['body']['pages'] = []
        for source in record['body'].get('sources', {}).values():
            source['pages'] = []
    assert not any(s.get('url') for r in progress.references(snap).values() for s in r['sources'])


def test_api_exports_preserve_real_bodies_and_refuse_existing_target(captured, tmp_path):
    from a_share_research.progress_import import import_exports
    files = []
    for report_id in (37, 39):
        path = tmp_path / f'report-{report_id}.json'
        path.write_text(json.dumps(service.report_view(captured, report_id), ensure_ascii=False))
        files.append(path)
    target = tmp_path / 'imported/app.db'
    import_exports(target, files)
    imported = db.connect(target)
    try:
        snap = progress.snapshot(imported, 39)
        assert snap['inputs']['before']['body'] == BODY[37]
        assert snap['inputs']['current']['body'] == BODY[39]
        assert snap['inputs']['current']['capture']['export_sha256']
        assert snap['fingerprint'] == progress.snapshot(captured, 39)['fingerprint']
        assert snap['inputs']['current']['material'] is None  # do not fabricate the original material snapshot
        assert imported.execute('SELECT count(*) FROM progress_calls').fetchone()[0] == 0
        with pytest.raises(FileExistsError):
            import_exports(target, files)
    finally:
        imported.close()


def test_api_import_preserves_recorded_replay_identity(captured, tmp_path):
    from a_share_research.progress_import import import_exports
    exported = service.report_view(captured, 143)
    assert exported['identity']['replay_of'] == 7
    path = tmp_path / 'replay.json'
    path.write_text(json.dumps(exported, ensure_ascii=False))
    target = tmp_path / 'replay-import/app.db'
    import_exports(target, [path])
    imported = db.connect(target)
    try:
        identity = json.loads(imported.execute('SELECT identity FROM runs WHERE id=?', (exported['run_id'],)).fetchone()[0])
        assert {k: v for k, v in identity.items() if k != 'progress_capture'} == exported['identity']
        assert identity['replay_of'] == 7
    finally:
        imported.close()


def test_gap_anchor_matches_the_existing_deduplicated_report_display(captured):
    snap = progress.snapshot(captured, 39)
    gaps = snap['inputs']['current']['body']['gaps']
    if gaps:
        gaps.append(gaps[0])
        refs = progress.references(snap)
        assert refs[f'current:gaps:{len(gaps)-1}']['url'].endswith('#claim-gaps-0')


def test_progress_template_preserves_six_questions_and_full_sources(captured):
    snap = progress.snapshot(captured, 39)
    advancement = progress.view(captured, 39)
    advancement.update(status='passed', body=candidate(), refs=progress.references(snap))
    # Template-only hand-bound record, deliberately not persisted or described as real model output.
    html = render('_progress.html', progress=advancement, progress_kinds=progress.KINDS,
                  selected={'id': 39}, code='603606.SH', progress_draft=BODY[39]['checkpoints'][0]['text'])
    for label in ['上次认识', '本次新增或修正', '判断影响及原因', '仍未解决', '下一步', '的依据']:
        assert label in html
    assert 'claim-business-2' in html and 'claim-answer-0' in html
    assert 'name="parent_report_id" value="39"' in html
    assert BODY[39]['checkpoints'][0]['text'] in html
    assert '同一期研究' in html


def test_http_read_refresh_and_duplicate_queue_never_call_models(captured, tmp_path, monkeypatch):
    import importlib
    from a_share_research import api_keys
    import a_share_research
    from starlette.testclient import TestClient

    token = api_keys.create(captured, 'temporary-progress-test')
    base_url = 'http://127.0.0.1:8627'
    monkeypatch.setattr(db, 'DATA', tmp_path)
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('PUBLIC_BASE_URL', base_url)
    monkeypatch.setenv('DEV_LOGIN', '1')
    monkeypatch.setenv('SESSION_SECRET', 'temporary-progress-http-only-secret')
    monkeypatch.setattr(a_share_research, 'web', None, raising=False)
    monkeypatch.delitem(sys.modules, 'a_share_research.web', raising=False)
    web_module = importlib.import_module('a_share_research.web')

    client = TestClient(web_module.app, base_url=base_url)
    route = '/api/reports/39/progress'
    assert client.get(route).status_code == 401
    client.headers['Authorization'] = f'Bearer {token}'
    for _ in range(2):
        result = client.get(route)
        assert result.status_code == 200 and result.json()['status'] == 'missing'
    first = client.post(route)
    assert first.status_code == 202 and first.json()['status'] == 'queued'
    assert client.post(route).json()['progress_id'] == first.json()['progress_id']
    assert client.post('/api/reports/37/progress').status_code == 409
    # Ordinary developer login; no production session or provider credentials.
    login = client.get('/login').text
    assert '本机开发登录' in login
    assert client.post('/auth/dev', headers={'Origin': base_url}).status_code == 200
    page = client.get('/stock/603606.SH?report=39')
    assert page.status_code == 200 and '等待整理' in page.text
    assert client.get('/stock/603606.SH?report=8').status_code == 404
    csrf = re.search(r'name="csrf" value="([^"]+)"', page.text)[1]
    assert client.post('/reports/39/progress', data={'csrf': csrf}, headers={'Origin': base_url}).status_code == 200
    target = client.get('/reports/37?claim=business:2#claim-business-2')
    assert 'id="claim-business-2"' in target.text
    fragment = target.text.split('id="claim-business-2"')[1].split('</li>')[0]
    assert '<details class="source" open>' in fragment
    assert captured.execute('SELECT count(*) FROM research_progress').fetchone()[0] == 1
    assert captured.execute('SELECT count(*) FROM progress_calls').fetchone()[0] == 0
    assert captured.execute('SELECT count(*) FROM calls').fetchone()[0] == 0
    web_module.conn.close()
