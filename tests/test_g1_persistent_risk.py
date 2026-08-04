import os
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

from core.live_safety import SafetyConfigurationError, StartupSafetyPolicy
from core.persistent_risk_guard import PersistentOrderSafetyGuard


class TestPersistentDailyRisk(unittest.TestCase):
    def test_restart_cannot_reset_daily_new_risk(self):
        policy = StartupSafetyPolicy(
            sandbox=True,
            exchange_id="binance",
            account_type="spot",
            symbols=("BTC/USDT",),
            allowed_exchanges=("binance",),
            allowed_account_types=("spot",),
            allowed_symbols=("BTC/USDT",),
            base_currency="USDT",
            max_order_notional=1000.0,
            max_daily_new_risk=1500.0,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "risk.db")
            first = PersistentOrderSafetyGuard(
                policy, path, clock=lambda: date(2026, 8, 2)
            )
            first.assert_order_allowed("BTC/USDT", "buy", 1, 800)
            first.close()

            restarted = PersistentOrderSafetyGuard(
                policy, path, clock=lambda: date(2026, 8, 2)
            )
            with self.assertRaisesRegex(SafetyConfigurationError, "daily new-risk"):
                restarted.assert_order_allowed("BTC/USDT", "buy", 1, 800)
            restarted.close()


if __name__ == "__main__":
    unittest.main()
