"""The judgment surface is an explicit, reviewable list, independent of the application hash."""
import hashlib
import inspect
import json
from decimal import Decimal

from a_share_research import forecasts, prompts, research, research_tools, reviewing, screening, stance, valuation

# Runtime call/round/time limits are deliberately absent: they do not define judgment semantics.
SURFACE = {
    **{f'prompts.{n}': getattr(prompts, n) for n in ('SCHEMA', 'CHECKPOINTS', 'VALUATION', 'STANCE', 'PRIOR', 'RULES')},
    'prompts.output_schema': inspect.getsource(prompts.output_schema),
    **{f'research.{n}': inspect.getsource(getattr(research, n))
       for n in ('compose', 'review', 'repair_prompt', 'repair', 'program_valuation')},
    'research.PATCH_SCHEMA': research.PATCH_SCHEMA,
    'research_tools.explore': inspect.getsource(research_tools.explore),
    **{f'research_tools.{n}': getattr(research_tools, n) for n in ('TOOLS', 'REPAIR_TOOLS', 'STOPPED_CALL')},
    'reviewing.PROTOCOL': reviewing.PROTOCOL,
    **{f'screening.{n}': getattr(screening, n) for n in ('QUESTION', 'SELECT_SCHEMA', 'RANK_SCHEMA')},
    **{f'screening.{n}': inspect.getsource(getattr(screening, n)) for n in ('selection_prompt', 'ranking_prompt')},
    **{f'valuation.{n}': getattr(valuation, n)
       for n in ('RULE', 'RULE_V2', 'SCENARIOS', 'PRICING_FIELDS', 'PROGRAM_OPERATION')},
    # Both v1 and v2 use compute's shared default hurdles; these are not module-level constants.
    'valuation.compute.hurdles': valuation.compute.__kwdefaults__,
    **{f'stance.{n}': getattr(stance, n) for n in ('ACTIONS', 'OPERATORS', 'WAIT_MONTHS', 'BOUNDARY')},
    **{f'forecasts.{n}': getattr(forecasts, n)
       for n in ('CONFIDENCE', 'MIN_FORECASTS', 'QUARTER_ENDS', 'BALANCES')},
}


def _encode(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(type(value).__name__)


def surface_sha256():
    parts = [(name, json.dumps(value, ensure_ascii=False, sort_keys=True, default=_encode))
             for name, value in sorted(SURFACE.items())]
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def test_judgment_surface_is_pinned():
    from a_share_research.contract import SURFACE_SHA256
    current = surface_sha256()
    assert current == SURFACE_SHA256, (
        f'判断契约面已变化，新 SHA256={current}。语义变化须递增 JUDGMENT_CONTRACT 并更新哈希；'
        '非语义重排/注释只更新哈希并说明原因。两者都须在 contract.py 留一行变更记录，遵守季度修改窗口。')


def test_contract_filter_and_report_counts_use_captured_records(tmp_path, monkeypatch, caplog):
    """Only contract metadata is added to a captured report; financial facts stay intact."""
    import logging
    from pathlib import Path
    import sqlite3
    import sys

    from a_share_research import validation, zones

    captured = json.loads(Path(__file__).with_name('data').joinpath('hypotheses_v3.json').read_text())
    database = tmp_path / 'contracts.db'
    with sqlite3.connect(database) as conn:
        for table in ('runs', 'reports'):
            records = captured[table]
            columns = list(records[0])
            conn.execute(f"CREATE TABLE {table} ({','.join(columns)})")
            conn.executemany(f"INSERT INTO {table} VALUES ({','.join('?' for _ in columns)})",
                             [[row[k] for k in columns] for row in records])
        conn.execute("ALTER TABLE reports ADD COLUMN coverage")
        conn.execute("UPDATE reports SET coverage=json_extract(body, '$.coverage')")
        conn.commit()
        eligible = validation.load_reports(str(database))
        chosen = eligible[0]['id']
        conn.execute("UPDATE reports SET body=json_set(body, '$.judgment', json(?)) WHERE id=?",
                     (json.dumps({'contract': 1}), chosen))
    all_rows = validation.load_reports(str(database))
    assert all(r['contract'] is None for r in eligible)
    selected = validation.load_reports(str(database), 1)
    assert [r['id'] for r in selected] == [chosen]
    assert all(r['contract'] is None for r in validation.load_reports(str(database), 0))
    assert validation.load_reports(str(database), 999) == []
    assert validation.filter_contract(all_rows, None) == all_rows
    assert validation.contract_counts(selected + selected) == [{'contract': 1, 'label': '1', 'count': 1}]
    assert '未记录' in validation.contract_line(eligible)
    assert zones.load_reports(str(database), 999) == []
    assert validation.load_forecasts(str(database), 999) == []
    assert validation.score_agenda(validation.load_agenda(str(database)), contract=999)["rows"] == []

    output = tmp_path / 'forecast-contracts.json'
    monkeypatch.setattr(sys, 'argv', ['validation', '--database', str(database), '--forecasts',
                                    '--contract', '999', '--json', str(output)])
    with caplog.at_level(logging.INFO):
        validation.main()
    assert '按判断契约的入样数：无' in caplog.text
    assert json.loads(output.read_text())['contracts'] == []


def test_batch_contract_is_taken_from_scored_ranking():
    from copy import deepcopy
    from pathlib import Path

    from a_share_research import hypotheses, validation

    capture = json.loads(Path(__file__).with_name('data').joinpath('hypotheses_v3.json').read_text())
    batch = deepcopy(capture['batch'])
    batch['selection']['ranking']['judgment'] = {'contract': 1}
    result = hypotheses.score_batch(batch, [], {}, {})
    assert result['contract'] == 1
    assert validation.contract_counts([result]) == [{'contract': 1, 'label': '1', 'count': 1}]
