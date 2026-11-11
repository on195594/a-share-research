"""S3a/P13/P22: synthetic policy sources, not confirmed personal settings."""

import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from a_share_research.policy.v0 import (
    cache,
    commands_holdings,
    db,
    paths,
    risk_budget,
    risk_policy,
)
from policy_v0.helpers import mock_price_quotes
from policy_v0.monitor.test_monitor_snapshot import _quotes, _seed_holding

NOW = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated_policy_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("A_STOCK_CONFIG_FILE", raising=False)
    monkeypatch.delenv("A_STOCK_RISK_POLICY_FILE", raising=False)


def _policy(**updates):
    return {
        "schema_version": 1,
        "policy_id": "fixture-policy",
        "account_scope": "fixture-account",
        "currency": "CNY",
        "effective_from": "2000-01-01T00:00:00+08:00",
        "effective_to": "2999-01-01T00:00:00+08:00",
        "confirmed_at": "2000-01-01T00:00:00+08:00",
        "confirmation_ref": "fixture:explicit-confirmation-not-a-token",
        "max_position_risk_pct": 1,
        "max_portfolio_risk_pct": 6,
        **updates,
    }


def _file(path, value=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_policy() if value is None else value))
    path.chmod(0o600)
    return str(path)


def _resolve(**kwargs):
    return risk_policy.resolve_risk_parameters(now=NOW, **kwargs)


def _account_file(**updates):
    value = {
        "schema_version": 1,
        "currency": "CNY",
        "account_scope": "fixture-account",
        "portfolio_value": 100000,
        "portfolio_value_as_of": None,
        "recorded_at": "2026-09-17T10:00:00+08:00",
        "source": "user_screenshot",
        **updates,
    }
    path = paths.cache_db_path().with_name("account-snapshot.json")
    _file(path, value)
    return path


def test_stored_assets_reuse_scope_without_inventing_valuation_time(tmp_path):
    path = _account_file()
    _file(tmp_path / "policy.json")
    before = path.read_bytes()
    result = _resolve(policy_file=str(tmp_path / "policy.json"))
    assert result.portfolio_value == 100000
    assert result.account_scope == "fixture-account"
    assert result.risk_policy["source"] == "policy_file"
    assert result.denominator_source() == "account_snapshot"
    evidence = result.denominator_evidence()
    assert evidence["portfolio_value_as_of"] is None
    assert evidence["denominator_requires_review"] is True
    assert evidence["portfolio_value_recorded_at"] == "2026-09-17T10:00:00+08:00"
    assert path.read_bytes() == before


def test_new_screenshot_replaces_default_value_without_a_ttl():
    _account_file(recorded_at="2000-01-01T00:00:00+08:00")
    assert _resolve().portfolio_value == 100000
    _account_file(portfolio_value=120000)
    assert _resolve().portfolio_value == 120000


def test_explicit_value_does_not_inherit_stored_scope_or_timestamp():
    _account_file(portfolio_value_as_of="2026-09-16T15:00:00+08:00")
    result = _resolve(portfolio_value=90000)
    assert result.portfolio_value == 90000
    assert result.account_scope is None and result.portfolio_value_as_of is None
    assert result.denominator_source() == "cli"


def test_stored_scope_conflict_is_not_silently_applied():
    _account_file()
    with pytest.raises(risk_policy.RiskPolicyError, match="does not match"):
        _resolve(account_scope="other-account")


@pytest.mark.parametrize(
    "stored_as_of",
    [None, "2026-09-16T15:00:00+08:00", "invalid", "2027-01-01T00:00:00+08:00"],
)
def test_cli_timestamp_cannot_relabel_a_stored_asset_observation(stored_as_of):
    path = _account_file(portfolio_value_as_of=stored_as_of)
    before = path.read_bytes()
    with pytest.raises(risk_policy.RiskPolicyError):
        _resolve(portfolio_value_as_of="2026-09-17T11:00:00+08:00")
    assert path.read_bytes() == before


def test_matching_timestamp_keeps_stored_provenance_and_explicit_value_can_override():
    stored_as_of = "2026-09-16T15:00:00+08:00"
    path = _account_file(portfolio_value_as_of=stored_as_of)
    before = path.read_bytes()
    stored = _resolve(portfolio_value_as_of="2026-09-16T07:00:00Z")
    assert stored.portfolio_value_as_of == stored_as_of
    assert stored.denominator_source() == "account_snapshot"
    assert stored.denominator_evidence()["denominator_requires_review"] is False
    explicit = _resolve(
        portfolio_value=90000,
        account_scope="fixture-account",
        portfolio_value_as_of="2026-09-17T11:00:00+08:00",
    )
    assert explicit.portfolio_value == 90000
    assert explicit.denominator_source() == "cli"
    assert explicit.portfolio_value_as_of == "2026-09-17T11:00:00+08:00"
    assert path.read_bytes() == before


def test_stored_asset_path_errors_use_the_risk_policy_error_contract(
    tmp_path, monkeypatch
):
    config = tmp_path / "invalid-permissions.env"
    config.write_text("# synthetic runtime config\n")
    config.chmod(0o644)
    monkeypatch.setenv("A_STOCK_CONFIG_FILE", str(config))
    with pytest.raises(
        risk_policy.RiskPolicyError, match="cannot read stored account snapshot"
    ):
        _resolve()


@pytest.mark.parametrize(
    "stored_scope,explicit_scope",
    [
        ("fixture-account", " fixture-account "),
        (" fixture-account ", "fixture-account"),
        (" fixture-account ", " fixture-account "),
    ],
)
def test_stored_scope_uses_existing_nonempty_string_semantics(
    stored_scope, explicit_scope
):
    path = _account_file(account_scope=stored_scope)
    before = path.read_bytes()
    result = _resolve(account_scope=explicit_scope)
    assert result.account_scope == "fixture-account"
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "updates",
    [
        {"schema_version": True},
        {"schema_version": 2},
        {"currency": "USD"},
        {"portfolio_value": 0},
        {"portfolio_value": True},
        {"portfolio_value": float("nan")},
        {"account_scope": ""},
        {"source": None},
        {"recorded_at": "2026-09-17"},
        {"portfolio_value_as_of": "2027-01-01T00:00:00+08:00"},
    ],
)
def test_invalid_stored_assets_fail_closed_but_explicit_value_can_replace_them(updates):
    _account_file(**updates)
    with pytest.raises(risk_policy.RiskPolicyError):
        _resolve()
    assert _resolve(portfolio_value=90000).portfolio_value == 90000


def test_stored_assets_require_private_permissions_and_ledger_locality(
    tmp_path, monkeypatch
):
    path = _account_file()
    path.chmod(0o644)
    with pytest.raises(risk_policy.RiskPolicyError, match="cannot read"):
        _resolve()
    monkeypatch.setenv("POLICY_V0_DB", str(tmp_path / "other" / "cache.db"))
    assert _resolve().portfolio_value is None


def test_absent_policy_does_not_create_files_and_retains_defaults(tmp_path):
    before = sorted(tmp_path.rglob("*"))
    result = _resolve(portfolio_value=100000)
    assert result.risk_policy == risk_budget.policy()
    assert result.denominator_evidence() == {
        "account_scope": None,
        "portfolio_value_as_of": None,
        "denominator_scope_status": "unknown",
        "denominator_freshness_status": "unknown",
        "denominator_requires_review": True,
        "denominator_components": {
            "account_value": 100000.0,
            "off_account_strategy_cash": 0.0,
        },
    }
    assert sorted(tmp_path.rglob("*")) == before
    assert "drawdown_observation_target_pct" not in result.risk_policy


def test_paths_priority_is_dynamic_cli_env_config_then_sibling_default(
    tmp_path, monkeypatch
):
    runtime = tmp_path / "custom/runtime.env"
    runtime.parent.mkdir()
    runtime.write_text(f"A_STOCK_RISK_POLICY_FILE={tmp_path / 'from-config.json'}\n")
    runtime.chmod(0o600)
    monkeypatch.setenv("A_STOCK_CONFIG_FILE", str(runtime))
    assert paths.risk_policy_path() == (tmp_path / "from-config.json", True)
    monkeypatch.setenv("A_STOCK_RISK_POLICY_FILE", str(tmp_path / "from-env.json"))
    assert paths.risk_policy_path() == (tmp_path / "from-env.json", True)
    assert paths.risk_policy_path(str(tmp_path / "from-cli.json")) == (
        tmp_path / "from-cli.json",
        True,
    )
    monkeypatch.delenv("A_STOCK_RISK_POLICY_FILE")
    runtime.write_text("# no selected policy\n")
    assert paths.risk_policy_path() == (runtime.with_name("risk-policy.json"), False)


@pytest.mark.parametrize("source", ["cli", "env", "runtime", "default"])
def test_policy_sources_load_without_creating_or_modifying_input(
    source, tmp_path, monkeypatch
):
    path = tmp_path / "policy.json"
    if source == "default":
        path, configured = paths.risk_policy_path()
        assert not configured
    _file(path)
    kwargs = {"account_scope": "fixture-account"}
    if source == "cli":
        kwargs["policy_file"] = str(path)
    elif source == "env":
        monkeypatch.setenv("A_STOCK_RISK_POLICY_FILE", str(path))
    elif source == "runtime":
        runtime = tmp_path / "runtime.env"
        runtime.write_text(f"A_STOCK_RISK_POLICY_FILE={path}\n")
        runtime.chmod(0o600)
        monkeypatch.setenv("A_STOCK_CONFIG_FILE", str(runtime))
    before = path.read_bytes()
    result = _resolve(**kwargs)
    assert result.risk_policy["source"] == "policy_file"
    assert result.risk_policy["policy_id"] == "fixture-policy"
    assert result.risk_policy["max_position_risk_pct"] == 1
    assert result.risk_policy["field_sources"] == {
        "max_position_risk_pct": "policy_file",
        "max_portfolio_risk_pct": "policy_file",
    }
    assert path.read_bytes() == before


def test_numeric_override_is_per_field_and_does_not_reconfirm_policy(tmp_path):
    path = _file(tmp_path / "policy.json", _policy(drawdown_observation_target_pct=7))
    result = _resolve(
        policy_file=path, account_scope="fixture-account", max_position_risk_pct=3
    )
    assert result.risk_policy["max_position_risk_pct"] == 3
    assert result.risk_policy["max_portfolio_risk_pct"] == 6
    assert result.risk_policy["drawdown_observation_target_pct"] == 7
    assert result.risk_policy["source"] == "cli_override"
    assert result.risk_policy["field_sources"]["max_position_risk_pct"] == "cli"
    assert (
        result.risk_policy["field_sources"]["max_portfolio_risk_pct"] == "policy_file"
    )
    assert json.loads(Path(path).read_text())["max_position_risk_pct"] == 1


@pytest.mark.parametrize(
    "field,bad",
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("schema_version", 1.0),
        ("policy_id", ""),
        ("account_scope", "other"),
        ("currency", "USD"),
        ("confirmation_ref", None),
        ("confirmed_at", ""),
        ("confirmed_at", "2999-01-01T00:00:00Z"),
        ("effective_from", "2999-01-01T00:00:00Z"),
        ("effective_to", "2001-01-01T00:00:00Z"),
        ("effective_to", "1900-01-01T00:00:00Z"),
        ("effective_from", "2026-01-01"),
        ("confirmed_at", "not-a-date"),
        ("max_position_risk_pct", True),
        ("max_position_risk_pct", float("nan")),
        ("max_portfolio_risk_pct", float("inf")),
        ("max_portfolio_risk_pct", 0),
        ("max_portfolio_risk_pct", 101),
        ("drawdown_observation_target_pct", -1),
        ("max_position_risk_pct", "2"),
    ],
)
def test_invalid_file_never_falls_back_even_with_cli_overrides(field, bad, tmp_path):
    path = _file(tmp_path / "policy.json", _policy(**{field: bad}))
    with pytest.raises(risk_policy.RiskPolicyError):
        _resolve(
            policy_file=path,
            account_scope="fixture-account",
            max_position_risk_pct=3,
            max_portfolio_risk_pct=9,
        )


@pytest.mark.parametrize("field", ["max_position_risk_pct", "max_portfolio_risk_pct"])
@pytest.mark.parametrize(
    "bad", [True, False, 0, -1, 100.01, float("inf"), float("nan")]
)
def test_invalid_override_is_controlled(field, bad):
    with pytest.raises(risk_policy.RiskPolicyError):
        _resolve(**{field: bad})


def test_file_without_explicit_matching_account_cannot_be_applied(tmp_path):
    path = _file(tmp_path / "policy.json")
    with pytest.raises(risk_policy.RiskPolicyError, match="account-scope"):
        _resolve(policy_file=path)


@pytest.mark.parametrize("source", ["cli", "env", "runtime"])
def test_explicit_missing_file_is_not_a_default(source, tmp_path, monkeypatch):
    missing = str(tmp_path / "does-not-exist.json")
    kwargs = {}
    if source == "cli":
        kwargs["policy_file"] = missing
    elif source == "env":
        monkeypatch.setenv("A_STOCK_RISK_POLICY_FILE", missing)
    else:
        path = tmp_path / "runtime.env"
        path.write_text(f"A_STOCK_RISK_POLICY_FILE={missing}")
        path.chmod(0o600)
        monkeypatch.setenv("A_STOCK_CONFIG_FILE", str(path))
    with pytest.raises(risk_policy.RiskPolicyError):
        _resolve(max_position_risk_pct=3, **kwargs)


@pytest.mark.parametrize(
    "case",
    [
        "permissions",
        "directory",
        "symlink",
        "owner",
        "duplicate",
        "malformed",
        "overflow",
        "array",
    ],
)
def test_discovered_bad_default_file_fails_closed(case, tmp_path, monkeypatch):
    path, _ = paths.risk_policy_path()
    _file(path)
    if case == "permissions":
        path.chmod(0o644)
    elif case in {"directory", "symlink"}:
        path.unlink()
        if case == "directory":
            path.mkdir()
        else:
            path.symlink_to(tmp_path / "missing-target")
    elif case == "owner":
        monkeypatch.setattr(
            paths.os,
            "fstat",
            lambda fd: SimpleNamespace(st_uid=os.getuid() + 1, st_mode=0o100600),
        )
    else:
        path.write_text(
            {
                "duplicate": '{"schema_version":1,"schema_version":1}',
                "malformed": "not-json",
                "overflow": '{"x":1e999}',
                "array": "[]",
            }[case]
        )
    with pytest.raises(risk_policy.RiskPolicyError):
        _resolve(account_scope="fixture-account", max_position_risk_pct=3)


@pytest.mark.parametrize(
    "scope,as_of,review",
    [
        (None, None, True),
        ("fixture-account", None, True),
        (None, "2026-09-16T16:00:00+08:00", True),
        ("fixture-account", "2000-01-01T16:00:00+08:00", False),
    ],
)
def test_denominator_provenance_never_uses_collection_time_or_invents_ttl(
    scope, as_of, review
):
    result = _resolve(
        portfolio_value=100000, account_scope=scope, portfolio_value_as_of=as_of
    )
    evidence = result.denominator_evidence()
    assert evidence["portfolio_value_as_of"] == as_of
    assert evidence["denominator_requires_review"] is review
    assert evidence["denominator_freshness_status"] == (
        "as_of_provided" if as_of else "unknown"
    )
    assert "drawdown_observation_target_pct" not in result.risk_policy


@pytest.mark.parametrize("bad", ["2026-09-16", "2999-01-01T00:00:00Z", "bad", True])
def test_bad_denominator_time_cannot_be_used(bad):
    with pytest.raises(risk_policy.RiskPolicyError):
        _resolve(portfolio_value_as_of=bad)


@pytest.mark.parametrize("override", [False, True])
@pytest.mark.parametrize("use_stored", [False, True])
def test_both_public_clis_use_identical_effective_policy_and_denominator(
    override, use_stored, tmp_path, isolated_cache_database, monkeypatch, capsys
):
    _seed_holding(isolated_cache_database, "600000")
    mock_price_quotes(monkeypatch, _quotes)
    path = _file(tmp_path / "policy.json")
    args = [
        "--portfolio-value",
        "100000",
        "--policy-file",
        path,
        "--account-scope",
        "fixture-account",
        "--portfolio-value-as-of",
        "2000-01-01T16:00:00+08:00",
    ]
    if use_stored:
        _account_file(portfolio_value_as_of="2000-01-01T16:00:00+08:00")
        args = ["--policy-file", path]
    if override:
        args += ["--max-position-risk-pct", "3"]
    assert cache.main(["monitor-snapshot", *args, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    account = payload["account"]
    assert account["risk_budget_status"] == (
        "within_budget" if override else "over_budget"
    )
    assert account["risk_policy"]["source"] == (
        "cli_override" if override else "policy_file"
    )
    assert account["portfolio_value_as_of"] == "2000-01-01T16:00:00+08:00"
    assert account["denominator_freshness_status"] == "as_of_provided"
    assert account["denominator_scope_status"] == "provided"
    assert payload["manifest"]["denominator_source"] == (
        "account_snapshot" if use_stored else "cli"
    )
    assert cache.main(["portfolio-risk", *args]) == 0
    text = capsys.readouterr().out
    assert ("已知超预算" in text) is not override
    assert "fixture-policy" in text and "fixture-account" in text
    assert "2000-01-01T16:00:00+08:00" in text
    assert account["risk_policy"]["source"] in text
    assert json.loads(Path(path).read_text())["max_position_risk_pct"] == 1


@pytest.mark.parametrize("command", ["portfolio-risk", "monitor-snapshot"])
def test_invalid_runtime_config_is_controlled_before_policy_use(
    command, tmp_path, monkeypatch, capsys
):
    runtime = tmp_path / "runtime.env"
    runtime.write_text("A_STOCK_RISK_POLICY_FILE=missing.json\n")
    runtime.chmod(0o644)
    monkeypatch.setenv("A_STOCK_CONFIG_FILE", str(runtime))
    args = [command, "--json"] if command == "monitor-snapshot" else [command]
    assert cache.main(args) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "运行配置不可用" in captured.err


@pytest.mark.parametrize("command", ["portfolio-risk", "monitor-snapshot"])
def test_policy_error_precedes_database_network_and_has_no_report(
    command, tmp_path, monkeypatch, capsys
):
    path = _file(tmp_path / "policy.json", _policy(confirmation_ref=""))
    monkeypatch.setattr(
        commands_holdings.db,
        "read_only_db_session",
        lambda *a: pytest.fail("database access"),
    )
    monkeypatch.setattr(socket.socket, "connect", lambda *a: pytest.fail("network"))
    args = [
        command,
        "--policy-file",
        path,
        "--account-scope",
        "fixture-account",
        "--max-position-risk-pct",
        "3",
    ]
    if command == "monitor-snapshot":
        args += ["--json"]
    assert cache.main(args) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "风险参数不可用" in captured.err


def test_off_account_strategy_cash_extends_cli_denominator_in_both_clis(
    tmp_path, isolated_cache_database, monkeypatch, capsys
):
    _seed_holding(isolated_cache_database, "600000")
    mock_price_quotes(monkeypatch, _quotes)
    path = _file(tmp_path / "policy.json", _policy(off_account_strategy_cash=150000))
    args = [
        "--portfolio-value",
        "100000",
        "--policy-file",
        path,
        "--account-scope",
        "fixture-account",
    ]
    assert cache.main(["monitor-snapshot", *args, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    account = payload["account"]
    # 2000 CNY at risk: 2% of the broker account alone, 0.8% of strategy capital.
    assert account["portfolio_value"] == 250000
    assert account["denominator_components"] == {
        "account_value": 100000.0,
        "off_account_strategy_cash": 150000.0,
    }
    assert account["risk_budget_status"] == "within_budget"
    assert account["risk_policy"]["field_sources"]["off_account_strategy_cash"] == (
        "policy_file"
    )
    assert payload["manifest"]["denominator_source"] == "cli+policy_file"
    assert payload["holdings"][0]["stop_risk_unrealized_gain"] == 0
    assert cache.main(["portfolio-risk", *args]) == 0
    text = capsys.readouterr().out
    assert "账户外策略现金 150000.00元" in text and "已知超预算" not in text


def test_off_account_strategy_cash_never_creates_a_denominator(tmp_path):
    path = _file(tmp_path / "policy.json", _policy(off_account_strategy_cash=5000))
    result = _resolve(policy_file=path, account_scope="fixture-account")
    assert result.portfolio_value is None
    assert result.denominator_evidence()["denominator_components"] is None
    assert result.denominator_source() is None


@pytest.mark.parametrize("bad", [-1, True, "1000", None])
def test_invalid_off_account_strategy_cash_fails_closed(bad, tmp_path):
    path = _file(tmp_path / "policy.json", _policy(off_account_strategy_cash=bad))
    with pytest.raises(risk_policy.RiskPolicyError):
        _resolve(portfolio_value=1, policy_file=path, account_scope="fixture-account")


@pytest.mark.parametrize(
    ("price", "reference_cost", "gain"),
    [
        (29.41, 25.2014, 800 * (29.41 - 25.2014)),  # 600900 on 2026-10-15
        (24.0, 25.2014, 0.0),  # below reference cost: all principal
        (29.41, 19.0, 800 * (29.41 - 20.665)),  # cost below stop: all gain
        (29.41, None, None),  # unknown cost: attribution unknown
    ],
)
def test_stop_risk_splits_into_principal_and_unrealized_gain(
    price, reference_cost, gain
):
    risk = commands_holdings.calculate_position_risk(
        800, price, 20.665, 75911.02, reference_cost
    )
    assert risk["stop_risk"] == pytest.approx(800 * (price - 20.665))
    assert risk["stop_risk_unrealized_gain"] == (
        None if gain is None else pytest.approx(gain)
    )


def test_breach_detail_reports_drivers_and_unknown_attribution():
    limits = risk_budget.policy(6, 18)
    positions = [
        {"code": "600900", "stop_risk": 6996.0, "stop_risk_unrealized_gain": 3366.88},
        {"code": "603606", "stop_risk": 8000.0, "stop_risk_unrealized_gain": None},
    ]
    breaches = risk_budget.assess(
        positions, 75911.02, limits, denominator_source="cli+policy_file"
    )["breaches"]
    detail = {item["code"]: item["detail"] for item in breaches}
    assert "principal=3629.12 CNY; unrealized_gain=3366.88 CNY" in detail["600900"]
    assert "denominator_source=cli+policy_file" in detail["600900"]
    assert "principal=unknown; unrealized_gain=unknown" in detail[None]


def test_portfolio_attribution_is_unknown_when_any_position_split_is_unknown():
    positions = [
        {"code": "600900", "stop_risk": 9000.0, "stop_risk_unrealized_gain": 3000.0},
        {"code": "603606", "stop_risk": None, "stop_risk_unrealized_gain": None},
    ]
    breaches = risk_budget.assess(positions, 100000, risk_budget.policy(10, 8))[
        "breaches"
    ]
    assert [item["code"] for item in breaches] == [None]
    assert "principal=unknown; unrealized_gain=unknown" in breaches[0]["detail"]


@pytest.mark.parametrize("with_value", [False, True])
@pytest.mark.parametrize("source", ["default", "explicit"])
def test_monitor_missing_scope_keeps_holding_layer_without_applying_policy(
    with_value, source, tmp_path, isolated_cache_database, monkeypatch, capsys
):
    holding_id = _seed_holding(
        isolated_cache_database, "600000", next_review="2000-01-01"
    )
    with db.db_session() as conn:
        conn.execute(
            """INSERT INTO holding_alerts
               (holding_id, code, level, category, reason_code, reason,
                opened_at, review_due, updated_at)
               VALUES (?, '600000', 'yellow', 'unverified', 'fixture_due',
                       'verify disclosure', '2000-01-01', '2000-01-01', '2000-01-01')""",
            (holding_id,),
        )
        conn.commit()
    mock_price_quotes(monkeypatch, _quotes)
    path = (
        paths.risk_policy_path()[0] if source == "default" else tmp_path / "policy.json"
    )
    _file(path, _policy(off_account_strategy_cash=150000))
    before = path.read_bytes()
    args = [
        "monitor-snapshot",
        "--json",
        "--max-position-risk-pct",
        "3",
        "--max-portfolio-risk-pct",
        "6",
    ]
    if source == "explicit":
        args += ["--policy-file", str(path)]
    if with_value:
        args += [
            "--portfolio-value",
            "100000",
            "--portfolio-value-as-of",
            "2026-01-01T00:00:00+08:00",
        ]
    assert cache.main(args) == 1
    payload = json.loads(capsys.readouterr().out)
    account = payload["account"]
    assert account["portfolio_value"] == (100000 if with_value else None)
    assert account["risk_policy"]["status"] == "not_applied_account_scope_required"
    assert account["risk_policy"]["source"] == "not_applied_account_scope_required"
    assert account["risk_policy"]["max_position_risk_pct"] is None
    assert account["risk_policy"]["max_portfolio_risk_pct"] is None
    assert account["account_scope"] is None
    assert account["denominator_requires_review"] is True
    assert account["risk_budget_status"] is None
    assert account["stock_weight_pct"] is None
    assert account["total_stop_risk_pct"] is None
    assert payload["data_status"] == "partial"
    assert payload["action_status"] == "review_candidate"
    assert payload["stop_reason"] is None
    assert payload["requires_user_confirmation"] is False
    fields = {gap["field"] for gap in payload["data_gaps"]}
    assert fields == (
        {"account_scope"} if with_value else {"account_scope", "portfolio_value"}
    )
    denominator_escalations = [
        item
        for item in payload["escalations"]
        if item["reason_code"] == "denominator_missing"
    ]
    assert len(denominator_escalations) == 1
    assert "account scope" in denominator_escalations[0]["detail"]
    if not with_value:
        assert "portfolio value" in denominator_escalations[0]["detail"]
    holding = payload["holdings"][0]
    assert holding["id"] == holding_id
    assert holding["quote"]["price"] == 100
    assert holding["stop_loss_15"] == 85
    assert holding["stop_loss_20"] == 80
    assert holding["portfolio_weight_pct"] is None
    assert holding["stop_risk_pct"] is None
    assert holding["l3"]["conditions"]
    assert holding["alerts"][0]["review_due_now"] is True
    assert holding["tier"]["tier1_status"] == "pending"
    assert any(
        item["reason_code"] == "alert_review_due" for item in payload["escalations"]
    )
    assert any(
        item["reason_code"] == "l3_review_due" for item in payload["escalations"]
    )
    assert not any(
        item["reason_code"] == "risk_budget_exceeded" for item in payload["escalations"]
    )
    assert path.read_bytes() == before
    assert payload["manifest"]["writes"] is False
    # The portfolio-risk command still requires a matching explicit scope.
    assert cache.main(["portfolio-risk", "--policy-file", str(path)]) == 1
    assert "account-scope" in capsys.readouterr().err


@pytest.mark.parametrize(
    "case", ["mismatch", "expired", "invalid", "unreadable", "missing"]
)
def test_monitor_partial_mode_does_not_hide_policy_errors(
    case, tmp_path, monkeypatch, capsys
):
    path = tmp_path / "policy.json"
    value = _policy()
    if case == "expired":
        value["effective_to"] = "2001-01-01T00:00:00+08:00"
    elif case == "invalid":
        value["max_position_risk_pct"] = -1
    _file(path, value)
    if case == "unreadable":
        path.chmod(0o644)
    elif case == "missing":
        path.unlink()
    monkeypatch.setattr(
        commands_holdings.db,
        "read_only_db_session",
        lambda *a: pytest.fail("database access"),
    )
    monkeypatch.setattr(socket.socket, "connect", lambda *a: pytest.fail("network"))
    args = ["monitor-snapshot", "--json", "--policy-file", str(path)]
    if case == "mismatch":
        args += ["--account-scope", "other-account"]
    assert cache.main(args) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "风险参数不可用" in captured.err


def test_untradable_boards_apply_with_or_without_account_scope(tmp_path):
    """0.1.36: the restriction only narrows advice, so it applies even when limits cannot."""
    path = _file(tmp_path / "policy.json", _policy(untradable_boards=["STAR"]))
    applied = _resolve(policy_file=path, account_scope="fixture-account").risk_policy
    assert (
        applied["untradable_boards"] == ["STAR"]
        and applied["max_position_risk_pct"] == 1
    )
    unscoped = _resolve(policy_file=path, allow_missing_scope=True).risk_policy
    assert unscoped["status"] == "not_applied_account_scope_required"
    assert unscoped["untradable_boards"] == ["STAR"]
    assert unscoped["field_sources"]["untradable_boards"] == "policy_file"
    assert (
        "untradable_boards"
        not in _resolve(
            policy_file=_file(tmp_path / "plain.json"), account_scope="fixture-account"
        ).risk_policy
    )


@pytest.mark.parametrize("bad", ["STAR", ["CHINEXT"], ["STAR", "STAR"], [None], {}])
def test_invalid_untradable_boards_fail_closed(bad, tmp_path):
    path = _file(tmp_path / "policy.json", _policy(untradable_boards=bad))
    with pytest.raises(risk_policy.RiskPolicyError, match="untradable_boards"):
        _resolve(policy_file=path, account_scope="fixture-account")
