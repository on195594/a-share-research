"""Reproducible SW constituent aggregates, using cached whole-market snapshots."""

import hashlib
import time
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from statistics import median

import pandas as pd

from a_share_research import conditions, db, sources, stock_codes

BASIS = ("当前申万成分回看，存在幸存者偏差，不是历史申万指数；收益用收盘价×复权因子，"
         "只纳入两端均有有效价格和因子的股票，停牌/未上市缺失不填零；等权口径为个股收益中位数，"
         "加权口径用起点流通市值（缺失或非正权重另行剔除），不做期间再平衡。"
         "中证800为价格指数，不含分红再投资，与个股复权口径有差异。"
         "估值为正PE_TTM的中位数，剔除亏损/缺失；近五年已完成月份月末样本，"
         "分位=历史中位数不高于当前值的样本数/有效历史样本数；当前成分随历史上市情况变化，非当时可投资组合。")
FIELDS = {"daily": "ts_code,trade_date,close", "adj_factor": "ts_code,trade_date,adj_factor",
          "daily_basic": "ts_code,trade_date,pe_ttm,circ_mv"}


def number(value):
    if value is None or pd.isna(value):
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise sources.SourceError("行业输入含非数值；请记录对应接口数据缺口") from None
    if isinstance(value, bool) or not result.is_finite():
        raise sources.SourceError("行业输入含非有限数值；请重新获取对应交易日截面")
    return result


def days_from_calendar(sample):
    frame = pd.DataFrame(sample["rows"])
    params = sample["params"]
    if sample['api'] != 'trade_cal' or params.get('exchange') != 'SSE' or params.get('is_open') != '1':
        raise sources.SourceError('行业交易日历查询身份错配；请查询 SSE 开市日历')
    if not {"cal_date", "is_open", "exchange"} <= set(frame):
        raise sources.SourceError("trade_cal 缺少日期/开市/交易所列；请重新取 SSE 交易日历")
    days = []
    for row in frame.to_dict("records"):
        day = date.fromisoformat(str(row["cal_date"])).strftime("%Y%m%d")
        if row['exchange'] != 'SSE' or str(row['is_open']) != '1' or not params['start_date'] <= day <= params['end_date']:
            raise sources.SourceError("trade_cal 交易日身份或范围不符；请重新取 SSE 开市日历")
        days.append(day)
    if len(days) != len(set(days)) or len(days) < 121:
        raise sources.SourceError("交易日历重复或不足121个交易日；请扩大日历范围")
    return sorted(days)


def snapshot(sample, api, day, members=None):
    if sample['api'] != api or sample['params'] != {'trade_date': day, 'fields': FIELDS[api]}:
        raise sources.SourceError("行业输入接口/期间错配；请使用所列交易日的原始截面")
    result = {}
    for row in sample['rows']:
        code = row['ts_code']
        if row['trade_date'] != day or code in result or members is not None and code not in members:
            raise sources.SourceError("行业输入成员或期间错配/重复；请按当前成分及指定交易日重新取数")
        result[code] = {field: number(row[field]) for field in FIELDS[api].split(',')[2:]}
    return result


def industry_rows(data):
    """Pure replay: validate identities, then calculate every aggregate with Decimal inputs."""
    level, code = data['level'], data['code']
    subject = pd.DataFrame(data['subject']['rows'])
    member_frame = pd.DataFrame(data['membership']['rows'])
    subject_row = sources._membership(subject, level)
    sources.peer_members(subject, member_frame, level)
    sector = subject_row[f'{level}_code']
    if (data['subject']['api'] != 'index_member_all' or data['membership']['api'] != 'index_member_all'
            or subject_row.ts_code != code or data['subject']['params'] != {'ts_code': code, 'is_new': 'Y'}
            or data['membership']['params'] != {f'{level}_code': sector, 'is_new': 'Y'}
            or not member_frame.is_new.eq('Y').all() or not subject.is_new.eq('Y').all()):
        raise sources.SourceError("行业主体/成员查询身份错配；请用 is_new=Y 重新取成分")
    members = sorted(member_frame.ts_code.tolist())
    if code not in members or len(members) != len(set(members)):
        raise sources.SourceError("行业成员重复或不含研究公司；请重新查询当前成分")
    days = days_from_calendar(data['calendar'])
    end = days[-1]
    month_ends = list({d[:6]: d for d in days if d[:6] < end[:6]}.values())[-60:]
    if len(month_ends) != 60:
        raise sources.SourceError("估值日历不足60个完整月份；请扩大日历范围")
    starts = {w: days[-w-1] for w in (20, 60, 120)}
    selected = sorted({end, *starts.values()})
    member_hash = hashlib.sha256(','.join(members).encode()).hexdigest()[:10]
    prefix = f'X.SW.{level}.{sector.split(".")[0]}.{stock_codes.normalize_key(code)}.{end}.{member_hash}'
    anchor = f'{prefix}.pe_median'
    common = {'provider': 'TuShare', 'api': 'industry_constituents', 'dataset_ref': anchor,
              'members': members, 'level': level, 'industry_code': sector, 'company': code,
              'as_of': end, 'basis': BASIS}
    rows = []

    def emit(metric, label, unit, values, **extra):
        exact = {k: str(v) for k, v in values.items()}
        rows.append({'id': f'{prefix}.{metric}', 'label': f'{subject_row[f"{level}_name"]}·{label}',
                     'unit': unit, 'values': {k: str(v.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)) for k, v in values.items()},
                     'exact_values': exact, 'source': common | extra})

    basic = {d: snapshot(data['daily_basic'][d], 'daily_basic', d, members) for d in sorted({end, *month_ends, *starts.values()})}
    pe, pe_coverage = {}, {}
    for day in [*month_ends, end]:
        eligible = {c: r['pe_ttm'] for c, r in basic[day].items() if r['pe_ttm'] is not None and r['pe_ttm'] > 0}
        pe_coverage[day] = sorted(eligible)
        if eligible:
            pe[date.fromisoformat(day).isoformat()] = median(eligible.values())
    current = date.fromisoformat(end).isoformat()
    if current not in pe:
        raise sources.SourceError("行业当前无正PE_TTM样本，不能计算估值位置；请换层级并记录缺口")
    emit('pe_median', '正PE_TTM中位数（当前成分）', '倍', pe, eligible=pe_coverage,
         formula='median(positive nonmissing pe_ttm)', dataset=data)
    history = [pe[date.fromisoformat(d).isoformat()] for d in month_ends if date.fromisoformat(d).isoformat() in pe]
    if not history:
        raise sources.SourceError("行业历史无有效估值样本；请记录缺口")
    emit('pe_percentile', '当前PE历史月末分位（当前成分，幸存者偏差）', '%',
         {current: Decimal(sum(v <= pe[current] for v in history)) / len(history) * 100},
         sample_dates=month_ends, valid_samples=len(history), formula='count(historical_median <= current_median) / valid_months * 100')
    prices = {d: snapshot(data['daily'][d], 'daily', d, members) for d in selected}
    factors = {d: snapshot(data['adj_factor'][d], 'adj_factor', d, members) for d in selected}
    bench_sample = data['benchmark']
    if bench_sample['api'] != 'index_daily' or bench_sample['params'] != {'ts_code': '000906.SH', 'start_date': selected[0], 'end_date': end, 'fields': 'ts_code,trade_date,close'}:
        raise sources.SourceError("中证800查询期间或代码错配；请重新获取同期价格指数")
    benchmark = {}
    for r in bench_sample['rows']:
        d = r['trade_date']
        if r['ts_code'] != '000906.SH' or not selected[0] <= d <= end or d in benchmark:
            raise sources.SourceError("中证800返回主体/期间错配或重复；请重新取同期指数")
        benchmark[d] = number(r['close'])
    for window, start in starts.items():
        returns = {}
        for c in members:
            values = [prices[d].get(c, {}).get('close') for d in (start, end)] + [factors[d].get(c, {}).get('adj_factor') for d in (start, end)]
            if all(v is not None and v > 0 for v in values):
                p0, p1, a0, a1 = values
                returns[c] = (p1 * a1 / (p0 * a0) - 1) * 100
        weighted = {c: basic[start].get(c, {}).get('circ_mv') for c in returns}
        weighted = {c: w for c, w in weighted.items() if w is not None and w > 0}
        if not returns or not weighted or code not in returns or any(benchmark.get(d) is None or benchmark[d] <= 0 for d in (start, end)):
            raise sources.SourceError(f'{window}交易日有效样本/本公司/基准不足；请查看两端行情缺失并换层级或记录缺口')
        meta = {'start': start, 'end': end, 'window': window, 'sessions': days[-window-1:],
                'included': sorted(returns), 'excluded': sorted(set(members)-returns.keys()),
                'weighted_members': sorted(weighted)}
        results = {'median': (median(returns.values()), '等权收益中位数', 'median((close_end * adj_end / (close_start * adj_start) - 1) * 100)'),
                   'weighted': (sum(returns[c] * w for c, w in weighted.items()) / sum(weighted.values()), '起点流通市值加权收益', 'sum(return_pct * start_circ_mv) / sum(start_circ_mv)'),
                   'breadth': (Decimal(sum(v > 0 for v in returns.values())) / len(returns) * 100, '上涨家数占比', 'count(return_pct > 0) / valid_members * 100'),
                   'company': (returns[code], '本公司复权收益', '(close_end * adj_end / (close_start * adj_start) - 1) * 100'),
                   'benchmark': ((benchmark[end] / benchmark[start] - 1) * 100, '中证800价格指数收益', '(index_close_end / index_close_start - 1) * 100')}
        peers = {c: w for c, w in weighted.items() if c != code}
        if peers:
            results['weighted_ex'] = (sum(returns[c] * w for c, w in peers.items()) / sum(peers.values()),
                                      '剔除本公司后的起点流通市值加权收益', 'sum(return_pct * start_circ_mv) / sum(start_circ_mv), excluding company')
        for metric, (value, label, formula) in results.items():
            emit(f'{metric}_{window}', f'{window}个交易日{label}（{start}至{end}）', '%', {current: value}, **meta, formula=formula)
    return rows


def validate_rows(rows, table):
    """Recompute cited aggregates from the saved input bundle, including membership and dates."""
    registry = {r['id']: r for r in table}
    anchors = {r['source']['dataset_ref'] for r in rows if r.get('source', {}).get('api') == 'industry_constituents'}
    for anchor in anchors:
        try:
            expected = {r['id']: r for r in industry_rows(registry[anchor]['source']['dataset'])}
            for row in rows:
                if row.get('source', {}).get('dataset_ref') == anchor and row != expected.get(row['id']):
                    raise ValueError('行业数值、成员或期间与原始输入复算不符')
        except (KeyError, TypeError, ArithmeticError, sources.SourceError) as exc:
            raise ValueError('行业出处输入不完整或成员/期间错配；请重新调用 industry_trend') from exc


def industry_trend(code, level='l2'):
    if level not in sources.SW_LEVELS:
        raise ValueError('level 可用 l1/l2/l3，默认 l2')
    code = sources.to_tushare_stock_code(code)
    started = time.monotonic()
    stats = {'upstream_calls': 0, 'cache_hits': 0, 'queries': []}

    def fetch(api, **params):
        t = time.monotonic()
        frame = conditions.query(api, **params)
        hit = frame.attrs.get('cache_hit', False)
        stats['cache_hits' if hit else 'upstream_calls'] += 1
        stats['queries'].append({'api': api, 'params': params, 'cache_hit': hit, 'seconds': round(time.monotonic()-t, 3)})
        if frame.empty:
            raise sources.SourceError(f'{api} 无数据：{params}；请检查该交易日数据是否已发布，稍后重试')
        if len(frame) >= 6000:
            raise sources.SourceError(f'{api} 达到6000行，可能截断；需升级分页取数，不能用不完整截面')
        return {'api': api, 'params': params, 'rows': frame.astype(object).where(pd.notna(frame), None).to_dict('records')}

    data = {'code': code, 'level': level, 'retrieved_at': db.now()}
    data['subject'] = fetch('index_member_all', ts_code=code, is_new='Y')
    subject = sources._membership(pd.DataFrame(data['subject']['rows']), level)
    data['membership'] = fetch('index_member_all', **{f'{level}_code': subject[f'{level}_code']}, is_new='Y')
    sources.peer_members(pd.DataFrame(data['subject']['rows']), pd.DataFrame(data['membership']['rows']), level)
    members = {r['ts_code'] for r in data['membership']['rows']}
    end = datetime.now(db.SHANGHAI).date() - timedelta(days=1)
    data['calendar'] = fetch('trade_cal', exchange='SSE', start_date=(end-timedelta(days=1900)).strftime('%Y%m%d'), end_date=end.strftime('%Y%m%d'), is_open='1')
    days = days_from_calendar(data['calendar'])
    current = days[-1]
    ends = list({d[:6]: d for d in days if d[:6] < current[:6]}.values())[-60:]
    dates = sorted({current, *(days[-w-1] for w in (20, 60, 120))})
    for api in FIELDS:
        data[api] = {}
        for day in sorted(set(dates + (ends if api == 'daily_basic' else []))):
            sample = fetch(api, trade_date=day, fields=FIELDS[api])
            sample['rows'] = [r for r in sample['rows'] if r['ts_code'] in members]
            data[api][day] = sample
    data['benchmark'] = fetch('index_daily', ts_code='000906.SH', start_date=dates[0], end_date=current, fields='ts_code,trade_date,close')
    rows = industry_rows(data)
    stats['seconds'] = round(time.monotonic()-started, 3)
    return {'rows': rows, 'basis': BASIS, 'metrics': stats}
