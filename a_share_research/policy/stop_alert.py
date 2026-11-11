"""Isolated v0 stop input capture and immutable, identity-bound alert receipts."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import sqlite3
import sys

from a_share_research import gate
from a_share_research.policy import monitor
from a_share_research.policy.stop_state import validate_state


def collect(parameters: dict, previous_state: dict, *, source: dict | None = None) -> dict:
    """Capture canonical stop lines and current holding quotes; never write notification state."""
    from a_share_research.policy import capture, evaluation, stop_evaluation
    from a_share_research.policy.v0 import db, domain, market_quotes
    previous_state = validate_state(previous_state)
    with db.read_only_scope(), capture._ledger_projection(source):
        now = domain.cst_now()
        capture._current_policy(parameters, now)
        if source is not None and parameters.get('account_scope') != source['watermark']['account_scope']:
            raise capture.CaptureError('ledger binding differs from selected policy scope')
        holdings, quotes = [], {}
        if domain.is_a_share_trading_hours(now):
            with db.read_only_db_session() as conn:
                rows = conn.execute('SELECT code,name,stop_loss_15,stop_loss_20 FROM holdings '
                                    'WHERE exit_date IS NULL ORDER BY code').fetchall()
            holdings = [dict(zip(('code', 'name', 'stop_loss_15', 'stop_loss_20'), row, strict=True)) for row in rows]
            if holdings:
                quotes = market_quotes.fetch_current_price_quotes([row['code'] for row in holdings])
        capture._current_policy(parameters, domain.cst_now())
        bundle = evaluation.pack(dict(risk_policy=parameters, now=now, holdings=holdings,
                                      quotes=quotes, previous_state=previous_state))
        stop_evaluation.evaluate(bundle, parameters)
        return bundle


def run(conn, principal, database, request_id: str, expected_activation_id: int, previous_state: dict) -> dict:
    """Retain one alert assessment; the caller owns sending and advancing its notification state."""
    from a_share_research.policy import stop_evaluation
    request = monitor._request(expected_activation_id) | dict(action='policy.stop_alert', previous_state=validate_state(previous_state))
    def collect_inputs(database, parameters, source):
        return monitor._capture(database, 'a_share_research.policy.stop_alert',
            dict(parameters=parameters, previous_state=request['previous_state'],
                 source=source if source['mode'] == 'ledger' else None))
    def evaluate_inputs(bundle, parameters):
        if bundle.get('previous_state') != request['previous_state']:
            raise ValueError('captured notification state differs from the request')
        return stop_evaluation.evaluate(bundle, parameters)
    return monitor._run(conn, principal, database, request_id, request, collect_inputs, evaluate_inputs, context_version=3)


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
        if not isinstance(value, dict) or set(value) != {'parameters', 'previous_state', 'source'}:
            raise ValueError('invalid capture fields')
        source = value['source']
        if source is not None and (not isinstance(source, dict) or source.get('mode') != 'ledger'):
            raise ValueError('explicit source must be a ledger binding')
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            bundle = collect(value['parameters'], value['previous_state'], source=source)
        print(gate.canonical(bundle))
        return 0
    except (ValueError, TypeError, KeyError, RuntimeError, OSError, sqlite3.Error, RecursionError) as exc:
        print(f'stop capture failed ({type(exc).__name__}); check policy and source readiness', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
