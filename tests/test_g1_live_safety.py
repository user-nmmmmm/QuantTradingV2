import logging
import os
import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from core.live_safety import (
    OrderSafetyGuard,
    SafetyConfigurationError,
    StartupSafetyPolicy,
    verify_live_permissions,
)


def policy(**overrides):
    values = {
        "sandbox": True,
        "exchange_id": "binance",
        "account_type": "spot",
        "symbols": ("BTC/USDT",),
        "allowed_exchanges": ("binance",),
        "allowed_account_types": ("spot",),
        "allowed_symbols": ("BTC/USDT",),
        "base_currency": "USDT",
        "max_order_notional": 1000.0,
        "max_daily_new_risk": 1500.0,
    }
    values.update(overrides)
    return StartupSafetyPolicy(**values)


class TestStartupSafetyPolicy(unittest.TestCase):
    def test_sandbox_has_safe_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            result = StartupSafetyPolicy.from_environment(
                sandbox=True,
                exchange_id="binance",
                account_type="spot",
                symbols=["BTC/USDT"],
                base_currency="USDT",
            )
        self.assertEqual(result.max_order_notional, 1000.0)

    def test_live_requires_explicit_limits(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(SafetyConfigurationError, "explicitly configured"):
                StartupSafetyPolicy.from_environment(
                    sandbox=False,
                    exchange_id="binance",
                    account_type="spot",
                    symbols=["BTC/USDT"],
                    base_currency="USDT",
                )

    def test_rejects_exchange_account_symbol_and_base_currency(self):
        cases = (
            {"exchange_id": "other"},
            {"account_type": "future"},
            {"symbols": ("DOGE/USDT",)},
            {"base_currency": "USD"},
        )
        for values in cases:
            with self.subTest(values=values):
                with self.assertRaises(SafetyConfigurationError):
                    policy(**values).validate()


class TestOrderSafetyGuard(unittest.TestCase):
    def test_kill_switch_blocks_new_orders(self):
        guard = OrderSafetyGuard(policy())
        with patch.dict(os.environ, {"QUANT_KILL_SWITCH": "true"}):
            with self.assertRaisesRegex(SafetyConfigurationError, "kill switch"):
                guard.assert_order_allowed("BTC/USDT", "buy", 0.01, 50_000)

    def test_per_order_and_daily_limits_fail_closed(self):
        guard = OrderSafetyGuard(policy())
        with self.assertRaisesRegex(SafetyConfigurationError, "maximum notional"):
            guard.assert_order_allowed("BTC/USDT", "buy", 1, 1001)
        guard.assert_order_allowed("BTC/USDT", "buy", 1, 800)
        with self.assertRaisesRegex(SafetyConfigurationError, "daily new-risk"):
            guard.assert_order_allowed("BTC/USDT", "buy", 1, 800)

    def test_close_order_does_not_consume_new_risk_limit(self):
        guard = OrderSafetyGuard(policy(max_daily_new_risk=100.0))
        guard.assert_order_allowed("BTC/USDT", "sell", 1, 900)


class TestLivePermissions(unittest.TestCase):
    def test_requires_trading_and_forbids_withdrawal(self):
        exchange = MagicMock()
        exchange.fetch_api_permissions.return_value = {
            "enableWithdrawals": False,
            "enableSpotAndMarginTrading": True,
        }
        verify_live_permissions(exchange, "spot")

        exchange.fetch_api_permissions.return_value["enableWithdrawals"] = True
        with self.assertRaisesRegex(SafetyConfigurationError, "withdrawal"):
            verify_live_permissions(exchange, "spot")

    def test_unverifiable_permissions_fail_closed(self):
        class Exchange:
            pass

        with self.assertRaisesRegex(SafetyConfigurationError, "cannot verify"):
            verify_live_permissions(Exchange(), "spot")


if __name__ == "__main__":
    unittest.main()
