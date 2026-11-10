"""V3 frozen H1/H3 scoring; read-only stored judgments and market observations."""
import json
import logging
import sqlite3
import statistics
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from a_share_research import market, batches, db, screening, validation, valuation
from a_share_research.db import SHANGHAI

log = logging.getLogger(__name__)
H3_FLAT = 0.01
H3_MAX_DAYS = 10
NOTE = ('H3 方向是固定价格下模型中性情景价值的变化，不是价格预测；目标年度滚动、'
        '期限不同时仍在同一价格和日期比较年化回报。latest 为中途参考；不计算 p 值。')


def direction_of(delta) -> str:
    return '不变' if abs(delta) < H3_FLAT else '上调' if delta > 0 else '下调'


def direction(prior, followup, close, day: str) -> dict:
    results = []
    for label, calc in (('先前', prior), ('跟进', followup)):
        result = valuation.at(calc, close, day)
        if result.get('reason'):
            return {'reason': f"{label}：{result['reason']}"}
        results.append(result)
    before, after = [r['annual_returns']['中性'] for r in results]
    # Decimal subtraction preserves the inclusive 1 percentage-point boundary.
    delta = float(Decimal(str(after)) - Decimal(str(before)))
    return {'direction': direction_of(delta), 'delta': delta, 'prior_return': before,
            'followup_return': after, 'prior_horizon': results[0]['horizon'],
            'followup_horizon': results[1]['horizon']}


def h1_dates(entry: str, months: list[int], trade_days: list[str], last: str) -> dict:
    entry = entry.replace('-', '')
    out = {}
    for m in months:
        target = screening._months_after(entry, m)
        day = next((d for d in trade_days if d >= target), None)
        if day and day <= last:
            out[f'{m}m'] = day
    if last >= entry:
        out['latest'] = last
    return out


def group_stats(values: list[float]) -> dict:
    return {'n': len(values), 'mean': statistics.fmean(values) if values else None,
            'median': statistics.median(values) if values else None,
            'left_tail': sum(v <= validation.LEFT_TAIL for v in values) / len(values) if values else None}


def score_batch(batch: dict, trade_days: list[str], bench: dict, stocks: dict) -> dict:
    entry = batch['entry']
    groups = dict(zip(('TOP', 'BOTTOM'), batches.ranking_halves(batch)))
    out = {'batch': batch['id'], 'entry': entry['date'], 'rows': [], 'horizons': {},
           'configured_horizons': [f'{m}m' for m in batch['rule']['horizons_months']],
           'unfilled': {g: sum(not entry['prices'].get(c, {}).get('filled') for c in codes)
                        for g, codes in groups.items()}}
    baseline = entry.get('benchmarks', {}).get(validation.BENCHMARK, {}).get('value')
    if not screening._positive(baseline):
        return out | {'reason': f'入场缺少有效 {validation.BENCHMARK}，批次不能记分'}
    dates = h1_dates(entry['date'], batch['rule']['horizons_months'], trade_days, max(bench)) if bench else {}
    for group, codes in groups.items():
        for code in codes:
            price = entry['prices'].get(code, {})
            if not price.get('filled'):
                continue
            returns, missing = {}, []
            for horizon, day in dates.items():
                result = validation.excess(stocks.get(code, {}), bench, entry['date'], day,
                                           stock_start=price['open'] * price['adj'], benchmark_start=baseline)
                if result is None:
                    missing.append(horizon)
                else:
                    returns[horizon] = result | {'date': day}
            out['rows'].append({'code': code, 'group': group, 'returns': returns, 'missing': missing})
    for horizon, day in dates.items():
        stats = {g: group_stats([r['returns'][horizon]['excess'] for r in out['rows']
                                if r['group'] == g and horizon in r['returns']]) for g in groups}
        # Missing filled-stock observations must not silently change the comparison universe.
        complete = all(horizon in r['returns'] for r in out['rows'])
        difference = (stats['BOTTOM']['mean'] - stats['TOP']['mean']
                      if complete and all(stats[g]['n'] for g in groups) else None)
        out['horizons'][horizon] = stats | {'date': day, 'difference': difference, 'complete': complete}
    return out


def summarize_h1(results: list[dict]) -> list[dict]:
    horizons = sorted({h for b in results for h in [*b['configured_horizons'], *b['horizons']]})
    out = []
    for h in horizons:
        eligible = [b for b in results if b['horizons'].get(h, {}).get('difference') is not None]
        diffs = [b['horizons'][h]['difference'] for b in eligible]
        pooled = {g: group_stats([r['returns'][h]['excess'] for b in eligible for r in b['rows']
                                 if r['group'] == g]) for g in ('TOP', 'BOTTOM')}
        out.append({'horizon': h, 'batches': len(eligible), 'matured_batches': len(eligible) if h != 'latest' else 0,
                    'mean': statistics.fmean(diffs) if diffs else None,
                    'median': statistics.median(diffs) if diffs else None,
                    'bottom_below_top': sum(d < 0 for d in diffs), 'pooled': pooled,
                    'left_tail_difference': pooled['BOTTOM']['left_tail'] - pooled['TOP']['left_tail'] if diffs else None})
    return out


def load_samples(conn) -> list[dict]:
    """Mirror service.company_card eligibility/order as-of run creation without changing live services."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT r.id AS report,r.run_id,r.code,r.company_key,u.created_at AS created,"
        "COALESCE(u.finished_at,r.created_at) AS completed,r.body,e.announced,e.doc_key "
        "FROM scan_events e JOIN runs u ON u.id=e.run_id JOIN reports r ON r.run_id=u.id "
        "WHERE e.action='queued_review' AND r.passed=1 AND e.company_key=r.company_key "
        "ORDER BY r.id,e.announced,e.id").fetchall()
    grouped = {}
    for r in rows:
        if r['report'] not in grouped:
            prior = conn.execute(
                "SELECT r.id,r.body FROM reports r JOIN runs u ON u.id=r.run_id "
                f"WHERE r.company_key=? AND {db.FORMAL_REPORT} "
                "AND julianday(COALESCE(u.finished_at,r.created_at)) < julianday(?) "
                "ORDER BY r.created_at DESC,r.id DESC LIMIT 1", (r['company_key'], r['created'])).fetchone()
            grouped[r['report']] = {k: r[k] for k in ('report', 'run_id', 'code', 'created', 'completed', 'announced')}
            grouped[r['report']].update(doc_keys=[], prior_report=prior['id'] if prior else None,
                                       prior_calc=json.loads(prior['body']).get('valuation_calc') if prior else None,
                                       followup_calc=json.loads(r['body']).get('valuation_calc'))
        if r['doc_key'] not in grouped[r['report']]['doc_keys']:
            grouped[r['report']]['doc_keys'].append(r['doc_key'])
    return list(grouped.values())


def sample_reason(sample: dict) -> str | None:
    for label, key in (('先前', 'prior_calc'), ('跟进', 'followup_calc')):
        calc = sample[key]
        if not isinstance(calc, dict) or not calc or calc.get('not_priced') or 'error' in calc:
            return f'{label}报告缺少已定价 valuation_calc（缺失、not_priced 或 error）'
    return None


def score_sample(sample: dict, trade_days: list[str], bench: dict, raw: dict, adjusted: dict) -> dict:
    completed = datetime.fromisoformat(sample['completed']).astimezone(SHANGHAI)
    row = {k: v for k, v in sample.items() if k not in ('prior_calc', 'followup_calc')}
    latency = (completed.date() - date.fromisoformat(sample['announced'])).days
    row.update(latency_days=latency, late=latency > H3_MAX_DAYS, entry=validation.entry_day(completed, trade_days),
               result=None, consistent=None)
    if reason := sample_reason(sample):
        return row | {'reason': reason}
    d = row['entry']
    if not d or d > max(bench, default=''):
        return row | {'pending': '待入场行情'}
    if d not in raw:
        return row | {'reason': '入场日缺少未复权收盘价，不顺延'}
    row.update(direction(sample['prior_calc'], sample['followup_calc'], raw[d], date.fromisoformat(d).isoformat()))
    if row.get('reason'):
        return row
    target = validation.horizon_dates(trade_days, d, max(bench)).get('60d')
    if target:
        row['result'] = validation.excess(adjusted, bench, d, target)
        if row['result'] is None:
            return row | {'reason': '60d 到期但缺少收益计算行情'}
        row['result']['date'] = target
        if row['direction'] != '不变' and not row['late']:
            value = row['result']['excess']
            row['consistent'] = value > 0 if row['direction'] == '上调' else value < 0
    return row


def summarize_h3(rows: list[dict]) -> list[dict]:
    out = []
    for direction_name in ('上调', '下调', '不变'):
        group = [r for r in rows if r.get('direction') == direction_name and not r.get('reason')]
        scored = [r for r in group if r['consistent'] is not None]
        out.append({'direction': direction_name, 'n': len(group), 'matured': sum(r['result'] is not None for r in group),
                    'eligible': len(scored), 'consistency': statistics.fmean(r['consistent'] for r in scored) if scored else None,
                    'median_latency': statistics.median(r['latency_days'] for r in group) if group else None,
                    'pending': sum(r['result'] is None for r in group), 'late': sum(r['late'] for r in group)})
    return out


def score(database: str, pro, today: date) -> dict:
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        selected = [b for r in conn.execute("SELECT * FROM batches WHERE status IN (?, ?)", batches.ENTERED)
                    if (b := batches.load(r)).get('entry') and batches.ranking_halves(b)]
        samples = load_samples(conn)
    starts = [b['entry']['date'] for b in selected] + [s['completed'][:10].replace('-', '') for s in samples]
    end = screening._compact(today)
    trade_days, bench, adjusted, raw = [], {}, {}, {}
    if starts:
        start = min(starts)
        trade_days = market.trade_days(pro, start, end)
        frame = screening._query(pro, 'index_daily', ts_code=validation.BENCHMARK, start_date=start, end_date=end)
        bench = dict(zip(frame['trade_date'].astype(str), frame['close'].astype(float)))
        if not bench:
            raise screening.ScreenError('H00906.CSI 无可用行情，不能确定到期日')
        codes = {c for b in selected for group in batches.ranking_halves(b) for c in group
                 if b['entry']['prices'].get(c, {}).get('filled')}
        codes |= {s['code'] for s in samples if not sample_reason(s)}
        for code in sorted(codes):
            adjusted[code] = validation._adjusted_closes(pro, code, start, max(bench))
        for code in sorted({s['code'] for s in samples if not sample_reason(s)}):
            frame = screening._query(pro, 'daily', ts_code=code, start_date=start, end_date=max(bench), fields='trade_date,close')
            raw[code] = dict(zip(frame['trade_date'].astype(str), frame['close'].astype(float)))
    h1 = [score_batch(b, trade_days, bench, adjusted) for b in selected]
    h3 = [score_sample(s, trade_days, bench, raw.get(s['code'], {}), adjusted.get(s['code'], {})) for s in samples]
    return {'benchmark': validation.BENCHMARK, 'benchmark_last': max(bench, default=None), 'note': NOTE,
            'rows': {'h1': h1, 'h3': h3}, 'summary': {'h1': summarize_h1(h1), 'h3': summarize_h3(h3),
            'h3_total': len(h3), 'h3_excluded': sum(bool(r.get('reason')) for r in h3),
            'h3_pending_direction': sum('direction' not in r and not r.get('reason') for r in h3)}}


def _pct(value) -> str:
    return validation._pct(value) if value is not None else '—'


def run(database: str, output: str | None) -> None:
    result = score(database, market.client(), datetime.now(SHANGHAI).date())
    log.info('V3 假设检验：基准 %s 最新 %s；%s', result['benchmark'], result['benchmark_last'], NOTE)
    for b in result['rows']['h1']:
        log.info('H1 批次 #%s 未成交 %s%s', b['batch'], b['unfilled'], ' 排除：' + b['reason'] if b.get('reason') else '')
        for h, stats in b['horizons'].items():
            for group in ('TOP', 'BOTTOM'):
                g = stats[group]
                log.info('H1 #%s %s%s %s n=%s 均值 %s 中位 %s 左尾 %s', b['batch'], h,
                         '（中途参考）' if h == 'latest' else '', group, g['n'],
                         _pct(g['mean']), _pct(g['median']), _pct(g['left_tail']))
            log.info('H1 #%s %s BOTTOM−TOP %s%s', b['batch'], h, _pct(stats['difference']),
                     '' if stats['complete'] else '（行情缺失，排除批次比较）')
    for g in result['summary']['h1']:
        log.info('H1 汇总 %s%s 可比批次 %s 到期 %s 差均值 %s 差中位 %s BOTTOM<TOP %s 左尾差 %s',
                 g['horizon'], '（中途参考）' if g['horizon'] == 'latest' else '', g['batches'],
                 g['matured_batches'], _pct(g['mean']), _pct(g['median']), g['bottom_below_top'],
                 _pct(g['left_tail_difference']))
    for r in result['rows']['h3']:
        log.info('H3 #%s %s %s 延迟 %s 天%s %s', r['report'], r['code'], r.get('direction', '—'),
                 r['latency_days'], '（超时）' if r['late'] else '', r.get('reason') or r.get('pending') or r['result'] or '待到期')
    for g in result['summary']['h3']:
        log.info('H3 汇总 %s n=%s 到期 %s 有效 %s 一致率 %s 延迟中位 %s 天 待到期 %s 超时 %s',
                 g['direction'], g['n'], g['matured'], g['eligible'], _pct(g['consistency']),
                 g['median_latency'], g['pending'], g['late'])
    log.info('H3 样本 %s 排除 %s 待方向 %s', result['summary']['h3_total'], result['summary']['h3_excluded'],
             result['summary']['h3_pending_direction'])
    if output:
        Path(output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
