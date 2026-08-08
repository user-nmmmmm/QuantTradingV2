import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.live_safety import StartupSafetyPolicy
from core.startup_preflight import build_startup_report, write_startup_report


NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def policy() -> StartupSafetyPolicy:
    return StartupSafetyPolicy(
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


class StartupPreflightTests(unittest.TestCase):
    def test_healthy_sandbox_report_contains_no_secret_values(self):
        engine = SimpleNamespace(
            health_assessment=SimpleNamespace(healthy=True, reason_codes=[]),
            risk_manager=SimpleNamespace(circuit_breaker_triggered=False),
            _last_account_sync_at=NOW,
            _last_order_sync_at=NOW,
        )
        credentials = {"apiKey": "private-key", "secret": "private-secret"}

        with patch.dict("os.environ", {"QUANT_KILL_SWITCH": ""}):
            report = build_startup_report(
                policy(), credentials, engine, checked_at=NOW,
            )

        self.assertTrue(report["ok"])
        self.assertEqual(report["mode"], "SANDBOX")
        serialized = json.dumps(report)
        self.assertNotIn("private-key", serialized)
        self.assertNotIn("private-secret", serialized)

    def test_unhealthy_baseline_fails_gate_and_is_written_atomically(self):
        engine = SimpleNamespace(
            health_assessment=SimpleNamespace(
                healthy=False, reason_codes=["MARKET_DATA_STALE"],
            ),
            risk_manager=SimpleNamespace(circuit_breaker_triggered=False),
            _last_account_sync_at=NOW,
            _last_order_sync_at=NOW,
        )
        with patch.dict("os.environ", {"QUANT_KILL_SWITCH": ""}):
            report = build_startup_report(
                policy(), {"apiKey": "set", "secret": "set"}, engine,
                checked_at=NOW,
            )
        with tempfile.TemporaryDirectory() as directory:
            path = write_startup_report(
                report, str(Path(directory) / "startup_preflight.json"),
            )
            persisted = json.loads(path.read_text(encoding="utf-8"))

        self.assertFalse(report["ok"])
        self.assertEqual(persisted["health_reason_codes"], ["MARKET_DATA_STALE"])


if __name__ == "__main__":
    unittest.main()
