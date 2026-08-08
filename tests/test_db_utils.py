import os
import tempfile
import unittest

from core.db_utils import DatabaseIntegrityError, ensure_sqlite_integrity
from core.order_store import OrderStore
from core.state_store_v2 import StateStore
from core.persistent_risk_guard import PersistentOrderSafetyGuard
from core.live_safety import StartupSafetyPolicy


class TestSqliteIntegrityCheck(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _corrupt(self, path: str) -> None:
        with open(path, "wb") as handle:
            handle.write(b"not a sqlite file at all, deliberately corrupted")

    def test_order_store_fails_closed_on_corrupted_file(self):
        path = os.path.join(self.temp_dir.name, "orders.db")
        self._corrupt(path)
        with self.assertRaises(Exception):
            OrderStore(path)

    def test_state_store_fails_closed_on_corrupted_file(self):
        path = os.path.join(self.temp_dir.name, "state.db")
        self._corrupt(path)
        with self.assertRaises(Exception):
            StateStore(path)

    def test_persistent_risk_guard_fails_closed_on_corrupted_file(self):
        path = os.path.join(self.temp_dir.name, "risk.db")
        self._corrupt(path)
        policy = StartupSafetyPolicy(
            sandbox=True, exchange_id="binance", account_type="spot",
            symbols=("BTC/USDT",), allowed_exchanges=("binance",),
            allowed_account_types=("spot",), allowed_symbols=("BTC/USDT",),
            base_currency="USDT", max_order_notional=1000.0,
            max_daily_new_risk=1000.0, kill_switch_env="TEST_KILL_SWITCH",
        )
        with self.assertRaises(Exception):
            PersistentOrderSafetyGuard(policy, path=path)

    def test_healthy_db_passes_integrity_check(self):
        path = os.path.join(self.temp_dir.name, "healthy.db")
        store = OrderStore(path)
        ensure_sqlite_integrity(store._connection, path)
        store.close()

    def test_order_store_uses_wal_journal_mode(self):
        path = os.path.join(self.temp_dir.name, "wal.db")
        store = OrderStore(path)
        mode = store._connection.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(mode.lower(), "wal")
        store.close()


if __name__ == "__main__":
    unittest.main()
