"""Regression for the cash/underwater BLOCK_NEW deadlock, across restarts."""
from types import SimpleNamespace
import json

import pandas as pd
import pytest

from core.risk import RiskManager, BreakerAction
from core.state_store_v2 import StateStore


def manager():
    return RiskManager(
        daily_loss_limit=0.05, portfolio_drawdown_reduce=0.10,
        portfolio_drawdown_block=0.15, portfolio_drawdown_liquidate=0.20,
        portfolio_drawdown_lock=0.25, recovery_policy={"enabled": True},
    )


def evaluate(risk, day, equity=84, daily=None):
    risk.reset_daily_breaker()
    return risk.check_circuit_breaker(
        equity, equity if daily is None else daily,
        occurred_at=pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(days=day),
        bar_index=day,
    )


def blocked():
    risk = manager()
    evaluate(risk, 0, 100)
    result = evaluate(risk, 1)
    assert result.action is BreakerAction.BLOCK_NEW
    assert result.allow_position_management
    assert not result.allow_new_entries
    return risk


def test_cash_resumes_after_exact_deadline_without_rebasing():
    risk = blocked()
    assert not evaluate(risk, 30).allow_new_entries
    result = evaluate(risk, 31)
    assert result.allow_new_entries and result.allow_position_management
    assert result.force_reduce_fraction is None
    assert risk.high_water_equity == 100
    assert risk.risk_multiplier == 0.25
    assert risk.calculate_position_size(84, 10, 9) == pytest.approx(0.21)
    assert evaluate(risk, 32).allow_new_entries
    assert risk.recovery_count == 1


def test_probation_loss_restarts_cooldown_and_can_recover_again():
    risk = blocked()
    evaluate(risk, 31)
    result = evaluate(risk, 32, 81)
    assert not result.allow_new_entries
    assert risk.probation_equity is None
    assert not evaluate(risk, 61, 81).allow_new_entries
    assert evaluate(risk, 62, 81).allow_new_entries
    assert risk.recovery_count == 2
    assert risk.high_water_equity == 100


@pytest.mark.parametrize("equity,action", [(79, BreakerAction.LIQUIDATE), (74, BreakerAction.LOCKED)])
def test_absolute_safety_thresholds_override_recovery(equity, action):
    risk = blocked()
    evaluate(risk, 31)
    result = evaluate(risk, 32, equity)
    assert result.action is action and result.force_liquidate
    assert risk.probation_equity is None
    assert not evaluate(risk, 1000, 95).allow_new_entries


def test_daily_loss_and_bad_health_prevent_timed_reopening():
    risk = blocked()
    assert not evaluate(risk, 31, 84, daily=90).allow_new_entries
    risk.health_assessment = SimpleNamespace(allows_new_risk=False)
    assert not evaluate(risk, 32).allow_new_entries
    risk.health_assessment = SimpleNamespace(allows_new_risk=True)
    assert evaluate(risk, 33).allow_new_entries
    result = evaluate(risk, 34, 84, daily=90)
    assert not result.allow_new_entries and risk.risk_multiplier == 0


def test_probation_exit_requires_actual_equity_recovery():
    risk = blocked()
    evaluate(risk, 31)
    assert evaluate(risk, 32, 91).force_reduce_fraction is None
    assert risk.probation_equity is None
    assert risk.risk_multiplier == 0.5
    assert evaluate(risk, 33, 91).force_reduce_fraction is None


def test_restart_preserves_deadline_high_water_and_probation(tmp_path):
    path = str(tmp_path / "state.db")
    risk = blocked()
    store = StateStore(path)
    store.set("portfolio_breaker_checkpoint", risk.breaker_checkpoint())
    store.close()
    store = StateStore(path)
    restored = manager()
    restored.restore_breaker_checkpoint(store.get("portfolio_breaker_checkpoint"))
    assert restored.breaker_checkpoint() == risk.breaker_checkpoint()
    assert not evaluate(restored, 30).allow_new_entries
    assert evaluate(restored, 31).allow_new_entries
    checkpoint = json.loads(json.dumps(restored.breaker_checkpoint(), allow_nan=False))
    restarted = manager()
    restarted.restore_breaker_checkpoint(checkpoint)
    assert evaluate(restarted, 32).allow_new_entries
    assert restarted.risk_multiplier == 0.25
    assert restarted.recovery_count == 1
    store.close()


def test_no_timestamp_cannot_expire_cooldown():
    risk = blocked()
    assert not risk.check_circuit_breaker(84, 84).allow_new_entries


@pytest.mark.parametrize("equity", [0, -10])
def test_insolvency_requests_liquidation_and_can_be_restored(equity):
    risk = blocked()
    decision = evaluate(risk, 31, equity)
    assert decision.terminal and decision.force_liquidate
    assert not decision.allow_new_entries
    restored = manager()
    restored.restore_breaker_checkpoint(risk.breaker_checkpoint())
    assert restored.breaker_action is BreakerAction.LOCKED


@pytest.mark.parametrize("policy", [{"cooldown_days": 0}, {"cooldown_days": float("nan")},
                                    {"probation_loss_limit": -1}, {"probation_risk_multiplier": 0.8},
                                    {"enabled": "false"}])
def test_invalid_policy_is_rejected(policy):
    with pytest.raises(ValueError):
        RiskManager(recovery_policy=policy)


def test_corrupt_checkpoint_fails_closed_without_partial_mutation():
    risk = blocked()
    state = risk.breaker_checkpoint()
    state["high_water_equity"] = float("nan")
    restored = manager()
    with pytest.raises(ValueError):
        restored.restore_breaker_checkpoint(state)
    assert restored.high_water_equity is None
