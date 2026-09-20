"""SYS-13: causal volatility, capped targets, immutable slices and real fills."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

import pandas as pd
import pytest

from core.broker import Broker
from core.domain import OrderStatus
from core.exchange.metadata import MarketSpecification
from core.portfolio import Portfolio
from core.risk.portfolio_governor import CorrelationClusterPolicy
from core.state_store_v2 import StateStore
from core.target_position import (
    FrozenTarget, PositionIdentity, TrancheFact, VolatilityPolicy,
    constrain_target_weights, freeze_target, next_tranche, volatility_target_weight,
)


NOW = datetime(2026, 1, 21, tzinfo=timezone.utc)
SYMBOL = "BTC/USDT"
IDENTITY = PositionIdentity("sandbox", SYMBOL, "long", "position-one")
MARKET = MarketSpecification(SYMBOL, amount_step=Decimal("0.1"), min_amount=Decimal("0.1"),
                             min_notional=Decimal("10"))


def observations(scale=1):
    return [(NOW - timedelta(days=20-i), scale * (.01 if i % 2 else -.01)) for i in range(20)]


def target(**updates):
    arguments = dict(plan_id="frozen-candidate-1", identity=IDENTITY, target_weight=.10,
                     equity=10000, current_qty=0, reference_price=100, initial_stop=90,
                     approved_risk_amount=100, remaining_entry_notional=1000,
                     maximum_position_notional=2000, maximum_tranche_qty=3, market=MARKET)
    arguments.update(updates)
    return freeze_target(**arguments)


def tranche(plan=None, **updates):
    plan = plan or target()
    arguments = dict(identity=plan.identity, current_qty=plan.original_qty, current_price=100,
                     market=MARKET, facts_reconciled=True, allows_new_risk=True,
                     remaining_entry_notional=1000, remaining_entry_risk=100,
                     effective_stop=90)
    arguments.update(updates)
    return next_tranche(plan, **arguments)


def fact(decision, sequence=0, filled=None, status=OrderStatus.FILLED):
    return TrancheFact(decision.decision_id, sequence, decision.quantity,
                       decision.quantity if filled is None else Decimal(str(filled)), status)


def test_disabled_volatility_preserves_existing_weight_without_reading_bad_returns():
    result = volatility_target_weight(.37, [(NOW, float("nan"))], as_of=NOW)
    assert result.weight == .37
    assert result.status == "disabled"
    assert result.annual_volatility is None


def test_volatility_uses_only_completed_history_and_scales_down_when_risk_doubles():
    policy = VolatilityPolicy(enabled=True, maximum_multiplier=10)
    first = volatility_target_weight(.6, observations(), as_of=NOW, policy=policy)
    doubled = volatility_target_weight(.6, observations(2), as_of=NOW, policy=policy)
    future = volatility_target_weight(.6, observations() + [(NOW, 100), (NOW+timedelta(days=1), -1)],
                                      as_of=NOW, policy=policy)
    assert future == first
    assert doubled.weight == pytest.approx(first.weight / 2)
    assert first.multiplier * first.annual_volatility == pytest.approx(.15)
    assert doubled.multiplier * doubled.annual_volatility == pytest.approx(.15)


@pytest.mark.parametrize("kind,reason", [
    ("missing", "insufficient_history"), ("zero", "invalid_volatility"),
    ("nan", "invalid_return"), ("inf", "invalid_return"),
    ("impossible", "invalid_return"), ("divergent", "invalid_volatility"),
    ("duplicate", "duplicate_observation"), ("stale", "stale_history"),
    ("missing_period", "irregular_history"),
])
def test_missing_or_invalid_statistics_never_expand_exposure(kind, reason):
    values = observations()
    if kind == "missing":
        values = values[:2]
    elif kind == "zero":
        values = [(moment, 0) for moment, _ in values]
    elif kind in {"nan", "inf", "impossible", "divergent"}:
        value = {"nan": float("nan"), "inf": float("inf"), "impossible": -2, "divergent": 1000}[kind]
        values[-1] = (values[-1][0], value)
    elif kind == "duplicate":
        values.append(values[-1])
    elif kind == "missing_period":
        values[0] = (values[0][0]-timedelta(days=1), values[0][1])
    else:
        values = [(moment-timedelta(days=10), value) for moment, value in values]
    result = volatility_target_weight(.5, values, as_of=NOW, policy=VolatilityPolicy(enabled=True))
    assert (result.weight, result.multiplier, result.annual_volatility) == (0, 0, None)
    assert result.reason == reason


def test_rebalance_uses_predeclared_elapsed_interval():
    policy = VolatilityPolicy(enabled=True)
    assert policy.rebalance_due(NOW, None)
    assert not policy.rebalance_due(NOW, NOW-timedelta(hours=23))
    assert policy.rebalance_due(NOW, NOW-timedelta(days=1))
    with pytest.raises(ValueError):
        policy.rebalance_due(NOW, NOW+timedelta(days=1))


def test_hourly_and_daily_volatility_derive_consistent_annualization():
    daily = volatility_target_weight(.6, observations(), as_of=NOW,
        policy=VolatilityPolicy(enabled=True))
    hourly_policy = VolatilityPolicy(enabled=True, observation_interval_seconds=3600)
    hourly_returns = [(NOW-timedelta(hours=20-i), value) for i, (_, value) in enumerate(observations())]
    hourly = volatility_target_weight(.6, hourly_returns, as_of=NOW, policy=hourly_policy)
    assert hourly_policy.periods_per_year == 8760
    assert daily.annual_volatility == pytest.approx((20 * .01**2 / 19 * 365)**.5)
    assert hourly.annual_volatility == pytest.approx((20 * .01**2 / 19 * 8760)**.5)
    assert hourly.weight == pytest.approx(daily.weight / 24**.5)
    with pytest.raises(ValueError, match="contradicts"):
        VolatilityPolicy(observation_interval_seconds=3600, periods_per_year=365)


def test_unknown_coins_share_cluster_and_cannot_gain_diversification_credit():
    policy = CorrelationClusterPolicy(clusters={"BTC": "major"}, max_cluster_exposure_pct=.4,
                                      max_crypto_beta_exposure=.7)
    values = {"BTC/USDT": .8, "NEW/USDT": .8, "OTHER/USDT": .8}
    weights = constrain_target_weights(values, max_gross_weight=.6, max_symbol_weight=.3,
                                       cluster_policy=policy)
    reverse = constrain_target_weights(dict(reversed(list(values.items()))), max_gross_weight=.6,
                                        max_symbol_weight=.3, cluster_policy=policy)
    assert weights == reverse
    assert sum(weights.values()) == pytest.approx(.6)
    assert max(weights.values()) <= .3
    assert weights["NEW/USDT"] + weights["OTHER/USDT"] <= .4


def test_symbol_aliases_cannot_bypass_single_market_weight_cap():
    with pytest.raises(ValueError, match="duplicate normalized"):
        constrain_target_weights({"BTC/USDT": .3, "BTC-USDT": .3},
            max_gross_weight=1, max_symbol_weight=.3,
            cluster_policy=CorrelationClusterPolicy(enabled=False))
    result = constrain_target_weights({" btc/usdt ": .3}, max_gross_weight=1,
        max_symbol_weight=.3, cluster_policy=CorrelationClusterPolicy(
            clusters={"BTC": "major"}, max_cluster_exposure_pct=.2))
    assert result == {" btc/usdt ": .2}


def test_frozen_target_caps_original_approval_notional_concentration_and_precision():
    assert target(approved_risk_amount=12.3456).target_qty == Decimal("1.2")
    assert target(remaining_entry_notional=234).target_qty == Decimal("2.3")
    assert target(maximum_position_notional=340).target_qty == Decimal("3.4")
    assert target(equity=10**8).target_qty == Decimal("10")
    assert target(current_qty=5, approved_risk_amount=10).target_qty == Decimal("6")
    assert target(current_qty=5.05, approved_risk_amount=0).target_qty == Decimal("5.05")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True])
def test_invalid_budget_and_weights_fail_explicitly(value):
    with pytest.raises(ValueError):
        target(approved_risk_amount=value)
    with pytest.raises(ValueError):
        target(target_weight=value)


def test_rejects_derivative_contracts_and_wrong_stop_direction():
    with pytest.raises(ValueError, match="unit-based"):
        target(market=replace(MARKET, market_type="swap"))
    with pytest.raises(ValueError, match="protect"):
        target(initial_stop=110)
    short = target(identity=replace(IDENTITY, side="short"), initial_stop=110)
    assert tranche(short, effective_stop=110).action == "short"


def test_checkpoint_roundtrip_and_sqlite_restart_preserve_decision(tmp_path):
    plan = target()
    first = tranche(plan)
    path = str(tmp_path / "targets.db")
    state = StateStore(path)
    state.set("target:1", plan.checkpoint())
    state.close()
    state = StateStore(path)
    try:
        restored = FrozenTarget.restore(state.get("target:1"))
        assert restored == plan
        assert tranche(restored, remaining_entry_risk=1000000,
                       remaining_entry_notional=1000000) == first
        changed = json.loads(json.dumps(plan.checkpoint()))
        changed["payload"]["target_qty"] = "100"
        with pytest.raises(ValueError, match="digest"):
            FrozenTarget.restore(changed)
    finally:
        state.close()


@pytest.mark.parametrize("status", [OrderStatus.CREATED, OrderStatus.SUBMITTING, OrderStatus.ACCEPTED,
                                    OrderStatus.UNKNOWN, OrderStatus.CANCEL_PENDING,
                                    OrderStatus.PARTIALLY_FILLED])
def test_pending_and_unknown_tranches_block_duplicate_risk(status):
    plan = target()
    first = tranche(plan)
    pending = fact(first, filled=1, status=status)
    assert tranche(plan, current_qty=1, order_facts=[pending]).reason == "orders_unresolved"


def test_terminal_partial_fill_uses_remaining_capacity_without_recycling_exit_budget():
    plan = target(target_weight=.03)
    first = tranche(plan)
    partial = fact(first, filled=1, status=OrderStatus.CANCELED)
    second = tranche(plan, current_qty=1, order_facts=[partial])
    assert second.quantity == 2
    assert second.decision_id != first.decision_id
    assert second.approved_risk_amount == 20
    # A separate stop/reduction closed two units: a completed plan cannot buy them again.
    done = tranche(plan, current_qty=1, order_facts=[partial, fact(second, sequence=1)])
    assert done.action == "HOLD"
    assert done.reason == "target_satisfied_or_capacity_consumed"


def test_malformed_or_foreign_order_history_blocks_decisions():
    first = tranche()
    completed = fact(first)
    assert tranche(order_facts=[completed, completed]).reason == "order_history_incomplete_or_duplicate"
    assert tranche(order_facts=[replace(completed, sequence=1)]).reason == "order_history_incomplete_or_duplicate"
    assert tranche(order_facts=[replace(completed, decision_id="other")]).reason == "order_identity_mismatch"


@pytest.mark.parametrize("change,reason", [
    ({"facts_reconciled": False}, "facts_not_reconciled"),
    ({"identity": replace(IDENTITY, position_id="new-position")}, "position_identity_changed"),
    ({"identity": replace(IDENTITY, side="short")}, "position_identity_changed"),
    ({"identity": replace(IDENTITY, account="other")}, "position_identity_changed"),
    ({"lifecycle_closed": True}, "position_lifecycle_closed"),
    ({"has_unresolved_orders": True}, "orders_unresolved"),
    ({"allows_new_risk": False}, "new_risk_blocked"),
    ({"effective_stop": None}, "protective_stop_unverified"),
    ({"effective_stop": 89}, "protective_stop_loosened"),
    ({"current_price": 89}, "price_crossed_protective_stop"),
    ({"current_price": 101}, "current_price_exceeds_approved_stop_risk"),
])
def test_facts_lifecycle_gate_and_stop_cannot_be_bypassed(change, reason):
    assert tranche(**change).reason == reason


def test_short_stops_reject_crossed_prices_and_gates_reject_truthy_strings():
    plan = target(identity=replace(IDENTITY, side="short"), initial_stop=110)
    assert tranche(plan, effective_stop=110, current_price=111).reason == "price_crossed_protective_stop"
    assert tranche(plan, effective_stop=110, current_price=99).reason == "current_price_exceeds_approved_stop_risk"
    with pytest.raises(ValueError, match="booleans"):
        tranche(allows_new_risk="false")


def test_each_tranche_obeys_fresh_headroom_exchange_maximum_and_minimum():
    assert tranche(remaining_entry_risk=12.345).quantity == Decimal("1.2")
    assert tranche(remaining_entry_notional=234).quantity == Decimal("2.3")
    assert tranche(market=replace(MARKET, max_amount=Decimal("1"))).quantity == 1
    assert tranche(market=replace(MARKET, max_notional=Decimal("100"))).quantity == 1
    assert tranche(remaining_entry_notional=9).reason == "below_venue_minimum_or_budget"


def test_reduction_persists_target_under_lock_and_leaves_dust_honest():
    plan = target(current_qty=10, target_weight=.04, maximum_tranche_qty=3)
    first = tranche(plan, allows_new_risk=False, remaining_entry_risk=0,
                     remaining_entry_notional=0, effective_stop=None)
    assert (first.action, first.quantity, first.target_qty, first.reduce_only) == ("sell", 3, 4, True)
    assert first.approved_risk_amount == 0
    second = tranche(plan, current_qty=7, order_facts=[fact(first)])
    assert (second.quantity, second.target_qty) == (3, 4)
    assert tranche(plan, current_qty=4, order_facts=[fact(first), fact(second, sequence=1)]).action == "HOLD"
    assert tranche(plan, current_qty=4.05).reason == "below_venue_minimum_or_budget"


def test_real_broker_partial_fills_restarts_and_remaining_risk_conserve_original_approval(tmp_path):
    broker = Broker(Portfolio(10000), commission_rate=0, slippage=0)
    def bar(day, volume):
        return {SYMBOL: pd.Series(dict(open=100, high=101, low=99, close=100, volume=volume),
                                  name=pd.Timestamp(NOW)+pd.Timedelta(days=day))}
    seed = broker.submit_order(SYMBOL, "buy", 1, 100, stop_loss=90, approved_risk_amount=10,
                               timestamp=bar(0, 10)[SYMBOL].name)
    broker.process_orders(bar(1, 10))
    assert seed.status is OrderStatus.FILLED
    identity = replace(IDENTITY, position_id=broker.portfolio.open_lots(SYMBOL)[0].position_id)
    plan = target(identity=identity, current_qty=1, target_weight=.09, approved_risk_amount=80)
    state = StateStore(str(tmp_path / "target.db"))
    state.set("target", plan.checkpoint())
    state.close()
    state = StateStore(str(tmp_path / "target.db"))
    plan = FrozenTarget.restore(state.get("target"))
    state.close()
    orders, decisions = [], []
    day = 1
    def facts():
        return [TrancheFact(decision.decision_id, index, Decimal(str(order.qty)),
                            Decimal(str(order.filled_qty)), order.status)
                for index, (decision, order) in enumerate(zip(decisions, orders))]
    while broker.portfolio.get_position(SYMBOL)["qty"] < 9:
        remaining_risk = 80 - sum(order.filled_qty * 10 for order in orders)
        decision = tranche(plan, current_qty=broker.portfolio.get_position(SYMBOL)["qty"],
                           order_facts=facts(), remaining_entry_risk=remaining_risk)
        assert decision.action == "buy"
        assert decision == tranche(FrozenTarget.restore(plan.checkpoint()),
            current_qty=broker.portfolio.get_position(SYMBOL)["qty"], order_facts=facts(),
            remaining_entry_risk=remaining_risk)
        args = dict(symbol=SYMBOL, side=decision.action, qty=float(decision.quantity), price=100,
                    timestamp=bar(day, 1)[SYMBOL].name, strategy_id=decision.decision_id,
                    sequence=len(orders), stop_loss=90,
                    approved_risk_amount=float(decision.approved_risk_amount))
        order = broker.submit_order(**args)
        assert broker.submit_order(**args) is order
        decisions.append(decision)
        orders.append(order)
        day += 1
        broker.process_orders(bar(day, 1))
        if order.status is OrderStatus.PARTIALLY_FILLED:
            assert tranche(plan, current_qty=broker.portfolio.get_position(SYMBOL)["qty"],
                           order_facts=facts()).reason == "orders_unresolved"
            day += 1
            broker.process_orders(bar(day, 10))
    assert broker.portfolio.get_position(SYMBOL)["qty"] == 9
    assert sum(order.filled_qty for order in orders) == 8
    assert sum(order.intent.approved_risk_amount for order in orders) == 80
    assert sum(lot.approved_risk_amount for lot in broker.portfolio.open_lots(SYMBOL)) == 90
    assert broker.portfolio.cash == 9100
    assert {lot.position_id for lot in broker.portfolio.open_lots(SYMBOL)} == {identity.position_id}
    assert tranche(plan, current_qty=9, order_facts=facts()).action == "HOLD"
