import unittest
import pandas as pd
from datetime import datetime
from core.broker import BacktestOrderStatus, Broker, OrderType
from core.portfolio import Portfolio
from backtest.reporting import ReportGenerator

class TestP2OrderExecution(unittest.TestCase):
    def setUp(self):
        self.portfolio = Portfolio(initial_capital=10000.0)
        self.broker = Broker(self.portfolio)
        self.symbol = "BTC-USDT"
        
    def test_limit_buy_execution(self):
        # 1. Submit Limit Buy at 9500
        self.broker.submit_order(self.symbol, "buy", 1.0, price=9500.0, order_type="limit")
        
        # 2. Bar 1: Low is 9600 (Should NOT execute)
        bar1 = pd.Series({
            "open": 9800, "high": 9900, "low": 9600, "close": 9700, "volume": 100
        }, name=pd.Timestamp("2023-01-01 00:00"))
        
        trades = self.broker.process_orders({self.symbol: bar1})
        self.assertEqual(len(trades), 0)
        self.assertEqual(len(self.broker.active_orders), 1)
        self.assertEqual(self.broker.active_orders[0].status, BacktestOrderStatus.SUBMITTED)
        
        # 3. Bar 2: Low is 9400 (Should execute)
        bar2 = pd.Series({
            "open": 9600, "high": 9650, "low": 9400, "close": 9500, "volume": 100
        }, name=pd.Timestamp("2023-01-01 01:00"))
        
        trades = self.broker.process_orders({self.symbol: bar2})
        self.assertEqual(len(trades), 1)
        # Expected Fill: Limit is 9500. Open is 9600. Low is 9400.
        # Since Open > Limit, we fill at Limit (9500).
        self.assertEqual(trades[0]["fill_price"], 9500.0)
        self.assertEqual(len(self.broker.active_orders), 0)

    def test_limit_buy_gap_execution(self):
        # 1. Submit Limit Buy at 9500
        self.broker.submit_order(self.symbol, "buy", 1.0, price=9500.0, order_type="limit")
        
        # 2. Bar: Open is 9400 (Gap down below limit)
        bar = pd.Series({
            "open": 9400, "high": 9600, "low": 9300, "close": 9500, "volume": 100
        }, name=pd.Timestamp("2023-01-01 00:00"))
        
        trades = self.broker.process_orders({self.symbol: bar})
        self.assertEqual(len(trades), 1)
        # Expected Fill: Open (9400) is better than Limit (9500). Fill at Open.
        self.assertEqual(trades[0]["fill_price"], 9400.0)

    def test_limit_buy_fill_never_exceeds_limit_with_slippage(self):
        # Slippage must never push a limit fill past the limit price: the
        # exchange guarantees the limit as a worst-case bound.
        broker = Broker(Portfolio(initial_capital=10000.0), slippage=0.05)
        broker.submit_order(self.symbol, "buy", 1.0, price=9500.0, order_type="limit")
        bar = pd.Series({
            "open": 9490, "high": 9550, "low": 9480, "close": 9500, "volume": 1000
        }, name=pd.Timestamp("2023-01-01 00:00"))
        trades = broker.process_orders({self.symbol: bar})
        self.assertEqual(len(trades), 1)
        self.assertLessEqual(trades[0]["fill_price"], 9500.0)

    def test_limit_sell_fill_never_undercuts_limit_with_slippage(self):
        broker = Broker(Portfolio(initial_capital=10000.0), slippage=0.05)
        broker.portfolio.update_position(self.symbol, 1.0, 9000.0, 0.0)
        broker.submit_order(self.symbol, "sell", 1.0, price=9500.0, order_type="limit")
        bar = pd.Series({
            "open": 9510, "high": 9550, "low": 9480, "close": 9500, "volume": 1000
        }, name=pd.Timestamp("2023-01-01 00:00"))
        trades = broker.process_orders({self.symbol: bar})
        self.assertEqual(len(trades), 1)
        self.assertGreaterEqual(trades[0]["fill_price"], 9500.0)

    def test_stop_buy_execution(self):
        # 1. Submit Stop Buy at 10100
        self.broker.submit_order(self.symbol, "buy", 1.0, price=10100.0, order_type="stop")
        
        # 2. Bar 1: High 10000 (No Trigger)
        bar1 = pd.Series({
            "open": 9900, "high": 10000, "low": 9800, "close": 9950, "volume": 100
        }, name=pd.Timestamp("2023-01-01 00:00"))
        
        trades = self.broker.process_orders({self.symbol: bar1})
        self.assertEqual(len(trades), 0)
        
        # 3. Bar 2: High 10200 (Trigger)
        bar2 = pd.Series({
            "open": 9950, "high": 10200, "low": 9900, "close": 10150, "volume": 100
        }, name=pd.Timestamp("2023-01-01 01:00"))
        
        trades = self.broker.process_orders({self.symbol: bar2})
        self.assertEqual(len(trades), 1)
        # Expected Fill: Stop is 10100. Open is 9950. High is 10200.
        # Triggered intraday. Fill at Stop (10100) (Simplified assumption).
        self.assertEqual(trades[0]["fill_price"], 10100.0)

    def test_stop_buy_gap_execution(self):
        # 1. Submit Stop Buy at 10100
        self.broker.submit_order(self.symbol, "buy", 1.0, price=10100.0, order_type="stop")
        
        # 2. Bar: Open 10200 (Gap Up)
        bar = pd.Series({
            "open": 10200, "high": 10300, "low": 10150, "close": 10250, "volume": 100
        }, name=pd.Timestamp("2023-01-01 00:00"))
        
        trades = self.broker.process_orders({self.symbol: bar})
        self.assertEqual(len(trades), 1)
        # Expected Fill: Open (10200) > Stop (10100). Fill at Open (Slippage/Gap).
        self.assertEqual(trades[0]["fill_price"], 10200.0)

class TestP2PnLDecomposition(unittest.TestCase):
    def test_pnl_breakdown(self):
        # Create dummy trades
        # Trade 1: Buy 1 BTC @ 10000. Comm 10. Slip 5.
        # Trade 2: Sell 1 BTC @ 11000. Comm 11. Slip 5.
        # Gross PnL: (11000 - 10000) = 1000
        # Total Comm: 21
        # Total Slip: 10
        # Net PnL: 1000 - 21 = 979
        
        trades = [
            {
                "symbol": "BTC", "side": "buy", "qty": 1.0, "fill_price": 10000.0,
                "commission": 10.0, "slip": 5.0, "strategy_id": "Test", "timestamp": "t1"
            },
            {
                "symbol": "BTC", "side": "sell", "qty": 1.0, "fill_price": 11000.0,
                "commission": 11.0, "slip": 5.0, "strategy_id": "Test", "timestamp": "t2"
            }
        ]
        
        trades_df = pd.DataFrame(trades)
        report_gen = ReportGenerator("dummy_output")
        metrics = report_gen._analyze_trades(trades_df)
        
        self.assertEqual(metrics["GrossPnL"], 1000.0)
        self.assertEqual(metrics["TotalCommission"], 21.0)
        self.assertEqual(metrics["TotalSlippage"], 10.0)
        self.assertEqual(metrics["NetPnL"], 979.0)

if __name__ == "__main__":
    unittest.main()
