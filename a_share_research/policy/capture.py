"""Capture v0 monitoring inputs under an already selected policy in an isolated process.

The caller owns authentication, policy activation and source consistency checks.
This module never selects another policy, writes account state or accepts paths.
"""
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
import io
import json
import sqlite3
import sys

from a_share_research import gate
from a_share_research.policy import evaluation
from a_share_research.policy.v0 import commands_holdings, commands_monitor, db, domain, monitor_research, risk_policy


class CaptureError(ValueError):
    """The selected policy and stored account inputs cannot be combined safely."""


def _current_policy(parameters: dict, current: datetime) -> None:
    evaluation.validate_parameters(parameters)
    if parameters['source'] == 'not_applied_account_scope_required':
        raise CaptureError('active monitoring requires a confirmed account scope')
    if 'effective_from' in parameters:
        if (risk_policy._timestamp(parameters['effective_from'], 'effective_from') > current
                or risk_policy._timestamp(parameters['confirmed_at'], 'confirmed_at') > current
                or parameters.get('effective_to') is not None
                and current > risk_policy._timestamp(parameters['effective_to'], 'effective_to')):
            raise CaptureError('selected policy is not currently effective and confirmed')


def _denominator(parameters: dict, current: datetime) -> risk_policy.RiskParameters:
    """Apply the saved policy to the local asset observation without inventing its age or value."""
    stored = risk_policy._load_account_snapshot()
    scope = parameters.get('account_scope')
    if stored is None:
        return risk_policy.RiskParameters(None, parameters, account_scope=scope)
    if scope is not None and scope != stored['account_scope']:
        raise CaptureError('stored asset scope differs from selected policy scope')
    scope = stored['account_scope']
    as_of = stored.get('portfolio_value_as_of')
    if as_of is not None:
        timestamp = risk_policy._timestamp(as_of, 'portfolio_value_as_of')
        if timestamp > current:
            raise CaptureError('stored asset valuation time is in the future')
        as_of = timestamp.isoformat()
    off_account = parameters.get('off_account_strategy_cash', 0.0)
    return risk_policy.RiskParameters(
        float(stored['portfolio_value']) + off_account, parameters,
        account_scope=scope, portfolio_value_as_of=as_of,
        off_account_strategy_cash=off_account,
        portfolio_value_source='account_snapshot', portfolio_value_recorded_at=stored['recorded_at'])


def collect(parameters: dict) -> dict:
    """Capture one read-only mirror, stored research and bounded quotes as a replayable JSON bundle."""
    with db.read_only_scope():
        current = domain.cst_now()
        _current_policy(parameters, current)
        # Own a JSON copy so collection cannot accidentally mutate the registered parameters.
        parameters = json.loads(gate.canonical(parameters))
        denominator = _denominator(parameters, current)
        local = commands_monitor._load_monitor_local_snapshot()
        research = monitor_research.fetch_cards()
        codes = list(dict.fromkeys(item['code'] for item in local['holdings']))
        quotes, industry = commands_holdings.fetch_monitor_price_quotes(codes)
        bundle = evaluation.pack(dict(
            local=local, quotes=quotes, portfolio_value=denominator.portfolio_value,
            industry_result=industry, now=domain.cst_now(), risk_policy=parameters,
            denominator_evidence=denominator.denominator_evidence(),
            account_scope_required=denominator.account_scope_required, research_result=research))
        # Validates the captured clock too, including a policy that expires during collection.
        evaluation.evaluate(bundle, parameters)
        return bundle


def main() -> int:
    """One bounded stdin JSON request and one JSON response; diagnostics never echo inputs."""
    try:
        if len(sys.argv) != 1:
            raise CaptureError('capture does not accept command arguments')
        raw = sys.stdin.buffer.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise CaptureError('capture parameter input too large')
        parameters = json.loads(raw.decode('utf-8'), object_pairs_hook=risk_policy._unique_object,
                                parse_constant=risk_policy._reject_constant, parse_float=risk_policy._json_float)
        # Vendor/legacy diagnostics are not protocol messages and may contain private input.
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            bundle = collect(parameters)
        print(gate.canonical(bundle))
        return 0
    except (ValueError, TypeError, KeyError, RuntimeError, OSError, sqlite3.Error, RecursionError) as exc:
        print(f'policy capture failed ({type(exc).__name__}); check policy validity and local source readiness', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
