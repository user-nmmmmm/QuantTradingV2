"""Stage A of docs/strategy_remediation_roadmap_20261110.md.

Safety and consistency fixes that must not change candidate backtest
behaviour: A1 time-exit latch and cumulative reduction proof, A3 live history
preflight, A4 health persistence frequency, A5 single source for ADX inputs.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd
import pytest

from core.broker import Broker
from core.trading.portfolio import Portfolio
from core.trading.state import MarketState
from live_trading.execution_adapter import AccountEntryBlocked, RecordedExecutionAdapter
from router.router import Router

SYMBOL = "A-USDT"
START = pd.Timestamp("2024-01-01", tz="UTC")


def _frame(periods: int) -> pd.DataFrame:
    frame = pd.DataFrame(
        {field: [100.0] * periods for field in ("open", "high", "low", "close")},
        index=pd.date_range(START, periods=periods, freq="1D"),
    )
    frame["volume"] = 1_000_000.0
    return frame


def _held_router_and_broker():
    portfolio = Portfolio(10_000)
    portfolio.update_position(SYMBOL, 2, 100, strategy_id="Probe",
                              order_id="opening", time=START)
    broker = Broker(portfolio, commission_rate=0, slippage=0)
    router = Router({}, regime_map={"TREND_UP": "Probe"}, max_holding_days=1)
    return router, broker, portfolio


# ---- A1: MaxHoldingPeriod exit latch -------------------------------------


def test_unfilled_time_exit_is_not_resubmitted_on_the_next_bar():
    router, broker, portfolio = _held_router_and_broker()
    frame = _frame(5)
    for i in (2, 3, 4):  # three expired bars, the first exit never fills
        assert router.process_position_management(
            SYMBOL, i, frame, MarketState.TREND_UP, portfolio, broker)
    exits = [order for order in broker.pending_orders
             if order.exit_reason == "MaxHoldingPeriod"]
    assert len(exits) == 1
    assert router.checkpoint()["time_exits"] == {SYMBOL: exits[0].id}


def test_terminal_time_exit_releases_the_latch_for_remaining_inventory():
    router, broker, portfolio = _held_router_and_broker()
    frame = _frame(5)
    router.process_position_management(SYMBOL, 2, frame, MarketState.TREND_UP, portfolio, broker)
    first = broker.pending_orders[0]
    broker.cancel_symbol_orders(SYMBOL)  # terminal fact, position unchanged
    router.process_position_management(SYMBOL, 3, frame, MarketState.TREND_UP, portfolio, broker)
    exits = [order for order in broker.pending_orders
             if order.exit_reason == "MaxHoldingPeriod"]
    assert [order.id for order in exits] != [first.id]
    assert len(exits) == 1


def test_flat_symbol_clears_latch_and_checkpoint_round_trips():
    router, broker, portfolio = _held_router_and_broker()
    frame = _frame(4)
    router.process_position_management(SYMBOL, 2, frame, MarketState.TREND_UP, portfolio, broker)
    saved = router.checkpoint()
    restored = Router({}, regime_map={"TREND_UP": "Probe"}, max_holding_days=1)
    restored.restore_checkpoint(saved)
    assert restored._time_exit_orders == router._time_exit_orders
    legacy = {key: value for key, value in saved.items() if key != "time_exits"}
    restored.restore_checkpoint(legacy)  # pre-A1 checkpoints stay readable
    assert restored._time_exit_orders == {}
    portfolio.update_position(SYMBOL, -2, 100, strategy_id="Probe", time=frame.index[3])
    router.process_position_management(SYMBOL, 3, frame, MarketState.TREND_UP, portfolio, broker)
    assert router._time_exit_orders == {}


# ---- A1: cumulative in-flight reduction proof ----------------------------


class _LedgerBroker:
    """Durable-ledger broker double: holds ``held`` and lists working orders."""

    def __init__(self, held: float, working: list[dict]):
        self.portfolio = Portfolio(10_000)
        if held:
            self.portfolio.update_position(SYMBOL, held, 100, strategy_id="Probe")
        self.order_store = MagicMock()
        self.order_store.list_non_terminal.return_value = working
        self.order_store.get.return_value = None
        self.submissions = []

    def submit_order(self, symbol, side, qty, *, reduce_only=False):
        self.submissions.append((symbol, side, qty))
        return "submitted"


def _working(side: str, qty: float, order_type: str = "market", order_id: str = "w1"):
    return {"symbol": SYMBOL, "side": side, "remaining_qty": qty,
            "order_type": order_type, "client_order_id": order_id}


def test_second_in_flight_sell_exceeding_position_is_rejected():
    broker = _LedgerBroker(1.5, [_working("sell", 1.0)])
    adapter = RecordedExecutionAdapter(broker, opening_guard=MagicMock(return_value=False))
    with pytest.raises(AccountEntryBlocked):
        adapter.submit_order(SYMBOL, "sell", 1.0)
    assert broker.submissions == []


def test_in_flight_sells_within_position_still_pass_without_the_guard():
    broker = _LedgerBroker(2.0, [_working("sell", 1.0)])
    guard = MagicMock(return_value=False)
    adapter = RecordedExecutionAdapter(broker, opening_guard=guard)
    assert adapter.submit_order(SYMBOL, "sell", 1.0) == "submitted"
    guard.assert_not_called()


def test_resting_protective_stop_does_not_consume_exit_inventory():
    broker = _LedgerBroker(1.0, [_working("sell", 1.0, order_type="stop")])
    guard = MagicMock(return_value=False)
    adapter = RecordedExecutionAdapter(broker, opening_guard=guard)
    assert adapter.submit_order(SYMBOL, "sell", 1.0) == "submitted"
    guard.assert_not_called()


def test_simulated_broker_book_is_counted_too():
    portfolio = Portfolio(10_000)
    portfolio.update_position(SYMBOL, 1.0, 100, strategy_id="Probe", order_id="o", time=START)
    broker = Broker(portfolio, commission_rate=0, slippage=0)
    broker.submit_order(SYMBOL, "sell", 1.0, price=100.0, timestamp=START,
                        strategy_id="Probe", exit_reason="signal")
    adapter = RecordedExecutionAdapter(broker, opening_guard=MagicMock(return_value=False))
    with pytest.raises(AccountEntryBlocked):
        adapter.submit_order(SYMBOL, "sell", 1.0, price=100.0, timestamp=START)
