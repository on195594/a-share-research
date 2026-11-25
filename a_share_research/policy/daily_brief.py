"""Capture monitor and dividend evidence for a replayable v0 daily brief."""

from concurrent.futures import ThreadPoolExecutor

from a_share_research.policy import monitor


def _dividend_response(code: str) -> dict:
    """Keep provider-normalized rows and safe provenance, including failed acquisition."""
    from a_share_research.core.providers import TushareDividendProvider
    from a_share_research.policy.evaluation import _json
    from a_share_research.policy.v0 import domain, paths
    failed = dict(rows=None, source='tushare.dividend', status='failed', fetched_at=domain.cst_now().isoformat(),
                  source_as_of=None, error_code='UNKNOWN_ERROR', error_type=None)
    try:
        result = TushareDividendProvider(token=paths.tushare_token()).fetch_dividend_history(code)
        # Missing cells are JSON null; no to_json float rounding or executable serialization.
        rows = (None if result.value is None else
                result.value.astype(object).where(result.value.notna(), None).to_dict('records'))
        value = dict(rows=rows, source=result.source, status=result.status, fetched_at=result.fetched_at,
                     source_as_of=result.source_as_of, error_code=result.error_code, error_type=None)
        _json(value)
        return value
    except Exception as exc:
        # The old scan isolates each provider failure. Never echo its body or credentials.
        return failed | {'error_type': type(exc).__name__}


def collect(parameters: dict, *, source: dict | None = None) -> dict:
    from a_share_research.policy import capture, daily_evaluation, evaluation
    from a_share_research.policy.v0 import commands_holdings, db, domain
    # daily-brief never included the separate research-card opportunity feed.
    bundle = capture.collect(parameters, source=source, include_research=False)
    with db.read_only_scope(), capture._ledger_projection(source):
        with db.read_only_db_session() as conn, conn:
            if not conn.in_transaction:
                conn.execute('BEGIN DEFERRED')
            holdings = conn.execute('SELECT id,code,name,shares FROM holdings WHERE exit_date IS NULL ORDER BY code').fetchall()
            events = {row[0]: commands_holdings._load_lifecycle_events(conn, row[0]) for row in holdings}
        today = domain.cst_today()
        # Per-holding TuShare calls are network-bound; serial fetches used ~9s of the 25s capture budget.
        with ThreadPoolExecutor(max(1, min(8, len(holdings)))) as pool:
            providers = list(pool.map(_dividend_response, [row[1] for row in holdings]))
        dividends = [dict(holding_id=hid, code=code, name=name, current_shares=shares,
                          events=[list(event) for event in events[hid]], provider=provider)
                     for (hid, code, name, shares), provider in zip(holdings, providers)]
        bundle.update(dividend_today=today, brief_today=domain.cst_today(), dividends=dividends)
        bundle = evaluation.pack(bundle)
        capture._current_policy(parameters, domain.cst_now())
        daily_evaluation.evaluate(bundle, parameters)
        return bundle


def run(conn, principal, database, request_id: str, expected_activation_id: int) -> dict:
    from a_share_research.policy import daily_evaluation
    request = monitor._request(expected_activation_id) | {'action': 'policy.daily_brief'}
    def collect_inputs(database, parameters, source):
        return monitor._capture(database, 'a_share_research.policy.daily_brief',
            dict(parameters=parameters, source=source if source['mode'] == 'ledger' else None), market_token=True)
    return monitor._run(conn, principal, database, request_id, request, collect_inputs,
                        daily_evaluation.evaluate, context_version=4)


def main() -> int:
    return monitor.collector_main('daily brief', collect)


if __name__ == '__main__':
    raise SystemExit(main())
