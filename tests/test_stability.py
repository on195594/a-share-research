"""Tests for scripts/stability.py."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from a_share_research import db


def _load():
    path = Path(__file__).resolve().parents[1] / 'scripts' / 'stability.py'
    spec = importlib.util.spec_from_file_location('stability_script', path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _insert(conn, run_id: int, code: str, *, passed: int = 1, created_at: str = '2026-11-19T10:00:00+08:00',
            action: str = '等价格', hurdle: float | None = 7.4, replay_of: int | None = None,
            model: str = 'gpt-6.2-sol') -> None:
    conn.execute(
        "INSERT INTO runs (id, code, name, kind, status, request, created_at) VALUES (?, ?, '中国铝业', 'research', ?, '{}', ?)",
        (run_id, code, 'done' if passed else 'failed', created_at),
    )
    conn.execute(
        "INSERT INTO calls (id, run_id, step, created_at, identity, prompt) VALUES (?, ?, 'explore', ?, ?, '')",
        (run_id, run_id, created_at, json.dumps({'actual': {'model': model}})),
    )
    body: dict = {
        'period': '2026-09-30',
        'stance': {'action': action},
        'stance_calc': {'price': hurdle} if hurdle is not None else {},
        'valuation_calc': {'close': 7.8, 'v2': {'support_below': hurdle}},
    }
    if replay_of is not None:
        body['replay_of'] = replay_of
    conn.execute(
        "INSERT INTO reports (id, run_id, code, name, period, passed, verdict, created_at, body) "
        "VALUES (?, ?, ?, '中国铝业', '2026-09-30', ?, '值得继续研究', ?, ?)",
        (run_id, run_id, code, passed, created_at, json.dumps(body, ensure_ascii=False)),
    )


def test_spread_ignores_non_positive_and_non_finite_values():
    stability = _load()
    assert stability.spread([]) == {'n': 0}
    assert stability.spread([0, -1.5, float('nan'), float('inf'), True, None]) == {'n': 0}
    assert stability.spread([0, 6.58, 7.44, 7.69]) == {
        'n': 3, 'min': 6.58, 'max': 7.69, 'median': 7.44, 'range_pct': 14.9,
    }


def test_observed_excludes_unpassed_and_replay_reports(tmp_path):
    stability = _load()
    database = tmp_path / 'app.db'
    conn = db.connect(database)
    _insert(conn, 1, '601600.SH', passed=1, action='现在分批', hurdle=8.5)
    _insert(conn, 2, '601600.SH', passed=0, action='回避', hurdle=1.0)
    _insert(conn, 3, '601600.SH', passed=1, action='回避', hurdle=2.0, replay_of=1)
    _insert(conn, 4, '601600.SH', passed=1, action='等价格', hurdle=7.5)
    conn.commit()
    conn.close()

    with stability.connect(database) as ro:
        result = stability.observed(ro, '2026-11-01', None)

    assert result['summary'] == {'companies': 1, 'groups': 1, 'with_flip': 1, 'median_range_pct': 12.5}
    group = result['groups'][0]
    assert [item['report'] for item in group['reports']] == [1, 4]
    assert group['flips'] == 1
    assert group['hurdle'] == {'n': 2, 'min': 7.5, 'max': 8.5, 'median': 8.0, 'range_pct': 12.5}


def test_observed_counts_independent_companies_across_suffixes_and_periods(tmp_path):
    stability = _load()
    database = tmp_path / 'app.db'
    conn = db.connect(database)
    _insert(conn, 1, '601600.SH', action='现在分批', hurdle=8.5)
    _insert(conn, 2, '601600', action='等价格', hurdle=7.5)  # Suffix-free code of the same company.
    _insert(conn, 3, '601600.SH', hurdle=7.0)
    _insert(conn, 4, '601600.SH', hurdle=7.2)
    conn.execute("UPDATE reports SET body=json_set(body, '$.period', '2026-06-30') WHERE id IN (3, 4)")
    conn.commit()
    conn.close()

    with stability.connect(database) as ro:
        result = stability.observed(ro, '2026-11-01', '601600')
    # Two financial periods are two rerun groups of one independent company.
    assert (result['summary']['companies'], result['summary']['groups'], result['summary']['with_flip']) == (1, 2, 1)
    assert {(g['code'], g['period']) for g in result['groups']} == {('601600', '2026-09-30'), ('601600', '2026-06-30')}


# Active experiments use real same-day reruns (stance-flip-samples-20261107.json: production reports on
# 2026-11-05, one close per company). Marked variants: the real 603606 report-37 document and financial
# table stand in as the start material (the samples kept only stance and valuation), and run statuses,
# missing ids, priors, dates, the unpassed body and the not-priced body are constructed.
SAMPLES = {s['report_id']: s for s in json.loads(
    (Path(__file__).parent / 'data' / 'stance-flip-samples-20261107.json').read_text())}
REQUEST = {'question': '', 'feedback': None, 'parent_report_id': None}


def _material(sample: dict, *, prior: int | None = 37, market_date: str | None = None) -> dict:
    from support.valuation import BODY
    data = {'code': sample['code'], 'market_date': market_date or sample['valuation_calc']['as_of'], 'table': [],
            'extra': {'question': '上次研究的风险与检查点现在怎样？', **({'prior': {'report_id': prior}} if prior else {})}}
    if sample['code'][:6] == BODY['document']['code'][:6]:
        data |= {'doc': BODY['document'], 'table': BODY['table']}
    return {'version': 2, 'sha256': 'variant', 'data': data}


def _trial(conn, run_id: int, sample: dict, *, status='done', passed=1, body=None, app='a' * 64, **material) -> None:
    identity = {'application_sha256': app} | ({'failure': {'category': 'model_error'}} if status == 'failed' else {})
    conn.execute('INSERT INTO runs (id, code, kind, status, request, created_at, identity, material) '
                 "VALUES (?, ?, 'research', ?, ?, '2026-11-05T16:00:00+08:00', ?, ?)",
                 (run_id, sample['code'], status, json.dumps(REQUEST), json.dumps(identity),
                  json.dumps(_material(sample, **material), ensure_ascii=False)))
    if status != 'done':
        return
    body = body or {'period': '2026-09-30', 'stance': {'action': sample['action']},
                    'valuation_calc': sample['valuation_calc'], 'prior': {'report_id': material.get('prior', 37)}}
    conn.execute("INSERT INTO reports (id, run_id, code, period, passed, created_at, body) "
                 "VALUES (?, ?, ?, '2026-09-30', ?, '2026-11-05T17:00:00+08:00', ?)",
                 (run_id, run_id, sample['code'], passed, json.dumps(body, ensure_ascii=False)))


def test_experiment_counts_only_listed_runs_with_every_outcome(tmp_path):
    stability = _load()
    database = tmp_path / 'copy.db'
    conn = db.connect(database)
    for report_id in (116, 125, 132, 118, 127, 134):
        _trial(conn, report_id, SAMPLES[report_id])
    _trial(conn, 117, SAMPLES[117])  # A real rerun of the same day that is not part of this group.
    _trial(conn, 9001, SAMPLES[116], status='failed')
    unpriced = {'rule': 'valuation-v1', 'not_priced': True, 'as_of': '2026-11-05', 'close': 41.0}
    _trial(conn, 9003, SAMPLES[116], body={'period': '2026-09-30', 'stance': {'action': '回避'},
                                           'valuation_calc': unpriced, 'prior': {'report_id': 37}})
    _trial(conn, 9004, SAMPLES[118], passed=0, body={'period': '2026-09-30', 'failure': {'category': 'citation_calculation'}})
    _trial(conn, 9005, SAMPLES[118], status='running')
    conn.commit()
    conn.close()
    runs = [116, 125, 132, 9001, 9002, 9003, 118, 127, 134, 9004, 9005]

    result = stability.experiment([(database, run) for run in runs], 'new')
    summary = result['summary']
    assert [r['run'] for r in result['runs']] == runs and 117 not in {r['report'] for r in result['runs']}
    assert summary['outcomes'] == {'priced': 6, 'not_priced': 1, 'unpassed': 1, 'failed': 1, 'unfinished': 1, 'missing': 1}
    assert (summary['label'], summary['requested'], summary['companies'], summary['unassigned']) == ('new', 11, 2, [9002])
    cable, catl = result['companies']
    assert (cable['code'], catl['code']) == ('603606', '300750')
    assert cable['outcomes'] == {'priced': 3, 'not_priced': 1, 'failed': 1}
    assert cable['actions'] == {'现在分批': 1, '等价格': 2, '回避': 1} and cable['agreement'] == 0.167
    assert cable['hurdle'] == stability.spread([SAMPLES[r]['valuation_calc']['v2']['support_below'] for r in (116, 125, 132)])
    assert cable['hurdle']['n'] == 3 and catl['hurdle']['n'] == 3
    assert next(r for r in result['runs'] if r['run'] == 9001)['reason'] == 'model_error'
    # Failed runs stay in outcomes. Only observations with an action contribute to stability.
    assert cable['controls']['market.close'] == {'values': [41.0], 'unknown': 0, 'same': True}
    assert cable['controlled'] and not catl['controlled']
    assert 'material.document' in summary['uncontrolled']['300750']
    assert cable['controls']['prior.report']['values'] == ['prior 37']
    assert summary['configs'] == [{'application_sha256': 'a' * 64, 'author': None, 'contract': None,
                                   'explore_model': None, 'reviewer': None, 'code_commit': None, 'profile_sha256': '未记录'}] and not summary['mixed_config']
    assert (summary['compared'], summary['median_agreement']) == (1, 0.167)


def test_experiment_flags_chained_priors_and_reads_one_copy_per_repeat(tmp_path):
    stability = _load()
    chained = tmp_path / 'one-copy.db'
    conn = db.connect(chained)
    # Repeats in one copy: each run takes the previous repeat as its prior; the last also ran a day later.
    _trial(conn, 124, SAMPLES[124], prior=116)
    _trial(conn, 133, SAMPLES[133], prior=124, market_date='2026-11-06')
    conn.commit()
    conn.close()
    result = stability.experiment([(chained, 124), (chained, 133)], 'old')
    assert result['summary']['uncontrolled'] == {'603606': ['market.date', 'prior.report']}

    copies = []
    for name, report_id in (('a', 124), ('b', 133)):
        copies.append(tmp_path / f'{name}.db')
        conn = db.connect(copies[-1])
        _trial(conn, 500, SAMPLES[report_id], app='b' * 64)  # Same run id in each production copy.
        conn.commit()
        conn.close()
    tokens = [f'{path}:500' for path in copies]
    separate = stability.experiment([stability.parse_target(t, chained) for t in tokens], 'old')
    assert separate['summary']['uncontrolled'] == {} and separate['summary']['companies'] == 1
    assert separate['companies'][0]['actions'] == {'等价格': 1, '现在分批': 1}
    # Old and new configurations are separate calls; their identities never merge.
    assert separate['summary']['configs'][0]['application_sha256'] == 'b' * 64
    with pytest.raises(ValueError, match='重复'):
        stability.experiment([(copies[0], 500), (copies[0], 500)])
    for bad in ('x', '0', ':5', 'a.db:'):
        with pytest.raises(ValueError):
            stability.parse_target(bad, chained)


def test_cli_keeps_modes_apart(tmp_path, capsys):
    stability = _load()
    database = tmp_path / 'copy.db'
    conn = db.connect(database)
    _trial(conn, 116, SAMPLES[116])
    conn.commit()
    conn.close()
    assert stability.main(['--db', str(database), '--runs', '116', '9002', '--label', 'new']) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed['summary']['outcomes'] == {'priced': 1, 'missing': 1}
    for argv in (['--runs', '116', '--since', '2026-11-01'], ['--runs', '116', '--code', '603606'], ['--label', 'new'],
                 ['--runs', str(tmp_path / 'absent.db') + ':1']):
        with pytest.raises(SystemExit):
            stability.main(['--db', str(database), *argv])

def test_profiles_separate_same_company_trials_including_failed_runs(tmp_path):
    from a_share_research.model_profile import PROFILE_SHA256, run_identity

    database = tmp_path / 'profiles.db'
    conn = db.connect(database)
    for run in (116, 125, 132):
        _trial(conn, run, SAMPLES[run])
    _trial(conn, 9001, SAMPLES[116], status='failed')
    # Only the profile metadata changes on these captured same-day trials.
    conn.execute("UPDATE runs SET identity=json_patch(identity, ?) WHERE id IN (125, 132, 9001)",
                 (json.dumps(run_identity(21)),))
    conn.commit()
    natural = _load().observed(conn, '2026-11-01', None)
    conn.close()
    group, = natural['groups']
    assert group['profile_sha256'] == PROFILE_SHA256 and [r['report'] for r in group['reports']] == [125, 132]
    result = _load().experiment([(database, r) for r in (116, 125, 132, 9001)])
    older, newer = result['profiles']
    assert result['summary']['companies'] == 1 and result['summary']['mixed_config']
    assert older['profile_sha256'] == '未记录' and older['median_agreement'] is None
    assert newer['profile_sha256'] == PROFILE_SHA256 and newer['outcomes'] == {'priced': 2, 'failed': 1}
    assert newer['median_agreement'] == 1


def test_stability_uses_visible_snapshot_and_forecast_express_anchors():
    from a_share_research import disclosure_checks, material

    stability = _load()
    capture = json.loads((Path(__file__).parent / 'data' / 'disclosure-checks.json').read_text())['tushare']
    snapshot = {'status': 'unheld', 'frozen_at': '2026-11-19', 'account': {'version': 1}}
    request = {'holdings_context': True, 'holdings_snapshot': snapshot}

    def controls(snap, table):
        return stability._controls(request, {'extra': {'holdings_snapshot': snap}, 'table': table}, {}, {})

    original = controls(snapshot, [])
    assert original == controls(snapshot | {'frozen_at': '2026-11-20', 'account': {'version': 2}}, [])
    assert original['holdings.snapshot'] != controls(snapshot | {'status': 'unverified'}, [])['holdings.snapshot']
    assert controls(None, [])['holdings.snapshot'] is None
    for api, code in (('forecast', '603960.SH'), ('express', '605016.SH')):
        rows = capture[api][code]
        normalized = (disclosure_checks.normalize_forecast(rows, code[:6]) if api == 'forecast' else
                      disclosure_checks.normalize_express(rows, code[:6], revenue_basis_matches=None))
        observation = normalized[0]
        anchors, reason = material.estimate_rows([observation], code=code, period=observation['period'],
                                                 announced=observation['announced'], revenue_basis=(None, None),
                                                 retrieved_at='2026-11-16')
        assert not reason and anchors
        assert controls(snapshot, anchors)['material.financials'] != original['material.financials']


def test_stability_partitions_contract_mode_and_period_for_the_same_profile(tmp_path):
    path = tmp_path / 'trials.db'
    conn = db.connect(path)
    # Marked variants of the same real repeat. Only grouping identities differ.
    groups = ((11, 'blind', '2026-09-30'), (12, 'blind', '2026-09-30'),
              (12, 'holdings_context', '2026-09-30'), (12, 'blind', '2026-06-30'))
    targets = []
    for index, (contract, mode, period) in enumerate(groups):
        for i in range(2):
            run = 100 + index * 2 + i
            _trial(conn, run, SAMPLES[116])
            conn.execute("UPDATE runs SET identity=json_set(identity,'$.contract',?), request=? WHERE id=?",
                         (contract, json.dumps(REQUEST | ({'holdings_context': True} if mode != 'blind' else {})), run))
            conn.execute("UPDATE reports SET body=json_set(body,'$.period',?) WHERE id=?", (period, run))
            targets.append((path, run))
    conn.commit()
    stability = _load()
    observed = stability.observed(conn, '2026-11-01', None)
    conn.close()
    active = stability.experiment(targets)
    for rows in (observed['groups'], active['companies']):
        assert {(g['contract'], g['input_mode'], g['period']) for g in rows} == set(groups)
    assert len(active['profiles']) == len(observed['profiles']) == 3
    assert 'median_range_pct' not in observed['summary']
