"""FIX-08: real candidate factories reach routing, signals, and fills."""
from __future__ import annotations

from analysis import optimize
from backtest.engine import BacktestEngine
from config.config import config
from core.state import MarketState
from router.router import Router
from strategies.trend_breakout import TrendBreakoutStrategy
from tests.engine_baseline_harness import build_synthetic_data_map


def test_optimizer_registry_keys_resolve_every_strategy_route():
    registry = optimize.build_optimization_strategies(20, 5)
    assert set(registry) == {"TrendBreakout", "TrendBreakdown", "RangeMeanReversion", "VolatilityReversion"}
    assert all(key == strategy.name for key, strategy in registry.items())
    router = Router(registry, regime_map={state.name: name for state, name in (
        (MarketState.TREND_UP, "TrendBreakout"),
        (MarketState.TREND_DOWN, "TrendBreakdown"),
        (MarketState.SIDEWAYS, "RangeMeanReversion"),
        (MarketState.VOLATILE, "VolatilityReversion"),
    )})
    for state in MarketState:
        name = router._map_state_to_strategy(state)
        assert name is None if state == MarketState.NO_TRADE else name in registry
    production_routes = (config.get("routing") or {}).values()
    assert all(name == "Cash" or name in registry for name in production_routes)


def test_distinct_optimizer_parameters_change_real_engine_decisions(monkeypatch):
    """Observe the real run without replacing routing, sizing, or execution.

    This fixed development fixture is not a final holdout and is never ranked.
    Both candidates use the production factory and the ordinary evaluator.
    """
    data = build_synthetic_data_map(seed=20260812, bars=240)
    captured = []
    decisions = {(20, 5): [], (30, 20): []}
    real_run = BacktestEngine.run
    real_entry = TrendBreakoutStrategy.should_enter

    def observe_entry(strategy, symbol, i, df, state, portfolio):
        signal = real_entry(strategy, symbol, i, df, state, portfolio)
        if signal:
            decisions[(strategy.entry_window, strategy.exit_window)].append(
                (symbol, df.index[i], signal.get("action"), signal.get("stop_loss")))
        return signal

    def observe_run(engine, data_map, **kwargs):
        registry = kwargs["strategies"]
        breakout = registry["TrendBreakout"]
        breakdown = registry["TrendBreakdown"]
        assert (breakout.entry_window, breakout.exit_window) == (breakdown.entry_window, breakdown.exit_window)
        result = real_run(engine, data_map, **kwargs)
        captured.append({"parameters": (breakout.entry_window, breakout.exit_window),
                         "warmup": engine.warmup_period, "result": result,
                         "raw_setups": breakout.raw_setup_count})
        return result

    monkeypatch.setattr(BacktestEngine, "run", observe_run)
    monkeypatch.setattr(TrendBreakoutStrategy, "should_enter", observe_entry)
    summaries = [optimize.evaluate_one_candidate(({symbol: frame.copy(deep=True) for symbol, frame in data.items()},
                                                  entry, exit_, 10000.))
                 for entry, exit_ in ((20, 5), (30, 20))]
    assert [item["parameters"] for item in captured] == [(20, 5), (30, 20)]
    assert captured[0]["warmup"] == captured[1]["warmup"] == 30
    assert all(item["result"]["trades"] for item in captured), "fixture must exercise actual Broker fills"
    assert all(summary["Trades"] > 0 for summary in summaries)
    signatures = [[(fill["fill_time"], fill["symbol"], fill.get("side"), fill["fill_price"], fill["qty"])
                   for fill in item["result"]["trades"]] for item in captured]
    assert signatures[0] != signatures[1]
    assert decisions[(20, 5)] and decisions[(30, 20)]
    assert decisions[(20, 5)] != decisions[(30, 20)]
    # Same first breakout, but the longer exit channel permits a lower stop.
    # Both signals go through the real sizing and Broker execution above.
    first_fast, first_slow = decisions[(20, 5)][0], decisions[(30, 20)][0]
    assert first_fast[:3] == first_slow[:3]
    assert first_fast[3] > first_slow[3]
    print({"candidates": [{"parameters": item["parameters"], "warmup": item["warmup"],
                           "fills": len(item["result"]["trades"]), "raw_setups": item["raw_setups"]}
                          for item in captured], "decisions": str(decisions)})
