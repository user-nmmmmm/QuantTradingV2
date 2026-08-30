import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pandas as pd

from config.config import config
from core.domain import SyncResult
from core.portfolio import Portfolio
from core.risk import RiskManager
from core.state_store_v2 import StateStore
from live_trading.engine import LiveTradingEngine


class TestEngineBarSafety(unittest.TestCase):
    def test_unknown_order_releases_bar_instead_of_marking_processed(self):
        now = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
        frame = pd.DataFrame(
            {"open": [100], "high": [101], "low": [99], "close": [100], "volume": [1000]},
            index=pd.to_datetime(["2026-08-01T00:00:00Z"]),
        )
        fetcher = MagicMock()
        fetcher.fetch_ccxt.return_value = frame
        broker = MagicMock()
        broker.exchange_id = "binance"
        broker.account_id = "spot"
        broker.market_type = "spot"
        broker.portfolio = Portfolio()
        broker.sync.return_value = SyncResult(True, now)
        broker.recover_open_orders.return_value = {}
        unknown = {"active": False}
        broker.has_unresolved_unknown.side_effect = lambda: unknown["active"]

        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(os.path.join(directory, "state.db"))
            engine = LiveTradingEngine(
                symbols=["BTC/USDT"], strategies={}, broker=broker,
                risk_manager=RiskManager(), configuration=config, data_fetcher=fetcher,
                clock=lambda: now, state_file=os.path.join(directory, "status.json"),
                state_store=store, close_grace_seconds=0,
            )
            engine.data_map["BTC/USDT"] = frame
            engine.state_machine.get_state = MagicMock(return_value=MagicMock())
            engine.router.collect_candidate = MagicMock(
                side_effect=lambda *args: unknown.update(active=True)
            )

            engine._tick()

            key = "binance|spot|BTC/USDT|1d|2026-08-02T00:00:00+00:00"
            self.assertIsNone(store.status(key))
            self.assertFalse(engine._healthy)
            store.close()


if __name__ == "__main__":
    unittest.main()
