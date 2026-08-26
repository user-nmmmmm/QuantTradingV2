"""Phase 1 acceptance suite (T-1.12).

One consolidated scenario exercising pyramiding, a partial reduce, all 5
exit paths (self-exit, hard stop, Router state-switch, circuit breaker,
EndOfBacktest), and a still-open tail position - then asserts, in one
place, everything Phase 1 (T-1.1..T-1.11) was meant to fix. (Direction
reversal within a single fill is exercised at the ledger level in
tests/test_lots.py::TestLotBookPyramidAndReduce - Broker._execute_trade
clips sell/cover fills to the currently open quantity, so a same-fill
reversal can never actually reach the broker/engine layer; it is a
LotBook-level guarantee, not an engine-observable scenario.)

- lifecycle coverage == 100% (T-1.3/T-1.4/T-1.5)
- the accounting identity holds every bar and cumulatively (T-1.8)
- cost-sensitivity's 1x/1x baseline equals the main report's NetPnL (T-1.7)
- every closed trade has initial_risk/mae/mfe populated (T-1.9/T-1.10)
- the tail-open position closes via EndOfBacktest and is included above (T-1.11)

This is the automated check the roadmap's Gates G1/G2/G3 point at.
"""
from typing import Optional

import pandas as pd
import pytest

from backtest.reporting import ReportGenerator
from core.accounting_check import AccountingReconciler
from core.broker import Broker
from core.portfolio import Portfolio
from core.state import MarketState
from strategies.base import Strategy


class _NoOpStrategy(Strategy):
    """A minimal strategy identity for callback attribution; fills in this
    suite are driven directly through the broker (not through on_bar) so the
    lot ledger and lifecycle-callback machinery can be exercised precisely."""

    def __init__(self, name: str):
        super().__init__(name, set(MarketState))

    def should_enter(self, symbol, i, df, state, portfolio):
        return None

    def should_exit(self, symbol, i, df, state, portfolio):
        return None


def _bar(ts, price, high=None, low=None, volume=1e18):
    return pd.Series(
        {
            "open": price, "high": high if high is not None else price,
            "low": low if low is not None else price, "close": price,
            "volume": volume,
        },
        name=pd.Timestamp(ts),
    )


class TestPhase1LifecycleSuite:
    def test_full_lifecycle_scenario(self):
        portfolio = Portfolio(initial_capital=100_000.0)
        broker = Broker(portfolio, commission_rate=0.001, slippage=0.0)
        alpha = _NoOpStrategy("Alpha")
        beta = _NoOpStrategy("Beta")
        strategies = {"Alpha": alpha, "Beta": beta}

        accounting = AccountingReconciler(initial_capital=100_000.0)
        equity_rows = []
        day = 1

        def fill(symbol, side, qty, price, strategy_id, exit_reason="signal",
                 stop_loss: float = 0.0, zero_cost: bool = False,
                 high: Optional[float] = None, low: Optional[float] = None):
            nonlocal day
            ts = pd.Timestamp(f"2024-01-{day:02d}")
            day += 1
            broker.submit_order(
                symbol, side, qty, price=price, order_type="market",
                timestamp=ts, strategy_id=strategy_id, exit_reason=exit_reason,
                stop_loss=stop_loss, zero_cost=zero_cost,
            )
            fill_ts = ts + pd.Timedelta(hours=1)
            broker.process_orders({symbol: _bar(fill_ts, price, high, low)})
            # T-1.10: sample MAE/MFE for every symbol's open lots this bar.
            for sym in list(portfolio.lot_books):
                if portfolio.lot_books[sym].open_lots:
                    portfolio.update_lot_extremes(
                        sym, high if high is not None else price,
                        low if low is not None else price,
                    )
            current_prices = {"BTC/USDT": price if symbol == "BTC/USDT" else _last_btc[0],
                               "ETH/USDT": price if symbol == "ETH/USDT" else _last_eth[0]}
            if symbol == "BTC/USDT":
                _last_btc[0] = price
            else:
                _last_eth[0] = price
            equity = portfolio.get_equity(current_prices)
            equity_rows.append({"timestamp": fill_ts, "equity": equity, "cash": portfolio.cash})
            accounting.check_bar(
                len(equity_rows), fill_ts, equity, portfolio, current_prices,
                broker.close_events,
            )
            for strategy in strategies.values():
                strategy._consume_execution_trades(symbol, len(equity_rows), portfolio, broker)

        _last_btc = [100.0]
        _last_eth = [2000.0]

        # --- BTC/USDT, strategy Alpha: pyramid, partial reduce, then one
        #     full close per remaining exit path (self-exit already covers
        #     the partial reduce; hard_stop/StateSwitch/MaxLoss each close a
        #     separate lot in full). ---
        fill("BTC/USDT", "buy", 10.0, 100.0, "Alpha", stop_loss=90.0)          # open lot 1
        fill("BTC/USDT", "buy", 5.0, 105.0, "Alpha", stop_loss=90.0)           # pyramid: lot 2
        fill("BTC/USDT", "sell", 6.0, 110.0, "Alpha", exit_reason="signal",
             high=112.0, low=108.0)                                            # partial reduce (self-exit)
        fill("BTC/USDT", "sell", 9.0, 90.0, "Router", exit_reason="StateSwitch",
             high=95.0, low=88.0)                                              # closes rest of lot1 + lot2
        fill("BTC/USDT", "buy", 8.0, 95.0, "Alpha", stop_loss=85.0)            # new lot after flat
        fill("BTC/USDT", "sell", 8.0, 80.0, "Alpha", exit_reason="hard_stop",
             high=94.0, low=78.0)                                              # full close (hard stop)
        fill("BTC/USDT", "buy", 4.0, 72.0, "Alpha", stop_loss=65.0)            # new lot after flat
        fill("BTC/USDT", "sell", 4.0, 70.0, "CircuitBreaker", exit_reason="MaxLoss",
             high=73.0, low=69.0)                                              # full close (circuit breaker)

        # --- ETH/USDT, strategy Beta: opened, never closed -> EndOfBacktest. ---
        fill("ETH/USDT", "buy", 3.0, 2000.0, "Beta", stop_loss=1900.0, high=2010.0, low=1990.0)

        # --- EndOfBacktest: close whatever Beta still has open. ---
        eob_price = 2200.0
        eob_ts = pd.Timestamp("2024-02-01")
        broker.submit_order(
            "ETH/USDT", "sell", 3.0, price=eob_price, order_type="market",
            timestamp=eob_ts, strategy_id="EndOfBacktest", exit_reason="EndOfBacktest",
            zero_cost=True,
        )
        broker.process_orders({"ETH/USDT": _bar(eob_ts + pd.Timedelta(hours=1), eob_price)})
        current_prices = {"BTC/USDT": _last_btc[0], "ETH/USDT": eob_price}
        equity = portfolio.get_equity(current_prices)
        equity_rows.append({"timestamp": eob_ts, "equity": equity, "cash": portfolio.cash})
        accounting.check_bar(
            len(equity_rows), eob_ts, equity, portfolio, current_prices, broker.close_events,
        )
        for strategy in strategies.values():
            strategy._consume_execution_trades("ETH/USDT", len(equity_rows), portfolio, broker)

        # ------------------------------------------------------------------
        # Assertions
        # ------------------------------------------------------------------

        # 1) Accounting identity holds every bar and cumulatively (T-1.8).
        result = accounting.result()
        assert result.ok is True, result.discrepancies
        assert result.checks_performed == len(equity_rows)

        # 2) Full analytics pipeline, including lifecycle coverage (T-1.3-1.5).
        equity_curve = pd.DataFrame(equity_rows).set_index("timestamp")
        observed_close_events = {
            name: strategy.observed_close_events for name, strategy in strategies.items()
        }
        metrics = ReportGenerator(".").generate(
            broker.trades, equity_curve, metrics_only=True,
            close_events=observed_close_events,
        )
        coverage = metrics["Diagnostics"]["lifecycle_coverage"]
        assert coverage["status"] == "ok"
        assert coverage["overall_coverage"] == pytest.approx(1.0)
        assert coverage["blind_strategies"] == []

        # 3) Cost-sensitivity 1x/1x baseline == main report NetPnL (T-1.7).
        cost_sensitivity = metrics["ExtendedAnalytics"]["cost_sensitivity"]
        assert cost_sensitivity["baseline_net_pnl"] == pytest.approx(metrics["NetPnL"])

        # 4) Every closed trade has initial_risk/mae/mfe populated (T-1.9/T-1.10).
        r_multiple = metrics["ExtendedAnalytics"]["r_multiple"]
        assert r_multiple["excluded_no_initial_risk"] == 0
        assert r_multiple["mae"]["status"] == "ok"
        assert r_multiple["mfe"]["status"] == "ok"

        # 5) The EndOfBacktest close is present and attributed to Beta.
        eob_trades = [t for t in broker.trades if t["exit_reason"] == "EndOfBacktest"]
        assert len(eob_trades) == 1
        eob_close_events = [
            e for e in broker.close_events if e.exit_reason == "EndOfBacktest"
        ]
        assert len(eob_close_events) == 1
        assert eob_close_events[0].opening_strategy_id == "Beta"

        # All 5 exit paths appear, each attributed back to the opening strategy.
        exit_reasons_seen = {e.exit_reason for e in broker.close_events}
        assert exit_reasons_seen == {
            "signal", "StateSwitch", "hard_stop", "MaxLoss", "EndOfBacktest",
        }
        assert all(e.opening_strategy_id == "Alpha" for e in broker.close_events if e.exit_reason != "EndOfBacktest")
