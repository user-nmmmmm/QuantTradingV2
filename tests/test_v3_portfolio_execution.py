"""Real Broker evidence for opt-in target execution and quote borrowing."""
from types import SimpleNamespace

import pandas as pd
import pytest

from backtest.protective_stops import ResidentStopSimulator
from core.broker import Broker
from core.domain import OrderStatus
from core.margin_rebalance import MarginBudgetSnapshot, MarginQuote, plan_margin_rebalance, plan_margin_targets
from core.portfolio import Portfolio
from core.portfolio_target_controller import PortfolioTargetController
from core.quote_borrow import QuoteBorrowFact, QuoteBorrowPolicy
from core.risk import BreakerAction, RiskManager
from core.runtime import EventProcessor, MarketDataSlice
from core.state_store_v2 import StateStore


SYMBOL = "BTC/USDT"
SUNDAY = pd.Timestamp("2026-09-20")


class Strategy:
    name = "TrendPortfolioV3"

    def __init__(self, health=1.):
        self.context = {}
        self.health = health

    def get_context(self, symbol):
        return self.context.setdefault(symbol, {})

    def health_risk_multiplier(self):
        return self.health


def bars(day=0, *, symbols=(SYMBOL,), volume=10000, price=100, low=99):
    stamp = SUNDAY+pd.Timedelta(days=day)
    return {symbol: pd.Series(dict(open=price, high=price+1, low=low, close=price, volume=volume),
                              name=stamp) for symbol in symbols}


def event(day=0, **kwargs):
    values = bars(day, **kwargs)
    stamp = next(iter(values.values())).name
    return MarketDataSlice(stamp, values,
                           {symbol: pd.DataFrame([bar]) for symbol, bar in values.items()},
                           timeframe="1d", positions={symbol: 0 for symbol in values})


def setup_controller(weights=None, *, health=1., broker=None, state_store=None, participation=1.):
    weights = weights or {SYMBOL: .1}
    strategy = Strategy(health)
    broker = broker or Broker(Portfolio(10000), commission_rate=0, slippage=0)
    risk = RiskManager(max_pos_size_pct=.3, min_entry_notional_pct=0.)

    def provider(**kwargs):
        return {"as_of": kwargs["as_of"].isoformat(), "target_weights": dict(weights),
                "stop_prices": {symbol: 95. for symbol in weights},
                "add_allowed": {symbol: True for symbol in weights}}

    controller = PortfolioTargetController(strategy=strategy, target_provider=provider,
                                           state_store=state_store, participation=participation)
    return controller, broker, risk, strategy


def process(controller, broker, risk, day=0, **kwargs):
    market = event(day, **kwargs)
    return controller.process(event=market, portfolio=broker.portfolio, broker=broker,
        risk_manager=risk, current_prices={symbol: float(bar["close"]) for symbol, bar in market.bars.items()},
        risk_decision=SimpleNamespace(allow_new_entries=True))


def test_borrow_future_evidence_never_grants_present_quote_credit():
    fact = QuoteBorrowFact("backtest", "USDT", "2026-09-01", "2026-09-25", .08, 5000,
                           source_uri="fixture://verified-account-borrow")
    policy = QuoteBorrowPolicy([fact], mode="verified_only")
    assert policy.resolve(account="backtest", as_of="2026-09-21").limit == 0
    assert policy.resolve(account="backtest", as_of="2026-09-25").limit == 5000
    assert policy.resolve(account="other", as_of="2026-09-25").limit == 0
    with pytest.raises(ValueError, match="duplicate"):
        QuoteBorrowPolicy([fact, fact])


def test_verified_only_actual_fill_includes_quote_debt_fees_and_gap():
    portfolio = Portfolio(1000, account_mode="spot_margin", initial_margin_rate=1/3)
    broker = Broker(portfolio, commission_rate=.01, slippage=0)
    broker.quote_borrow_policy = QuoteBorrowPolicy(mode="verified_only")
    order = broker.submit_order(SYMBOL, "buy", 15, 100, timestamp=SUNDAY,
                                stop_loss=90, approved_risk_amount=150)
    broker.process_orders(bars(1, price=120))
    assert 0 < order.filled_qty < 10
    assert portfolio.cash >= -1e-8
    assert any(row["reason"] == "quote_borrow_limit" for row in broker.execution_audit)


def test_assumed_quote_borrow_is_explicit_and_rate_is_account_wide():
    def run(reverse):
        portfolio = Portfolio(1000, account_mode="spot_margin", initial_margin_rate=1/3)
        broker = Broker(portfolio, commission_rate=0, slippage=0)
        broker.quote_borrow_policy = QuoteBorrowPolicy()
        portfolio.update_position(SYMBOL, 15, 100, 0, stop_price=95, approved_risk_amount=75)
        first, second = bars(0, symbols=(SYMBOL, "ETH/USDT")), bars(1, symbols=(SYMBOL, "ETH/USDT"))
        first[SYMBOL]["quote_borrow_rate_annual"] = .99
        first["ETH/USDT"]["quote_borrow_rate_annual"] = .01
        second[SYMBOL]["quote_borrow_rate_annual"] = .99
        second["ETH/USDT"]["quote_borrow_rate_annual"] = .01
        if reverse:
            first, second = dict(reversed(list(first.items()))), dict(reversed(list(second.items())))
        broker.accrue_carry(first)
        return broker.accrue_carry(second)
    forward, reverse = run(False), run(True)
    assert forward == reverse
    assert forward[0]["amount"] == pytest.approx(500*.08/365)
    assert "assumed" in forward[0]["source"]


def test_causal_piecewise_rates_do_not_reprice_previous_days():
    fact = QuoteBorrowFact("backtest", "USDT", "2026-09-01", "2026-09-22", .16, 5000,
                           source_uri="fixture://published-rate")
    policy = QuoteBorrowPolicy([fact])
    amount, rows = policy.interest(account="backtest", start="2026-09-21", end="2026-09-23", principal=1000)
    assert amount == pytest.approx(1000*(.08+.16)/365)
    assert [row["status"] for row in rows] == ["assumed", "verified"]


def test_margin_planner_prorates_and_does_not_spend_unfilled_sells():
    kwargs = dict(requested_buys={"Z": 10., "A": 10.}, requested_sells={"S": 10.},
                  quotes={symbol: MarginQuote(100, 10000, .01, 1) for symbol in ("A", "Z", "S")},
                  approved_buy_notional={"A": 1000, "Z": 1000}, cash=100,
                  quote_borrow_limit=0, available_margin=1000, initial_margin_rate=1,
                  gross_headroom=10000, remaining_turnover_notional=10000,
                  remaining_stop_risk=1000, stop_distances={"A": 5, "Z": 5},
                  fee_rate=0, slippage_rate=0, facts_reconciled=True, participation=1)
    plan = plan_margin_rebalance(**kwargs)
    bought = {row["symbol"]: row["qty"] for row in plan["orders"] if row["side"] == "buy"}
    assert bought == {"A": .5, "Z": .5}
    with pytest.raises(ValueError, match="reconciled"):
        plan_margin_rebalance(**{**kwargs, "facts_reconciled": False})


def test_monday_targets_fill_later_partial_orders_block_duplicate_submission():
    controller, broker, risk, _ = setup_controller()
    orders = process(controller, broker, risk)
    assert len(orders) == 1
    assert broker.portfolio.get_position(SYMBOL)["qty"] == 0
    assert process(controller, broker, risk) == []
    broker.process_orders(bars(1, volume=2))
    assert orders[0].status is OrderStatus.PARTIALLY_FILLED
    assert orders[0].filled_qty == 2
    assert process(controller, broker, risk, 1) == []
    assert len(broker.opening_orders) == 1
    broker.process_orders(bars(2))
    assert broker.portfolio.get_position(SYMBOL)["qty"] == pytest.approx(10)
    assert process(controller, broker, risk, 2) == []
    assert sum(lot.approved_risk_amount for lot in broker.portfolio.open_lots(SYMBOL)) == pytest.approx(50)


def test_held_adjustment_preserves_position_identity_and_protection():
    controller, broker, risk, strategy = setup_controller()
    broker.portfolio.update_position(SYMBOL, 2, 100, 0, strategy_id=strategy.name,
                                     stop_price=95, approved_risk_amount=10)
    identity = broker.portfolio.open_lots(SYMBOL)[0].position_id
    orders = process(controller, broker, risk)
    assert orders[0].qty == pytest.approx(8)
    broker.process_orders(bars(1))
    assert {lot.position_id for lot in broker.portfolio.open_lots(SYMBOL)} == {identity}
    simulator = ResidentStopSimulator(broker, {strategy.name: strategy})
    simulator.step(event(1, low=90), bar_index=1)
    assert broker.portfolio.get_position(SYMBOL)["qty"] == 0
    assert simulator.triggered_stops == 1
    assert process(controller, broker, risk, 1) == []


def test_checkpoint_reopen_partial_order_retains_capacity_and_no_duplicates(tmp_path):
    store = StateStore(str(tmp_path/"target.db"))
    controller, broker, risk, _ = setup_controller(state_store=store)
    orders = process(controller, broker, risk)
    broker.process_orders(bars(1, volume=2))
    process(controller, broker, risk, 1)
    store.close()
    store = StateStore(str(tmp_path/"target.db"))
    restored, _, _, _ = setup_controller(broker=broker, state_store=store)
    assert process(restored, broker, risk, 1) == []
    assert list(broker.opening_orders) == [orders[0].id]
    broker.cancel_opening_orders([SYMBOL], timestamp=SUNDAY+pd.Timedelta(days=2))
    next_orders = process(restored, broker, risk, 2)
    assert len(next_orders) == 1
    assert next_orders[0].qty == pytest.approx(8)
    assert next_orders[0].intent.approved_risk_amount == pytest.approx(40)
    assert next_orders[0].id != orders[0].id
    store.close()


def test_unknown_and_missing_accepted_order_facts_block_new_intents():
    controller, broker, risk, _ = setup_controller()
    order = process(controller, broker, risk)[0]
    broker._set_status(order, OrderStatus.UNKNOWN, SUNDAY)
    assert process(controller, broker, risk, 1) == []
    assert controller.audit[-1]["reason"] == "unreconciled_orders"
    restored, empty, _, _ = setup_controller()
    restored.restore(controller.checkpoint())
    assert process(restored, empty, risk, 1) == []
    assert restored.audit[-1]["reason"] == "unreconciled_orders"


def test_probation_applies_once_to_target_not_to_every_remaining_tranche():
    controller, broker, risk, strategy = setup_controller(health=.5)
    order = process(controller, broker, risk)[0]
    assert order.qty == pytest.approx(5)
    broker.process_orders(bars(1))
    strategy.health = 1.
    assert process(controller, broker, risk, 1) == []
    assert broker.portfolio.get_position(SYMBOL)["qty"] == pytest.approx(5)


def test_no_initial_target_until_monday_and_zero_target_ignores_tolerance():
    controller, broker, risk, _ = setup_controller()
    assert process(controller, broker, risk, 2) == []
    controller, broker, risk, _ = setup_controller({SYMBOL: 0.})
    broker.portfolio.update_position(SYMBOL, 1, 100, 0, stop_price=95, approved_risk_amount=5)
    orders = process(controller, broker, risk)
    assert len(orders) == 1 and orders[0].side == "sell"


def test_runtime_keeps_nonselected_held_position_management_during_warmup():
    portfolio = Portfolio(10000)
    portfolio.update_position(SYMBOL, 1, 100, 0, stop_price=95, approved_risk_amount=5)
    broker = Broker(portfolio)
    calls = []

    class Router:
        def process_position_management(self, symbol, *args):
            calls.append(symbol)
            return True

        def collect_entry_candidate(self, *args):
            raise AssertionError("warmup may not collect entries")

    runtime = EventProcessor(portfolio=portfolio, execution=broker, risk_manager=RiskManager(),
        state_machine=SimpleNamespace(get_state=lambda frame, index: "UP"), router=Router(),
        allocator=SimpleNamespace(allocate=lambda *args, **kwargs: None), warmup_period=120)
    runtime.process(event(), symbols=[], execute_market_event=False)
    assert calls == [SYMBOL]


def test_weekly_turnover_is_shared_prorata_and_fills_keep_consuming_it():
    symbols = tuple(f"COIN{index}/USDT" for index in range(6))
    controller, broker, risk, _ = setup_controller({symbol: .2 for symbol in symbols})
    original = controller.target_provider

    def provider(**kwargs):
        snapshot = original(**kwargs)
        snapshot["stop_prices"] = {symbol: 99. for symbol in symbols}
        return snapshot

    controller.target_provider = provider
    orders = process(controller, broker, risk, symbols=symbols)
    assert len(orders) == len(symbols)
    assert max(order.qty for order in orders) == min(order.qty for order in orders)
    assert sum(order.qty*100 for order in orders) == pytest.approx(5000, abs=.0001)
    broker.process_orders(bars(1, symbols=symbols))
    assert process(controller, broker, risk, 1, symbols=symbols) == []
    assert controller.audit[-1]["turnover_committed"] == pytest.approx(5000, abs=.0001)


def test_daily_parent_risk_is_shared_prorata_across_all_candidates():
    symbols = tuple(f"COIN{index}/USDT" for index in range(6))
    controller, broker, risk, _ = setup_controller({symbol: .2 for symbol in symbols})
    orders = process(controller, broker, risk, symbols=symbols)
    assert len(orders) == len(symbols)
    assert max(order.qty for order in orders) == min(order.qty for order in orders)
    assert sum(order.intent.approved_risk_amount for order in orders) == pytest.approx(200, abs=1e-6)
    assert process(controller, broker, risk, symbols=symbols) == []


def test_forced_exit_bypasses_weekly_turnover_and_never_cancels_protection():
    controller, broker, risk, strategy = setup_controller()
    broker.portfolio.update_position(SYMBOL, 80, 100, 0, strategy_id=strategy.name,
                                     stop_price=95, approved_risk_amount=400)
    strategy.context[SYMBOL] = {"stop_loss": 95.}
    controller.metadata = {SYMBOL: {"events": [{"kind": "spot_delisted", "source_status": "verified",
        "effective_at": "2026-09-25", "available_at": "2026-09-21"}]}}
    orders = process(controller, broker, risk)
    assert len(orders) == 1
    assert orders[0].side == "sell" and orders[0].qty == 80
    assert orders[0].exit_reason == "v3_forced_exit"
    assert SYMBOL in controller.checkpoint()["state"]["suppressed"]


def test_new_quote_debt_is_not_charged_for_the_day_before_its_fill():
    portfolio = Portfolio(1000, account_mode="spot_margin", initial_margin_rate=1/3)
    broker = Broker(portfolio, commission_rate=0, slippage=0)
    broker.quote_borrow_policy = QuoteBorrowPolicy()
    broker.accrue_carry(bars(0))
    broker.submit_order(SYMBOL, "buy", 15, 100, timestamp=SUNDAY,
                        stop_loss=95, approved_risk_amount=75)
    broker.process_orders(bars(1))
    assert broker.accrue_carry(bars(1)) == []
    entries = broker.accrue_carry(bars(2))
    assert entries[0]["amount"] == pytest.approx(500*.08/365)


def test_explicit_health_lock_and_unknown_market_state_prevent_new_risk():
    controller, broker, risk, strategy = setup_controller()
    strategy.check_health = lambda now: False
    assert process(controller, broker, risk) == []
    strategy.check_health = lambda now: True
    strategy.entry_risk_multiplier = lambda state: 1 if state == "UP" else 0
    market = event()
    assert controller.process(event=market, portfolio=broker.portfolio, broker=broker, risk_manager=risk,
        current_prices={SYMBOL: 100.}, risk_decision=SimpleNamespace(allow_new_entries=True),
        market_states={SYMBOL: "NO_TRADE"}) == []


def test_resting_broker_protective_submitted_alias_is_not_unknown():
    controller, broker, risk, strategy = setup_controller()
    process(controller, broker, risk)
    broker.process_orders(bars(1))
    simulator = ResidentStopSimulator(broker, {strategy.name: strategy})
    simulator.step(event(1), bar_index=1)
    assert broker.active_orders
    assert broker.active_orders[0].status is OrderStatus.SUBMITTED
    process(controller, broker, risk, 1)
    assert controller.audit[-1].get("reason") != "unreconciled_orders"


def test_account_reduce_multiplier_is_frozen_once_and_recovery_does_not_refill():
    controller, broker, risk, _ = setup_controller()
    risk.portfolio_breaker_action = BreakerAction.REDUCE
    risk.reduced_risk_multiplier = .5
    assert risk.risk_multiplier == .5
    order = process(controller, broker, risk)[0]
    assert order.qty == pytest.approx(5)
    broker.process_orders(bars(1))
    risk.portfolio_breaker_action = BreakerAction.NORMAL
    assert process(controller, broker, risk, 1) == []


@pytest.mark.parametrize("account_mode", ["spot_margin", "perpetual"])
@pytest.mark.parametrize("side", [1, -1])
def test_fifo_partial_reduction_keeps_collateral_and_unrealized_pnl_consistent(account_mode, side):
    portfolio = Portfolio(10000, account_mode=account_mode, initial_margin_rate=1/3)
    portfolio.update_position(SYMBOL, side, 100, 0, order_id="first")
    portfolio.update_position(SYMBOL, side, 200, 0, order_id="second")
    before = portfolio.get_equity({SYMBOL: 200.})
    portfolio.update_position(SYMBOL, -side, 200, 0)
    assert portfolio.get_position(SYMBOL)["avg_price"] == 200
    assert portfolio.get_equity({SYMBOL: 200.}) == pytest.approx(before)
    assert portfolio.cash == pytest.approx(10000+side*100)


def test_spot_partial_reduction_retains_legacy_position_average_display():
    portfolio = Portfolio(10000)
    portfolio.update_position(SYMBOL, 1, 100, 0, order_id="first")
    portfolio.update_position(SYMBOL, 1, 200, 0, order_id="second")
    portfolio.update_position(SYMBOL, -1, 200, 0)
    assert portfolio.get_position(SYMBOL)["avg_price"] == 150
    assert portfolio.get_equity({SYMBOL: 200.}) == 10100


def test_verified_only_multiple_margin_buys_cannot_reuse_collateral_as_quote_cash():
    portfolio = Portfolio(1000, account_mode="spot_margin", initial_margin_rate=1/3)
    broker = Broker(portfolio, commission_rate=0, slippage=0)
    broker.quote_borrow_policy = QuoteBorrowPolicy(mode="verified_only")
    broker.submit_order(SYMBOL, "buy", 6, 100, timestamp=SUNDAY,
                        stop_loss=95, approved_risk_amount=30)
    broker.process_orders(bars(1))
    second = broker.submit_order(SYMBOL, "buy", 6, 100, timestamp=SUNDAY+pd.Timedelta(days=1),
                                  stop_loss=95, approved_risk_amount=30)
    broker.process_orders(bars(2))
    assert second.filled_qty == pytest.approx(4)
    assert portfolio.get_total_exposure({SYMBOL: 100.}) <= portfolio.get_equity({SYMBOL: 100.})+1e-7


def test_gap_fill_cannot_exceed_order_or_weekly_turnover_approval():
    symbols = tuple(f"COIN{index}/USDT" for index in range(6))
    controller, broker, risk, _ = setup_controller({symbol: .2 for symbol in symbols})
    original = controller.target_provider

    def provider(**kwargs):
        snapshot = original(**kwargs)
        snapshot["stop_prices"] = {symbol: 99. for symbol in symbols}
        return snapshot

    controller.target_provider = provider
    orders = process(controller, broker, risk, symbols=symbols)
    broker.process_orders(bars(1, symbols=symbols, price=120))
    assert sum(trade["qty"]*trade["fill_price"] for trade in broker.trades) <= 5000
    for order in orders:
        assert order.filled_qty*order.avg_fill_price <= order.qty*100
    assert any(row["reason"] == "weekly_turnover_fill_limit" for row in broker.execution_audit)


def test_portfolio_runtime_selects_once_and_only_calculates_relevant_states():
    symbols = ("HELD/USDT", "CLOSED/USDT", "ORDER/USDT", "BUY/USDT", "UNRELATED/USDT")
    controller, broker, risk, _ = setup_controller({"BUY/USDT": 0.})
    broker.portfolio.update_position("HELD/USDT", 1, 100, 0, stop_price=95, approved_risk_amount=5)
    broker.close_events.append(SimpleNamespace(close_event_id="close-one", symbol="CLOSED/USDT",
                                               timestamp=SUNDAY, exit_reason="protective_stop"))
    broker.submit_order("ORDER/USDT", "buy", 1, 100, timestamp=SUNDAY,
                        stop_loss=95, approved_risk_amount=5)
    source = controller.target_provider
    counts = {"provider": 0, "state": 0}

    def provider(**kwargs):
        counts["provider"] += 1
        return source(**kwargs)

    def get_state(frame, index):
        counts["state"] += 1
        return "UP"

    managed = []

    class Router:
        def process_position_management(self, symbol, *args):
            managed.append(symbol)
            return bool(broker.portfolio.get_position(symbol)["qty"])

        def collect_entry_candidate(self, *args):
            raise AssertionError("controller mode must not collect legacy entries")

    controller.target_provider = provider
    runtime = EventProcessor(portfolio=broker.portfolio, execution=broker, risk_manager=risk,
        state_machine=SimpleNamespace(get_state=get_state), router=Router(),
        allocator=SimpleNamespace(allocate=lambda *args, **kwargs: None), portfolio_controller=controller)
    runtime.process(event(symbols=symbols), execute_market_event=False)
    assert counts == {"provider": 1, "state": 4}
    assert set(managed) == set(symbols)-{"UNRELATED/USDT"}
    assert set(runtime.last_prices) == set(symbols)


def test_actual_quote_credit_recheck_includes_interest_accrued_before_fill():
    portfolio = Portfolio(1000, account_mode="spot_margin", initial_margin_rate=1/3)
    broker = Broker(portfolio, commission_rate=0, slippage=0)
    broker.quote_borrow_policy = QuoteBorrowPolicy([
        QuoteBorrowFact("backtest", "USDT", "2026-01-01", "2026-01-01", .365, 500,
                        source_uri="fixture://borrow-limit")], mode="verified_only")
    broker.quote_borrow_market_metadata = {SYMBOL: {"events": [{"kind": "margin_eligible",
        "available_at": "2026-01-01", "effective_at": "2026-01-01", "source_status": "verified"}]}}
    broker.submit_order(SYMBOL, "buy", 14, 100, timestamp=SUNDAY,
                        stop_loss=95, approved_risk_amount=70)
    broker.process_orders(bars(1))
    order = broker.submit_order(SYMBOL, "buy", 1, 100, timestamp=SUNDAY+pd.Timedelta(days=1),
                                stop_loss=95, approved_risk_amount=5)
    broker.process_orders(bars(2))
    assert order.filled_qty == pytest.approx(.996)
    assert portfolio.get_total_exposure({SYMBOL: 100.})-portfolio.get_equity({SYMBOL: 100.}) <= 500+1e-8


def test_verified_market_margin_eligibility_requires_effective_and_knowledge_time():
    policy = QuoteBorrowPolicy(mode="verified_only")
    fact = {"kind": "margin_eligible", "effective_at": "2026-09-22", "available_at": "2026-09-21",
            "source_status": "verified"}
    metadata = {SYMBOL: {"events": [fact]}}
    assert not policy.market_eligibility(symbol=SYMBOL, as_of="2026-09-21", metadata=metadata).allowed
    assert policy.market_eligibility(symbol=SYMBOL, as_of="2026-09-22", metadata=metadata).allowed
    fact["available_at"] = "2026-09-25"
    assert not policy.market_eligibility(symbol=SYMBOL, as_of="2026-09-22", metadata=metadata).allowed
    assert policy.market_eligibility(symbol=SYMBOL, as_of="2026-09-25", metadata=metadata).allowed


def test_known_margin_removal_overrides_assumption_but_future_announcement_does_not():
    policy = QuoteBorrowPolicy()
    removal = {"kind": "margin_delisted", "effective_at": "2026-09-24", "available_at": "2026-09-22",
               "source_status": "verified"}
    metadata = {SYMBOL: {"events": [removal]}}
    before = policy.market_eligibility(symbol=SYMBOL, as_of="2026-09-21", metadata=metadata)
    assert before.allowed and before.status == "assumed"
    known = policy.market_eligibility(symbol=SYMBOL, as_of="2026-09-22", metadata=metadata)
    assert not known.allowed and known.reason == "announced_margin_removal"


def test_base_borrow_suspension_does_not_disable_usdt_but_explicit_quote_scope_does():
    policy = QuoteBorrowPolicy()
    fact = {"kind": "borrow_suspended", "effective_at": "2026-09-20", "available_at": "2026-09-20",
            "source_status": "verified", "borrow_asset": "BTC"}
    metadata = {SYMBOL: {"events": [fact]}}
    assert policy.market_eligibility(symbol=SYMBOL, as_of="2026-09-21", metadata=metadata).allowed
    fact["borrow_asset"] = "USDT"
    assert not policy.market_eligibility(symbol="ETH/USDT", as_of="2026-09-21", metadata=metadata).allowed
    fact["available_at"] = "2026-09-22"
    assert policy.market_eligibility(symbol=SYMBOL, as_of="2026-09-21", metadata=metadata).allowed


def test_fill_rechecks_new_margin_removal_and_still_allows_equity_funded_spot_buy():
    portfolio = Portfolio(1000, account_mode="spot_margin", initial_margin_rate=1/3)
    broker = Broker(portfolio, commission_rate=0, slippage=0)
    broker.quote_borrow_policy = QuoteBorrowPolicy()
    broker.quote_borrow_market_metadata = {SYMBOL: {"events": [{"kind": "margin_delisted",
        "available_at": "2026-09-21", "effective_at": "2026-09-24", "source_status": "verified"}]}}
    order = broker.submit_order(SYMBOL, "buy", 15, 100, timestamp=SUNDAY,
                                stop_loss=95, approved_risk_amount=75)
    broker.process_orders(bars(1))
    assert order.filled_qty == pytest.approx(10)
    assert portfolio.get_position(SYMBOL)["qty"] > 0
    assert any(row.get("market_financing_reason") == "announced_margin_removal" for row in broker.execution_audit)


def test_cash_only_markets_share_own_funds_and_keep_credit_for_eligible_markets():
    plan = plan_margin_rebalance(requested_buys={"A": 10, "B": 10, "Z": 10}, requested_sells={},
        quotes={symbol: MarginQuote(100, 10000, .01, 1) for symbol in ("A", "B", "Z")},
        approved_buy_notional={symbol: 1000 for symbol in ("A", "B", "Z")},
        cash=100, quote_borrow_limit=1000, available_margin=1000, initial_margin_rate=1/3,
        gross_headroom=10000, remaining_turnover_notional=10000, remaining_stop_risk=1000,
        stop_distances={symbol: 5 for symbol in ("A", "B", "Z")},
        financing_allowed={"A": False, "B": False, "Z": True},
        fee_rate=0, slippage_rate=0, participation=1, facts_reconciled=True)
    assert [(row["symbol"], row["qty"]) for row in plan["orders"]] == [("A", .5), ("B", .5), ("Z", 10)]


@pytest.mark.parametrize("net_quote,gross,expected_cash,expected_debt,financing", [
    (500., 500., 500., 0., 0.), (-500., 1500., 0., 500., 100.)])
def test_budget_snapshot_separates_cash_assets_current_debt_and_proposed_financing(
        net_quote, gross, expected_cash, expected_debt, financing):
    budget = MarginBudgetSnapshot(account="research", as_of="2026-09-21T00:00:00Z", equity=1000,
        net_quote_cash=net_quote, current_gross_exposure=gross, quote_borrow_limit=1000,
        available_margin=500, initial_margin_rate=1/3, approved_buy_notional={SYMBOL: 100},
        pending_buy_notional=0, gross_headroom=1000, remaining_turnover_notional=500,
        remaining_stop_risk=100, facts_reconciled=True)
    result = plan_margin_targets(budget=budget, requested_buys={SYMBOL: 1}, requested_sells={},
        quotes={SYMBOL: MarginQuote(100, 1000)}, stop_distances={SYMBOL: 5},
        fee_rate=0, slippage_rate=0, participation=1)
    assert result["cash_asset"] == expected_cash
    assert result["borrow_liability"] == expected_debt
    assert result["proposed_financing_requirement"] == financing
    assert result["target_gross_exposure"] == gross+100
    assert result["target_gross_weight"] == pytest.approx((gross+100)/1000)
    assert result["proposal_only"]
    assert budget.net_quote_cash == net_quote
