"""Capture full ledger history and total-return benchmark inputs, without trade attribution."""
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
import io
import json
import sqlite3
import sys

from a_share_research import gate
from a_share_research.policy import monitor, registry


def _end(value):
    if not isinstance(value, str):
        raise registry.PolicyError('end 须为 YYYY-MM-DD 日期', 422)
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError:
        raise registry.PolicyError('end 须为 YYYY-MM-DD 日期', 422) from None
    return value


def _response(call, *, kind: str, source: str) -> dict:
    from a_share_research.policy.evaluation import _json
    from a_share_research.policy.v0 import domain
    field = 'rows' if kind == 'index' else 'value'
    failed = {field: None, 'status': 'failed', 'source': source, 'fetched_at': domain.cst_now().isoformat(),
              'source_as_of': None, 'error_code': 'UNKNOWN_ERROR', 'error_type': None,
              'fallback_reason': None, 'freshness_days': None}
    try:
        result = call()
        value = result.value
        if kind == 'index' and value is not None:
            value = value.astype(object).where(value.notna(), None).to_dict('records')
        response = {field: value, 'status': result.status, 'source': result.source, 'fetched_at': result.fetched_at,
                    'source_as_of': result.source_as_of, 'error_code': result.error_code, 'error_type': None,
                    'fallback_reason': result.fallback_reason, 'freshness_days': result.freshness_days}
        _json(response)
        return response
    except Exception as exc:
        return failed | {'error_type': type(exc).__name__}


def collect(parameters: dict, end: str, *, source: dict | None = None) -> dict:
    from a_share_research.core.providers import TushareMarketDataProvider
    from a_share_research.policy import benchmark_evaluation, capture, evaluation
    from a_share_research.policy.v0 import db, domain, paths
    from a_share_research.policy.v0.benchmark_input import BENCHMARKS, prepare
    end = _end(end)
    with db.read_only_scope(), capture._ledger_projection(source):
        now = domain.cst_now()
        capture._current_policy(parameters, now)
        if source is not None and parameters.get('account_scope') != source['watermark']['account_scope']:
            raise capture.CaptureError('ledger binding differs from selected policy scope')
        with db.read_only_db_session() as conn:
            events = conn.execute('SELECT code,event_type,event_date,shares,price,fees,tax,cash_amount,inferred '
                                  'FROM holding_events WHERE event_date<=? ORDER BY event_date,id', (end,)).fetchall()
        indices, prices = {}, {}
        if events:
            provider = TushareMarketDataProvider(token=paths.tushare_token())
            for code, _ in BENCHMARKS:
                indices[code] = _response(lambda: provider.fetch_index_bars(code, events[0][2], end),
                                           kind='index', source='tushare.index_daily')
            # Use the same pure validation/selection as replay; never select a day from a failed index.
            closes = benchmark_evaluation.index_closes(indices, end)
            if closes is not None:
                try:
                    prepared = prepare(events, closes)
                except ValueError:
                    prepared = None  # Pure replay retains the calculation failure with the original inputs.
                for code, shares in (prepared['open_shares'] if prepared else {}).items():
                    if shares:
                        prices[code] = _response(lambda: provider.fetch_close_price(code, prepared['valuation_day']),
                                                kind='stock', source='tushare.daily')
        capture._current_policy(parameters, domain.cst_now())
        bundle = evaluation.pack(dict(risk_policy=parameters, now=now, end=end,
                                      events=[list(row) for row in events], indices=indices, prices=prices))
        benchmark_evaluation.evaluate(bundle, parameters)
        return bundle


def run(conn, principal, database, request_id: str, expected_activation_id: int, end: str) -> dict:
    from a_share_research.policy import benchmark_evaluation
    request = monitor._request(expected_activation_id) | dict(action='policy.benchmark', end=_end(end))
    def collect_inputs(database, parameters, source):
        return monitor._capture(database, 'a_share_research.policy.benchmark',
            dict(parameters=parameters, end=request['end'], source=source if source['mode'] == 'ledger' else None), market_token=True)
    def evaluate_inputs(bundle, parameters):
        if bundle.get('end') != request['end']:
            raise ValueError('captured end date differs from request')
        return benchmark_evaluation.evaluate(bundle, parameters)
    return monitor._run(conn, principal, database, request_id, request, collect_inputs, evaluate_inputs, context_version=5)


def main() -> int:
    from a_share_research.policy.v0 import risk_policy
    try:
        if len(sys.argv) != 1: raise ValueError('collector takes no arguments')
        raw = sys.stdin.buffer.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024: raise ValueError('input too large')
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=risk_policy._unique_object,
                           parse_constant=risk_policy._reject_constant, parse_float=risk_policy._json_float)
        if not isinstance(value, dict) or set(value) != {'parameters', 'source', 'end'}:
            raise ValueError('invalid capture fields')
        source = value['source']
        if source is not None and (not isinstance(source, dict) or source.get('mode') != 'ledger'):
            raise ValueError('explicit source must be a ledger binding')
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            bundle = collect(value['parameters'], value['end'], source=source)
        print(gate.canonical(bundle))
        return 0
    except (ValueError, TypeError, KeyError, RuntimeError, OSError, sqlite3.Error, RecursionError) as exc:
        print(f'benchmark capture failed ({type(exc).__name__}); check policy and source readiness', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
