"""Capture the legacy holdings check, including persisted P3 review evidence, without writes."""

from a_share_research.policy import monitor


def collect(parameters: dict, *, source: dict | None = None) -> dict:
    from a_share_research.policy import capture, evaluation, holdings_check_evaluation
    from a_share_research.policy.v0 import db, domain, market_quotes
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
        quotes, market_snapshot = {}, None
        if holdings:
            codes = [row[1] for row in holdings]
            quotes = market_quotes.fetch_current_price_quotes(codes)
            # The v0 provider currently returns None. Retain that absence, never invent a market count.
            market_snapshot = market_quotes.fetch_market_limit_down_snapshot()
        capture._current_policy(parameters, domain.cst_now())
        bundle = evaluation.pack(dict(risk_policy=parameters, now=now, holdings=[list(row) for row in holdings],
                                      defers=[list(row) for row in defers], quotes=quotes,
                                      market_snapshot=market_snapshot))
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
    return monitor.collector_main('holdings check', collect)


if __name__ == '__main__':
    raise SystemExit(main())
