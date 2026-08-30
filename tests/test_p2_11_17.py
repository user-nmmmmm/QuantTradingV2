from __future__ import annotations

import inspect
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from config.config import config
from core.events import TradingEventPipeline
from core.health import DataHealthMonitor
from core.market_data import HistoricalMarketDataAdapter
from core.portfolio import Portfolio
from core.risk import RiskManager
from core.runtime import EventProcessor, MarketDataSlice
from core.state import MarketState
from live_trading.engine import LiveTradingEngine
from router.router import Router


NOW = pd.Timestamp("2026-08-13T12:00:00Z").to_pydatetime()
ROOT = Path(__file__).resolve().parents[1]


class TestP211ToP217(unittest.TestCase):
    def test_health_fast_path_reuses_normalized_index_and_tail_window(self):
        index = pd.date_range("2026-01-01", periods=500, freq="h")
        frame = pd.DataFrame({"close": range(500)}, index=index)
        now = (index[-1] + pd.Timedelta(hours=1)).tz_localize("UTC").to_pydatetime()
        monitor = DataHealthMonitor()
        with patch(
            "core.health.pd.to_datetime",
            side_effect=AssertionError("normalized index must not be reparsed"),
        ):
            assessment = monitor.assess(
                now=now,
                symbols=["BTC/USDT"],
                timeframe="1h",
                data_map={"BTC/USDT": frame},
                account_synced_at=now,
                order_synced_at=now,
            )
        self.assertTrue(assessment.healthy)

    def _live_engine(self, export_interval=3):
        broker = MagicMock()
        broker.market_type = "spot"
        broker.exchange_id = "binance"
        broker.account_id = "spot"
        broker.portfolio = Portfolio()
        broker.has_unresolved_unknown.return_value = False
        return LiveTradingEngine(
            symbols=[],
            strategies={},
            broker=broker,
            risk_manager=RiskManager(),
            configuration=config,
            clock=lambda: NOW,
            state_export_interval_ticks=export_interval,
        )

    def test_live_state_export_is_periodic_or_triggered_by_transition(self):
        engine = self._live_engine(export_interval=3)
        engine._export_state = MagicMock(return_value=True)

        engine._tick_count = 1
        self.assertTrue(engine._maybe_export_state())
        engine._tick_count = 2
        self.assertFalse(engine._maybe_export_state())
        engine._tick_count = 3
        self.assertFalse(engine._maybe_export_state())
        engine._tick_count = 4
        self.assertTrue(engine._maybe_export_state())

        engine._tick_count = 5
        engine._operational_state = "HALTED"
        self.assertTrue(engine._maybe_export_state())
        self.assertEqual(engine._export_state.call_count, 3)

    def test_historical_stream_carries_precomputed_positions(self):
        frame = pd.DataFrame(
            {"close": [1.0, 2.0, 3.0]},
            index=pd.date_range("2026-01-01", periods=3, freq="D"),
        )
        adapter = HistoricalMarketDataAdapter(
            {"BTC/USDT": frame}, calculate_indicators=False
        )
        events = list(adapter.stream())
        self.assertEqual(
            [event.positions["BTC/USDT"] for event in events],
            [0, 1, 2],
        )

    def test_runtime_uses_supplied_position_without_timestamp_lookup(self):
        frame = pd.DataFrame(
            {"close": [100.0]},
            index=[pd.Timestamp("2026-01-01")],
        )
        event = MarketDataSlice(
            timestamp=pd.Timestamp("2099-01-01"),
            bars={"BTC/USDT": frame.iloc[0]},
            histories={"BTC/USDT": frame},
            positions={"BTC/USDT": 0},
        )
        router = MagicMock()
        processor = EventProcessor(
            portfolio=Portfolio(),
            execution=MagicMock(portfolio=Portfolio()),
            risk_manager=RiskManager(),
            state_machine=MagicMock(get_state=MagicMock(return_value=MarketState.SIDEWAYS)),
            router=router,
            allocator=MagicMock(),
        )
        self.assertTrue(processor.process_symbol(event, "BTC/USDT"))
        router.collect_candidate.assert_called_once()

    def test_strategy_hot_paths_use_positional_iat_not_chained_iloc(self):
        files = list((ROOT / "strategies").glob("*.py")) + [
            ROOT / "router" / "router.py"
        ]
        offenders = [
            str(path.relative_to(ROOT))
            for path in files
            if ".iloc[i]" in path.read_text(encoding="utf-8")
        ]
        self.assertEqual(offenders, [])

    def test_event_pipeline_uses_fast_ephemeral_ids_and_bounded_retention(self):
        pipeline = TradingEventPipeline(run_id="bounded", retention_limit=3)
        with patch(
            "core.events.event_id_for",
            side_effect=AssertionError("ephemeral event should skip payload hash"),
        ):
            for index in range(5):
                pipeline.publish({"index": index}, occurred_at=NOW)
        self.assertEqual(len(pipeline.events), 3)
        self.assertEqual(len(pipeline._by_id), 3)
        self.assertEqual(
            [event.payload["index"] for event in pipeline.events],
            [2, 3, 4],
        )

    def test_router_logging_flushes_in_small_batches_and_can_be_disabled(self):
        regime_map = {
            "TREND_UP": "Cash",
            "TREND_DOWN": "Cash",
            "SIDEWAYS": "Cash",
            "VOLATILE": "Cash",
        }
        disabled = Router({}, regime_map=regime_map, log_path=None)
        disabled._log_routing(
            NOW, "BTC", "SIDEWAYS", "CASH", 0,
            route_event="cash", strategy_changed=False,
        )
        self.assertEqual(disabled.log_buffer, [])

        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "routing.csv")
            enabled = Router(
                {}, regime_map=regime_map, log_path=path, log_flush_every=2
            )
            for symbol in ("BTC", "ETH"):
                enabled._log_routing(
                    NOW, symbol, "SIDEWAYS", "CASH", 0,
                    route_event="cash", strategy_changed=False,
                )
            self.assertEqual(enabled.log_buffer, [])
            self.assertTrue(Path(path).exists())
            rows = pd.read_csv(path)
            self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()
