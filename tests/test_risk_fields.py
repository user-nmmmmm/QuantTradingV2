"""Phase 1 (T-1.9 / T-1.10): initial_risk persistence and R/SQN/MAE/MFE.

Entering a position now threads stop_loss through Broker.submit_order onto
the opened lot (initial_risk = |entry - stop| * qty), and the engine loop
samples every open lot's MAE/MFE against each bar's high/low. Both flow
through to _reconstruct_closed_trades so core.metrics.calculate_r_multiple_stats
gets real inputs instead of an always-empty set (I-04).
"""
import pandas as pd
import pytest

from backtest.reporting import ReportGenerator
from core.broker import Broker
from core.metrics import calculate_r_multiple_stats
from core.portfolio import Portfolio


def _bar(ts, o, h, l, c, v=1000.0):
    return pd.Series({"open": o, "high": h, "low": l, "close": c, "volume": v}, name=pd.Timestamp(ts))


class TestInitialRiskThreading:
    def test_stop_loss_flows_from_submit_order_to_lot_initial_risk(self):
        portfolio = Portfolio(initial_capital=10_000.0)
        broker = Broker(portfolio, commission_rate=0.0, slippage=0.0)
        broker.submit_order(
            "BTC/USDT", "buy", 1.0, price=100.0, order_type="market",
            timestamp=pd.Timestamp("2024-01-01"), strategy_id="S", stop_loss=95.0,
        )
        broker.process_orders({"BTC/USDT": _bar("2024-01-02", 100.0, 101.0, 99.0, 100.0)})
        lot = portfolio.open_lots("BTC/USDT")[0]
        assert lot.stop_price == 95.0
        assert lot.initial_risk == pytest.approx(5.0)  # |100-95| * 1.0


class TestMaeMfeAndRMultipleEndToEnd:
    def test_closed_trade_carries_initial_risk_mae_mfe(self):
        portfolio = Portfolio(initial_capital=10_000.0)
        broker = Broker(portfolio, commission_rate=0.0, slippage=0.0)
        broker.submit_order(
            "BTC/USDT", "buy", 1.0, price=100.0, order_type="market",
            timestamp=pd.Timestamp("2024-01-01"), strategy_id="S", stop_loss=95.0,
        )
        broker.process_orders({"BTC/USDT": _bar("2024-01-02", 100.0, 101.0, 99.0, 100.0)})
        # Simulate the engine's per-bar MAE/MFE sampling while the lot is open.
        portfolio.update_lot_extremes("BTC/USDT", high=108.0, low=97.0)
        portfolio.update_lot_extremes("BTC/USDT", high=104.0, low=90.0)

        broker.submit_order(
            "BTC/USDT", "sell", 1.0, price=110.0, order_type="market",
            timestamp=pd.Timestamp("2024-01-03"), strategy_id="S",
        )
        broker.process_orders({"BTC/USDT": _bar("2024-01-04", 110.0, 111.0, 109.0, 110.0)})

        report_gen = ReportGenerator.__new__(ReportGenerator)
        closed_trades = report_gen._reconstruct_closed_trades(pd.DataFrame(broker.trades))
        assert len(closed_trades) == 1
        trade = closed_trades[0]
        assert trade["initial_risk"] == pytest.approx(5.0)
        assert trade["mfe"] == pytest.approx(8.0)   # best high (108) - entry (100)
        assert trade["mae"] == pytest.approx(10.0)  # entry (100) - worst low (90)

        stats = calculate_r_multiple_stats(closed_trades)
        assert stats["excluded_no_initial_risk"] == 0
        assert stats["r_multiple"]["status"] == "insufficient"  # only 1 sample (< 2)
        assert stats["mae"]["sample_size"] == 1
        assert stats["mfe"]["sample_size"] == 1

    def test_trade_without_stop_loss_has_no_initial_risk(self):
        portfolio = Portfolio(initial_capital=10_000.0)
        broker = Broker(portfolio, commission_rate=0.0, slippage=0.0)
        broker.submit_order(
            "BTC/USDT", "buy", 1.0, price=100.0, order_type="market",
            timestamp=pd.Timestamp("2024-01-01"), strategy_id="S",
        )
        broker.process_orders({"BTC/USDT": _bar("2024-01-02", 100.0, 101.0, 99.0, 100.0)})
        lot = portfolio.open_lots("BTC/USDT")[0]
        assert lot.stop_price is None
        assert lot.initial_risk is None
