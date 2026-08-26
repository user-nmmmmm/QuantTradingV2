"""Phase 1 (T-1.11): EndOfBacktest tail-position handling.

A position still open when the data runs out must be closed through the
same Broker/CloseEvent path as every other exit (I-37), in either of two
modes: mark_to_market (zero extra commission/slippage - matches the
existing mark-to-market equity curve) or forced_liquidation (a normal
exit, with real commission/slippage).
"""
import pandas as pd
import pytest

from backtest.engine import BacktestEngine
from config.config import config
from core.system_factory import build_strategy_registry
from strategies.base import Strategy
from core.state import MarketState


class _AlwaysLongStrategy(Strategy):
    """Enters long on the first bar and never exits - guarantees a tail
    position is still open when the run ends, regardless of regime."""

    def __init__(self):
        super().__init__("AlwaysLong", set(MarketState))

    def should_enter(self, symbol, i, df, state, portfolio):
        if i == 30:
            return {"action": "buy", "order_type": "market"}
        return None

    def should_exit(self, symbol, i, df, state, portfolio):
        return None


def _flat_price_data(bars=60, price=100.0):
    index = pd.date_range("2024-01-01", periods=bars, freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "open": price, "high": price + 1, "low": price - 1, "close": price,
            "volume": 1_000_000.0,
        },
        index=index,
    )


def _run_with_tail_position(mode: str):
    strategies = {"AlwaysLong": _AlwaysLongStrategy()}
    # Route everything to our strategy regardless of regime by monkeypatching
    # build_router indirectly is overkill - instead drive the engine with a
    # regime map covering every state onto our one strategy via a minimal
    # router built directly.
    from router.router import Router

    original_get_state = None
    engine = BacktestEngine(initial_capital=10_000.0, slippage=0.0005, warmup_period=25)

    # Patch build_router for this run only so every state routes to our strategy.
    import backtest.engine as engine_module

    def _router_factory(strats, log_path=None):
        return Router(strats, regime_map={s.name: "AlwaysLong" for s in MarketState}, log_path=log_path)

    original_build_router = engine_module.build_router
    engine_module.build_router = _router_factory
    try:
        original_mode = config.get("backtest", "end_of_backtest_mode")
        config._config["backtest"]["end_of_backtest_mode"] = mode
        try:
            result = engine.run(
                {"BTC/USDT": _flat_price_data()},
                strategies=strategies,
                routing_log_enabled=False,
            )
        finally:
            config._config["backtest"]["end_of_backtest_mode"] = original_mode
    finally:
        engine_module.build_router = original_build_router
    return result


class TestEndOfBacktestMarkToMarket:
    def test_tail_position_closed_with_zero_extra_cost(self):
        result = _run_with_tail_position("mark_to_market")
        eob_trades = [t for t in result["trades"] if t["exit_reason"] == "EndOfBacktest"]
        assert len(eob_trades) == 1
        trade = eob_trades[0]
        assert trade["commission"] == pytest.approx(0.0)
        assert trade["slip"] == pytest.approx(0.0)
        assert trade["fill_price"] == pytest.approx(trade["theoretical_price"])
        assert result["accounting_check"]["ok"] is True


class TestEndOfBacktestForcedLiquidation:
    def test_tail_position_closed_with_real_exit_cost(self):
        result = _run_with_tail_position("forced_liquidation")
        eob_trades = [t for t in result["trades"] if t["exit_reason"] == "EndOfBacktest"]
        assert len(eob_trades) == 1
        trade = eob_trades[0]
        assert trade["commission"] > 0.0
        assert trade["slip"] > 0.0
        assert result["accounting_check"]["ok"] is True
