"""Phase 1 (T-1.6 / T-1.7): cost-field contract and cost-sensitivity fix.

T-1.6: every fill persists theoretical_price (zero-cost reference price)
alongside the existing slip-inflated fill_price, with consistent units
and sign (theoretical_price is always the pre-slip reference; slip always
worsens the trader's fill relative to it).

T-1.7: core.metrics.calculate_cost_sensitivity's 1.0x/1.0x baseline must
reproduce the main report's NetPnL exactly - previously it double-counted
slippage (I-25) because it subtracted total_slippage from a gross_pnl that
already had slippage baked into the fill price.
"""
import pandas as pd
import pytest

from backtest.reporting import ReportGenerator
from core.broker import Broker
from core.metrics import calculate_cost_sensitivity
from core.portfolio import Portfolio


class TestCostFieldContract:
    def test_buy_fill_theoretical_price_is_below_fill_price(self):
        portfolio = Portfolio(initial_capital=10_000.0)
        broker = Broker(portfolio, commission_rate=0.0, slippage=0.01)
        broker.submit_order(
            "BTC/USDT", "buy", 1.0, price=100.0, order_type="market",
            timestamp=pd.Timestamp("2024-01-01"), strategy_id="S",
        )
        trades = broker.process_orders({
            "BTC/USDT": pd.Series(
                {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1000.0},
                name=pd.Timestamp("2024-01-02"),
            )
        })
        trade = trades[0]
        assert trade["theoretical_price"] == pytest.approx(100.0)
        # Buying always fills at or above the theoretical reference price.
        assert trade["fill_price"] > trade["theoretical_price"]
        assert trade["slip"] == pytest.approx(
            abs(trade["fill_price"] - trade["theoretical_price"])
        )

    def test_sell_fill_theoretical_price_is_above_fill_price(self):
        portfolio = Portfolio(initial_capital=10_000.0)
        broker = Broker(portfolio, commission_rate=0.0, slippage=0.01)
        broker.submit_order(
            "BTC/USDT", "buy", 1.0, price=100.0, order_type="market",
            timestamp=pd.Timestamp("2024-01-01"), strategy_id="S",
        )
        broker.process_orders({
            "BTC/USDT": pd.Series(
                {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1000.0},
                name=pd.Timestamp("2024-01-02"),
            )
        })
        broker.submit_order(
            "BTC/USDT", "sell", 1.0, price=110.0, order_type="market",
            timestamp=pd.Timestamp("2024-01-02"), strategy_id="S",
        )
        trades = broker.process_orders({
            "BTC/USDT": pd.Series(
                {"open": 110.0, "high": 111.0, "low": 109.0, "close": 110.0, "volume": 1000.0},
                name=pd.Timestamp("2024-01-03"),
            )
        })
        trade = trades[0]
        assert trade["theoretical_price"] == pytest.approx(110.0)
        # Selling always fills at or below the theoretical reference price.
        assert trade["fill_price"] < trade["theoretical_price"]


class TestCostSensitivityNoDoubleCounting:
    def test_unit_level_1x_baseline_matches_gross_minus_commission(self):
        # A round trip with slippage on both legs: theoretical prices are
        # slip-free, gross_pnl (fill-price based) already reflects the cost.
        trades = [{
            "gross_pnl": 290.0,          # (10300 fill - 10 sell slip) - (10000 fill + 5? ...) simplified below
            "gross_pnl_theoretical": 300.0,
            "commission": 21.0,
            "slippage": 10.0,
        }]
        result = calculate_cost_sensitivity(trades)
        one_by_one = next(
            g for g in result["grid"]
            if g["commission_multiplier"] == 1.0 and g["slippage_multiplier"] == 1.0
        )
        # gross_theoretical - commission - slippage == gross_pnl - commission
        # (since gross_pnl = gross_theoretical - slippage by construction).
        expected = trades[0]["gross_pnl"] - trades[0]["commission"]
        assert one_by_one["net_pnl"] == pytest.approx(expected)
        assert result["baseline_net_pnl"] == pytest.approx(expected)

    def test_end_to_end_report_cost_sensitivity_matches_net_pnl(self):
        equity_curve = pd.DataFrame(
            {"equity": [10000.0, 10300.0], "cash": [10000.0, 10300.0]},
            index=pd.date_range("2026-01-01", periods=2, freq="D", tz="UTC"),
        )
        trades = [
            {
                "symbol": "BTC/USDT", "side": "buy", "qty": 1.0,
                "fill_price": 10010.0, "theoretical_price": 10000.0,
                "commission": 10.0, "slip": 10.0, "strategy_id": "S",
                "fill_time": pd.Timestamp("2026-01-01", tz="UTC"),
            },
            {
                "symbol": "BTC/USDT", "side": "sell", "qty": 1.0,
                "fill_price": 10290.0, "theoretical_price": 10300.0,
                "commission": 11.0, "slip": 10.0, "strategy_id": "S",
                "fill_time": pd.Timestamp("2026-01-02", tz="UTC"),
            },
        ]
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            metrics = ReportGenerator(directory).generate(
                trades, equity_curve, metrics_only=True,
            )
        net_pnl = metrics["NetPnL"]
        cost_sensitivity = metrics["ExtendedAnalytics"]["cost_sensitivity"]
        assert cost_sensitivity["baseline_net_pnl"] == pytest.approx(net_pnl)
