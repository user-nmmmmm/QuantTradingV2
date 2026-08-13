from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

import main as backtest_entrypoint
from core.domain import SyncResult
from core.live_broker import LiveBroker
from core.market_data import LiveMarketDataAdapter, normalize_market_frame
from core.order_store import OrderStore, OrderStoreClosedError
from core.timeframes import closed_bars
from dashboard.__main__ import load_dashboard, render_text


ROOT = Path(__file__).resolve().parents[1]


class TestP201ToP210(unittest.TestCase):
    def test_placeholder_ml_package_is_absent_and_boundary_is_documented(self):
        self.assertFalse((ROOT / "models").exists())
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("no machine-learning training or prediction subsystem", readme)

    def test_dashboard_is_a_runnable_read_only_consumer(self):
        with tempfile.TemporaryDirectory() as directory:
            status = Path(directory) / "status.json"
            status.write_text(
                '{"healthy": true, "operational_state": "HEALTHY", '
                '"health_assessment": {"reasons": []}}',
                encoding="utf-8",
            )
            data = load_dashboard(str(status), str(Path(directory) / "alerts.jsonl"))
            self.assertTrue(data["status_valid"])
            self.assertIn("QuantTrading live operations", render_text(data))

    def test_generated_output_directories_are_ignored(self):
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("dummy_output/", ignore)
        self.assertIn("reports/*/", ignore)

    def test_cli_no_args_and_invalid_dates_return_nonzero(self):
        self.assertEqual(backtest_entrypoint.main([]), 2)
        self.assertEqual(
            backtest_entrypoint.main(
                ["--start", "2026-02-02", "--end", "2026-01-01"]
            ),
            2,
        )

    def test_cli_no_data_and_empty_backtest_return_nonzero(self):
        with patch.object(
            backtest_entrypoint, "get_data", return_value=pd.DataFrame()
        ):
            self.assertEqual(
                backtest_entrypoint.main(["--source", "synthetic", "--days", "20"]),
                3,
            )

        frame = pd.DataFrame(
            {
                "open": range(12),
                "high": range(1, 13),
                "low": range(12),
                "close": range(1, 13),
                "volume": [100] * 12,
            },
            index=pd.date_range("2026-01-01", periods=12, freq="D"),
        )
        engine = MagicMock()
        engine.run.return_value = {"equity_curve": pd.DataFrame()}
        with tempfile.TemporaryDirectory() as directory, patch.object(
            backtest_entrypoint, "get_data", return_value=frame
        ), patch.object(
            backtest_entrypoint.DataHandler,
            "generate_quality_report",
            return_value={},
        ), patch.object(
            backtest_entrypoint, "BacktestEngine", return_value=engine
        ), patch.object(
            backtest_entrypoint.os, "getcwd", return_value=directory
        ):
            self.assertEqual(
                backtest_entrypoint.main(["--source", "synthetic", "--days", "20"]),
                4,
            )

    def test_live_sync_failure_logs_traceback(self):
        broker = object.__new__(LiveBroker)
        broker._retry_exchange_call = MagicMock(side_effect=ValueError("bad balance"))
        broker._load_portfolio_fact = MagicMock()
        broker._clock = lambda: datetime(2026, 8, 13, tzinfo=timezone.utc)
        broker._alert = MagicMock()
        broker.retry_max_attempts = 3
        with self.assertLogs("core.live_broker", level="ERROR") as captured:
            result = broker.sync()
        self.assertIsInstance(result, SyncResult)
        self.assertFalse(result.ok)
        self.assertIn("Traceback", "\n".join(captured.output))
        self.assertIn("bad balance", "\n".join(captured.output))

    def test_order_store_access_after_close_has_clear_error(self):
        store = OrderStore(":memory:")
        store.close()
        with self.assertRaisesRegex(
            OrderStoreClosedError, "OrderStore connection is closed"
        ):
            store.get("missing")
        store.close()

    def test_legacy_entrypoints_are_absent_and_canonical_name_documented(self):
        for name in ("Trading_V1_Model.py", "verify_system.py", "verify_logic.py"):
            self.assertFalse((ROOT / name).exists())
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Canonical repository directory: `QuantTradingV1`", readme)
        self.assertIn("QuantTradingV1/", readme)

    def test_live_refresh_is_bounded_and_normalizes_once_per_fetch(self):
        first = pd.DataFrame(
            {"open": range(5), "high": range(5), "low": range(5),
             "close": range(5), "volume": [10] * 5},
            index=pd.date_range("2026-01-01", periods=5, freq="D"),
        )
        second = pd.DataFrame(
            {"open": range(5, 10), "high": range(5, 10), "low": range(5, 10),
             "close": range(5, 10), "volume": [10] * 5},
            index=pd.date_range("2026-01-06", periods=5, freq="D"),
        )
        fetcher = MagicMock()
        fetcher.fetch_ccxt.side_effect = [first, second]
        adapter = LiveMarketDataAdapter(
            ["BTC/USDT"], fetcher, lookback=3, close_grace_seconds=0
        )
        with patch(
            "core.market_data.normalize_market_frame",
            wraps=normalize_market_frame,
        ) as normalize, patch("core.market_data.Indicators.calculate_all"):
            adapter.refresh()
            adapter.refresh()
        self.assertEqual(normalize.call_count, 2)
        self.assertEqual(len(adapter.data_map["BTC/USDT"]), 3)
        self.assertEqual(
            adapter.data_map["BTC/USDT"].index[-1], pd.Timestamp("2026-01-10")
        )

    def test_closed_bars_uses_ordered_prefix_for_naive_and_aware_indices(self):
        for index in (
            pd.date_range("2026-01-01", periods=5, freq="h"),
            pd.date_range("2026-01-01", periods=5, freq="h", tz="UTC"),
        ):
            frame = pd.DataFrame({"close": range(5)}, index=index)
            result = closed_bars(
                frame,
                "1h",
                datetime(2026, 1, 1, 3, 30, tzinfo=timezone.utc),
            )
            self.assertEqual(len(result), 3)
            self.assertTrue(result.index.equals(frame.index[:3]))


if __name__ == "__main__":
    unittest.main()
