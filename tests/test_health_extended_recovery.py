"""Opt-in recovery must never silently unlock an explicit manual lock."""
import pytest

from core.strategy_health import StrategyHealthMachine, StrategyHealthPolicy, HealthStatus


def machine(**kwargs):
    return StrategyHealthMachine("test", StrategyHealthPolicy.from_mapping({
        "consecutive_negative_cohorts": 1, "cooldown_days": 1,
        "probation_required_cohorts": 1, "max_failed_probation_cycles": 2,
        "repeated_failure_action": "extended_cooldown", **kwargs,
    }))


def close(m, day, pnl=-10):
    m.ingest_close(close_event_id=day, symbol="BTC-USDT", realized_pnl=pnl,
                   initial_risk=100, timestamp=day)
    return m.evaluate(day)


def failed_twice():
    m = machine()
    close(m, "2020-01-01")
    m.evaluate("2020-01-02")
    close(m, "2020-01-03")
    m.evaluate("2020-01-04")
    close(m, "2020-01-05")
    return m


def test_extended_recovery_expiry_and_new_evidence_gate():
    m = failed_twice()
    assert m.status == HealthStatus.COOLDOWN
    assert not m.allows_new_entries("2020-04-03")
    assert m.allows_new_entries("2020-04-04")
    assert m.risk_multiplier == 0.1
    assert m.probation_closed_cohorts == 0
    close(m, "2020-04-05", 10)
    assert m.status == HealthStatus.ACTIVE
    assert m.failed_probation_cycles == 0


def test_retry_failure_keeps_reduced_budget_and_restarts_full_cooldown():
    m = failed_twice()
    m.evaluate("2020-04-04")
    close(m, "2020-04-05")
    assert m.failed_probation_cycles == 3
    assert not m.allows_new_entries("2020-07-03")
    assert m.allows_new_entries("2020-07-04")
    assert m.risk_multiplier == 0.1


def test_restart_preserves_absolute_deadline_and_failure_counter():
    m = failed_twice()
    restored = machine()
    restored.load(m.to_dict())
    assert restored.cooldown_until == m.cooldown_until
    assert not restored.allows_new_entries("2020-04-03")
    assert restored.allows_new_entries("2020-04-04")
    assert restored.risk_multiplier == 0.1


def test_explicit_and_preexisting_manual_locks_remain_terminal():
    m = failed_twice()
    m.manual_lock("operator halt", at="2020-02-01")
    restored = machine()
    restored.load(m.to_dict())
    assert not restored.allows_new_entries("2030-01-01")
    assert restored.manual_lock_reason == "operator halt"


def test_no_timestamp_anchors_extended_not_ordinary_cooldown():
    m = machine()
    m.failed_probation_cycles = 1
    m._fail_probation(None, reason="test")
    assert m.cooldown_until is None
    m.evaluate("2020-01-05")
    assert not m.allows_new_entries("2020-04-03")
    assert m.allows_new_entries("2020-04-04")
    assert m.risk_multiplier == 0.1


@pytest.mark.parametrize("overrides", [
    {"repeated_failure_action": "unknown"}, {"extended_cooldown_days": 0},
    {"extended_cooldown_days": float("nan")}, {"extended_cooldown_days": float("inf")},
    {"extended_cooldown_days": 0.5}, {"recovery_risk_multiplier": 0.5},
    {"recovery_risk_multiplier": 0}, {"recovery_risk_multiplier": float("nan")},
])
def test_invalid_recovery_policy_rejected(overrides):
    with pytest.raises(ValueError):
        machine(**overrides)
