"""Tests for scripts/stability.py."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

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

    assert result['summary'] == {'companies': 1, 'with_flip': 1, 'median_range_pct': 12.5}
    group = result['groups'][0]
    assert [item['report'] for item in group['reports']] == [1, 4]
    assert group['flips'] == 1
    assert group['hurdle'] == {'n': 2, 'min': 7.5, 'max': 8.5, 'median': 8.0, 'range_pct': 12.5}
