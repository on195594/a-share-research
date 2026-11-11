"""Capture the legacy holdings check, including persisted P3 review evidence, without writes."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import sqlite3
import sys

from a_share_research import gate
from a_share_research.policy import monitor


def collect(parameters: dict, *, source: dict | None = None) -> dict:
    from a_share_research.policy import capture, evaluation, holdings_check_evaluation
    from a_share_research.policy.v0 import db, domain, market_quotes, store
    with db.read_only_scope(), capture._ledger_projection(source):
        capture._current_policy(parameters, domain.cst_now())
        if source is not None and parameters.get('account_scope') != source['watermark']['account_scope']:
            raise capture.CaptureError('ledger binding differs from selected policy scope')
        with db.read_only_db_session() as conn, conn:
            if not conn.in_transaction:
                conn.execute('BEGIN DEFERRED')
            holdings = conn.execute('SELECT id,code,name,cost_price,stop_loss_15,stop_loss_20 '
                                    'FROM holdings WHERE exit_date IS NULL ORDER BY code').fetchall()
            defers = conn.execute("SELECT holding_id,evidence,review_due FROM holding_alerts "
                                 "WHERE reason_code='price_stop2_liquidity_defer' AND status!='resolved'").fetchall()
        now = domain.cst_now()
        quotes, fundamentals, market_snapshot = {}, {}, None
        if holdings:
            codes = [row[1] for row in holdings]
            quotes = market_quotes.fetch_current_price_quotes(codes)
            # The v0 provider currently returns None. Retain that absence, never invent a market count.
            market_snapshot = market_quotes.fetch_market_limit_down_snapshot()
            fundamentals = {code: store.get_fundamentals(code) for code in codes}
        capture._current_policy(parameters, domain.cst_now())
        bundle = evaluation.pack(dict(risk_policy=parameters, now=now, holdings=[list(row) for row in holdings],
                                      defers=[list(row) for row in defers], quotes=quotes,
                                      fundamentals=fundamentals, market_snapshot=market_snapshot))
        holdings_check_evaluation.evaluate(bundle, parameters)
        return bundle


def run(conn, principal, database, request_id: str, expected_activation_id: int) -> dict:
    from a_share_research.policy import holdings_check_evaluation
    request = monitor._request(expected_activation_id) | dict(action='policy.holdings_check')
    def collect_inputs(database, parameters, source):
        return monitor._capture(database, 'a_share_research.policy.holdings_check',
                                dict(parameters=parameters, source=source if source['mode'] == 'ledger' else None))
    return monitor._run(conn, principal, database, request_id, request, collect_inputs,
                        holdings_check_evaluation.evaluate, context_version=8)


def main() -> int:
    from a_share_research.policy.v0 import risk_policy
    try:
        if len(sys.argv) != 1:
            raise ValueError('collector takes no arguments')
        raw = sys.stdin.buffer.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError('input too large')
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=risk_policy._unique_object,
                           parse_constant=risk_policy._reject_constant, parse_float=risk_policy._json_float)
        if not isinstance(value, dict) or set(value) != {'parameters', 'source'}:
            raise ValueError('invalid capture fields')
        source = value['source']
        if source is not None and (not isinstance(source, dict) or source.get('mode') != 'ledger'):
            raise ValueError('explicit source must be a ledger binding')
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            bundle = collect(value['parameters'], source=source)
        print(gate.canonical(bundle))
        return 0
    except (ValueError, TypeError, KeyError, RuntimeError, OSError, sqlite3.Error, RecursionError) as exc:
        print(f'holdings check capture failed ({type(exc).__name__}); check policy and source readiness', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
