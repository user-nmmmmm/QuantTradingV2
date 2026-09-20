"""Unknown venue holdings follow a durable exit policy using the real broker."""
from unittest.mock import patch

import pytest

from core.live_broker.safe import SafeLiveBroker
from core.order_store import OrderStore
from core.portfolio import Portfolio
from core.state_store_v2 import StateStore
from tests.test_revalidation_execution import Harness, NOW, SYMBOL, broker, enter
from tests.test_live_risk_action_lifecycle import finish_pending


def external_engine(broker, path):
    engine = Harness(broker)
    engine.state_store = StateStore(str(path))
    engine._ensure_state_store = lambda: engine.state_store
    broker.exchange.fetch_ticker = lambda symbol: {"last": 100, "timestamp": NOW.timestamp() * 1000}
    broker._clock = lambda: NOW
    engine.protective_orders_enabled = False
    return engine


def test_unknown_position_is_named_flattened_and_never_given_fictional_owner(broker, tmp_path):
    broker.exchange.qty = 1.0
    assert broker.sync()
    assert broker.unowned_positions[SYMBOL]["owner"] is None
    assert broker.unowned_positions[SYMBOL]["entry_time"] is None
    opening = broker.submit_order(SYMBOL, "buy", 1, reference_price=100, stop_loss=90, sequence=40)
    assert not opening.accepted
    engine = external_engine(broker, tmp_path / "state.db")
    assert engine._reconcile_external_positions()
    action = engine.state_store.get("external_position_actions")[SYMBOL]
    assert action["status"] == "completed" and action["completion"] == "flat"
    assert action["target_qty"] == 0 and action["owner"] is None
    assert broker.portfolio.get_position(SYMBOL)["qty"] == 0
    assert not broker.close_events  # no invented alpha return or entry cost
    exits = [row for row in broker.order_store.list_all() if row["side"] == "sell"]
    assert len(exits) == 1
    assert exits[0]["intent"]["exit_reason"] == "ExternalPositionExit"
    # Spot venues have no native reduceOnly flag; broker inventory validation
    # enforces the same zero-target bound before submission.
    assert exits[0]["requested_qty"] == 1
    assert not engine._reconcile_external_positions()
    engine.state_store.close()


def test_partial_external_exit_survives_restart_without_duplicate_submission(broker, tmp_path):
    broker.exchange.qty = 1.0
    broker.exchange.exit_fraction = 0.5
    broker.sync()
    path = tmp_path / "state.db"
    engine = external_engine(broker, path)
    assert engine._reconcile_external_positions()
    pending = broker.order_store.list_non_terminal()[0]
    action = engine.state_store.get("external_position_actions")[SYMBOL]
    assert action["status"] == "pending" and action["remaining_qty"] == 0.5
    count = len(broker.exchange.requests)
    engine.state_store.close()
    engine = external_engine(broker, path)
    assert engine._reconcile_external_positions()
    assert len(broker.exchange.requests) == count
    assert engine.state_store.get("external_position_actions")[SYMBOL]["action_id"] == action["action_id"]
    finish_pending(broker, pending)
    assert engine._reconcile_external_positions()
    assert engine.state_store.get("external_position_actions")[SYMBOL]["status"] == "completed"
    assert len(broker.exchange.requests) == count
    engine.state_store.close()


def test_unknown_external_exit_failure_remains_pending_and_blocks_risk(broker, tmp_path):
    broker.exchange.qty = 1.0
    broker.sync()
    engine = external_engine(broker, tmp_path / "state.db")
    broker.retry_max_attempts = 1
    with patch.object(broker.exchange, "create_order", side_effect=TimeoutError("unconfirmed")):
        assert engine._reconcile_external_positions()
    assert engine.state_store.get("external_position_actions")[SYMBOL]["status"] == "pending"
    assert engine._operational_state == "DEGRADED"
    assert broker.has_unresolved_unknown()
    count = len(broker.order_store.list_all())
    assert engine._reconcile_external_positions()
    assert len(broker.order_store.list_all()) == count
    engine.state_store.close()


def test_owned_restart_inventory_keeps_original_strategy_and_is_not_flattened(broker, tmp_path):
    enter(broker)
    broker.sync()
    assert broker.unowned_positions == {}
    lot = broker.portfolio.open_lots(SYMBOL)[0]
    path = broker.order_store.path
    broker.order_store.close()
    with patch("core.live_broker.ccxt.binance", return_value=broker.exchange):
        restarted = SafeLiveBroker(Portfolio(), broker.safety_guard,
                                  order_store=OrderStore(path), require_market_metadata=True)
    restarted.sync()
    engine = external_engine(restarted, tmp_path / "state.db")
    assert not engine._reconcile_external_positions()
    restored = restarted.portfolio.open_lots(SYMBOL)[0]
    assert (restored.strategy_id, restored.position_id, restored.entry_time) == (lot.strategy_id, lot.position_id, lot.entry_time)
    assert restarted.portfolio.get_position(SYMBOL)["qty"] == 1
    engine.state_store.close()
    restarted.close()


@pytest.mark.parametrize("boundary", [1, 2])
def test_crash_after_external_checkpoint_write_cannot_duplicate_exit(broker, tmp_path, boundary):
    broker.exchange.qty = 1
    broker.sync()
    path = tmp_path / "state.db"
    engine = external_engine(broker, path)
    original = engine.state_store.set
    writes = 0
    def crash_after_write(key, value):
        nonlocal writes
        original(key, value)
        writes += 1
        if writes == boundary:
            raise RuntimeError("simulated crash")
    with patch.object(engine.state_store, "set", side_effect=crash_after_write):
        with pytest.raises(RuntimeError, match="simulated crash"):
            engine._reconcile_external_positions()
    engine.state_store.close()
    engine = external_engine(broker, path)
    engine._reconcile_external_positions()
    exits = [row for row in broker.order_store.list_all() if row["side"] == "sell"]
    assert len(exits) == 1
    assert broker.portfolio.get_position(SYMBOL)["qty"] == 0
    assert engine.state_store.get("external_position_actions")[SYMBOL]["status"] == "completed"
    engine.state_store.close()


def test_later_external_inventory_has_new_action_even_with_same_clock(broker, tmp_path):
    engine = external_engine(broker, tmp_path / "state.db")
    broker.exchange.qty = 1
    broker.sync()
    engine._reconcile_external_positions()
    first = engine.state_store.get("external_position_actions")[SYMBOL]["action_id"]
    broker.exchange.qty = 2
    broker.sync()
    engine._reconcile_external_positions()
    second = engine.state_store.get("external_position_actions")[SYMBOL]
    assert second["action_id"] != first
    assert second["previous_action_id"] == first and second["status"] == "completed"
    assert broker.portfolio.get_position(SYMBOL)["qty"] == 0
    engine.state_store.close()
