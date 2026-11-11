"""Collector boundaries use explicit empty-account structural fixtures, not financial evidence."""
from copy import deepcopy
from datetime import datetime
import io
import json
import sys

import pytest

from a_share_research.policy import capture, evaluation
from a_share_research.policy.v0 import db, risk_budget


NOW = datetime.fromisoformat('2026-11-11T12:00:00+08:00')


@pytest.fixture
def collector(monkeypatch):
    calls = []
    monkeypatch.setattr(capture.domain, 'cst_now', lambda: NOW)
    monkeypatch.setattr(capture.risk_policy, '_load_account_snapshot', lambda: None)
    def local():
        assert db.is_read_only()
        calls.append('local')
        return {'holdings': [], 'closed_count': 0}
    def research():
        assert db.is_read_only()
        calls.append('research')
        return {'status': 'not_configured', 'cards': []}
    def quotes(codes):
        assert db.is_read_only() and codes == []
        calls.append('quotes')
        return {}, None
    monkeypatch.setattr(capture.commands_monitor, '_load_monitor_local_snapshot', local)
    monkeypatch.setattr(capture.monitor_research, 'fetch_cards', research)
    monkeypatch.setattr(capture.commands_holdings, 'fetch_monitor_price_quotes', quotes)
    monkeypatch.setattr(capture.risk_policy, '_load_policy', lambda *a, **kw: pytest.fail('must not read another risk policy'))
    return calls


def snapshot(**changes):
    # Explicit asset-envelope fixture to exercise denominator metadata, not an account record.
    return dict(schema_version=1, currency='CNY', portfolio_value=100., account_scope='structural-scope',
                source='structural-only', recorded_at='2026-11-11T10:00:00+08:00') | changes


def scoped_parameters(**changes):
    return dict(policy_id='structural-policy', source='policy_file', account_scope='structural-scope', currency='CNY',
                effective_from='2026-01-01T00:00:00+08:00', effective_to=None,
                confirmed_at='2026-01-01T00:00:00+08:00', confirmation_ref='structural-only',
                max_position_risk_pct=2., max_portfolio_risk_pct=8.,
                field_sources={'max_position_risk_pct':'policy_file','max_portfolio_risk_pct':'policy_file'}) | changes


def test_collection_is_read_only_and_retains_unavailable_inputs(collector):
    parameters = risk_budget.policy()
    before = deepcopy(parameters)
    bundle = capture.collect(parameters)
    assert collector == ['local', 'research', 'quotes']
    assert parameters == before and not db.is_read_only()
    assert bundle['portfolio_value'] is None
    assert bundle['denominator_evidence']['denominator_requires_review'] is True
    assert bundle['research_result']['status'] == 'not_configured'
    assert bundle['industry_result'] is None
    evaluation.Bundle.model_validate(bundle)


def test_off_account_cash_without_asset_observation_cannot_create_denominator(collector):
    parameters = risk_budget.policy() | {'off_account_strategy_cash': 30.}
    bundle = capture.collect(parameters)
    assert bundle['portfolio_value'] is None
    assert bundle['denominator_evidence']['denominator_components'] is None


def test_observation_retains_unknown_valuation_time_and_saved_source(collector,monkeypatch):
    monkeypatch.setattr(capture.risk_policy, '_load_account_snapshot', lambda: snapshot())
    parameters = scoped_parameters(off_account_strategy_cash=30.)
    parameters['field_sources']['off_account_strategy_cash']='policy_file'
    bundle = capture.collect(parameters)
    assert bundle['portfolio_value'] == 130.
    evidence = bundle['denominator_evidence']
    assert evidence['denominator_components'] == {'account_value':100.,'off_account_strategy_cash':30.}
    assert evidence['portfolio_value_as_of'] is None and evidence['denominator_requires_review'] is True
    assert evidence['portfolio_value_source'] == 'account_snapshot'
    assert evidence['portfolio_value_recorded_at'] == snapshot()['recorded_at']


def test_observation_keeps_real_as_of_separate_from_capture_clock(collector,monkeypatch):
    stamp='2026-11-10T15:00:00+08:00'
    monkeypatch.setattr(capture.risk_policy, '_load_account_snapshot', lambda: snapshot(portfolio_value_as_of=stamp))
    bundle=capture.collect(scoped_parameters())
    assert bundle['denominator_evidence']['portfolio_value_as_of']==stamp
    assert bundle['now']==NOW.isoformat()


@pytest.mark.parametrize('changes', [{'account_scope':'another-scope'},
    {'portfolio_value_as_of':'2026-11-12T15:00:00+08:00'}])
def test_scope_or_future_valuation_rejected_before_market_call(collector,monkeypatch,changes):
    monkeypatch.setattr(capture.risk_policy, '_load_account_snapshot', lambda: snapshot(**changes))
    with pytest.raises(capture.CaptureError): capture.collect(scoped_parameters())
    assert collector==[] and not db.is_read_only()


@pytest.mark.parametrize('changes', [
    {'effective_from':'2027-01-01T00:00:00+08:00'},
    {'confirmed_at':'2027-01-01T00:00:00+08:00'},
    {'effective_to':'2026-02-01T00:00:00+08:00'},
])
def test_invalid_policy_window_rejected_before_market_call(collector,changes):
    with pytest.raises(capture.CaptureError): capture.collect(scoped_parameters(**changes))
    assert collector==[]


def test_unconfirmed_scope_cannot_collect_live_inputs(collector):
    marker='not_applied_account_scope_required'
    parameters=dict(policy_id='structural',source=marker,status=marker,max_position_risk_pct=None,
                    max_portfolio_risk_pct=None,field_sources=dict(max_position_risk_pct=marker,max_portfolio_risk_pct=marker))
    with pytest.raises(capture.CaptureError): capture.collect(parameters)
    assert collector==[]


def test_policy_expiring_during_collection_is_not_returned(collector,monkeypatch):
    clocks=iter((NOW,datetime.fromisoformat('2026-11-11T12:00:02+08:00')))
    monkeypatch.setattr(capture.domain,'cst_now',lambda:next(clocks))
    with pytest.raises(ValueError,match='not effective'):
        capture.collect(scoped_parameters(effective_to='2026-11-11T12:00:01+08:00'))
    assert collector==['local','research','quotes']


def test_failed_collection_restores_read_only_scope(collector,monkeypatch):
    def broken(): raise OSError('structural storage fault')
    monkeypatch.setattr(capture.commands_monitor,'_load_monitor_local_snapshot',broken)
    with pytest.raises(OSError): capture.collect(risk_budget.policy())
    assert not db.is_read_only()


def run_main(monkeypatch,raw):
    monkeypatch.setattr(sys,'argv',['capture'])
    monkeypatch.setattr(sys,'stdin',io.TextIOWrapper(io.BytesIO(raw),encoding='utf-8'))
    return capture.main()


@pytest.mark.parametrize('raw',[b'{bad}',b'{"x":1,"x":2}',b'{"x":NaN}',b'{"x":1e999}',b'[]',b'x'*(1024*1024+1),b'\xff'])
def test_cli_strict_input_and_safe_failure(collector,monkeypatch,capsys,raw):
    assert run_main(monkeypatch,raw)==1
    output=capsys.readouterr()
    assert output.out=='' and 'policy capture failed' in output.err
    assert collector==[]


def test_cli_does_not_echo_exception_or_vendor_output(collector,monkeypatch,capsys):
    def bad(_):
        print('private-vendor-text')
        print('private-provider-body',file=sys.stderr)
        raise ValueError('private-secret-value')
    monkeypatch.setattr(capture,'collect',bad)
    assert run_main(monkeypatch,b'{}')==1
    output=capsys.readouterr()
    assert output.out=='' and 'private' not in output.err and 'ValueError' in output.err


def test_cli_only_returns_json_bundle(collector,monkeypatch,capsys):
    assert run_main(monkeypatch,json.dumps(risk_budget.policy()).encode())==0
    output=capsys.readouterr()
    assert output.err==''
    evaluation.Bundle.model_validate(json.loads(output.out))


def test_cli_rejects_all_command_arguments(collector,monkeypatch,capsys):
    monkeypatch.setattr(sys,'argv',['capture','--path','/private/source'])
    monkeypatch.setattr(sys,'stdin',io.TextIOWrapper(io.BytesIO(b'{}'),encoding='utf-8'))
    assert capture.main()==1
    output=capsys.readouterr()
    assert output.out=='' and '/private/source' not in output.err
    assert collector==[]
