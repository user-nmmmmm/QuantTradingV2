"""Acceptance against the actual configured policy, not an opt-in fixture."""
from copy import deepcopy

from config.config import config
from core.strategy_health import HealthStatus, StrategyHealthMachine, StrategyHealthPolicy


def test_configured_automatic_failures_recover_after_restart_but_manual_lock_does_not():
    policy = StrategyHealthPolicy.from_mapping(deepcopy(config.get("strategy_health")))
    assert policy.repeated_failure_action == "extended_cooldown"
    machine = StrategyHealthMachine("TrendBreakout", policy)
    # Isolate the automatic-failure transition, keeping production timing and
    # risk policy; existing cohort tests exercise how evidence reaches it.
    import pandas as pd
    at = pd.Timestamp("2022-08-20", tz="UTC").to_pydatetime()
    machine.failed_probation_cycles = policy.max_failed_probation_cycles - 1
    machine._fail_probation(at, reason="probation_total_r_below_gate")
    assert machine.status is HealthStatus.COOLDOWN
    restored = StrategyHealthMachine("TrendBreakout", policy)
    restored.load(machine.to_dict())
    assert not restored.allows_new_entries("2022-11-17")
    assert restored.allows_new_entries("2022-11-18")
    assert restored.risk_multiplier == .1
    assert restored.probation_closed_cohorts == 0
    assert restored.status is HealthStatus.PROBATION
    restored.manual_lock("operator investigation", at="2022-11-19")
    again = StrategyHealthMachine("TrendBreakout", policy)
    again.load(restored.to_dict())
    assert not again.allows_new_entries("2030-01-01")
    assert again.snapshot()["recovery_requires_operator"]


def test_legacy_manual_lock_is_not_silently_reinterpreted_by_new_configuration():
    old = StrategyHealthMachine("TrendBreakout", StrategyHealthPolicy())
    old.failed_probation_cycles = 1
    old._fail_probation(None, reason="probation_total_r_below_gate")
    current = StrategyHealthMachine("TrendBreakout", StrategyHealthPolicy.from_mapping(config.get("strategy_health")))
    current.load(old.to_dict())
    assert current.status is HealthStatus.MANUAL_LOCK
    assert not current.allows_new_entries("2030-01-01")
