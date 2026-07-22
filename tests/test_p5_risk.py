
import unittest
import logging
import sys
import os

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.risk import RiskManager
from core.portfolio import Portfolio

# Configure logging to capture output
logging.basicConfig(level=logging.INFO)

class TestRiskManager(unittest.TestCase):
    def setUp(self):
        self.risk_manager = RiskManager(
            risk_per_trade=0.01,  # 1% risk
            max_leverage=3.0,
            max_drawdown_limit=0.20,
            max_pos_size_pct=0.20 # 20% max pos size
        )
        self.portfolio = Portfolio(initial_capital=10000.0)

    def test_position_sizing_risk_pct(self):
        """Test position sizing based on Stop Loss risk."""
        equity = 10000.0
        entry = 100.0
        stop = 90.0
        # Risk Amount = 10000 * 0.01 = 100
        # Risk per Share = 100 - 90 = 10
        # Qty = 100 / 10 = 10
        qty = self.risk_manager.calculate_position_size(equity, entry, stop)
        self.assertAlmostEqual(qty, 10.0)

    def test_position_sizing_fixed_pct(self):
        """Test position sizing based on Fixed Percentage (fallback)."""
        equity = 10000.0
        entry = 100.0
        pct = 0.10 # 10%
        # Allocation = 10000 * 0.10 = 1000
        # Qty = 1000 / 100 = 10
        qty = self.risk_manager.calculate_position_size_fixed_pct(equity, entry, pct)
        self.assertAlmostEqual(qty, 10.0)

    def test_concentration_check(self):
        """Test rejection of concentrated positions."""
        equity = 10000.0
        price = 100.0
        
        # Try to buy 30% of equity (Max is 20%)
        # 30% = 3000 USD
        # Qty = 30
        qty = 30.0
        
        # Fake current prices for portfolio check
        current_prices = {"BTC": 100.0}
        
        # Should be rejected
        allowed = self.risk_manager.check_entry_risk(
            self.portfolio, "BTC", qty, price, current_prices=current_prices
        )
        self.assertFalse(allowed, "Should reject 30% concentration when max is 20%")
        
        # Try to buy 10% (Should pass)
        qty = 10.0
        allowed = self.risk_manager.check_entry_risk(
            self.portfolio, "BTC", qty, price, current_prices=current_prices
        )
        self.assertTrue(allowed, "Should allow 10% concentration")

    def test_leverage_check(self):
        """Test rejection of excessive leverage."""
        equity = 10000.0
        price = 100.0
        
        # Try to buy 4x equity (Max is 3x)
        # 40000 USD
        # Qty = 400
        qty = 400.0
        
        current_prices = {"BTC": 100.0}
        
        allowed = self.risk_manager.check_entry_risk(
            self.portfolio, "BTC", qty, price, current_prices=current_prices
        )
        self.assertFalse(allowed, "Should reject 4x leverage")

    def test_reset_daily_breaker(self):
        self.risk_manager.circuit_breaker_triggered = True

        self.risk_manager.reset_daily_breaker()

        self.assertFalse(self.risk_manager.circuit_breaker_triggered)

if __name__ == '__main__':
    unittest.main()
