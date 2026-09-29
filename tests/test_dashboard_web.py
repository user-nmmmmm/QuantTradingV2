"""Contract checks for the local, read-only monitoring page."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from dashboard.web import DashboardHTTPServer, DashboardSource


class DashboardWebTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.status = root / "live_status.json"
        self.alerts = root / "live_alerts.jsonl"
        self.market_data = root / "market_data"
        self.reports = root / "reports"
        self.market_data.mkdir()
        self.reports.mkdir()
        self.server = DashboardHTTPServer(
            ("127.0.0.1", 0), DashboardSource(
                self.status, self.alerts,
                data_dir=self.market_data, reports_dir=self.reports,
            ),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()
        self.directory.cleanup()

    def test_missing_snapshot_fails_closed_and_never_invents_financial_values(self) -> None:
        with urlopen(self.base + "/api/status") as response:
            payload = json.load(response)
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
        self.assertFalse(payload["status_valid"])
        self.assertIsNone(payload["equity"])
        self.assertIsNone(payload["cash"])
        self.assertEqual(payload["positions"], {})
        self.assertEqual(payload["mode"], "live")

    def test_live_snapshot_projects_approved_fields_and_observes_history(self) -> None:
        base = {
            "last_update": "2026-09-29T10:00:00+00:00",
            "healthy": True,
            "operational_state": "HEALTHY",
            "health_assessment": {"reasons": []},
            "equity": 100.0,
            "cash": 75.0,
            "positions": {"BTC/USDT": {"qty": 0.01, "avg_price": 2500.0}},
            "account_entry_gate": {"allows_new_risk": True},
            "api_key": "must-never-be-exposed",
        }
        self.status.write_text(json.dumps(base), encoding="utf-8")
        with urlopen(self.base + "/api/status") as response:
            first = json.load(response)
        self.assertTrue(first["status_valid"])
        self.assertEqual(first["equity"], 100.0)
        self.assertEqual(first["details"]["account_entry_gate"], {"allows_new_risk": True})
        self.assertNotIn("api_key", json.dumps(first))
        self.assertEqual(len(first["history"]), 1)

        base["last_update"] = "2026-09-29T10:01:00+00:00"
        base["equity"] = 105.0
        self.status.write_text(json.dumps(base), encoding="utf-8")
        with urlopen(self.base + "/api/status") as response:
            second = json.load(response)
        self.assertEqual([item["equity"] for item in second["history"]], [100.0, 105.0])

    def test_static_page_is_served_and_write_requests_are_rejected(self) -> None:
        with urlopen(self.base + "/") as response:
            page = response.read().decode("utf-8")
        self.assertIn("交易监控", page)
        self.assertNotIn('class="hero"', page)
        self.assertIn('id="themeToggle"', page)
        with urlopen(self.base + "/assets/app.js") as response:
            self.assertIn("renderPositions", response.read().decode("utf-8"))
        with urlopen(self.base + "/assets/theme.js") as response:
            self.assertIn("stillwater-dashboard-theme", response.read().decode("utf-8"))
        with urlopen(self.base + "/assets/theme.css") as response:
            self.assertIn("status-pill", response.read().decode("utf-8"))
        with urlopen(self.base + "/assets/visual.js") as response:
            self.assertIn("renderCandles", response.read().decode("utf-8"))
        with self.assertRaises(HTTPError) as raised:
            urlopen(Request(self.base + "/api/status", data=b"{}", method="POST"))
        self.assertEqual(raised.exception.code, 405)
        with self.assertRaises(HTTPError) as raised:
            urlopen(self.base + "/../core/status_snapshot.py")
        self.assertEqual(raised.exception.code, 404)

    def test_historical_candles_are_bounded_and_source_labelled(self) -> None:
        candle_file = self.market_data / "BTC_USDT.csv"
        candle_file.write_text(
            "timestamp,open,high,low,close,volume\n"
            "2026-09-18,10,12,9,11,100\n"
            "2026-09-19,11,13,10,12,120\n"
            "2026-09-20,12,10,11,12,10\n",
            encoding="utf-8",
        )
        with urlopen(self.base + "/api/markets") as response:
            self.assertEqual(json.load(response)["markets"], ["BTC/USDT"])
        with urlopen(self.base + "/api/candles?symbol=BTC%2FUSDT&limit=60") as response:
            candles = json.load(response)
        self.assertEqual(candles["mode"], "historical_cache")
        self.assertEqual(candles["timeframe"], "1d")
        self.assertEqual(candles["last_candle"], "2026-09-19")
        self.assertEqual(candles["invalid_rows"], 1)
        self.assertEqual(len(candles["candles"]), 2)
        with self.assertRaises(HTTPError) as raised:
            urlopen(self.base + "/api/candles?symbol=..%2FUSDT&limit=60")
        self.assertEqual(raised.exception.code, 400)

    def test_backtest_exposes_computed_return_and_drawdown(self) -> None:
        run = self.reports / "sample_run"
        run.mkdir()
        (run / "equity.csv").write_text(
            "timestamp,equity\n"
            "2026-01-01,100\n"
            "2026-01-02,120\n"
            "2026-01-03,90\n"
            "2026-01-04,110\n",
            encoding="utf-8",
        )
        (run / "benchmark.csv").write_text(
            "timestamp,reference\n2026-01-01,100\n2026-01-04,105\n",
            encoding="utf-8",
        )
        (run / "trades.csv").write_text("timestamp,side\n2026-01-02,buy\n", encoding="utf-8")
        with urlopen(self.base + "/api/backtests") as response:
            runs = json.load(response)["runs"]
        self.assertEqual(runs[0]["id"], "sample_run")
        self.assertTrue(runs[0]["has_benchmark"])
        with urlopen(self.base + "/api/backtest?id=sample_run") as response:
            payload = json.load(response)
        self.assertEqual(payload["metrics"]["fills_count"], 1)
        self.assertAlmostEqual(payload["metrics"]["total_return"], .1)
        self.assertAlmostEqual(payload["metrics"]["max_drawdown"], -.25)
        self.assertEqual(payload["points"][-1]["benchmark"], 105)
        with self.assertRaises(HTTPError) as raised:
            urlopen(self.base + "/api/backtest?id=..")
        self.assertEqual(raised.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
