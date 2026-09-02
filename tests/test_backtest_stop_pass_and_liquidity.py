"""The two per-bar matching passes: who fills what, and out of whose budget.

``BacktestEngine`` matches each bar twice - the general order book at the
open, then :class:`~backtest.protective_stops.ResidentStopSimulator`'s
resident-stop pass (STR-P1-01).  Two properties of that split were never
pinned end to end and both were broken:

* the general pass carried no order filter, so a resident stop armed on an
  *earlier* bar was filled there instead of in the stop pass - invisible to
  ``stop_order_audit`` / ``triggered_stops`` and skipping the reconciliation
  that cancels a stop whose position the open already closed;
* ``process_orders`` rebuilt the participation budget on every call, so each
  extra pass over the same bar handed the book a fresh allowance and the
  effective cap became a multiple of ``max_participation_rate``.

``tests/test_sr2_backtest_intrabar_stops.py`` drives the simulator directly
and so cannot see either; these tests go through the engine and the broker.
"""
from __future__ import annotations

import pandas as pd
import pytest

from backtest.engine import BacktestEngine
from core.broker import Broker
from core.portfolio import Portfolio
from core.state import MarketState
from strategies.base import Strategy

SYMBOL = "BTC/USDT"
ENTRY_BAR = 30
BREACH_BAR = 50
STOP_PRICE = 95.0


class _StopArmingStrategy(Strategy):
    """Buys once with a fixed protective level and never exits on its own."""

    def __init__(self) -> None:
        super().__init__("StopArming", set(MarketState))

    def should_enter(self, symbol, i, df, state, portfolio):
        if i == ENTRY_BAR:
            return {"action": "buy", "order_type": "market", "stop_loss": STOP_PRICE}
        return None

    def should_exit(self, symbol, i, df, state, portfolio):
        return None


def _price_data(bars: int = 60) -> pd.DataFrame:
    """Flat at 100 except one bar that dips through the stop and closes back up.

    The close never breaches, so the legacy close-based ``hard_stop_exit``
    cannot fire: only the resident intrabar stop can end this position.
    """
    index = pd.date_range("2024-01-01", periods=bars, freq="D", tz="UTC")
    frame = pd.DataFrame(
        {
            "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
            "volume": 1_000_000.0,
        },
        index=index,
    )
    frame.iloc[BREACH_BAR, frame.columns.get_loc("low")] = 90.0
    return frame


def _run_engine() -> dict:
    import backtest.engine as engine_module
    from router.router import Router

    def _router_factory(strategies, _configuration, log_path=None):
        return Router(
            strategies,
            regime_map={state.name: "StopArming" for state in MarketState},
            log_path=log_path,
        )

    engine = BacktestEngine(
        initial_capital=10_000.0, slippage=0.0, warmup_period=ENTRY_BAR - 5,
    )
    original_build_router = engine_module.build_router
    engine_module.build_router = _router_factory
    try:
        return engine.run(
            {SYMBOL: _price_data()},
            strategies={"StopArming": _StopArmingStrategy()},
            routing_log_enabled=False,
        )
    finally:
        engine_module.build_router = original_build_router


@pytest.fixture(scope="module")
def result() -> dict:
    return _run_engine()


class TestResidentStopFillsInItsOwnPass:
    """A stop that rests across bars must still be the stop pass's fill."""

    def test_the_run_actually_armed_and_breached_a_resident_stop(self, result):
        summary = result["protective_stop_summary"]
        assert summary["backtest_resident_enabled"] is True
        placements = [
            row for row in result["stop_order_audit"] if row.get("action") == "place"
        ]
        assert placements, "the entry never armed a resident stop"
        assert placements[0]["stop_price"] == pytest.approx(STOP_PRICE)

    def test_stop_resting_across_bars_is_counted_and_audited(self, result):
        """The regression: filled in the general pass, this row did not exist."""
        fills = [
            row for row in result["stop_order_audit"] if row.get("action") == "fill"
        ]
        assert len(fills) == 1
        assert result["protective_stop_summary"]["triggered_stops"] == 1

        fill = fills[0]
        assert fill["reason"] == "stop_triggered"
        assert fill["stop_price"] == pytest.approx(STOP_PRICE)
        # min(open, stop) for a long - the bar opened at 100 and traded down,
        # so the level itself is the reference price the costs are applied to.
        assert fill["trigger_price"] == pytest.approx(STOP_PRICE)
        assert fill["fill_price"] < STOP_PRICE
        assert fill["bar_index"] == BREACH_BAR

    def test_the_stop_rested_for_many_bars_before_it_fired(self, result):
        """Without this the fill could be a same-bar re-arm, which never regressed."""
        placements = [
            row for row in result["stop_order_audit"] if row.get("action") == "place"
        ]
        fill = next(
            row for row in result["stop_order_audit"] if row.get("action") == "fill"
        )
        assert min(row["bar_index"] for row in placements) < fill["bar_index"] - 1

    def test_the_position_is_closed_by_the_protective_stop(self, result):
        stop_trades = [
            trade for trade in result["trades"]
            if trade["exit_reason"] == "protective_stop"
        ]
        assert len(stop_trades) == 1
        assert stop_trades[0]["side"] == "sell"
        # One entry, one stop exit - the stop did not fill on top of another close.
        assert len(result["trades"]) == 2
        assert result["accounting_check"]["ok"] is True


class TestParticipationCapIsPerBar:
    """The volume budget belongs to the bar, not to one ``process_orders`` call."""

    @staticmethod
    def _bar(timestamp: str, volume: float = 1_000.0) -> pd.Series:
        return pd.Series(
            {
                "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                "volume": volume,
            },
            name=pd.Timestamp(timestamp),
        )

    @staticmethod
    def _broker() -> Broker:
        return Broker(
            Portfolio(initial_capital=1_000_000.0),
            commission_rate=0.0, commission_rate_maker=0.0, slippage=0.0,
            max_participation_rate=0.1,
        )

    def _queue(self, broker: Broker, *quantities: float) -> None:
        for qty in quantities:
            broker.submit_order(
                SYMBOL, "buy", qty, price=100.0, order_type="market",
                timestamp=pd.Timestamp("2024-01-01"),
            )

    def test_a_second_pass_over_one_bar_gets_no_fresh_allowance(self):
        """The regression: the second pass refilled the budget to 100 again."""
        broker = self._broker()
        self._queue(broker, 80.0, 80.0)
        bar = self._bar("2024-01-02")

        first = broker.process_orders({SYMBOL: bar})
        second = broker.process_orders({SYMBOL: bar})

        filled = sum(trade["qty"] for trade in first + second)
        assert filled == pytest.approx(100.0)  # 1000 volume * 10% cap
        assert sum(trade["qty"] for trade in second) == pytest.approx(0.0)

    def test_the_next_bar_restores_the_full_allowance(self):
        broker = self._broker()
        self._queue(broker, 80.0, 80.0)

        broker.process_orders({SYMBOL: self._bar("2024-01-02")})
        later = broker.process_orders({SYMBOL: self._bar("2024-01-03")})

        assert sum(trade["qty"] for trade in later) == pytest.approx(60.0)

    def test_a_bar_with_more_volume_resizes_the_allowance(self):
        broker = self._broker()
        self._queue(broker, 500.0)

        first = broker.process_orders({SYMBOL: self._bar("2024-01-02", volume=1_000.0)})
        second = broker.process_orders({SYMBOL: self._bar("2024-01-03", volume=3_000.0)})

        assert sum(trade["qty"] for trade in first) == pytest.approx(100.0)
        assert sum(trade["qty"] for trade in second) == pytest.approx(300.0)
