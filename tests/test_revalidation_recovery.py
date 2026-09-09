import sqlite3
from datetime import datetime, timezone

import pytest

from core.domain import OrderIntent
from core.order_store import OrderStore
from core.runtime_identity import RuntimeIdentity
from core.sqlite_backup import SQLiteSnapshotManager, restore_snapshot, validate_database
from core.sqlite_utils import DatabaseIntegrityError
from core.live_safety import StartupSafetyPolicy, SafetyConfigurationError
from core.risk.persistent_guard import PersistentOrderSafetyGuard
from core.risk.actions import plan_risk_action
from core.risk.circuit_breaker import RiskControlDecision, BreakerAction


def test_account_isolation_and_snapshot_restore(tmp_path):
    a = RuntimeIdentity("binance", "sandbox", "test_a", "margin")
    b = RuntimeIdentity("binance", "live", "test_a", "margin")
    path = str(tmp_path / "orders.db")
    store = OrderStore(path, identity=a)
    one = store.action_sequence("replace:1")
    assert one == store.action_sequence("replace:1")
    snapshot = SQLiteSnapshotManager(path).create_snapshot()
    store.close()
    with pytest.raises(DatabaseIntegrityError):
        validate_database(snapshot, expected_identity=b)
    restore_snapshot(str(snapshot), path, expected_identity=a)
    store = OrderStore(path, identity=a)
    assert store.action_sequence("replace:1") == one
    assert store.action_sequence("replace:2") > one
    store.close()
    with pytest.raises(ValueError, match="identity mismatch"):
        OrderStore(path, identity=b)


def test_legacy_migration_preserves_id_and_requires_ownership(tmp_path):
    path = str(tmp_path / "legacy.db")
    store = OrderStore(path)
    intent = OrderIntent("binance", "spot", "BTC/USDT", "1d", "2020-01-01", "Legacy", "buy", 0, 1, price=100)
    store.create_intent(intent, datetime.now(timezone.utc).isoformat())
    store.close()
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE schema_metadata SET version=1 WHERE component='order_store'")
    store = OrderStore(path)
    assert store.get(intent.client_order_id)["client_order_id"] == intent.client_order_id
    assert store.get(intent.client_order_id)["intent"]["initial_stop"] is None
    store.close()
    assert list((tmp_path / "legacy.db.snapshots").glob("*.sqlite3"))
    with pytest.raises(ValueError, match="legacy shared"):
        OrderStore(path, identity=RuntimeIdentity("binance", "sandbox", "new", "spot"))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_risk_rejected(value):
    policy = StartupSafetyPolicy(True,"binance","spot",("BTC/USDT",),("binance",),("spot",),("BTC/USDT",),"USDT",1000,5000)
    guard = PersistentOrderSafetyGuard(policy, ":memory:")
    try:
        with pytest.raises(SafetyConfigurationError):
            guard.assert_order_allowed("BTC/USDT", "buy", value, 100)
        with pytest.raises(SafetyConfigurationError):
            guard.assert_order_allowed("BTC/USDT", "sell", 1, value)
    finally:
        guard.close()


@pytest.mark.parametrize("action,fraction", [(BreakerAction.REDUCE,.5),(BreakerAction.LIQUIDATE,0),(BreakerAction.LOCKED,0)])
def test_shared_risk_action_targets(action,fraction):
    decision = RiskControlDecision(action, True, False, force_reduce_fraction=.5 if action is BreakerAction.REDUCE else None,
                                   force_liquidate=action in {BreakerAction.LIQUIDATE,BreakerAction.LOCKED})
    assert plan_risk_action(decision,"2026-06-30").remaining_fraction == fraction
