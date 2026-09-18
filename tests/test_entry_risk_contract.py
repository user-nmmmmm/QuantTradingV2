"""Approved money risk survives allocation, fills, replay and restarts."""
from dataclasses import asdict, replace
from datetime import timedelta
from types import SimpleNamespace

import pandas as pd
import pytest

from backtest.engine import BacktestEngine
from backtest.execution_adapter import SimulatedExecutionAdapter
from core.allocation import EntryCandidate
from core.broker import Broker
from core.domain import OrderIntent, OrderStatus, RiskDecision, RiskReservation
from core.entry_risk import resolve_approved_risk
from core.order_store import OrderStore
from core.portfolio import Portfolio
from core.protective_stops import EntryRiskPolicy, evaluate_fill_risk
from core.risk import RiskManager
from core.risk.circuit_breaker import BreakerAction
from core.risk.portfolio_governor import CorrelationClusterPolicy, PortfolioRiskGovernor
from core.state import MarketState
from core.state_store_v2 import StateStore
from strategies.trend_breakout import TrendBreakoutStrategy
from tests.test_revalidation_execution import Harness, NOW, SYMBOL, broker


def bar(day, price, volume):
    return {SYMBOL: pd.Series(dict(open=price, high=price, low=price,
                                  close=price, volume=volume),
                              name=pd.Timestamp("2024-01-01") + pd.Timedelta(days=day))}


def recheck(sim, checkpoints, audit, day):
    BacktestEngine._recheck_entry_risk(
        None, portfolio=sim.portfolio, execution=SimulatedExecutionAdapter(sim),
        prices={SYMBOL: 125.0}, timestamp=bar(day, 125, 100)[SYMBOL].name,
        bar_index=day, policy=EntryRiskPolicy(), checked_entries=checkpoints, audit=audit,
    )


def test_actual_allocation_approval_survives_health_account_and_cluster_scaling():
    portfolio = Portfolio(10000)
    sim = Broker(portfolio, commission_rate=0)
    risk = RiskManager(risk_per_trade=.02, reduced_risk_multiplier=.5)
    risk.portfolio_breaker_action = BreakerAction.REDUCE
    strategy = TrendBreakoutStrategy()
    strategy.health_risk_multiplier = lambda: .25
    governor = PortfolioRiskGovernor(CorrelationClusterPolicy(max_same_session_entry_risk=.001))
    governor.begin_session(0)
    frame = pd.DataFrame({"close": [100.]}, index=[bar(0, 100, 1)[SYMBOL].name])
    candidate = EntryCandidate(SYMBOL, strategy, 0, frame, MarketState.TREND_UP,
                               {"action": "buy", "price": 100., "stop_loss": 90.}, 1.)
    order = strategy.submit_entry_candidate(candidate, portfolio=portfolio, broker=sim,
                                            risk_manager=risk, current_prices={SYMBOL: 100},
                                            risk_governor=governor)
    assert order.accepted
    assert order.qty == pytest.approx(1)
    assert order.intent.approved_risk_amount == pytest.approx(10)
    approvals = [event.payload for event in sim.event_pipeline.events
                 if isinstance(event.payload, (RiskDecision, RiskReservation, OrderIntent))]
    assert len(approvals) == 3
    assert all(float(item.approved_risk_amount) == pytest.approx(10) for item in approvals)
    # The fill occurs after health and account state recover and equity grows.
    strategy.health_risk_multiplier = lambda: 1.
    risk.portfolio_breaker_action = BreakerAction.NORMAL
    portfolio.cash += 10000
    sim.process_orders(bar(1, 110, 100))
    audit = []
    recheck(sim, {}, audit, 1)
    assert audit[0]["risk_budget"] == 10
    assert audit[0]["actual_total_risk"] == 20
    assert audit[0]["breached"]


def test_reduced_budget_gap_cannot_be_replaced_by_base_risk():
    risk = RiskManager(risk_per_trade=.02, reduced_risk_multiplier=.5)
    risk.portfolio_breaker_action = BreakerAction.REDUCE
    qty = risk.calculate_position_size(10000, 100, 90)
    assessment = evaluate_fill_risk(
        symbol=SYMBOL, lot_id="gap", side="long", fill_price=105,
        protective_stop=90, filled_qty=qty, approved_risk_amount=qty * 10,
    )
    assert assessment.risk_budget == 100
    assert assessment.actual_total_risk == 150
    assert assessment.breached and assessment.action == "resize"
    assert assessment.resize_qty == pytest.approx(10 / 3)


def test_real_partial_fills_recheck_same_lot_and_retry_canceled_resize():
    sim = Broker(Portfolio(10000), commission_rate=0)
    order = sim.submit_order(SYMBOL, "buy", 20, price=100, stop_loss=90,
                             approved_risk_amount=200, timestamp=bar(0, 100, 1)[SYMBOL].name)
    checkpoints, audit = {}, []
    sim.process_orders(bar(1, 100, 5))
    lot_id = sim.portfolio.open_lots(SYMBOL)[0].lot_id
    recheck(sim, checkpoints, audit, 1)
    assert not audit[-1]["breached"]
    sim.process_orders(bar(2, 125, 15))
    assert sim.portfolio.open_lots(SYMBOL)[0].lot_id == lot_id
    recheck(sim, checkpoints, audit, 2)
    assert audit[-1]["actual_total_risk"] == 575
    assert audit[-1]["risk_budget"] == 200
    reductions = checkpoints[order.id]["resize_orders"]
    assert len(reductions) == 1
    assert reductions[0].qty == pytest.approx(20 - 200 / 28.75)
    recheck(sim, checkpoints, audit, 2)
    assert len(reductions) == 1
    sim.cancel_symbol_orders(SYMBOL)
    recheck(sim, checkpoints, audit, 2)
    assert len(reductions) == 2
    assert reductions[1].qty == pytest.approx(reductions[0].qty)


def test_breach_cancels_unfilled_entry_remainder():
    sim = Broker(Portfolio(10000), commission_rate=0)
    order = sim.submit_order(SYMBOL, "buy", 20, price=100, stop_loss=90,
                             approved_risk_amount=200, timestamp=bar(0, 100, 1)[SYMBOL].name)
    sim.process_orders(bar(1, 125, 10))
    assert order.status is OrderStatus.PARTIALLY_FILLED
    recheck(sim, {}, [], 1)
    assert order.status is OrderStatus.CANCELED
    sim.process_orders(bar(2, 125, 100))
    assert order.filled_qty == 10


def live_engine(broker, path):
    engine = Harness(broker)
    engine.entry_risk_policy = EntryRiskPolicy()
    engine._snapshot = SimpleNamespace(equity=10000, prices={SYMBOL: 100})
    engine.state_store = StateStore(str(path))
    engine._ensure_state_store = lambda: engine.state_store
    return engine


def test_live_approval_persists_and_cumulative_fill_rechecks_after_restart(broker, tmp_path):
    broker.exchange.entry_fraction = .5
    result = broker.submit_order(SYMBOL, "buy", 10, reference_price=100,
                                 stop_loss=90, approved_risk_amount=100,
                                 strategy_id="TrendBreakout")
    row = broker.order_store.get(result.client_order_id)
    assert row["intent"]["approved_risk_amount"] == 100
    engine = live_engine(broker, tmp_path / "state.db")
    assert engine._recheck_live_entry_risk()
    assert engine._live_fill_risk_audit[-1]["actual_total_risk"] == 50
    engine.state_store.close()
    # A later, more expensive fill is discovered from the durable venue fact.
    venue_order = broker.exchange.orders[row["exchange_order_id"]]
    venue_order["trades"].append({"id": "second-fill", "amount": 5., "price": 120.,
                                  "datetime": (NOW + timedelta(seconds=1)).isoformat(),
                                  "fee": {"cost": 0, "currency": "USDT"}})
    venue_order.update(status="closed", filled=10., remaining=0., average=110.)
    broker.exchange.qty += 5
    broker.exchange.cash -= 600
    broker.reconcile_order(result.client_order_id)
    restarted = live_engine(broker, tmp_path / "state.db")
    restarted._snapshot.equity = 50000  # No effect on the order's approval.
    assert restarted._recheck_live_entry_risk()
    assert restarted._live_fill_risk_audit[-1]["risk_budget"] == 100
    assert restarted._live_fill_risk_audit[-1]["actual_total_risk"] == 200
    assert broker.portfolio.get_position(SYMBOL)["qty"] == pytest.approx(5)
    count = len(broker.exchange.requests)
    assert restarted._recheck_live_entry_risk()
    assert len(broker.exchange.requests) == count
    restarted.state_store.close()
    # Reopening the actual order database preserves the budget independently.
    reopened = OrderStore(broker.order_store.path)
    assert reopened.get(result.client_order_id)["intent"]["approved_risk_amount"] == 100
    reopened.close()


def test_legacy_budget_requires_original_order_facts():
    amount, source = resolve_approved_risk({"requested_qty": 10, "reference_price": 100,
                                          "initial_stop": 90})
    assert (amount, source) == (100, "legacy_order_reference")
    with pytest.raises(ValueError):
        resolve_approved_risk({"requested_qty": 10, "initial_stop": 90})


@pytest.mark.parametrize("amount", [0, -1, float("nan"), float("inf"), True])
def test_invalid_explicit_budget_cannot_fall_back(amount):
    with pytest.raises(ValueError):
        resolve_approved_risk({"approved_risk_amount": amount, "requested_qty": 10,
                               "reference_price": 100, "initial_stop": 90})


def test_order_identity_and_serialization_preserve_approval():
    original = OrderIntent("binance", "a", SYMBOL, "1d", NOW.isoformat(), "TrendBreakout",
                           "buy", 0, 10., initial_stop=90, reference_price=100)
    approved = replace(original, approved_risk_amount=100)
    assert original.client_order_id == approved.client_order_id
    assert OrderIntent(**asdict(approved)) == approved


def test_venue_quantity_rounding_tightens_budget_and_restart_restores_reservation(broker):
    broker.exchange.entry_fraction = .5
    result = broker.submit_order(SYMBOL, "buy", 1.23456, reference_price=100,
                                 stop_loss=90, approved_risk_amount=12.3456,
                                 strategy_id="TrendBreakout")
    row = broker.order_store.get(result.client_order_id)
    assert row["requested_qty"] == pytest.approx(1.2345)
    assert row["intent"]["approved_risk_amount"] == pytest.approx(12.345)
    assert row["intent"]["reference_price"] == 100
    from core.events import TradingEventPipeline
    from core.risk.reservation import RiskReservationProjection
    broker.event_pipeline = TradingEventPipeline()
    broker.reservation_projection = RiskReservationProjection(broker.event_pipeline)
    broker._restore_reservations()
    assert broker.pending_open_notional({SYMBOL: 100})[SYMBOL] > 0
    approvals = [event.payload for event in broker.event_pipeline.events
                 if isinstance(event.payload, OrderIntent)]
    assert approvals[0].approved_risk_amount == pytest.approx(12.345)


def test_old_checkpoint_is_rechecked_against_frozen_budget():
    from tests.test_sr2_protective_orders import TestLiveFillRiskRecheck
    harness = TestLiveFillRiskRecheck()
    record = harness._record()
    engine = harness._engine(record)
    engine.state_store.set("entry_risk_check:ENTRY-1", {
        "checked_filled_qty": 300, "requested_resize_qty": 0,
        "last_assessment": {"risk_budget": 4000, "breached": False},
    })
    assert engine._recheck_live_entry_risk()
    assert len(engine.broker.submitted) == 1
    assert engine._live_fill_risk_audit[-1]["risk_budget"] == 2000
    assert engine.state_store.get("entry_risk_check:ENTRY-1")["budget_contract_version"] == 1


def test_live_missing_original_approval_blocks_new_risk_without_checkpoint():
    from tests.test_sr2_protective_orders import TestLiveFillRiskRecheck
    harness = TestLiveFillRiskRecheck()
    record = harness._record()
    record["intent"].pop("approved_risk_amount")
    engine = harness._engine(record)
    assert not engine._recheck_live_entry_risk()
    assert engine._operational_state == "DEGRADED"
    assert engine.alerts[-1][1] == "entry_risk_approval_missing"
    assert engine.state_store.get("entry_risk_check:ENTRY-1") is None


def test_closed_order_cannot_resize_a_later_position():
    from tests.test_sr2_protective_orders import TestLiveFillRiskRecheck
    harness = TestLiveFillRiskRecheck()
    engine = harness._engine(harness._record())
    engine.broker.portfolio.open_lots(SYMBOL)[0].order_id = "NEW-ENTRY"
    assert engine._recheck_live_entry_risk()
    assert not engine.broker.submitted


def test_authoritative_average_price_correction_is_rechecked_without_new_quantity():
    from tests.test_sr2_protective_orders import TestLiveFillRiskRecheck
    harness = TestLiveFillRiskRecheck()
    record = harness._record(filled_qty=300, average_fill_price=95)
    engine = harness._engine(record)
    assert engine._recheck_live_entry_risk()
    assert not engine.broker.submitted
    record["average_fill_price"] = 100
    assert engine._recheck_live_entry_risk()
    assert len(engine.broker.submitted) == 1


def test_short_partial_fill_uses_the_same_order_budget():
    sim = Broker(Portfolio(10000, account_mode="spot_margin"), commission_rate=0)
    sim.submit_order(SYMBOL, "short", 20, price=100, stop_loss=110,
                     approved_risk_amount=200, timestamp=bar(0, 100, 1)[SYMBOL].name)
    audit, checkpoints = [], {}
    sim.process_orders(bar(1, 100, 5))
    recheck(sim, checkpoints, audit, 1)
    sim.process_orders(bar(2, 75, 15))
    recheck(sim, checkpoints, audit, 2)
    assert audit[-1]["actual_total_risk"] == 575
    assert audit[-1]["risk_budget"] == 200
    assert sim.pending_orders[-1].side == "cover"
    assert sim.pending_orders[-1].qty == pytest.approx(20 - 200 / 28.75)


def test_live_late_fill_after_cancel_and_resize_only_reduces_increment_after_restart(broker, tmp_path):
    broker.exchange.entry_fraction = .8
    result = broker.submit_order(SYMBOL, "buy", 10, reference_price=95,
                                 stop_loss=90, approved_risk_amount=50,
                                 strategy_id="TrendBreakout")
    engine = live_engine(broker, tmp_path / "late-state.db")
    assert engine._recheck_live_entry_risk()
    assert broker.portfolio.get_position(SYMBOL)["qty"] == pytest.approx(5)
    record = broker.order_store.get(result.client_order_id)
    assert record["status"] == "canceled"
    engine.state_store.close()
    venue = broker.exchange.orders[record["exchange_order_id"]]
    venue["trades"].append({"id": "late-fill", "amount": 2., "price": 100.,
                             "datetime": (NOW + timedelta(seconds=1)).isoformat(),
                             "fee": {"cost": 0, "currency": "USDT"}})
    venue.update(status="closed", filled=10., remaining=0., average=100.)
    broker.exchange.qty += 2
    broker.exchange.cash -= 200
    # Deliver an authoritative late fact through the real persistence path.
    # Ordinary polling intentionally does not re-query terminal orders.
    broker._persist_exchange_payload(result.client_order_id, venue)
    assert broker.sync()
    assert broker.order_store.get(result.client_order_id)["filled_qty"] == 10
    assert broker.portfolio.get_position(SYMBOL)["qty"] == pytest.approx(7)
    restarted = live_engine(broker, tmp_path / "late-state.db")
    assert restarted._recheck_live_entry_risk()
    assert broker.portfolio.get_position(SYMBOL)["qty"] == pytest.approx(5)
    sell_requests = [req for req in broker.exchange.requests if req["side"] == "sell"]
    assert [req["amount"] for req in sell_requests] == pytest.approx([3, 2])
    assert restarted._recheck_live_entry_risk()
    assert len([req for req in broker.exchange.requests if req["side"] == "sell"]) == 2
    restarted.state_store.close()


def test_partial_resize_completion_before_restart_is_not_forgotten(broker, tmp_path):
    broker.exchange.entry_fraction = .8
    broker.exchange.exit_fraction = .5
    entry = broker.submit_order(SYMBOL, "buy", 10, reference_price=92,
                                stop_loss=90, approved_risk_amount=20,
                                strategy_id="TrendBreakout")
    engine = live_engine(broker, tmp_path / "partial-resize.db")
    assert not engine._recheck_live_entry_risk()
    assert broker.portfolio.get_position(SYMBOL)["qty"] == 5
    assert engine.state_store.get(f"entry_risk_check:{entry.client_order_id}") is None
    engine.state_store.close()
    pending = next(row for row in broker.order_store.list_non_terminal() if row["side"] == "sell")
    venue_exit = broker.exchange.orders[pending["exchange_order_id"]]
    venue_exit["trades"].append({"id": "complete-resize", "amount": 3., "price": 100.,
                                  "datetime": (NOW + timedelta(seconds=1)).isoformat(),
                                  "fee": {"cost": 0, "currency": "USDT"}})
    venue_exit.update(status="closed", filled=6., remaining=0., average=100.)
    broker.exchange.qty -= 3
    broker.exchange.cash += 300
    broker.reconcile_order(pending["client_order_id"])
    assert broker.portfolio.get_position(SYMBOL)["qty"] == 2
    restarted = live_engine(broker, tmp_path / "partial-resize.db")
    assert restarted._recheck_live_entry_risk()
    checkpoint = restarted.state_store.get(f"entry_risk_check:{entry.client_order_id}")
    assert checkpoint["requested_resize_qty"] == 6
    # Receipt of another opening fill must reduce only those two new units.
    record = broker.order_store.get(entry.client_order_id)
    venue_entry = broker.exchange.orders[record["exchange_order_id"]]
    venue_entry["trades"].append({"id": "late-after-resize", "amount": 2., "price": 100.,
                                   "datetime": (NOW + timedelta(seconds=2)).isoformat(),
                                   "fee": {"cost": 0, "currency": "USDT"}})
    venue_entry.update(status="closed", filled=10., remaining=0., average=100.)
    broker.exchange.qty += 2
    broker.exchange.cash -= 200
    broker._persist_exchange_payload(entry.client_order_id, venue_entry)
    assert broker.sync()
    broker.exchange.exit_fraction = 1.
    assert restarted._recheck_live_entry_risk()
    assert broker.portfolio.get_position(SYMBOL)["qty"] == 2
    assert [req["amount"] for req in broker.exchange.requests if req["side"] == "sell"] == [6, 2]
    restarted.state_store.close()
