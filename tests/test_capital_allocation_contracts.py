"""Shared capital invariants across planning, submission, fills and recovery."""
from dataclasses import replace

import pandas as pd
import pytest

from core.allocation import EntryCandidate, PortfolioSignalAllocator
from core.broker import Broker
from core.domain import OrderIntent, OrderStatus
from core.order_store import OrderStore
from core.portfolio import Portfolio
from core.position_management import CapitalAllocationPolicy, SmartCapitalPlanner
from core.risk import RiskManager
from core.risk.portfolio_governor import (
    CorrelationClusterPolicy, PortfolioRiskGovernor, open_risk_by_cluster,
)
from core.state import MarketState
from core.state_store_v2 import StateStore
from strategies.trend_breakout import TrendBreakoutStrategy


def account(symbols, *, margin=False, cluster_policy=None):
    portfolio = Portfolio(10000., account_mode="spot_margin" if margin else "spot",
                          initial_margin_rate=1 / 3 if margin else 1.)
    broker = Broker(portfolio, commission_rate=.001, commission_rate_maker=.001)
    manager = RiskManager(risk_per_trade=.02, max_pos_size_pct=.3,
                          drawdown_budget_policy={"enabled": True})
    strategy = TrendBreakoutStrategy()
    frame = pd.DataFrame({"close": [100.] * 30},
                         index=pd.date_range("2024-01-01", periods=30))
    prices = dict.fromkeys(symbols, 100.)
    bars = {symbol: pd.Series({"open": 100., "high": 100., "low": 100.,
                               "close": 100., "volume": 1000000.}, name=frame.index[-1])
            for symbol in symbols}
    manager.cluster_policy = cluster_policy
    manager.drawdown_budget.bind(broker, {strategy.name: strategy})
    manager.drawdown_budget.update(prices, bars, frame.index[-1])
    return portfolio, broker, manager, strategy, frame, prices


def entry(symbol, strategy, frame, *, score=1., stop=90., order_type="market", price=100.):
    return EntryCandidate(symbol, strategy, len(frame) - 1, frame, MarketState.TREND_UP,
                          {"action": "buy", "price": price, "stop_loss": stop,
                           "order_type": order_type}, score)


def plan(planner, candidates, portfolio, broker, manager, prices, governor=None):
    return planner.plan(candidates, portfolio=portfolio, broker=broker,
                        risk_manager=manager, current_prices=prices, risk_governor=governor)


def test_default_cluster_limit_covers_held_and_pending_risk_with_drawdown_overlay():
    policy = CorrelationClusterPolicy(max_correlated_stop_risk=.10,
                                      max_same_session_entry_risk=.06)
    held = [f"H{i}/USDT" for i in range(4)]
    new = ["A/USDT", "B/USDT", "C/USDT"]
    p, b, r, s, frame, prices = account(held + new, cluster_policy=policy, margin=True)
    for symbol in held:
        p.update_position(symbol, 20., 100., strategy_id=s.name, stop_price=90.,
                          approved_risk_amount=200., time=frame.index[0])
        prices[symbol] = 95.
    r.high_water_equity = 10000.
    r.drawdown_budget.update(prices, r.drawdown_budget.bars, frame.index[-1])
    allocator = PortfolioSignalAllocator(PortfolioRiskGovernor(policy))
    decisions = allocator.allocate(
        [entry(symbol, s, frame, score=3. - i) for i, symbol in enumerate(new)],
        portfolio=p, broker=b, risk_manager=r, current_prices=prices)
    pending = sum(b.reservation_projection.pending_stop_risk().values())
    filled = sum(open_risk_by_cluster(policy, p).values())
    assert filled == 800.
    assert pending == pytest.approx(160.)
    assert filled + pending <= p.get_equity(prices) * .10 + 1e-8
    assert [decision.accepted for decision in decisions] == [True, False, False]
    assert not r.drawdown_budget.snapshot().over_budget


def test_cluster_budget_keeps_old_session_orders_reserved():
    policy = CorrelationClusterPolicy(max_correlated_stop_risk=.025,
                                      max_same_session_entry_risk=.06)
    p, b, r, s, frame, prices = account(["A/USDT", "B/USDT"], cluster_policy=policy)
    allocator = PortfolioSignalAllocator(PortfolioRiskGovernor(policy))
    first = allocator.allocate([entry("A/USDT", s, frame)], portfolio=p, broker=b,
                               risk_manager=r, current_prices=prices)
    tomorrow = frame.set_axis(frame.index + pd.Timedelta(days=1))
    second = allocator.allocate([entry("B/USDT", s, tomorrow)], portfolio=p, broker=b,
                                risk_manager=r, current_prices=prices)
    assert first[0].accepted and second[0].accepted
    assert b.reservation_projection.pending_stop_risk() == pytest.approx(
        {"A/USDT": 200., "B/USDT": 50.})


def test_partial_fill_transfers_cluster_risk_and_cancel_releases_only_remainder():
    policy = CorrelationClusterPolicy(max_correlated_stop_risk=.03)
    p, b, r, s, frame, prices = account(["A/USDT", "B/USDT"], cluster_policy=policy)
    governor = PortfolioRiskGovernor(policy)
    PortfolioSignalAllocator(governor).allocate([entry("A/USDT", s, frame)],
        portfolio=p, broker=b, risk_manager=r, current_prices=prices)

    def allowed():
        return governor.evaluate(symbol="B/USDT", planned_risk=200., equity=10000.,
            portfolio=p, pending_stop_risk=b.reservation_projection.pending_stop_risk()).allowed_risk

    assert allowed() == pytest.approx(100.)
    bar = r.drawdown_budget.bars["A/USDT"].copy()
    bar.name += pd.Timedelta(days=1)
    bar["volume"] = 10.
    b.process_orders({"A/USDT": bar})
    assert p.get_position("A/USDT")["qty"] == pytest.approx(10.)
    assert allowed() == pytest.approx(100.)
    b.cancel_opening_orders(timestamp=bar.name)
    assert allowed() == pytest.approx(200.)


def test_pending_risk_is_charged_to_its_own_cluster():
    policy = CorrelationClusterPolicy(clusters={"BTC": "major", "ETH": "major"},
                                      max_correlated_stop_risk=.03)
    governor = PortfolioRiskGovernor(policy)
    result = governor.evaluate(symbol="ETH/USDT", planned_risk=300., equity=10000.,
        portfolio=Portfolio(10000.), pending_stop_risk={"BTC/USDT": 50., "SOL/USDT": 300.})
    assert result.allowed_risk == pytest.approx(250.)


@pytest.mark.parametrize("amount", [float("nan"), float("inf"), -1., True, None, "invalid"])
def test_invalid_pending_cluster_risk_blocks_new_entries(amount):
    governor = PortfolioRiskGovernor(CorrelationClusterPolicy(max_correlated_stop_risk=.03))
    result = governor.evaluate(symbol="A/USDT", planned_risk=10., equity=10000.,
        portfolio=Portfolio(10000.), pending_stop_risk={"B/USDT": amount})
    assert not result.allowed and result.allowed_risk == 0.


@pytest.mark.parametrize("smart", [False, True])
@pytest.mark.parametrize("mark", [80., 100., 100.2, 120.])
def test_long_submission_uses_current_mark_for_risk_cash_and_reservation(smart, mark):
    p, b, r, s, frame, prices = account(["A/USDT"])
    prices["A/USDT"] = mark
    r.drawdown_budget.update(prices, r.drawdown_budget.bars, frame.index[-1])
    allocator = PortfolioSignalAllocator(capital_policy=CapitalAllocationPolicy(enabled=smart))
    result = allocator.allocate([entry("A/USDT", s, frame, stop=99.)],
        portfolio=p, broker=b, risk_manager=r, current_prices=prices)
    assert result[0].accepted
    order = b.pending_orders[0]
    assert order.intent.reference_price == max(mark, 100.)
    assert order.intent.price == 100.
    assert order.intent.approved_risk_amount <= 200. + 1e-8
    cap = .2 if smart else .3
    assert order.qty * mark <= p.cash * cap + 1e-8
    assert b.reservation_projection.pending_stop_risk()["A/USDT"] == pytest.approx(
        order.qty * (max(mark, 100.) - 99.))


@pytest.mark.parametrize("order_type,order_price", [("market", 100.), ("limit", 95.), ("stop", 125.)])
def test_latest_risk_reference_preserves_limit_and_stop_execution_prices(order_type, order_price):
    p, b, r, s, frame, prices = account(["A/USDT"])
    prices["A/USDT"] = 120.
    r.drawdown_budget.update(prices, r.drawdown_budget.bars, frame.index[-1])
    candidate = entry("A/USDT", s, frame, order_type=order_type, price=order_price)
    allocator = PortfolioSignalAllocator(capital_policy=CapitalAllocationPolicy(enabled=True))
    assert allocator.allocate([candidate], portfolio=p, broker=b, risk_manager=r,
                              current_prices=prices)[0].accepted
    order = b.pending_orders[0]
    assert order.price == order_price
    assert order.intent.reference_price == max(120., order_price)
    assert order.intent.approved_risk_amount <= 200. + 1e-8


def test_smart_planned_notional_covers_available_current_market_quote():
    p, b, r, s, frame, prices = account(["A/USDT"])
    prices["A/USDT"] = 120.
    planner = SmartCapitalPlanner(CapitalAllocationPolicy(enabled=True))
    decisions, _ = plan(planner, [entry("A/USDT", s, frame)], p, b, r, prices)
    decision = decisions["A/USDT"]
    assert decision["approved_qty"] > 0
    assert decision["reference_price"] == 120.
    assert decision["approved_notional"] == pytest.approx(decision["approved_qty"] * 120.)
    assert decision["approved_notional"] <= 2000.


def test_long_reference_change_preserves_short_limit_order_sizing():
    p, b, r, s, frame, prices = account(["A/USDT"], margin=True)
    candidate = replace(entry("A/USDT", s, frame, price=95., stop=110., order_type="limit"),
                        signal={"action": "short", "price": 95., "stop_loss": 110., "order_type": "limit"})
    assert PortfolioSignalAllocator().allocate([candidate], portfolio=p, broker=b,
        risk_manager=r, current_prices=prices)[0].accepted
    order = b.pending_orders[0]
    assert order.qty == 20.
    assert order.intent.price == order.intent.reference_price == 95.
    assert order.intent.approved_risk_amount == 200.


@pytest.mark.parametrize("mark", [0., -1., float("nan"), float("inf"), True])
def test_invalid_current_long_mark_cannot_submit_an_order(mark):
    p, b, r, s, frame, prices = account(["A/USDT"])
    prices["A/USDT"] = mark
    result = PortfolioSignalAllocator().allocate([entry("A/USDT", s, frame)],
        portfolio=p, broker=b, risk_manager=r, current_prices=prices)
    assert not result[0].accepted
    assert not b.pending_orders


def test_default_spot_batch_reserves_cash_including_pending_execution_costs():
    symbols = ["A/USDT", "B/USDT", "C/USDT", "D/USDT"]
    p, b, r, s, frame, prices = account(symbols)
    results = PortfolioSignalAllocator().allocate(
        [entry(symbol, s, frame, score=4. - i, stop=99.) for i, symbol in enumerate(symbols)],
        portfolio=p, broker=b, risk_manager=r, current_prices=prices)
    assert all(result.accepted for result in results)
    pending_cash = b.reservation_projection.pending_cash(prices, cost=r.drawdown_budget.cost)
    assert pending_cash <= p.cash + 1e-8
    assert pending_cash == pytest.approx(p.cash)


def test_atomic_intent_approval_uses_pending_cash_with_fees():
    p, b, r, s, frame, prices = account(["A/USDT", "B/USDT"])
    r.max_pos_size_pct = 1.
    assert b.submit_order("A/USDT", "buy", 30., 100., stop_loss=90.,
                          approved_risk_amount=300., timestamp=frame.index[-1]).accepted
    intent = OrderIntent("backtest", "backtest", "B/USDT", "1d", frame.index[-1].isoformat(),
                         s.name, "buy", 0, 69.94, price=100., reference_price=100.,
                         initial_stop=99., approved_risk_amount=69.94)
    decision, approved = r.approve_and_create_intent(p, intent, reference_price=100.,
        event_pipeline=b.event_pipeline, reservation_projection=b.reservation_projection,
        occurred_at=frame.index[-1].tz_localize("UTC").to_pydatetime(), current_prices=prices)
    assert not decision.approved and approved is None
    assert b.reservation_projection.pending_cash(prices, cost=r.drawdown_budget.cost) == 3003.


@pytest.mark.parametrize("blocked_count", [1, 2])
@pytest.mark.parametrize("cluster_cap", [.02, .0205])
def test_infeasible_or_dust_candidates_release_slots_for_ranked_backfill(blocked_count, cluster_cap):
    blocked = [f"A{i}/USDT" for i in range(blocked_count)]
    clusters = {"H": "full", **{f"A{i}": "full" for i in range(blocked_count)}, "B": "free"}
    policy = CorrelationClusterPolicy(clusters=clusters, max_correlated_stop_risk=cluster_cap)
    p, b, r, s, frame, prices = account(["H/USDT", "B/USDT", *blocked], cluster_policy=policy)
    p.update_position("H/USDT", 20., 100., stop_price=90., approved_risk_amount=200.,
                      strategy_id=s.name, time=frame.index[0])
    planner = SmartCapitalPlanner(CapitalAllocationPolicy(enabled=True, max_positions=2))
    candidates = [entry(symbol, s, frame, score=5. - i) for i, symbol in enumerate(blocked)]
    candidates.append(entry("B/USDT", s, frame))
    governor = PortfolioRiskGovernor(policy)
    decisions, summary = plan(planner, candidates, p, b, r, prices, governor)
    reversed_decisions, _ = plan(planner, candidates[::-1], p, b, r, prices, governor)
    assert decisions == reversed_decisions
    assert decisions["B/USDT"]["approved_notional"] == pytest.approx(2000.)
    assert all(decisions[symbol]["approved_qty"] == 0. for symbol in blocked)
    assert summary["approved_count"] + summary["occupied_positions"] == 2
    assert all(row["used"] <= row["budget"] + 1e-8 for row in summary["constraints"].values())


def test_backfill_cannot_bypass_pending_position_slots():
    p, b, r, s, frame, prices = account(["H/USDT", "P/USDT", "A/USDT", "B/USDT"])
    p.update_position("H/USDT", 20., 100., stop_price=90., approved_risk_amount=200.,
                      strategy_id=s.name, time=frame.index[0])
    assert b.submit_order("P/USDT", "buy", 10., 100., stop_loss=90.,
                          approved_risk_amount=100., timestamp=frame.index[-1]).accepted
    planner = SmartCapitalPlanner(CapitalAllocationPolicy(enabled=True, max_positions=2))
    decisions, summary = plan(planner, [entry(symbol, s, frame) for symbol in ("A/USDT", "B/USDT")],
                              p, b, r, prices)
    assert summary["occupied_positions"] == 2
    assert all(row["reason"] == "position_slots_exhausted" for row in decisions.values())


def test_backfill_terminates_when_the_whole_cash_budget_is_unavailable():
    p, b, r, s, frame, prices = account([f"A{i}/USDT" for i in range(5)])
    planner = SmartCapitalPlanner(CapitalAllocationPolicy(enabled=True, max_positions=1,
                                                         cash_reserve_pct=.999))
    decisions, summary = plan(planner, [entry(symbol, s, frame) for symbol in prices], p, b, r, prices)
    assert summary["approved_count"] == 0
    assert all(row["reason"] == "below_minimum_allocation" for row in decisions.values())
    assert all(row["used"] == 0. for row in summary["constraints"].values())


def stored_intent(session, **changes):
    return replace(OrderIntent("binance", "test", "A/USDT", "1d",
        (session + pd.Timedelta(days=1)).isoformat(), "TrendBreakout", "buy", 0, 20.,
        price=100., reference_price=100., initial_stop=90., approved_risk_amount=50.), **changes)


@pytest.mark.parametrize("status,attempted,filled,expected", [
    (OrderStatus.REJECTED, True, 0., 0.),
    (OrderStatus.EXPIRED_UNSUBMITTED, False, 0., 0.),
    (OrderStatus.SUBMITTING, False, 0., 50.),
    (OrderStatus.SUBMITTING, True, 0., 50.),
    (OrderStatus.CANCELED, False, 0., 0.),
    (OrderStatus.EXPIRED, False, 0., 0.),
    (OrderStatus.UNKNOWN, True, 0., 50.),
    (OrderStatus.UNKNOWN, False, 0., 50.),
    (OrderStatus.ACCEPTED, True, 0., 50.),
    (OrderStatus.PARTIALLY_FILLED, True, 5., 50.),
    (OrderStatus.FILLED, True, 20., 50.),
    (OrderStatus.CANCELED, True, 5., 50.),
])
def test_session_recovery_respects_order_facts_and_original_approved_risk(
        tmp_path, status, attempted, filled, expected):
    session = pd.Timestamp("2024-01-30")
    store = OrderStore(str(tmp_path / "orders.db"))
    state = StateStore(str(tmp_path / "state.db"))
    try:
        intent = stored_intent(session)
        store.create_intent(intent, intent.bar_time)
        store.update(intent.client_order_id, status=status.value, submission_attempted=attempted,
                     filled_qty=filled)
        governor = PortfolioRiskGovernor(CorrelationClusterPolicy(max_same_session_entry_risk=.06))
        governor.bind_state_store(state, store)
        governor.begin_session(session)
        assert governor.session_risk_used == expected
    finally:
        state.close()
        store.close()


def test_rejected_orders_cannot_exhaust_session_after_restart(tmp_path):
    session = pd.Timestamp("2024-01-30")
    store = OrderStore(str(tmp_path / "orders.db"))
    state = StateStore(str(tmp_path / "state.db"))
    try:
        for i in range(3):
            intent = stored_intent(session, sequence=i, approved_risk_amount=200.)
            store.create_intent(intent, intent.bar_time)
            store.transition(intent.client_order_id, OrderStatus.REJECTED, intent.bar_time,
                             remaining_qty=0.)
        governor = PortfolioRiskGovernor(CorrelationClusterPolicy(max_same_session_entry_risk=.06))
        governor.bind_state_store(state, store)
        governor.begin_session(session)
        assert governor.session_risk_used == 0.
        assert governor.evaluate(symbol="B/USDT", planned_risk=100., equity=10000.,
                                  portfolio=Portfolio(10000.)).allowed
        state.set(f"session_risk:{session}", 200.)
        restored = PortfolioRiskGovernor(governor.policy)
        restored.bind_state_store(state, store)
        restored.begin_session(session)
        assert restored.session_risk_used == 200.
    finally:
        state.close()
        store.close()
