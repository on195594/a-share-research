#!/usr/bin/env python3
"""Same-material stability of research stances and hurdle prices. Read-only, no model calls.

Natural observation (default): consecutive passed reports of one company on the same financial
period (no new periodic report in between) are treated as reruns on the same material; this
prints each company's stance sequence, flips and hurdle-price range. Nothing about them was
controlled, so they describe production noise, not one configuration.

Active experiment (--runs): exactly the listed runs form one repeated trial, whatever else the
database holds. Every listed run gets an outcome (priced, not_priced, no_stance, unpassed, failed,
unfinished, excluded, missing), so failures count against a configuration instead of vanishing.
Runs from different frozen profiles can be listed together; each profile has its own summary. Controls are read back
from each run's request, material snapshot and report and reported per company, never assumed:

- market: the material's market date and the report's close. Run every repeat on one trading day
  after the close, so the price and that day's data are identical.
- material: the start document (url and sha256) and the gathered T.* financial rows. No new
  periodic report may land between repeats.
- question: the stored request (question, feedback, parent report, events) and the question
  research actually answered. Submit every repeat with the same request.
- prior: the report research carried as its agenda, or the explicit parent report. Research picks
  the latest formal report before the run, so repeats in one database chain onto each other; give
  each repeat its own copy of the same production backup (PATH:RUN_ID), where all of them see the
  same production prior.

The scenarios are chosen during evidence gathering, so replaying only the final writing call
reproduces them and measures nothing; replays are excluded.

  uv run python scripts/stability.py [--since 2026-11-01] [--code 601600] [--db PATH]
  uv run python scripts/stability.py --label new --runs 301 302 303 [--db PATH]
  uv run python scripts/stability.py --label old --runs c1/app.db:301 c2/app.db:301 c3/app.db:301
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import statistics
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from a_share_research import report as report_body  # noqa: E402
from a_share_research import stock_codes  # noqa: E402

DATABASE = Path('/home/lin/a-share-research-data/app.db')
NATURAL_SINCE = '2026-11-01'
OUTCOMES = ('priced', 'not_priced', 'no_stance', 'unpassed', 'failed', 'unfinished', 'excluded', 'missing')
CONTROLS = ('market.date', 'market.close', 'material.document', 'material.financials',
            'question.request', 'question.answered', 'prior.report')


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _price(value) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
        return float(value)
    return None


def spread(prices: list) -> dict:
    """Range relative to the median; the boundary band must exceed this to mean anything."""
    valid = [price for price in map(_price, prices) if price is not None]
    if not valid:
        return dict(n=0)
    middle = statistics.median(valid)
    return dict(n=len(valid), min=round(min(valid), 2), max=round(max(valid), 2),
                median=round(middle, 2), range_pct=round((max(valid) - min(valid)) / middle * 100, 1))


def company_key(code) -> str | None:
    try:
        return stock_codes.normalize_key(code)
    except ValueError:
        return None


def hurdle_of(body: dict):
    calc = body.get('stance_calc') or {}
    v2 = (body.get('valuation_calc') or {}).get('v2') or {}
    return calc.get('price') if calc.get('price') is not None else v2.get('support_below')


def _explore_model(conn, run_id: int) -> str | None:
    row = conn.execute("SELECT identity FROM calls WHERE run_id = ? AND step = 'explore' ORDER BY id DESC LIMIT 1",
                       (run_id,)).fetchone()
    return ((_object(row['identity']) or {}).get('actual') or {}).get('model') if row else None


def observed(conn, since: str, code: str | None) -> dict:
    rows = conn.execute(
        "SELECT r.id, r.code, r.created_at, r.body, c.identity, json_extract(u.identity, '$.profile_sha256') AS profile, "
        "json_extract(u.identity, '$.code_commit') AS code_commit FROM reports r JOIN runs u ON u.id = r.run_id "
        "LEFT JOIN calls c ON c.id = (SELECT max(id) FROM calls WHERE run_id = r.run_id AND step = 'explore') "
        "WHERE r.passed = 1 AND r.created_at >= ? AND (? IS NULL OR r.code LIKE ? || '%') "
        "ORDER BY substr(r.code, 1, 6), r.id", (since, code, company_key(code) or code)).fetchall()
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        body: dict = dict(report_body.load(row['body']))
        if not body.get('stance') or body.get('replay_of') is not None:
            continue
        # 600900 and 600900.SH are one company; grouping on the raw code would split it.
        groups.setdefault((company_key(row['code']) or row['code'], body.get('period'), row['profile'] or '未记录'), []).append(dict(
            report=row['id'], date=row['created_at'][:10], code_commit=row['code_commit'], model=((_object(row['identity']) or {}).get('actual') or {}).get('model'),
            close=(body.get('valuation_calc') or {}).get('close'), action=body['stance'].get('action'),
            hurdle=hurdle_of(body)))
    result = []
    for (company, period, profile), items in groups.items():
        if len(items) < 2:
            continue
        actions = [item['action'] for item in items]
        result.append(dict(code=company, period=period, profile_sha256=profile, reports=items,
                           flips=sum(a != b for a, b in zip(actions, actions[1:])),
                           hurdle=spread([item['hurdle'] for item in items])))
    ranges = [group['hurdle']['range_pct'] for group in result if group['hurdle'].get('n', 0) >= 2]
    # A company with reruns on two periods is two groups but one independent company.
    return dict(since=since, groups=result,
                summary=dict(companies=len({group['code'] for group in result}), groups=len(result),
                             with_flip=len({group['code'] for group in result if group['flips'] > 0}),
                             median_range_pct=statistics.median(ranges) if ranges else None))


# ---------- active experiment ----------

def parse_target(token: str, default: Path) -> tuple[Path, int]:
    """`RUN_ID` in --db, or `PATH:RUN_ID` for a repeat kept in its own database copy."""
    path, separator, number = token.rpartition(':')
    if separator and not path:
        raise ValueError(f'{token}：PATH:RUN_ID 缺少数据库路径')
    if not number.isdigit() or int(number) <= 0:
        raise ValueError(f'{token}：任务编号须为正整数，写成 RUN_ID 或 PATH:RUN_ID')
    return (Path(path) if separator else default), int(number)


def _object(text) -> dict | None:
    try:
        value = json.loads(text) if text else None
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]


def _financials(table) -> str | None:
    """Digest of the gathered T.* rows (id, unit, values); provenance timestamps are left out."""
    rows = sorted(([r.get('id'), r.get('unit'), r.get('values'), r.get('exact_values')] for r in table or []
                   if isinstance(r, dict) and str(r.get('id', '')).startswith('T.')), key=lambda r: str(r[0]))
    return _digest(rows) if rows else None


def _controls(request: dict | None, data: dict, body: dict, calc: dict) -> dict:
    """Each control's value for one run; None when this run never recorded it (not a mismatch)."""
    extra = data.get('extra') if isinstance(data.get('extra'), dict) else {}
    doc = data.get('doc') or body.get('document') or {}
    known = bool(data) or bool(body)
    if request and request.get('parent_report_id') is not None:
        prior = f"parent {request['parent_report_id']}"
    elif known:
        agenda = extra.get('prior') if data else body.get('prior')
        prior = f"prior {agenda['report_id']}" if isinstance(agenda, dict) and agenda.get('report_id') else '无前序'
    else:
        prior = None
    answered = extra.get('question') if 'question' in extra else body.get('question')
    return {
        'market.date': data.get('market_date') or body.get('market_date'),
        'market.close': _price(calc.get('close')),
        'material.document': f"{doc.get('url')}#{doc.get('sha256')}" if doc.get('url') and doc.get('sha256') else None,
        'material.financials': _financials(data.get('table') or body.get('table')),
        'question.request': json.dumps(request, ensure_ascii=False, sort_keys=True) if request is not None else None,
        'question.answered': answered,
        'prior.report': prior,
    }


def trial(conn, run_id: int) -> dict:
    """One listed run with its outcome, stance, hurdle, configuration and control values."""
    run = conn.execute('SELECT * FROM runs WHERE id = ?', (run_id,)).fetchone()
    if run is None:
        return dict(run=run_id, code=None, status=None, report=None, outcome='missing', reason='数据库中没有该任务')
    columns = set(run.keys())
    request = _object(run['request'] or '{}') if 'request' in columns else None
    identity = _object(run['identity']) if 'identity' in columns else None
    identity = identity or {}
    snapshot = _object(run['material']) if 'material' in columns else None
    data = (snapshot or {}).get('data') if isinstance((snapshot or {}).get('data'), dict) else {}
    report = conn.execute('SELECT id, passed, body FROM reports WHERE run_id = ?', (run_id,)).fetchone()
    body: dict = dict(report_body.load(report['body'])) if report else {}
    calc = body.get('valuation_calc') if isinstance(body.get('valuation_calc'), dict) else {}
    judgment = body.get('judgment') or {}
    replay = run['replay_of'] if 'replay_of' in columns else identity.get('replay_of')
    kind = run['kind'] if 'kind' in columns else 'research'
    record = dict(run=run_id, code=company_key(run['code']), status=run['status'],
                  report=report['id'] if report else None, outcome=None, action=None, hurdle=None,
                  config=dict(application_sha256=identity.get('application_sha256'), code_commit=identity.get('code_commit'),
                              profile_sha256=identity.get('profile_sha256') or '未记录',
                              contract=identity.get('contract', judgment.get('contract')),
                              author=identity.get('author', judgment.get('author')),
                              reviewer=identity.get('reviewer', judgment.get('reviewer')),
                              explore_model=_explore_model(conn, run_id)),
                  controls=_controls(request, data, body, calc))
    action = (body.get('stance') or {}).get('action') if isinstance(body.get('stance'), dict) else None
    if replay is not None or body.get('replay_of') is not None or kind not in ('research', 'diagnostic'):
        return record | dict(outcome='excluded', reason=f'重放或非全流程研究（kind={kind}, replay_of={replay}）')
    if run['status'] in ('queued', 'running'):
        return record | dict(outcome='unfinished')
    if run['status'] != 'done' or report is None:
        failure = identity.get('failure') or {}
        return record | dict(outcome='failed', reason=failure.get('category') or run['error']
                             or ('已完成但没有报告' if run['status'] == 'done' else f"状态 {run['status']}"))
    if not report['passed']:
        return record | dict(outcome='unpassed', reason=(body.get('failure') or {}).get('category'))
    if not action:
        return record | dict(outcome='no_stance')
    hurdle = _price(hurdle_of(body))
    if not calc or calc.get('not_priced') or 'error' in calc or hurdle is None:
        return record | dict(outcome='not_priced', action=action,
                             reason='not_priced' if calc.get('not_priced') else calc.get('error') or '无可用门槛价')
    return record | dict(outcome='priced', action=action, hurdle=hurdle)


def _control(values: list) -> dict:
    known = [v for v in values if v is not None]
    distinct = sorted(set(known), key=str)
    return dict(values=distinct, unknown=len(values) - len(known), same=len(distinct) == 1 and len(known) == len(values))


def experiment(targets: list[tuple[Path, int]], label: str | None = None) -> dict:
    """Exactly the listed runs as one repeated trial; nothing else in the databases is read as a sample."""
    identities = [(path.resolve(), run_id) for path, run_id in targets]
    if len(set(identities)) != len(identities):
        raise ValueError('同一任务重复列出会被重复计数；每个任务只列一次')
    records, connections = [], {}
    try:
        for path, run_id in targets:
            if path.resolve() not in connections:
                if not path.is_file():
                    raise ValueError(f'{path}：数据库不存在')
                connections[path.resolve()] = connect(path)
            records.append(dict(db=str(path)) | trial(connections[path.resolve()], run_id))
    finally:
        for conn in connections.values():
            conn.close()
    cohorts = {}
    for record in records:
        if record['code'] is not None:
            cohorts.setdefault(record['config']['profile_sha256'], []).append(record)
    profiles, groups = [], []
    for profile, items in cohorts.items():
        summary, companies = _profile_summary(items)
        profiles.append(dict(profile_sha256=profile, **summary))
        groups.extend(companies)
    # Across different profiles only sample counts can be combined.
    summary = profiles[0].copy() if len(profiles) == 1 else dict(
        mixed_config=len(profiles) > 1, configs=[c for p in profiles for c in p['configs']])
    summary.update(label=label, requested=len(records), companies=len({g['code'] for g in groups}),
                   outcomes=dict(Counter(r['outcome'] for r in records)),
                   unassigned=[r['run'] for r in records if r['code'] is None])
    return dict(label=label, runs=records, companies=groups, profiles=profiles, summary=summary)


def _profile_summary(records: list[dict]) -> tuple[dict, list[dict]]:
    companies = {}
    for record in records:
        companies.setdefault(record['code'], []).append(record)
    groups = []
    for code, items in companies.items():
        judged = [item for item in items if item['action']]
        actions = [item['action'] for item in judged]
        counts = Counter(actions)
        controls = {name: _control([item['controls'][name] for item in judged]) for name in CONTROLS}
        pairs = len(actions) * (len(actions) - 1) // 2
        agreeing = sum(n * (n - 1) // 2 for n in counts.values())
        groups.append(dict(
            code=code, profile_sha256=items[0]['config']['profile_sha256'], runs=[item['run'] for item in items],
            outcomes={o: n for o in OUTCOMES if (n := sum(item['outcome'] == o for item in items))},
            actions=dict(counts), agreement=round(agreeing / pairs, 3) if pairs else None,
            flips=sum(a != b for a, b in zip(actions, actions[1:])),
            hurdle=spread([item['hurdle'] for item in items if item['outcome'] == 'priced']),
            controls=controls, controlled=all(c['same'] for c in controls.values())))
    configs = sorted({json.dumps(r['config'], ensure_ascii=False, sort_keys=True) for r in records})
    profiles = [json.loads(config) for config in configs]
    keys = ['contract', 'author', 'reviewer', 'explore_model']
    if profiles[0]['profile_sha256'] == '未记录':
        keys.append('application_sha256')
    # Missing audit fields on a failed run do not establish a different configuration.
    mixed = any(len({json.dumps(p[key], sort_keys=True) for p in profiles if p[key] is not None}) > 1 for key in keys)
    comparable = [g for g in groups if g['controlled'] and not mixed]
    ranges = [g['hurdle']['range_pct'] for g in comparable if g['hurdle'].get('n', 0) >= 2]
    agreements = [g['agreement'] for g in comparable if g['agreement'] is not None]
    return dict(
        requested=len(records),
        outcomes={o: n for o in OUTCOMES if (n := sum(r['outcome'] == o for r in records))},
        companies=len(groups),
        with_disagreement=sum(1 for a in agreements if a < 1),
        median_agreement=statistics.median(agreements) if agreements else None,
        compared=len(ranges), median_range_pct=statistics.median(ranges) if ranges else None,
        uncontrolled={g['code']: [n for n, c in g['controls'].items() if not c['same']]
                      for g in groups if not g['controlled']},
        configs=profiles, mixed_config=mixed), groups


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', type=Path, default=DATABASE)
    parser.add_argument('--since', help=f'自然观测起始日，默认 {NATURAL_SINCE}')
    parser.add_argument('--code')
    parser.add_argument('--runs', nargs='+', metavar='[PATH:]RUN_ID',
                        help='主动实验：按冻结配置档统计列出的独立重复试验，PATH 省略时取 --db')
    parser.add_argument('--label', help='主动实验组名，如 old/new，原样写入输出')
    args = parser.parse_args(argv)
    if not args.runs:
        if args.label:
            parser.error('--label 只用于 --runs 主动实验')
        with closing(connect(args.db)) as conn:
            result = observed(conn, args.since or NATURAL_SINCE, args.code)
    else:
        if args.since or args.code:
            parser.error('--runs 只统计列出的任务，不能与 --since/--code 合用')
        try:
            result = experiment([parse_target(token, args.db) for token in args.runs], args.label)
        except (ValueError, sqlite3.Error) as exc:
            parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
