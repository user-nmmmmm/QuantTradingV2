"""PM1: authoritative epochs govern ratchets, including unseen flat and restart."""
from dataclasses import replace
from types import SimpleNamespace

import pandas as pd
import pytest

from backtest.protective_stops import ResidentStopSimulator
from core.broker import Broker
from core.domain import OrderStatus
from core.order_store import OrderStore
from core.portfolio import Portfolio
from core.protective_orders import (
    ProtectiveAction, ProtectiveOrder, ProtectiveOrderManager,
    authoritative_position_ids, parse_protective_position_reference,
    protective_position_reference,
)
from tests.test_revalidation_execution import broker, enter, Harness, NOW, SYMBOL
from tests.test_sr2_protective_orders import _StubBroker, _StubEngine


def order(ids=("old",), stop=95, status="open"):
    return ProtectiveOrder("old-stop", SYMBOL, "sell", 1, stop, status, position_ids=ids)


def evaluate(manager, ids=("old",), stop=95, orders=(), **kwargs):
    return manager.evaluate(symbol=SYMBOL, position_qty=1, desired_stop=stop,
                            position_ids=ids, open_protective_orders=orders, **kwargs)


def test_same_direction_complete_replacement_without_flat_does_not_inherit_ratchet():
    manager = ProtectiveOrderManager()
    evaluate(manager)
    new = evaluate(manager, ids=("new",), stop=70)
    assert new.intents[0].stop_price == 70
    assert new.intents[0].position_ids == ("new",)


def test_old_venue_stop_is_canceled_before_new_epoch_level_can_be_placed():
    manager = ProtectiveOrderManager()
    evaluate(manager, orders=[order()])
    changed = evaluate(manager, ids=("new",), stop=70, orders=[order()])
    assert changed.intents[0].action is ProtectiveAction.REPLACE
    assert changed.intents[0].cancel_order_id == "old-stop"
    assert changed.intents[0].reason == "position_epoch_changed"
    assert changed.intents[0].stop_price == 70


def test_intersecting_position_set_retains_ratchet_through_partial_exit():
    manager = ProtectiveOrderManager()
    evaluate(manager, ids=("first", "shared"))
    plan = evaluate(manager, ids=("shared", "added"), stop=70)
    assert plan.intents[0].stop_price == 95
    # An entirely different set is a new lifecycle even at identical quantity.
    assert evaluate(manager, ids=("replacement",), stop=60).intents[0].stop_price == 60


def test_restart_uses_attributed_venue_stop_for_same_epoch_and_discards_other_epoch():
    same = evaluate(ProtectiveOrderManager(), stop=70, orders=[order()])
    assert same.effective_stop == 95
    assert same.intents == []
    fresh = evaluate(ProtectiveOrderManager(), ids=("new",), stop=70, orders=[order()])
    assert fresh.intents[0].stop_price == 70


def test_legacy_unattributed_protection_cannot_loosen_during_migration():
    legacy = order(ids=None)
    refusal = evaluate(ProtectiveOrderManager(), stop=70, orders=[legacy])
    assert refusal.requires_flatten
    assert refusal.intents[0].reason == "legacy_protective_position_unverified"
    tightened = evaluate(ProtectiveOrderManager(), stop=97, orders=[legacy])
    assert tightened.intents[0].reason == "legacy_position_identity_migration"
    assert tightened.intents[0].stop_price == 97
    # Legacy callers without identity keep their historical API semantics.
    compat = ProtectiveOrderManager().evaluate(symbol=SYMBOL, position_qty=1,
        desired_stop=70, open_protective_orders=[legacy])
    assert compat.effective_stop == 95


def test_fully_reserved_inventory_is_not_flat_and_does_not_retire_ratchet():
    manager = ProtectiveOrderManager()
    evaluate(manager)
    reserved = manager.evaluate(symbol=SYMBOL, position_qty=0, total_position_qty=1,
                               position_ids=("old",), desired_stop=70,
                               open_protective_orders=[order()])
    assert reserved.intents[0].reason == "exit_inventory_reserved"
    assert SYMBOL in manager.tracked_symbols
    assert evaluate(manager, stop=70).intents[0].stop_price == 95


@pytest.mark.parametrize("status", ["unknown", "cancel_pending", "submitting"])
def test_unknown_old_epoch_order_never_generates_new_stop(status):
    plan = evaluate(ProtectiveOrderManager(), ids=("new",), stop=70, orders=[order(status=status)])
    assert plan.requires_flatten
    assert all(intent.action not in {ProtectiveAction.PLACE, ProtectiveAction.REPLACE}
               for intent in plan.intents)


def test_position_reference_roundtrip_and_missing_facts_fail_explicitly():
    assert parse_protective_position_reference(protective_position_reference(("b", "a", "a"))) == ("a", "b")
    assert parse_protective_position_reference("legacy-reference") is None
    with pytest.raises(ValueError):
        parse_protective_position_reference("protective-position-v1:invalid")
    with pytest.raises(ValueError):
        evaluate(ProtectiveOrderManager(), ids=())
    portfolio = Portfolio(10000)
    portfolio.positions[SYMBOL] = {"qty": 1, "avg_price": 100}
    with pytest.raises(ValueError, match="ledger_mismatch"):
        authoritative_position_ids(portfolio, SYMBOL)


def test_live_order_intent_persists_identity_and_restart_after_cancel_keeps_old_level(broker):
    enter(broker)
    engine = Harness(broker)
    engine.strategies["TrendBreakout"].context[SYMBOL]["stop_loss"] = 95
    engine._reconcile_protective_orders()
    row = [item for item in broker.order_store.list_all() if item["order_type"] == "stop"][0]
    expected_ids = authoritative_position_ids(broker.portfolio, SYMBOL)
    assert parse_protective_position_reference(row["intent"]["causation_id"]) == expected_ids
    # The database can be independently reopened; no manager memory is needed.
    store = OrderStore(broker.order_store.path)
    try:
        assert store.get(row["client_order_id"])["intent"]["causation_id"] == row["intent"]["causation_id"]
    finally:
        store.close()
    assert broker.cancel_order(row["client_order_id"]).status is OrderStatus.CANCELED
    restarted = Harness(broker)
    restarted.strategies["TrendBreakout"].context[SYMBOL]["stop_loss"] = 70
    restarted._reconcile_protective_orders()
    active = restarted._venue_protective_orders()
    assert len(active) == 1
    assert active[0].stop_price == 95
    assert active[0].position_ids == expected_ids


class EpochStubBroker(_StubBroker):
    def submit_order(self, *args, **kwargs):
        result = super().submit_order(*args, **kwargs)
        if kwargs.get("order_type") == "stop":
            self.order_store._records[-1]["intent"]["causation_id"] = kwargs["causation_id"]
        return result


def make_portfolio():
    value = Portfolio(10000)
    value.update_position(SYMBOL, 1, 100, stop_price=95, order_id="seed", time=NOW)
    return value


def replace_whole_position(portfolio):
    portfolio.update_position(SYMBOL, -1, 100, order_id="close", time=NOW)
    portfolio.update_position(SYMBOL, 1, 100, stop_price=70, order_id="reopen", time=NOW)


def test_live_same_side_unseen_flat_replacement_cancels_then_rearms_new_level():
    broker = EpochStubBroker(make_portfolio(), [])
    strategy = SimpleNamespace(context={SYMBOL: {"effective_stop": 95}})
    engine = _StubEngine(broker, {"TrendBreakout": strategy})
    engine._reconcile_protective_orders()
    old_ids = authoritative_position_ids(broker.portfolio, SYMBOL)
    replace_whole_position(broker.portfolio)
    strategy.context[SYMBOL]["effective_stop"] = 70
    engine._reconcile_protective_orders()
    assert len(broker.cancelled) == 1
    assert broker.submitted[-1][3]["trigger_price"] == 70
    new_ids = parse_protective_position_reference(broker.submitted[-1][3]["causation_id"])
    assert not set(old_ids).intersection(new_ids)


def test_live_cancel_timeout_never_places_new_epoch_stop():
    broker = EpochStubBroker(make_portfolio(), [])
    strategy = SimpleNamespace(context={SYMBOL: {"effective_stop": 95}})
    engine = _StubEngine(broker, {"TrendBreakout": strategy})
    engine._reconcile_protective_orders()
    replace_whole_position(broker.portfolio)
    strategy.context[SYMBOL]["effective_stop"] = 70
    broker.cancel_order = lambda _: SimpleNamespace(status=OrderStatus.UNKNOWN)
    engine._reconcile_protective_orders()
    assert len(broker.submitted) == 1
    assert engine._operational_state == "DEGRADED"


def test_live_rechecks_epoch_after_authoritative_cancel_before_replacement():
    broker = EpochStubBroker(make_portfolio(), [])
    strategy = SimpleNamespace(context={SYMBOL: {"effective_stop": 95}})
    engine = _StubEngine(broker, {"TrendBreakout": strategy})
    engine._reconcile_protective_orders()
    strategy.context[SYMBOL]["effective_stop"] = 97
    original_cancel = broker.cancel_order
    def cancel_and_external_replace(order_id):
        result = original_cancel(order_id)
        replace_whole_position(broker.portfolio)
        return result
    broker.cancel_order = cancel_and_external_replace
    engine._reconcile_protective_orders()
    assert len(broker.submitted) == 1
    assert engine._operational_state == "DEGRADED"
    assert engine.alerts[-1][1] == "protective_position_changed_before_submit"


def test_live_missing_lot_facts_degrades_without_guessing_an_epoch():
    portfolio = Portfolio(10000)
    portfolio.positions[SYMBOL] = {"qty": 1, "avg_price": 100}
    broker = EpochStubBroker(portfolio, [])
    engine = _StubEngine(broker, {})
    engine._reconcile_protective_orders()
    assert broker.submitted == []
    assert engine._operational_state == "DEGRADED"
    assert engine.alerts[-1][1] == "protective_position_unverifiable"


def test_real_backtest_same_side_epoch_replacement_has_same_shared_target():
    broker = Broker(make_portfolio(), commission_rate=0)
    strategies = {"TrendBreakout": SimpleNamespace(context={SYMBOL: {"effective_stop": 95}})}
    simulator = ResidentStopSimulator(broker, strategies)
    bar = pd.Series(dict(open=100, high=101, low=99, close=100, volume=100), name=pd.Timestamp(NOW))
    simulator._sync({SYMBOL: bar}, timestamp=NOW, bar_index=0)
    old_order = simulator._resident_orders()[0]
    replace_whole_position(broker.portfolio)
    strategies["TrendBreakout"].context[SYMBOL]["effective_stop"] = 70
    simulator._sync({SYMBOL: bar}, timestamp=NOW, bar_index=1)
    current = simulator._resident_orders()
    assert len(current) == 1
    assert current[0].stop_price == 70
    assert not set(old_order.position_ids).intersection(current[0].position_ids)
    assert current[0].position_ids == authoritative_position_ids(broker.portfolio, SYMBOL)
