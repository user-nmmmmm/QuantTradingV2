"""SR2-5 protective-order lifecycle tests.

Covers docs/current_strategy_remediation_roadmap.md §4.4 and §13.2: the
protective order must exist only after the entry fills, must equal the net
position, must ratchet in one direction only, must never survive a flat
position, and must fail closed on any state the venue cannot confirm.
"""

from __future__ import annotations

import unittest

import pandas as pd
from datetime import datetime, timezone
from types import SimpleNamespace

from core.portfolio import Portfolio
from core.protective_orders import (
    ProtectiveAction,
    ProtectiveOrder,
    ProtectiveOrderManager,
    ProtectiveState,
)
from core.protective_stops import EntryRiskPolicy
from live_trading.tick_orchestrator import TickOrchestratorMixin


def _order(
    order_id: str = "P1", *, symbol: str = "BTC/USDT", side: str = "sell",
    qty: float = 1.0, stop_price: float = 90.0, status: str = "open",
    reduce_only: bool = True,
) -> ProtectiveOrder:
    return ProtectiveOrder(
        order_id=order_id, symbol=symbol, side=side, qty=qty,
        stop_price=stop_price, status=status, reduce_only=reduce_only,
    )


class TestProtectionCreation(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = ProtectiveOrderManager()

    def test_a_pending_entry_is_not_protected_yet(self):
        """Nothing may assume a stop exists before the entry actually fills."""
        plan = self.manager.evaluate(
            symbol="BTC/USDT", position_qty=0.0, desired_stop=90.0,
            entry_pending=True,
        )
        self.assertEqual(plan.state, ProtectiveState.PENDING_ENTRY)
        self.assertEqual(plan.intents, [])

    def test_a_filled_entry_places_a_reduce_only_stop_for_the_net_position(self):
        plan = self.manager.evaluate(
            symbol="BTC/USDT", position_qty=2.5, desired_stop=90.0,
        )
        self.assertEqual(plan.state, ProtectiveState.ARMED)
        self.assertEqual(len(plan.intents), 1)
        intent = plan.intents[0]
        self.assertEqual(intent.action, ProtectiveAction.PLACE)
        self.assertEqual(intent.side, "sell")
        self.assertAlmostEqual(intent.qty, 2.5)
        self.assertAlmostEqual(intent.stop_price, 90.0)

    def test_a_short_position_is_protected_with_a_cover(self):
        plan = self.manager.evaluate(
            symbol="BTC/USDT", position_qty=-2.0, desired_stop=110.0,
        )
        self.assertEqual(plan.intents[0].side, "cover")
        self.assertAlmostEqual(plan.intents[0].qty, 2.0)

    def test_a_partial_entry_fill_is_protected_for_what_filled(self):
        first = self.manager.evaluate(
            symbol="BTC/USDT", position_qty=1.0, desired_stop=90.0,
        )
        self.assertAlmostEqual(first.intents[0].qty, 1.0)
        # The rest of the entry fills on the next tick.
        second = self.manager.evaluate(
            symbol="BTC/USDT", position_qty=3.0, desired_stop=90.0,
            open_protective_orders=[_order(qty=1.0)],
        )
        self.assertEqual(second.intents[0].action, ProtectiveAction.REPLACE)
        self.assertEqual(second.intents[0].reason, "qty_mismatch")
        self.assertAlmostEqual(second.intents[0].qty, 3.0)

    def test_a_non_reduce_only_protective_order_is_replaced(self):
        plan = self.manager.evaluate(
            symbol="BTC/USDT", position_qty=1.0, desired_stop=90.0,
            open_protective_orders=[_order(reduce_only=False)],
        )
        self.assertEqual(plan.intents[0].reason, "not_reduce_only")


class TestRatchet(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = ProtectiveOrderManager()

    def test_a_rising_stop_is_cancel_replaced_upward(self):
        plan = self.manager.evaluate(
            symbol="BTC/USDT", position_qty=1.0, desired_stop=95.0,
            open_protective_orders=[_order(stop_price=90.0)],
        )
        self.assertEqual(plan.intents[0].action, ProtectiveAction.REPLACE)
        self.assertEqual(plan.intents[0].reason, "ratchet_up")
        self.assertAlmostEqual(plan.intents[0].stop_price, 95.0)
        self.assertAlmostEqual(plan.effective_stop, 95.0)

    def test_a_falling_desired_stop_is_ignored(self):
        """The one invariant a widening ATR must never break."""
        plan = self.manager.evaluate(
            symbol="BTC/USDT", position_qty=1.0, desired_stop=80.0,
            open_protective_orders=[_order(stop_price=90.0)],
        )
        self.assertEqual(plan.state, ProtectiveState.ARMED)
        self.assertEqual(plan.intents, [])
        self.assertAlmostEqual(plan.effective_stop, 90.0)

    def test_the_ratchet_survives_a_replace_in_flight(self):
        """A venue that has not shown the new level yet must not lose it."""
        self.manager.evaluate(
            symbol="BTC/USDT", position_qty=1.0, desired_stop=95.0,
            open_protective_orders=[_order(stop_price=90.0)],
        )
        # The cancel landed, the replacement has not appeared yet, and the
        # strategy momentarily proposes the older level.
        plan = self.manager.evaluate(
            symbol="BTC/USDT", position_qty=1.0, desired_stop=90.0,
            open_protective_orders=[],
        )
        self.assertAlmostEqual(plan.intents[0].stop_price, 95.0)

    def test_short_side_ratchets_downward_only(self):
        tightened = self.manager.evaluate(
            symbol="BTC/USDT", position_qty=-1.0, desired_stop=105.0,
            open_protective_orders=[_order(side="cover", stop_price=110.0)],
        )
        self.assertEqual(tightened.intents[0].reason, "ratchet_up")
        self.assertAlmostEqual(tightened.intents[0].stop_price, 105.0)
        loosened = self.manager.evaluate(
            symbol="BTC/USDT", position_qty=-1.0, desired_stop=120.0,
            open_protective_orders=[_order(side="cover", stop_price=105.0)],
        )
        self.assertEqual(loosened.intents, [])


class TestSingleAuthoritativeClose(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = ProtectiveOrderManager()

    def test_a_flat_position_cancels_every_remaining_protective_order(self):
        """After a stop fill / strategy exit / breaker exit, no stop survives."""
        plan = self.manager.evaluate(
            symbol="BTC/USDT", position_qty=0.0, desired_stop=90.0,
            open_protective_orders=[_order("P1"), _order("P2", stop_price=92.0)],
        )
        self.assertEqual(plan.state, ProtectiveState.FLAT)
        self.assertEqual(
            {intent.cancel_order_id for intent in plan.intents}, {"P1", "P2"}
        )
        self.assertTrue(all(
            intent.action is ProtectiveAction.CANCEL for intent in plan.intents
        ))

    def test_duplicate_protective_orders_are_reduced_to_the_tightest(self):
        plan = self.manager.evaluate(
            symbol="BTC/USDT", position_qty=1.0, desired_stop=90.0,
            open_protective_orders=[
                _order("P1", stop_price=88.0), _order("P2", stop_price=91.0),
            ],
        )
        cancels = [
            intent for intent in plan.intents
            if intent.action is ProtectiveAction.CANCEL
        ]
        self.assertEqual([intent.cancel_order_id for intent in cancels], ["P1"])
        self.assertAlmostEqual(plan.effective_stop, 91.0)

    def test_an_external_liquidation_leaves_no_armed_stop_behind(self):
        self.manager.evaluate(
            symbol="BTC/USDT", position_qty=1.0, desired_stop=90.0,
        )
        after = self.manager.evaluate(
            symbol="BTC/USDT", position_qty=0.0, desired_stop=90.0,
            open_protective_orders=[_order("P1")],
        )
        self.assertEqual(after.intents[0].action, ProtectiveAction.CANCEL)
        # Memory of the level is dropped with the position, so a later
        # position does not inherit a stale ratchet.
        fresh = self.manager.evaluate(
            symbol="BTC/USDT", position_qty=1.0, desired_stop=50.0,
        )
        self.assertAlmostEqual(fresh.intents[0].stop_price, 50.0)


class TestFailClosed(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = ProtectiveOrderManager()

    def test_an_unknown_protective_order_forces_a_flatten(self):
        plan = self.manager.evaluate(
            symbol="BTC/USDT", position_qty=1.0, desired_stop=90.0,
            open_protective_orders=[_order(status="unknown")],
        )
        self.assertEqual(plan.state, ProtectiveState.FAILED)
        self.assertTrue(plan.requires_flatten)
        self.assertEqual(plan.intents[0].reason, "protective_order_state_unknown")

    def test_a_pending_cancel_is_also_indeterminate(self):
        plan = self.manager.evaluate(
            symbol="BTC/USDT", position_qty=1.0, desired_stop=90.0,
            open_protective_orders=[_order(status="pending_cancel")],
        )
        self.assertTrue(plan.requires_flatten)

    def test_a_rejected_protective_order_is_treated_as_missing(self):
        plan = self.manager.evaluate(
            symbol="BTC/USDT", position_qty=1.0, desired_stop=90.0,
            open_protective_orders=[_order(status="rejected")],
        )
        self.assertEqual(plan.intents[0].action, ProtectiveAction.PLACE)

    def test_an_open_position_with_no_level_is_flattened_not_carried(self):
        plan = self.manager.evaluate(
            symbol="BTC/USDT", position_qty=1.0, desired_stop=None,
        )
        self.assertEqual(plan.state, ProtectiveState.FAILED)
        self.assertEqual(plan.intents[0].reason, "no_protective_level")


class TestRestartRecovery(unittest.TestCase):
    def test_missing_protection_is_recreated_and_orphans_cancelled(self):
        manager = ProtectiveOrderManager()
        plans = {
            plan.symbol: plan
            for plan in manager.reconcile_after_restart(
                positions={"BTC/USDT": 1.0, "ETH/USDT": 0.0},
                desired_stops={"BTC/USDT": 90.0, "ETH/USDT": 1800.0},
                venue_orders=[
                    # Orphan: ETH is flat but a stop is still resting.
                    _order("ORPHAN", symbol="ETH/USDT", stop_price=1800.0),
                ],
            )
        }
        self.assertEqual(plans["BTC/USDT"].intents[0].action, ProtectiveAction.PLACE)
        self.assertEqual(plans["ETH/USDT"].intents[0].action, ProtectiveAction.CANCEL)
        self.assertEqual(plans["ETH/USDT"].intents[0].cancel_order_id, "ORPHAN")

    def test_recovery_uses_venue_state_not_remembered_state(self):
        manager = ProtectiveOrderManager()
        manager.evaluate(symbol="BTC/USDT", position_qty=1.0, desired_stop=95.0)
        # The venue says the stop is actually still at 90 after the restart:
        # the venue is authoritative for what exists, and the ratchet keeps
        # the tighter of the two.
        plan = manager.reconcile_after_restart(
            positions={"BTC/USDT": 1.0},
            desired_stops={"BTC/USDT": 90.0},
            venue_orders=[_order("P1", stop_price=90.0)],
        )[0]
        self.assertEqual(plan.intents[0].action, ProtectiveAction.REPLACE)
        self.assertAlmostEqual(plan.intents[0].stop_price, 95.0)

    def test_an_in_sync_book_produces_no_actions(self):
        manager = ProtectiveOrderManager()
        plans = manager.reconcile_after_restart(
            positions={"BTC/USDT": 1.0},
            desired_stops={"BTC/USDT": 90.0},
            venue_orders=[_order("P1", qty=1.0, stop_price=90.0)],
        )
        self.assertEqual(plans[0].state, ProtectiveState.ARMED)
        self.assertEqual(plans[0].intents, [])


class TestAudit(unittest.TestCase):
    def test_every_evaluation_produces_an_audit_row(self):
        manager = ProtectiveOrderManager()
        manager.evaluate(symbol="BTC/USDT", position_qty=1.0, desired_stop=90.0)
        manager.evaluate(
            symbol="BTC/USDT", position_qty=1.0, desired_stop=90.0,
            open_protective_orders=[_order(qty=1.0)],
        )
        self.assertEqual(len(manager.audit), 2)
        self.assertEqual(manager.audit[0]["action"], "place")
        self.assertEqual(manager.audit[1]["action"], "none")
        for row in manager.audit:
            self.assertIn("state", row)
            self.assertIn("effective_stop", row)


class _StubOrderStore:
    def __init__(self, records):
        self._records = records

    def list_non_terminal(self):
        return list(self._records)

    def list_with_fills(self):
        return [
            record for record in self._records
            if float(record.get("filled_qty") or 0.0) > 0
        ]


class _DictStateStore:
    def __init__(self):
        self.values = {}

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value


class _StubBroker:
    def __init__(self, portfolio, records):
        self.portfolio = portfolio
        self.order_store = _StubOrderStore(records)
        self.submitted = []
        self.cancelled = []

    def submit_order(self, symbol, side, qty, **kwargs):
        self.submitted.append((symbol, side, qty, kwargs))
        return SimpleNamespace(accepted=True)

    def cancel_order(self, client_order_id):
        self.cancelled.append(client_order_id)
        return SimpleNamespace(accepted=True)


class _StubEngine(TickOrchestratorMixin):
    """Just enough of the live engine to exercise the SR2-5 wiring."""

    def __init__(self, broker, strategies):
        self.broker = broker
        self.strategies = strategies
        self.protective_orders_enabled = True
        self._protective_order_manager = None
        self._operational_state = "RUNNING"
        self.alerts = []
        self.state_store = _DictStateStore()

    def _now(self):
        return datetime(2021, 5, 19, tzinfo=timezone.utc)

    def _alert(self, level, event, context):
        self.alerts.append((level, event, context))

    def _ensure_state_store(self):
        return self.state_store


class TestLiveWiring(unittest.TestCase):
    def _portfolio(self, qty: float) -> Portfolio:
        portfolio = Portfolio(initial_capital=100_000.0)
        if qty:
            portfolio.update_position(
                "BTC/USDT", qty_delta=qty, price=100.0,
                time=pd.Timestamp("2021-05-18T00:00:00Z"),
                strategy_id="TrendBreakout", order_id="E1", stop_price=90.0,
            )
        return portfolio

    def _strategy(self, stop):
        return SimpleNamespace(context={"BTC/USDT": {"effective_stop": stop}})

    def test_an_open_position_gets_a_reduce_only_stop_at_the_venue(self):
        broker = _StubBroker(self._portfolio(1.0), [])
        engine = _StubEngine(broker, {"TrendBreakout": self._strategy(92.0)})
        engine._reconcile_protective_orders()
        self.assertEqual(len(broker.submitted), 1)
        symbol, side, qty, kwargs = broker.submitted[0]
        self.assertEqual((symbol, side), ("BTC/USDT", "sell"))
        self.assertAlmostEqual(qty, 1.0)
        self.assertEqual(kwargs["order_type"], "stop")
        self.assertTrue(kwargs["reduce_only"])
        self.assertAlmostEqual(kwargs["price"], 92.0)

    def test_a_ratchet_cancels_then_replaces(self):
        records = [{
            "client_order_id": "P1", "symbol": "BTC/USDT", "side": "sell",
            "order_type": "stop", "price": 90.0, "requested_qty": 1.0,
            "remaining_qty": 1.0, "status": "open", "intent": {"reduce_only": True},
        }]
        broker = _StubBroker(self._portfolio(1.0), records)
        engine = _StubEngine(broker, {"TrendBreakout": self._strategy(95.0)})
        engine._reconcile_protective_orders()
        self.assertEqual(broker.cancelled, ["P1"])
        self.assertAlmostEqual(broker.submitted[0][3]["price"], 95.0)

    def test_an_orphan_stop_on_a_flat_symbol_is_cancelled(self):
        records = [{
            "client_order_id": "ORPHAN", "symbol": "ETH/USDT", "side": "sell",
            "order_type": "stop", "price": 1800.0, "requested_qty": 1.0,
            "remaining_qty": 1.0, "status": "open", "intent": {"reduce_only": True},
        }]
        broker = _StubBroker(self._portfolio(0.0), records)
        engine = _StubEngine(broker, {})
        engine._reconcile_protective_orders()
        self.assertEqual(broker.cancelled, ["ORPHAN"])
        self.assertEqual(broker.submitted, [])

    def test_an_unknown_stop_flattens_and_pages(self):
        records = [{
            "client_order_id": "P1", "symbol": "BTC/USDT", "side": "sell",
            "order_type": "stop", "price": 90.0, "requested_qty": 1.0,
            "remaining_qty": 1.0, "status": "unknown", "intent": {"reduce_only": True},
        }]
        broker = _StubBroker(self._portfolio(1.0), records)
        engine = _StubEngine(broker, {"TrendBreakout": self._strategy(90.0)})
        engine._reconcile_protective_orders()
        self.assertEqual(engine._operational_state, "DEGRADED")
        self.assertEqual(
            [event for _, event, _ in engine.alerts], ["position_unprotected"]
        )
        self.assertEqual(broker.submitted[0][3]["exit_reason"], "unprotected_flatten")

    def test_disabling_the_feature_makes_the_tick_a_no_op(self):
        broker = _StubBroker(self._portfolio(1.0), [])
        engine = _StubEngine(broker, {"TrendBreakout": self._strategy(92.0)})
        engine.protective_orders_enabled = False
        engine._reconcile_protective_orders()
        self.assertEqual(broker.submitted, [])


class TestLiveFillRiskRecheck(unittest.TestCase):
    def _record(self, filled_qty=300.0, average_fill_price=100.0):
        return {
            "client_order_id": "ENTRY-1",
            "symbol": "BTC/USDT",
            "side": "buy",
            "filled_qty": filled_qty,
            "average_fill_price": average_fill_price,
            "intent": {
                "action": "buy",
                "strategy_id": "TrendBreakout",
                "reduce_only": False,
            },
        }

    def _engine(self, record):
        portfolio = Portfolio(initial_capital=100_000.0)
        portfolio.update_position(
            "BTC/USDT", qty_delta=float(record["filled_qty"]),
            price=float(record["average_fill_price"]),
            strategy_id="TrendBreakout", order_id="ENTRY-1", stop_price=90.0,
        )
        broker = _StubBroker(portfolio, [record])
        strategy = SimpleNamespace(
            context={"BTC/USDT": {"effective_stop": 90.0}},
            health_risk_multiplier=lambda: 1.0,
        )
        engine = _StubEngine(broker, {"TrendBreakout": strategy})
        engine.entry_risk_policy = EntryRiskPolicy(tolerance=0.0)
        engine.risk_manager = SimpleNamespace(risk_per_trade=0.02)
        engine._snapshot = SimpleNamespace(equity=100_000.0)
        engine._live_fill_risk_audit = []
        return engine

    def test_gap_fill_is_resized_and_audited_once(self):
        engine = self._engine(self._record())
        engine._recheck_live_entry_risk()
        self.assertEqual(len(engine.broker.submitted), 1)
        symbol, side, qty, kwargs = engine.broker.submitted[0]
        self.assertEqual((symbol, side), ("BTC/USDT", "sell"))
        self.assertAlmostEqual(qty, 100.0)
        self.assertEqual(kwargs["strategy_id"], "GapRiskResize")
        self.assertTrue(kwargs["reduce_only"])
        self.assertTrue(engine._live_fill_risk_audit[-1]["breached"])

        engine._recheck_live_entry_risk()
        self.assertEqual(len(engine.broker.submitted), 1)

    def test_later_partial_fill_only_requests_incremental_resize(self):
        record = self._record(filled_qty=100.0)
        engine = self._engine(record)
        engine._recheck_live_entry_risk()
        self.assertEqual(engine.broker.submitted, [])

        record["filled_qty"] = 300.0
        engine.broker.portfolio.positions["BTC/USDT"]["qty"] = 300.0
        engine._recheck_live_entry_risk()
        self.assertEqual(len(engine.broker.submitted), 1)
        self.assertAlmostEqual(engine.broker.submitted[0][2], 100.0)

    def test_checkpoint_survives_engine_restart(self):
        record = self._record()
        first = self._engine(record)
        first._recheck_live_entry_risk()
        second = self._engine(record)
        second.state_store = first.state_store
        second._recheck_live_entry_risk()
        self.assertEqual(second.broker.submitted, [])

    def test_rejected_resize_fails_closed_and_is_not_checkpointed(self):
        engine = self._engine(self._record())

        def reject(symbol, side, qty, **kwargs):
            engine.broker.submitted.append((symbol, side, qty, kwargs))
            return SimpleNamespace(accepted=False)

        engine.broker.submit_order = reject
        self.assertFalse(engine._recheck_live_entry_risk())
        self.assertEqual(engine._operational_state, "DEGRADED")
        self.assertEqual(engine.alerts[-1][1], "gap_risk_resize_failed")
        self.assertIsNone(engine.state_store.get("entry_risk_check:ENTRY-1"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
