
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

    def test_pending_open_orders_count_toward_concentration_and_leverage(self):
        current_prices = {"BTC": 100.0}

        allowed = self.risk_manager.check_entry_risk(
            self.portfolio,
            "BTC",
            qty=10.0,
            price=100.0,
            current_prices=current_prices,
            pending_open_notional={"BTC": 1500.0},
        )

        self.assertFalse(allowed, "Pending entry exposure must be reserved")

    def test_no_price_map_rejects_when_positions_are_open(self):
        self.portfolio.positions = {"BTC": {"qty": 1.0, "avg_price": 100.0}}

        allowed = self.risk_manager.check_entry_risk(
            self.portfolio, "BTC", qty=1.0, price=100.0,
        )

        self.assertFalse(
            allowed,
            "Cannot verify exposure of existing positions without current prices",
        )

    def test_no_price_map_allows_flat_portfolio(self):
        allowed = self.risk_manager.check_entry_risk(
            self.portfolio, "BTC", qty=1.0, price=100.0,
        )

        self.assertTrue(allowed)

    def test_cash_sufficiency_rejects_buy_beyond_free_cash(self):
        """Spot buys must be fully funded by cash; no implicit margin financing."""
        # 10000 cash, price 100 -> buying 150 qty needs 15000, only 10000 free.
        # Concentration/leverage limits alone (20%/3x) would not catch this at
        # this qty/price combo unless cash is checked directly, so raise them
        # out of the way to isolate the cash check.
        risk_manager = RiskManager(
            max_leverage=100.0, max_pos_size_pct=1.0, liquidity_limit_pct=1.0
        )
        allowed = risk_manager.check_entry_risk(
            self.portfolio, "BTC", qty=150.0, price=100.0,
        )
        self.assertFalse(allowed, "Buy notional exceeding free cash must be rejected")

    def test_cash_sufficiency_accounts_for_reserved_notional(self):
        risk_manager = RiskManager(
            max_leverage=100.0, max_pos_size_pct=1.0, liquidity_limit_pct=1.0
        )
        # 10000 cash, 6000 already reserved by other pending opens -> only
        # 4000 free; a further 5000 buy must be rejected even though it would
        # pass leverage/concentration.
        allowed = risk_manager.check_entry_risk(
            self.portfolio,
            "ETH",
            qty=50.0,
            price=100.0,
            pending_open_notional={"BTC": 6000.0},
        )
        self.assertFalse(allowed, "Reserved cash from other pending opens must be honored")

    def test_cash_sufficiency_allows_funded_buy(self):
        risk_manager = RiskManager(
            max_leverage=100.0, max_pos_size_pct=1.0, liquidity_limit_pct=1.0
        )
        allowed = risk_manager.check_entry_risk(
            self.portfolio, "BTC", qty=50.0, price=100.0,
        )
        self.assertTrue(allowed, "Buy fully covered by free cash should be allowed")

    def test_cash_sufficiency_skipped_for_short(self):
        """Shorts don't consume cash in this model (margin is not_modeled)."""
        risk_manager = RiskManager(
            max_leverage=100.0, max_pos_size_pct=2.0, liquidity_limit_pct=1.0
        )
        allowed = risk_manager.check_entry_risk(
            self.portfolio, "BTC", qty=150.0, price=100.0, action="short",
        )
        self.assertTrue(allowed, "Short notional should not be gated by cash")

    def test_clamp_reduces_oversized_entry_to_concentration_cap(self):
        """A tight stop makes risk-based sizing exceed the cap; clamp, don't reject.

        This is the structural bug that silenced RangeMeanReversion: with
        risk_per_trade/max_pos_size_pct = 0.02/0.20, any stop tighter than 10%
        of price sizes above the cap and used to be dropped entirely.
        """
        # 10000 equity, 20% cap -> 2000 max notional. Ask for 5000.
        clamped = self.risk_manager.clamp_entry_qty(
            self.portfolio, "BTC", qty=50.0, price=100.0,
            current_prices={"BTC": 100.0},
        )

        self.assertGreater(clamped, 0.0, "Oversized entry must be clamped, not dropped")
        self.assertLessEqual(clamped * 100.0, 2000.0)
        self.assertAlmostEqual(clamped, 20.0, places=6)

    def test_clamped_qty_passes_the_gate_it_was_clamped_to(self):
        """Clamp and gate must agree — no float-rounding standoff between them."""
        prices = {"BTC": 100.0}
        clamped = self.risk_manager.clamp_entry_qty(
            self.portfolio, "BTC", qty=50.0, price=100.0, current_prices=prices,
        )

        self.assertTrue(
            self.risk_manager.check_entry_risk(
                self.portfolio, "BTC", clamped, 100.0, current_prices=prices,
            ),
            "check_entry_risk must accept the qty clamp_entry_qty produced",
        )

    def test_clamp_respects_the_binding_constraint(self):
        """Cash can bind tighter than concentration; the minimum must win."""
        risk_manager = RiskManager(max_leverage=100.0, max_pos_size_pct=1.0)
        # 10000 cash, 6000 reserved -> 4000 free cash is the binding cap,
        # looser concentration (100%) must not override it.
        clamped = risk_manager.clamp_entry_qty(
            self.portfolio, "ETH", qty=90.0, price=100.0,
            pending_open_notional={"BTC": 6000.0},
        )

        self.assertAlmostEqual(clamped * 100.0, 4000.0, delta=1e-3)

    def test_clamp_skips_dust_entries(self):
        """When almost no headroom remains, skip rather than pay fees for dust."""
        # 20% cap on 10000 equity = 2000; 1999 already reserved leaves 1,
        # far below the 1%-of-equity (100) minimum.
        clamped = self.risk_manager.clamp_entry_qty(
            self.portfolio, "BTC", qty=50.0, price=100.0,
            current_prices={"BTC": 100.0},
            pending_open_notional={"BTC": 1999.0},
        )

        self.assertEqual(clamped, 0.0)

    def test_clamp_returns_zero_when_breaker_or_health_blocks(self):
        risk_manager = RiskManager()
        risk_manager.circuit_breaker_triggered = True

        self.assertEqual(
            risk_manager.clamp_entry_qty(
                self.portfolio, "BTC", qty=1.0, price=100.0,
            ),
            0.0,
        )

    def test_clamp_leaves_compliant_size_untouched(self):
        clamped = self.risk_manager.clamp_entry_qty(
            self.portfolio, "BTC", qty=10.0, price=100.0,
            current_prices={"BTC": 100.0},
        )

        self.assertAlmostEqual(clamped, 10.0, places=9)

    def test_reset_daily_breaker(self):
        self.risk_manager.circuit_breaker_triggered = True

        self.risk_manager.reset_daily_breaker()

        self.assertFalse(self.risk_manager.circuit_breaker_triggered)

if __name__ == '__main__':
    unittest.main()
