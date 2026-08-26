"""Phase 1 (T-1.3 / T-1.4 / T-1.5): unified CloseEvent contract tests.

Every exit path (strategy self-exit, hard stop, Router state-switch,
circuit breaker, EndOfBacktest) closes a position through the same
Broker._execute_trade choke point, so regardless of which order tagged
the closing fill with which exit_reason, the resulting CloseEvent must
have an identical field set and must attribute back to the strategy
that originally opened the lot (T-1.4), and must only be delivered once
per unique close_event_id even if consumption runs redundantly (T-1.5).
"""
import pandas as pd
import pytest

from core.broker import Broker
from core.portfolio import Portfolio
from strategies.mean_reversion import RangeStrategy


EXIT_REASONS = ["signal", "hard_stop", "StateSwitch", "MaxLoss", "EndOfBacktest"]


def _open_and_close(exit_reason: str):
    """Open a long via strategy A, then close it tagging the order with
    ``exit_reason`` (as any of the 5 exit paths would), and return the
    resulting CloseEvent."""
    portfolio = Portfolio(initial_capital=10_000.0)
    broker = Broker(portfolio, commission_rate=0.0, slippage=0.0)

    broker.submit_order(
        "BTC/USDT", "buy", 1.0, price=100.0, order_type="market",
        timestamp=pd.Timestamp("2024-01-01"), strategy_id="TrendBreakout",
    )
    broker.process_orders({
        "BTC/USDT": pd.Series(
            {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1000.0},
            name=pd.Timestamp("2024-01-02"),
        )
    })
    assert portfolio.get_position("BTC/USDT")["qty"] == 1.0

    # Whichever of the 5 exit paths triggered this, it always ends up as a
    # sell order tagged with its own strategy_id/exit_reason - the closing
    # code path itself (Broker._execute_trade) is identical.
    broker.submit_order(
        "BTC/USDT", "sell", 1.0, price=110.0, order_type="market",
        timestamp=pd.Timestamp("2024-01-02"), strategy_id="Router",
        exit_reason=exit_reason,
    )
    broker.process_orders({
        "BTC/USDT": pd.Series(
            {"open": 110.0, "high": 111.0, "low": 109.0, "close": 110.0, "volume": 1000.0},
            name=pd.Timestamp("2024-01-03"),
        )
    })
    assert portfolio.get_position("BTC/USDT") == {"qty": 0.0, "avg_price": 0.0}
    assert len(broker.close_events) == 1
    return broker.close_events[0]


class TestUnifiedCloseEventContract:
    @pytest.mark.parametrize("exit_reason", EXIT_REASONS)
    def test_every_exit_path_produces_structurally_identical_close_event(self, exit_reason):
        event = _open_and_close(exit_reason)
        # Same field set regardless of which path closed it.
        assert event.exit_reason == exit_reason
        assert event.symbol == "BTC/USDT"
        assert event.qty == pytest.approx(1.0)
        assert event.exit_price == pytest.approx(110.0)
        assert event.realized_pnl == pytest.approx(10.0)
        assert event.is_position_fully_closed is True
        # T-1.4: attribution always goes back to the opening strategy
        # (TrendBreakout), never to whichever strategy_id closed it (Router).
        assert event.opening_strategy_id == "TrendBreakout"
        assert event.close_event_id
        assert event.lot_id
        assert event.position_id


class TestIdempotentCallbackDelivery:
    def test_duplicate_close_event_only_updates_strategy_health_once(self):
        strategy = RangeStrategy()
        strategy.context["BTC/USDT"] = {"entry_price": 100.0}
        portfolio = Portfolio(initial_capital=10_000.0)
        broker = Broker(portfolio, commission_rate=0.0, slippage=0.0)
        broker.submit_order(
            "BTC/USDT", "buy", 1.0, price=100.0, order_type="market",
            timestamp=pd.Timestamp("2024-01-01"), strategy_id=strategy.name,
        )
        broker.process_orders({
            "BTC/USDT": pd.Series(
                {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1000.0},
                name=pd.Timestamp("2024-01-02"),
            )
        })
        broker.submit_order(
            "BTC/USDT", "sell", 1.0, price=90.0, order_type="market",
            timestamp=pd.Timestamp("2024-01-02"), strategy_id="Router",
            exit_reason="StateSwitch",
        )
        broker.process_orders({
            "BTC/USDT": pd.Series(
                {"open": 90.0, "high": 91.0, "low": 89.0, "close": 90.0, "volume": 1000.0},
                name=pd.Timestamp("2024-01-03"),
            )
        })

        # Simulate the callback running more than once for the same bar/event
        # (e.g. called again due to routing retries) - must stay idempotent.
        strategy._consume_execution_trades("BTC/USDT", 1, portfolio, broker)
        strategy._consume_execution_trades("BTC/USDT", 1, portfolio, broker)
        strategy._consume_execution_trades("BTC/USDT", 2, portfolio, broker)

        assert strategy.observed_close_events == 1
        assert len(strategy._consumed_close_event_ids) == 1
