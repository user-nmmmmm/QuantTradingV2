from copy import deepcopy
from types import SimpleNamespace

import pandas as pd
import pytest

from backtest.engine import BacktestEngine
from composition.factory import build_portfolio_target_controller
from config.config import config
from scripts.run_trend_portfolio_v3 import effective_config
from tests.test_v3_engine_integration import synthetic_market, spec


def test_configured_cost_allocator_reaches_normal_engine_without_custom_controller(monkeypatch):
    frames, metadata = synthetic_market(length=225, count=3)
    settings = effective_config(deepcopy(config._config), spec(financing="verified_only"))
    settings["account"]["mode"] = "spot"
    settings["execution"]["fee_schedule"]["market_type"] = "spot"
    settings["portfolio_targets"] = {"enabled": True, "metadata": metadata,
        "cost_aware": {"enabled": True, "rebalance": "partial", "max_turnover_weight": .15}}
    monkeypatch.setattr(config, "_config", settings)
    engine = BacktestEngine(initial_capital=10000., warmup_period=0, timeframe="1d",
        trading_start="2023-07-19", terminal_policy="valuation_only", calculate_benchmarks=False)
    result = engine.run(frames, routing_log_enabled=False)
    assert result["trades"] and result["accounting_check"]["ok"]
    audit = result["portfolio_controller"]["audit"]
    weekly = [row["weekly_snapshot"] for row in audit if row.get("weekly_snapshot")]
    assert weekly and any(row["sizing"]["cost_aware_audit"]["enabled"] for row in weekly)
    assert result["temporal_data"]["audit"]["policy"] == "retrospective"
    # Reusing an Engine must create a fresh controller for the fresh account and
    # strategy registry instead of inheriting the previous run's weekly state.
    again = engine.run(frames, routing_log_enabled=False)
    fields = ["symbol", "side", "qty", "fill_price", "fill_time", "commission"]
    pd.testing.assert_frame_equal(pd.DataFrame(result["trades"])[fields], pd.DataFrame(again["trades"])[fields])


def test_config_does_not_invent_metadata_or_enable_default_trading(monkeypatch):
    assert build_portfolio_target_controller(config, {}) is None
    settings = deepcopy(config._config)
    settings["portfolio_targets"] = {"enabled": True}
    monkeypatch.setattr(config, "_config", settings)
    with pytest.raises(ValueError, match="explicitly routed"):
        build_portfolio_target_controller(config, {})
    with pytest.raises(ValueError, match="membership metadata"):
        build_portfolio_target_controller(config, {"TrendPortfolioV3": object()})


def test_bar_scheduler_rejects_delay_that_would_backdate_orders():
    with pytest.raises(ValueError, match="delayed decisions"):
        BacktestEngine(temporal_policy={"mode": "strict", "decision_delay_seconds": 1})


@pytest.mark.parametrize("policy", ["signal_meta_layer", "signal_adaptive", "signal_meta_replay"])
def test_strict_training_is_configured_to_use_versioned_label_producer(policy):
    engine = BacktestEngine(temporal_policy="strict", **{policy: {"enabled": True}})
    assert engine.temporal_policy.mode == "strict"
    assert engine.signal_meta_policy.enabled
    assert engine.observation_policy.enabled


def test_live_quote_sampler_lifecycle_is_started_and_stopped_even_on_tick_interrupt():
    from live_trading.engine import LiveTradingEngine
    events = []
    engine = LiveTradingEngine.__new__(LiveTradingEngine)
    engine.quote_sampler = SimpleNamespace(start=lambda: events.append("quote_start"),
        stop=lambda **kw: events.append(("quote_stop", kw)))
    engine.market_data_adapter = SimpleNamespace(close=lambda: events.append("data_close"))
    engine._owns_fetcher = False
    def stop():
        events.append("tick")
        raise KeyboardInterrupt
    engine._tick = stop
    engine.run()
    assert events == ["quote_start", "tick", ("quote_stop", {"join_timeout_seconds": 1.}), "data_close"]
