import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pandas as pd

from config.config import config
from core.domain import OrderStatus, SyncResult
from core.exchange import OrderParser
from core.live_broker import LiveBroker
from core.lots import CloseEvent
from core.order_store import OrderStore
from core.risk.persistent_guard import PersistentOrderSafetyGuard
from core.portfolio import Portfolio
from core.risk import RiskManager
from core.state import MarketState
from core.state_store_v2 import StateStore
from composition.factory import build_strategy_registry
from live_trading.engine import LiveTradingEngine
from strategies.mean_reversion import RangeStrategy
from strategies.trend_breakout import TrendBreakoutStrategy


NOW = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)


class TestRemainingP1LiveSafety(unittest.TestCase):
    @patch("core.live_broker.ccxt")
    def test_spot_sell_clamps_inventory_without_reduce_only(self, ccxt_mock):
        exchange = MagicMock()
        exchange.create_order.return_value = {
            "id": "sell-1", "status": "open", "amount": 1,
            "filled": 0, "remaining": 1,
        }
        ccxt_mock.binance.return_value = exchange
        portfolio = Portfolio()
        portfolio.positions["BTC/USDT"] = {"qty": 1.0, "avg_price": 100.0}
        broker = LiveBroker(portfolio, market_type="spot", clock=lambda: NOW)

        result = broker.submit_order("BTC/USDT", "sell", 2.0, 100.0)

        self.assertEqual(result.status, OrderStatus.ACCEPTED)
        kwargs = exchange.create_order.call_args.kwargs
        self.assertEqual(kwargs["amount"], 1.0)
        self.assertNotIn("reduceOnly", kwargs["params"])
        broker.close()

    def _strategy_failure_engine(self, directory):
        frame = pd.DataFrame(
            {
                "open": [100.0], "high": [101.0], "low": [99.0],
                "close": [100.0], "volume": [1000.0],
            },
            index=pd.to_datetime(["2026-08-12T00:00:00Z"]),
        )
        fetcher = MagicMock()
        fetcher.fetch_ccxt.return_value = frame
        broker = MagicMock()
        broker.exchange_id = "binance"
        broker.account_id = "spot"
        broker.market_type = "spot"
        broker.portfolio = Portfolio()
        broker.sync.return_value = SyncResult(True, NOW)
        broker.recover_open_orders.return_value = {}
        broker.has_unresolved_unknown.return_value = False
        store = StateStore(os.path.join(directory, "state.db"))
        engine = LiveTradingEngine(
            symbols=["BTC/USDT"],
            strategies={},
            broker=broker,
            risk_manager=RiskManager(),
            configuration=config,
            data_fetcher=fetcher,
            clock=lambda: NOW,
            state_file=os.path.join(directory, "status.json"),
            state_store=store,
            close_grace_seconds=0,
            strategy_failure_threshold=2,
        )
        engine.data_map["BTC/USDT"] = frame
        engine.event_processor.process_symbol = MagicMock(
            side_effect=RuntimeError("strategy exploded")
        )
        return engine, store

    def test_strategy_failures_degrade_then_halt_and_are_exported(self):
        with tempfile.TemporaryDirectory() as directory:
            engine, store = self._strategy_failure_engine(directory)

            self.assertTrue(engine._tick())
            self.assertEqual(engine._operational_state, "DEGRADED")
            self.assertFalse(engine._healthy)
            self.assertTrue(engine._tick())
            self.assertEqual(engine._operational_state, "HALTED")
            with open(engine.state_file, encoding="utf-8") as handle:
                state = json.load(handle)
            self.assertEqual(state["consecutive_strategy_failures"], 2)
            self.assertEqual(state["last_strategy_error"], "RuntimeError")
            store.close()

    def test_reconciliation_runs_on_its_own_interval_and_records_counts(self):
        broker = MagicMock()
        broker.market_type = "spot"
        broker.portfolio = Portfolio()
        broker.has_unresolved_unknown.return_value = False
        broker.recover_open_orders.return_value = {"order-1": MagicMock(status=OrderStatus.ACCEPTED)}
        engine = LiveTradingEngine(
            symbols=[], strategies={}, broker=broker,
            risk_manager=RiskManager(), configuration=config, clock=lambda: NOW,
            reconciliation_interval_seconds=60,
        )

        first = engine._run_reconciliation_if_due(NOW)
        second = engine._run_reconciliation_if_due(NOW + timedelta(seconds=30))
        third = engine._run_reconciliation_if_due(NOW + timedelta(seconds=61))

        self.assertEqual(broker.recover_open_orders.call_count, 2)
        self.assertEqual(first["checked_count"], 1)
        self.assertEqual(second["last_run_at"], first["last_run_at"])
        self.assertNotEqual(third["last_run_at"], first["last_run_at"])

    def test_export_failure_logs_traceback(self):
        broker = MagicMock()
        broker.market_type = "spot"
        broker.portfolio = Portfolio()
        broker.has_unresolved_unknown.return_value = False
        engine = LiveTradingEngine(
            symbols=[], strategies={}, broker=broker,
            risk_manager=RiskManager(), configuration=config, clock=lambda: NOW,
        )
        with patch("builtins.open", side_effect=OSError("disk full")), patch(
            "live_trading.engine.logger.exception"
        ) as logged:
            engine._export_state()
        logged.assert_called_once_with("Failed to export state")


class TestRemainingP1PersistenceAndStrategies(unittest.TestCase):
    def test_all_sqlite_stores_have_schema_versions_and_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = os.path.join(directory, "state.db")
            order_path = os.path.join(directory, "orders.db")
            risk_path = os.path.join(directory, "risk.db")
            state = StateStore(state_path)
            orders = OrderStore(order_path)
            policy = MagicMock()
            risk = PersistentOrderSafetyGuard(policy, path=risk_path)

            expected = [
                (state_path, "state_store"),
                (order_path, "order_store"),
                (risk_path, "persistent_risk_guard"),
            ]
            for path, component in expected:
                connection = sqlite3.connect(path)
                try:
                    row = connection.execute(
                        "SELECT version FROM schema_metadata WHERE component=?",
                        (component,),
                    ).fetchone()
                finally:
                    connection.close()
                self.assertEqual(row, (1,))

            self.assertIsNotNone(orders.snapshot_if_due())
            self.assertIsNotNone(risk.snapshot_if_due())
            state.close()
            orders.close()
            risk.close()

    def test_exit_signal_does_not_book_a_fake_trade(self):
        strategy = TrendBreakoutStrategy(entry_window=1, exit_window=1)
        portfolio = Portfolio()
        portfolio.positions["BTC/USDT"] = {"qty": 1.0, "avg_price": 100.0}
        strategy.context["BTC/USDT"] = {"entry_price": 100.0}
        frame = pd.DataFrame(
            {
                "open": [100.0, 90.0],
                "high": [101.0, 91.0],
                "low": [99.0, 89.0],
                "close": [100.0, 90.0],
                strategy.col_high_max: [float("nan"), 101.0],
                strategy.col_low_min: [float("nan"), 95.0],
            }
        )

        signal = strategy.should_exit(
            "BTC/USDT", 1, frame, MarketState.TREND_UP, portfolio
        )

        self.assertEqual(signal["action"], "sell")
        self.assertEqual(strategy.health_stats["total_trades"], 0)

    def test_base_consumes_real_fill_for_mean_reversion_cooldown(self):
        strategy = RangeStrategy()
        strategy.context["BTC/USDT"] = {"entry_price": 100.0}
        portfolio = Portfolio()
        broker = MagicMock()
        broker.close_events = [
            CloseEvent(
                close_event_id="LOT-000000001:1",
                position_id="POS-000000001",
                lot_id="LOT-000000001",
                symbol="BTC/USDT",
                opening_strategy_id=strategy.name,
                exit_reason="signal",
                qty=1.0,
                exit_price=90.0,
                theoretical_exit_price=90.0,
                realized_pnl=-11.0,
                timestamp=NOW,
                is_position_fully_closed=True,
            )
        ]

        strategy._consume_execution_trades(
            "BTC/USDT", 12, portfolio, broker
        )

        self.assertEqual(
            strategy.get_trade_state("BTC/USDT")["consecutive_losses"], 1
        )

    def test_trend_health_records_only_realized_fill_and_restores(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(os.path.join(directory, "state.db"))
            first = TrendBreakoutStrategy()
            first.bind_state_store(store)
            first.on_trade_closed(
                "BTC/USDT", -12.5,
                {"fill_price": 90.0, "commission": 1.0},
                20,
            )
            self.assertEqual(first.health_stats["total_trades"], 1)
            self.assertEqual(first.health_stats["rolling_pnl"], [-12.5])

            restored = TrendBreakoutStrategy()
            restored.bind_state_store(store)
            self.assertEqual(restored.health_stats, first.health_stats)
            restored.reset_runtime_state()
            self.assertEqual(restored.health_stats["total_trades"], 0)
            self.assertTrue(restored.health_stats["is_alive"])
            store.close()

    def test_mean_reversion_cooldown_uses_realized_fill_pnl(self):
        strategy = RangeStrategy()
        for bar_index in (10, 11, 12):
            strategy.on_trade_closed(
                "BTC/USDT", -1.0,
                {"fill_price": 90.0, "commission": 0.1},
                bar_index,
            )
        state = strategy.get_trade_state("BTC/USDT")
        self.assertEqual(state["cooldown_until"], 36)
        self.assertEqual(state["consecutive_losses"], 0)

    def test_strategy_registry_contains_only_routable_implementations(self):
        registry = build_strategy_registry()
        routed = {
            name for name in config.require("routing").values() if name != "Cash"
        }
        self.assertNotIn("TrendUp", registry)
        self.assertNotIn("TrendDown", registry)
        self.assertTrue(routed.issubset(registry))


class TestRemainingP1OrderParsing(unittest.TestCase):
    def test_canceled_partial_payload_stays_terminal_at_boundary(self):
        parsed = OrderParser().parse(
            {
                "status": "canceled", "amount": 10,
                "filled": 3, "remaining": 7,
            }
        )
        self.assertEqual(parsed.status, OrderStatus.CANCELED)


if __name__ == "__main__":
    unittest.main()
