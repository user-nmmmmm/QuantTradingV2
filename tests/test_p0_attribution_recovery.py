"""P0 audit passivity, exact entry linkage and evidence-gated risk recovery."""
from copy import deepcopy

import pytest

from core.entry_audit import capture, note, reconcile, forced_trade_cost
from core.strategy_health import StrategyHealthMachine, StrategyHealthPolicy, HealthStatus
from tests.test_health_extended_recovery import close


def staged():
    m = StrategyHealthMachine("test", StrategyHealthPolicy(
        consecutive_negative_cohorts=1, cooldown_days=1, probation_required_cohorts=1,
        repeated_failure_action="extended_cooldown", recovery_stages=(0.1, 0.25, 0.5, 1.0)))
    close(m, "2020-01-01")
    m.evaluate("2020-01-02")
    close(m, "2020-01-03")
    m.evaluate("2020-01-04")
    close(m, "2020-01-05")
    m.evaluate("2020-04-04")
    return m


def test_positive_evidence_needs_minimum_time_and_cannot_skip_stages():
    m = staged()
    assert m.risk_multiplier == 0.1
    close(m, "2020-04-05", 10)
    assert m.risk_multiplier == 0.1
    m.evaluate("2020-05-04")
    assert m.risk_multiplier == 0.25
    assert m.probation_closed_cohorts == 0
    m.evaluate("2020-06-03")
    assert m.risk_multiplier == 0.25  # elapsed time alone is insufficient
    close(m, "2020-06-04", 10)
    assert m.risk_multiplier == 0.5
    close(m, "2020-06-05", 10)
    assert m.risk_multiplier == 0.5
    m.evaluate("2020-07-04")
    assert m.status == HealthStatus.ACTIVE and m.risk_multiplier == 1.0


def test_negative_evidence_fails_before_minimum_time_and_resets_stage():
    m = staged()
    close(m, "2020-04-05", 10)
    m.evaluate("2020-05-04")
    close(m, "2020-05-05", -10)
    assert m.status == HealthStatus.COOLDOWN
    assert m.recovery_stage == 0 and m.risk_multiplier == 0
    m.evaluate("2020-08-03")
    assert m.risk_multiplier == 0.1


def test_staged_restart_preserves_risk_time_and_cohort_boundary():
    m = staged()
    close(m, "2020-04-05", 10)
    m.evaluate("2020-05-04")
    restored = StrategyHealthMachine("test", m.policy)
    restored.load(deepcopy(m.to_dict()))
    assert restored.risk_multiplier == 0.25
    assert restored.probation_started_at == m.probation_started_at
    assert restored.probation_closed_cohorts == 0
    assert restored.evaluate("2020-06-03") == HealthStatus.PROBATION
    restored.manual_lock("operator", at="2020-06-04")
    assert not restored.allows_new_entries("2030-01-01")


@pytest.mark.parametrize("stages", [(0.25, 0.5, 1), (0.1, 0.1, 1), (0.1, 0.5), (0.1, float("nan"), 1)])
def test_bad_stages_rejected(stages):
    with pytest.raises(ValueError):
        StrategyHealthPolicy(repeated_failure_action="extended_cooldown", recovery_stages=stages)


def test_capture_is_nested_and_reset_even_on_failure():
    outer, inner = {}, {}
    with capture(outer):
        note("outer")
        with pytest.raises(ValueError), capture(inner):
            note("inner")
            raise ValueError("test")
        note(value=2)
    note("must_not_leak")
    assert outer == {"reason": "outer", "value": 2}
    assert inner == {"reason": "inner"}


def test_partial_fills_and_exits_do_not_inflate_opening_chains():
    rows = [{"reason": "order_accepted", "order_id": "a"}, {"reason": "obv_filter"},
            {"reason": "order_accepted", "order_id": "b"}]
    trades = [{"side": "buy", "order_id": "a", "qty": 2},
              {"side": "buy", "order_id": "a", "qty": 3},
              {"side": "sell", "order_id": "exit", "qty": 5}]
    linked, report = reconcile(rows, trades, [{"order_id": "b", "outcome": "expired", "reason": "ttl"}])
    assert linked[0]["fill_count"] == 2 and linked[0]["filled_qty"] == 5
    assert report["filled_opening_orders"] == 1
    assert report["partition_ok"] and report["linkage_ok"]
    assert linked[2]["execution_last_fact"]["reason"] == "ttl"


def test_unlinked_opening_fill_is_explicit_failure_not_silently_dropped():
    _, report = reconcile([], [{"side": "short", "order_id": "missing", "qty": 1}], [])
    assert not report["linkage_ok"]
    assert report["unmatched_filled_order_ids"] == ["missing"]


def test_duplicate_order_link_rejected():
    with pytest.raises(ValueError):
        reconcile([{"reason": "x", "order_id": "a"}] * 2, [], [])


def test_forced_cost_uses_qty_and_does_not_mutate_fills():
    trades = [{"qty": 0.1, "slip": 100, "commission": 2},
              {"qty": 1000, "slip": 0.01, "commission": 3}]
    original = deepcopy(trades)
    assert forced_trade_cost(trades) == 25
    assert trades == original


@pytest.mark.parametrize("enabled,high_water,reason", [
    (False, 12000, None), (True, 12000, "DrawdownReduce"),
    (True, 13000, "AccountLiquidation"),
])
def test_block_derisk_once_per_epoch_and_terminal_overrides(enabled, high_water, reason, monkeypatch):
    import pandas as pd
    from unittest.mock import patch
    from backtest.engine import BacktestEngine
    from config.config import config
    from core.broker import Broker
    from core.risk import RiskManager

    settings = deepcopy(config._config)
    settings["research"] = {"block_remaining_fraction": 0.5} if enabled else {}
    monkeypatch.setattr(config, "_config", settings)
    risk = RiskManager(daily_loss_limit=0.5, portfolio_drawdown_reduce=0.1,
                       portfolio_drawdown_block=0.15, portfolio_drawdown_liquidate=0.2,
                       portfolio_drawdown_lock=0.25)
    risk.high_water_equity = high_water
    frame = pd.DataFrame({"open": 100., "high": 101., "low": 99., "close": 100., "volume": 10000.},
                         index=pd.date_range("2024-01-01", periods=4))
    with patch("backtest.engine.build_risk_manager", return_value=risk), \
            patch.object(Broker, "force_liquidate", return_value=[]) as force:
        result = BacktestEngine(initial_capital=10000, warmup_period=30).run(
            {"BTC-USDT": frame}, routing_log_enabled=False)
    if reason is None:
        force.assert_not_called()
    else:
        force.assert_called_once()
        assert force.call_args.kwargs["reason"] == reason
        assert force.call_args.kwargs.get("remaining_fraction", 0) == (0.5 if reason == "DrawdownReduce" else 0)
    assert result["accounting_check"]["ok"]
