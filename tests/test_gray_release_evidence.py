"""Offline admission consumer tests; fixture approvals are never operational evidence."""
import copy
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from core.gray_release import (
    AdmissionBoundOrderGuard, GrayReleasePolicy, canonical_bundle_sha256,
    read_pinned_admission_evidence,
)
from core.live_safety import SafetyConfigurationError, StartupSafetyPolicy
from core.runtime_identity import RuntimeIdentity
from core.state_store_v2 import StateStore
from tests.test_phase6_operational_readiness import passing_bundle


NOW = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
SOURCE, CONFIG = "a" * 64, "b" * 64


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANT_R8_APPROVED", "approved")
    identity = RuntimeIdentity("binance", "live", "test-account", "spot")
    startup = StartupSafetyPolicy(False, "binance", "spot", ("BTC/USDT",),
                                  ("binance",), ("spot",), ("BTC/USDT",), "USDT", 10, 20)
    snapshot = tmp_path / "snapshot.db"
    store = StateStore(str(snapshot), identity=identity)
    store.close()
    expected = {"source_sha256": SOURCE, "config_sha256": CONFIG,
                "runtime": asdict(identity), "strategy": "TrendBreakout"}
    scope = {"exchange": "binance", "symbol": "BTC/USDT", "strategy": "TrendBreakout",
             "base_currency": "USDT", "max_order_notional": 10.0, "max_daily_new_risk": 20.0}
    bundle = passing_bundle()
    approved_at = (NOW - timedelta(days=1)).isoformat()
    bundle["admission_approval"] = {"approved": True, "operator": "fixture-risk-owner", "approved_at": approved_at}
    bundle["holdout_report"].update(source_sha256=SOURCE, config_sha256=CONFIG,
                                    strategy="TrendBreakout", evaluated_at=approved_at)
    # Launching micro-live requires T-6.6, not prior successful micro-live/expansion.
    bundle["micro_live_observations"] = []
    bundle["expansion_approval"] = {}
    envelope = {"schema_version": "r8-admission-evidence/v1", "identity": expected, "scope": scope,
                "phase6_bundle": bundle,
                "approval": {**bundle["admission_approval"], "expires_at": (NOW + timedelta(days=1)).isoformat(),
                             "identity": copy.deepcopy(expected), "scope": copy.deepcopy(scope),
                             "phase6_bundle_sha256": canonical_bundle_sha256(bundle)}}
    path = tmp_path / "evidence.json"
    policy = GrayReleasePolicy("binance", "BTC/USDT", 10, 20, str(path), str(snapshot),
                               runtime_identity=identity, source_sha256=SOURCE, config_sha256=CONFIG,
                               strategy="TrendBreakout")
    exchange = MagicMock()
    exchange.fetch_api_permissions.return_value = {"enableWithdrawals": False, "enableSpotAndMarginTrading": True}
    return SimpleNamespace(envelope=envelope, policy=policy, startup=startup, exchange=exchange, path=path)


def pin(case, *, reapprove=True):
    if reapprove and isinstance(case.envelope.get("approval"), dict):
        case.envelope["approval"]["phase6_bundle_sha256"] = canonical_bundle_sha256(case.envelope["phase6_bundle"])
    data = json.dumps(case.envelope, ensure_ascii=False, allow_nan=False).encode()
    case.path.write_bytes(data)
    case.policy = replace(case.policy, evidence_sha256=hashlib.sha256(data).hexdigest())
    return case.policy


def validate(case):
    return case.policy.validate(case.startup, case.exchange, checked_at=NOW)


def test_valid_raw_bundle_recomputes_admission_and_preserves_permission_check(setup):
    pin(setup)
    result = validate(setup)
    assert result["passed"] is True
    assert result["paper"]["elapsed_days"] == 56
    assert len(result["paper"]["regimes"]) == 2
    assert result["identity"]["source_sha256"] == SOURCE
    assert result["evidence_sha256"] == setup.policy.evidence_sha256
    setup.exchange.fetch_api_permissions.assert_called_once()


@pytest.mark.parametrize("summary", [{"passed": True}, {"admission_passed": True},
                                     {"tasks": {"T-6.6": {"passed": True}}}])
def test_pinned_legacy_summaries_are_rejected(setup, summary):
    setup.envelope = summary
    pin(setup, reapprove=False)
    with pytest.raises(SafetyConfigurationError, match="summary reports"):
        validate(setup)
    setup.exchange.fetch_api_permissions.assert_not_called()


@pytest.mark.parametrize("mode", ["14_days", "missing_day", "single_regime", "future", "naive",
                                  "unhealthy", "missing_layer", "open_p0", "holdout_rejected",
                                  "other_strategy", "other_symbol", "future_monitoring"])
def test_recomputed_raw_evidence_rejects_bad_observations(setup, mode):
    bundle = setup.envelope["phase6_bundle"]
    rows = bundle["paper_observations"]
    if mode == "14_days":
        bundle["paper_observations"] = rows[:14]
        bundle["minimum_paper_days"] = 14
    elif mode == "missing_day":
        del rows[25]
    elif mode == "single_regime":
        for row in rows:
            row["regime"] = "TREND_UP"
    elif mode == "future":
        for number, row in enumerate(rows):
            row["timestamp"] = (NOW + timedelta(days=number + 1)).isoformat()
    elif mode == "naive":
        rows[0]["timestamp"] = "2026-01-01T00:00:00"
    elif mode == "unhealthy":
        bundle["monitoring_snapshots"][0]["data_quality"]["ok"] = False
    elif mode == "missing_layer":
        bundle["actual_lifecycle"].pop("costs")
    elif mode == "open_p0":
        bundle["p0_issues"][0]["status"] = "open"
    elif mode == "holdout_rejected":
        bundle["holdout_report"]["decision"] = "reject"
    elif mode in {"other_strategy", "other_symbol"}:
        field = "strategy" if mode == "other_strategy" else "symbol"
        bundle["backtest_signals"][0][field] = "different"
        bundle["shadow_signals"][0][field] = "different"
    elif mode == "future_monitoring":
        bundle["monitoring_snapshots"][0]["timestamp"] = (NOW + timedelta(seconds=1)).isoformat()
    pin(setup)
    with pytest.raises(SafetyConfigurationError):
        validate(setup)


@pytest.mark.parametrize("field", ["source", "config", "account", "environment", "market_type", "strategy",
                                  "symbol", "exchange", "currency", "order_cap", "daily_cap", "holdout_source"])
def test_scope_and_identity_do_not_come_from_the_evidence(setup, field):
    identity, scope = setup.envelope["identity"], setup.envelope["scope"]
    if field in {"source", "config"}:
        identity[field + "_sha256"] = "c" * 64
    elif field in {"account", "environment", "market_type"}:
        identity["runtime"][field] = "another"
    elif field == "strategy":
        identity["strategy"] = "RangeTrading"
    elif field in {"symbol", "exchange"}:
        scope[field] = "different"
    elif field == "currency":
        scope["base_currency"] = "USD"
    elif field in {"order_cap", "daily_cap"}:
        scope["max_order_notional" if field == "order_cap" else "max_daily_new_risk"] = 10000
    elif field == "holdout_source":
        setup.envelope["phase6_bundle"]["holdout_report"]["source_sha256"] = "d" * 64
    pin(setup)
    with pytest.raises(SafetyConfigurationError):
        validate(setup)


@pytest.mark.parametrize("mode", ["expired", "future", "not_approved", "blank_operator", "wrong_account",
                                  "wrong_scope", "wrong_bundle", "different_signer", "no_environment_approval"])
def test_operator_approval_must_bind_current_valid_evidence(setup, monkeypatch, mode):
    approval = setup.envelope["approval"]
    if mode == "expired":
        approval["expires_at"] = NOW.isoformat()
    elif mode == "future":
        approval["approved_at"] = (NOW + timedelta(seconds=1)).isoformat()
    elif mode == "not_approved":
        approval["approved"] = False
    elif mode == "blank_operator":
        approval["operator"] = " "
    elif mode == "wrong_account":
        approval["identity"]["runtime"]["account"] = "wrong"
    elif mode == "wrong_scope":
        approval["scope"]["symbol"] = "ETH/USDT"
    elif mode == "wrong_bundle":
        approval["phase6_bundle_sha256"] = "0" * 64
    elif mode == "different_signer":
        setup.envelope["phase6_bundle"]["admission_approval"]["operator"] = "other"
    elif mode == "no_environment_approval":
        monkeypatch.delenv("QUANT_R8_APPROVED")
    pin(setup, reapprove=mode != "wrong_bundle")
    with pytest.raises(SafetyConfigurationError):
        validate(setup)


def test_changed_file_without_external_repin_is_rejected(setup):
    pin(setup)
    setup.path.write_bytes(setup.path.read_bytes() + b" ")
    with pytest.raises(SafetyConfigurationError, match="SHA-256 mismatch"):
        validate(setup)


@pytest.mark.parametrize("text", ['{"schema_version":"r8-admission-evidence/v1","schema_version":"r8-admission-evidence/v1"}',
                                 '{"schema_version":"r8-admission-evidence/v1","x":NaN}'])
def test_duplicate_and_nonfinite_json_fail_closed(setup, text):
    setup.path.write_text(text, encoding="utf-8")
    with pytest.raises(SafetyConfigurationError):
        read_pinned_admission_evidence(str(setup.path), hashlib.sha256(text.encode()).hexdigest())


def test_exponent_overflow_is_rejected_even_inside_unused_bundle_field(setup):
    setup.envelope["phase6_bundle"]["unused_float"] = "OVERFLOW"
    text = json.dumps(setup.envelope).replace('"OVERFLOW"', '1e999')
    setup.path.write_text(text, encoding="utf-8")
    with pytest.raises(SafetyConfigurationError, match="nonfinite"):
        read_pinned_admission_evidence(str(setup.path), hashlib.sha256(text.encode()).hexdigest())


@pytest.mark.parametrize("mode", ["missing", "corrupt", "wrong_account", "withdrawal_permission"])
def test_rollback_and_exchange_permission_requirements_remain(setup, mode):
    pin(setup)
    snapshot = Path(setup.policy.rollback_snapshot)
    if mode == "missing":
        snapshot.unlink()
    elif mode == "corrupt":
        snapshot.write_bytes(b"not a database")
    elif mode == "wrong_account":
        wrong = snapshot.with_name("other.db")
        store = StateStore(str(wrong), identity=RuntimeIdentity("binance", "live", "other", "spot"))
        store.close()
        setup.policy = replace(setup.policy, rollback_snapshot=str(wrong))
    else:
        setup.exchange.fetch_api_permissions.return_value["enableWithdrawals"] = True
    with pytest.raises(SafetyConfigurationError):
        validate(setup)


def test_runtime_expiry_blocks_new_risk_before_reservation_but_keeps_exits(setup, monkeypatch):
    setup.envelope["approval"]["expires_at"] = NOW.isoformat()
    pin(setup)
    guard = SimpleNamespace(policy=setup.startup, assert_order_allowed=MagicMock())
    expected = {"source_sha256": SOURCE, "config_sha256": CONFIG, "strategy": "TrendBreakout"}
    bound = AdmissionBoundOrderGuard(guard, setup.policy, lambda: expected)
    original = GrayReleasePolicy.validate_evidence
    monkeypatch.setattr(GrayReleasePolicy, "validate_evidence",
                        lambda policy, startup: original(policy, startup, checked_at=NOW))
    with pytest.raises(SafetyConfigurationError, match="expired"):
        bound.assert_order_allowed("BTC/USDT", "buy", 1, 10)
    guard.assert_order_allowed.assert_not_called()
    bound.assert_order_allowed("BTC/USDT", "sell", 1, 10)
    guard.assert_order_allowed.assert_called_once_with("BTC/USDT", "sell", 1, 10)


def test_runtime_identity_drift_is_checked_before_new_risk(setup):
    pin(setup)
    guard = SimpleNamespace(policy=setup.startup, assert_order_allowed=MagicMock())
    bound = AdmissionBoundOrderGuard(guard, setup.policy, lambda: {"source_sha256": "changed"})
    with pytest.raises(SafetyConfigurationError, match="identity changed"):
        bound.assert_order_allowed("BTC/USDT", "buy", 1, 10)
    guard.assert_order_allowed.assert_not_called()


@pytest.mark.parametrize("where", ["policy", "startup", "scope", "approval"])
@pytest.mark.parametrize("value", [True, 0, -1, float("nan"), float("inf")])
def test_all_risk_caps_require_finite_positive_nonboolean_values(setup, where, value):
    pin(setup)
    if where == "policy":
        setup.policy = replace(setup.policy, max_order_notional=value)
    elif where == "startup":
        setup.startup = replace(setup.startup, max_daily_new_risk=value)
    else:
        scope = setup.envelope["scope"] if where == "scope" else setup.envelope["approval"]["scope"]
        scope["max_order_notional"] = value
        data = json.dumps(setup.envelope, allow_nan=True).encode()
        setup.path.write_bytes(data)
        setup.policy = replace(setup.policy, evidence_sha256=hashlib.sha256(data).hexdigest())
    with pytest.raises(SafetyConfigurationError):
        validate(setup)


def test_live_cli_requires_external_pin_before_credentials_or_network(monkeypatch):
    import run_live
    credentials = MagicMock(side_effect=AssertionError("credentials must not be read"))
    monkeypatch.setattr(run_live, "credentials_from_environment", credentials)
    monkeypatch.setattr("sys.argv", ["run_live.py", "--live", "--r8-evidence", "missing.json"])
    with pytest.raises(SystemExit) as exc:
        run_live.main()
    assert exc.value.code == 2
    credentials.assert_not_called()


def test_expected_release_identity_is_computed_from_actual_configuration(monkeypatch):
    import run_live
    actual = run_live.current_release_identity()
    assert len(actual["source_sha256"]) == 64 and len(actual["config_sha256"]) == 64
    assert actual["strategy"] == "TrendBreakout"
    config = copy.deepcopy(run_live.config._config)
    config["routing"]["TREND_DOWN"] = "RangeTrading"
    monkeypatch.setattr(run_live.config, "_config", config)
    with pytest.raises(SafetyConfigurationError, match="loaded runtime configuration differs"):
        run_live.current_release_identity()
