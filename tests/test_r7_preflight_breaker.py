import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from core.live_safety import StartupSafetyPolicy
from core.startup_preflight import build_startup_report


class RestoredBreakerPreflightTests(unittest.TestCase):
    def test_restored_breaker_allows_monitor_loop_but_reports_active_state(self):
        policy = StartupSafetyPolicy(
            sandbox=True,
            exchange_id="binance",
            account_type="spot",
            symbols=("BTC/USDT",),
            allowed_exchanges=("binance",),
            allowed_account_types=("spot",),
            allowed_symbols=("BTC/USDT",),
            base_currency="USDT",
            max_order_notional=1000,
            max_daily_new_risk=5000,
        )
        now = datetime(2026, 8, 8, 12, tzinfo=timezone.utc)
        engine = SimpleNamespace(
            health_assessment=SimpleNamespace(healthy=True, reason_codes=[]),
            risk_manager=SimpleNamespace(circuit_breaker_triggered=True),
            _last_account_sync_at=now,
            _last_order_sync_at=now,
        )
        with patch.dict("os.environ", {"QUANT_KILL_SWITCH": ""}):
            report = build_startup_report(
                policy,
                {"apiKey": "set", "secret": "set"},
                engine,
                checked_at=now,
            )

        breaker = next(
            item for item in report["checks"]
            if item["name"] == "circuit_breaker_state_restored"
        )
        self.assertTrue(report["ok"])
        self.assertTrue(breaker["passed"])
        self.assertEqual(breaker["detail"], "active")


if __name__ == "__main__":
    unittest.main()
