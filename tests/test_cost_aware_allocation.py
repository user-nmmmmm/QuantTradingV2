"""Cost-aware native targets: causal forecasts, real reservations and fills."""
from dataclasses import asdict, replace
from copy import deepcopy

import numpy as np
import pandas as pd
import pytest

from core.cost_aware_allocation import CostAwareAllocationPolicy, allocate_cost_aware
from core.selection_v2 import select_all_qualified, size_portfolio_targets
from core.portfolio_target_controller import PortfolioTargetController
from tests.test_trend_portfolio_v3 import frame, metadata, selection
from tests.test_v3_portfolio_execution import (
    SYMBOL, Strategy, bars, event, process, setup_controller,
)


def policy(**kwargs):
    return CostAwareAllocationPolicy(enabled=True, **kwargs)


@pytest.mark.parametrize("kwargs", [{"max_gross_weight": 1.01}, {"rebalance": "unknown"},
    {"horizon_days": True}, {"holding_cost_bps_per_day": -1}, {"trading_cost_bps": float("nan")}])
def test_policy_rejects_unbounded_or_ambiguous_research_inputs(kwargs):
    with pytest.raises(ValueError):
        policy(**kwargs)


def test_disabled_targets_and_controller_identity_remain_identical():
    chosen = selection(5)
    default = size_portfolio_targets(chosen)
    disabled = size_portfolio_targets(chosen, cost_aware_policy=CostAwareAllocationPolicy())
    assert disabled.to_dict() == default.to_dict()
    assert "cost_aware_audit" not in default.to_dict()
    controller, _, _, strategy = setup_controller()
    other = PortfolioTargetController(strategy=strategy, target_provider=controller.target_provider,
                                     participation=1., cost_aware_policy=CostAwareAllocationPolicy())
    assert controller.policy_identity == other.policy_identity
    assert controller.checkpoint() == other.checkpoint()


def test_diagonal_solution_and_holding_transaction_costs_have_measurable_effect():
    basic = policy(trading_cost_bps=0, impact_penalty=0, risk_aversion=1., max_turnover_weight=1.)
    # .5*.1*w^2 - .02*w has optimum .2; transaction cost .005 lowers it to .15.
    weights, audit = allocate_cost_aware([.02], [[.1]], [0.], [.8], policy=basic)
    assert weights == pytest.approx([.2], abs=1e-7)
    taxed, _ = allocate_cost_aware([.02], [[.1]], [0.], [.8],
        policy=replace(basic, trading_cost_bps=50))
    assert taxed == pytest.approx([.15], abs=1e-7)
    carry, _ = allocate_cost_aware([.02], [[.1]], [0.], [.8],
        policy=replace(basic, horizon_days=1, holding_cost_bps_per_day=50))
    assert carry == pytest.approx(taxed)
    assert audit["holding_cost_is_objective_estimate_only"]


def test_turnover_is_solved_jointly_not_proportional_post_shrink():
    p = policy(trading_cost_bps=0, impact_penalty=0, risk_aversion=1., max_turnover_weight=.1)
    weights, audit = allocate_cost_aware([.08, .02], np.eye(2)*.1, [0., 0.], [.5, .5], policy=p)
    assert weights == pytest.approx([.1, 0.], abs=1e-7)
    assert audit["discretionary_turnover_weight"] <= .1+1e-9
    assert audit["turnover_constraint"].startswith("inside_convex")


@pytest.mark.parametrize("mode", ["immediate", "partial", "band"])
def test_mandatory_risk_reduction_precedes_adjustment_and_hard_turnover(mode):
    p = policy(rebalance=mode, max_turnover_weight=0., no_trade_band=1.)
    weights, audit = allocate_cost_aware([.1, .1], np.eye(2)*.01,
                                        [.7, .1], [.2, .3], policy=p)
    assert weights == pytest.approx([.2, .1])
    assert audit["mandatory_turnover_weight"] == pytest.approx(.5)
    assert audit["discretionary_turnover_weight"] == 0


def test_partial_and_band_are_applied_once_to_frozen_discretionary_target():
    p = policy(trading_cost_bps=0, impact_penalty=0, risk_aversion=1., max_turnover_weight=1.)
    args = ([.03], [[.1]], [.1], [.8])
    partial, _ = allocate_cost_aware(*args, policy=replace(p, rebalance="partial", partial_fraction=.5))
    band, _ = allocate_cost_aware(*args, policy=replace(p, rebalance="band", no_trade_band=.25))
    assert partial == pytest.approx([.2], abs=1e-7)
    assert band == pytest.approx([.1])


def test_future_data_cannot_change_native_cost_aware_targets():
    histories = {s: frame(300, phase=i/3) for i, s in enumerate(("BTC/USDT", "ETH/USDT"))}
    past = {s: f.iloc[:240].copy() for s, f in histories.items()}
    as_of = next(iter(past.values())).index[-1]+pd.Timedelta(days=1)
    chosen = select_all_qualified(past, metadata(histories), as_of=as_of)
    before = size_portfolio_targets(chosen, cost_aware_policy=policy()).to_dict()
    for history in histories.values():
        history.iloc[240:, :5] *= 100
    future = select_all_qualified(histories, metadata(histories), as_of=as_of)
    assert size_portfolio_targets(future, cost_aware_policy=policy()).to_dict() == before
    native = size_portfolio_targets(chosen)
    assert all(w <= native.target_weights[s] for s, w in before["target_weights"].items())


def test_zero_qualified_selection_and_failure_audit_request_real_cash_target(monkeypatch):
    chosen = selection(2)
    existing = {s: .1 for s in chosen.selected_symbols}
    original = np.linalg.eigvalsh
    def broken(*args, **kwargs):
        raise np.linalg.LinAlgError("simulated solver failure")
    monkeypatch.setattr(np.linalg, "eigvalsh", broken)
    result = size_portfolio_targets(chosen, existing_weights=existing, cost_aware_policy=policy())
    assert result.cost_aware_audit["cash_fallback"]
    assert all(w == 0 for w in result.target_weights.values())
    assert result.mandatory_reduction_weights == dict.fromkeys(existing, 0.)
    monkeypatch.setattr(np.linalg, "eigvalsh", original)
    empty = replace(chosen, selected_symbols=())
    result = size_portfolio_targets(empty, existing_weights=existing, cost_aware_policy=policy())
    assert result.mandatory_reduction_weights == dict.fromkeys(existing, 0.)


def cost_controller(weight=.1, *, max_turnover=.5, fail=False):
    base, broker, risk, strategy = setup_controller()
    p = policy(max_turnover_weight=0. if fail else .25, rebalance="band", no_trade_band=1.)
    def provider(**kwargs):
        existing = kwargs["existing_weights"].get(SYMBOL, 0.)
        target, audit = allocate_cost_aware([np.nan if fail else .01], [[.01]], [existing], [weight], policy=p)
        mandatory = {SYMBOL: 0. if fail else weight} if existing > weight or fail else {}
        return {"as_of": kwargs["as_of"], "target_weights": {SYMBOL: float(target[0])},
                "stop_prices": {SYMBOL: 95.}, "add_allowed": {SYMBOL: True},
                "cost_aware_audit": audit, "mandatory_reduction_weights": mandatory}
    controller = PortfolioTargetController(strategy=strategy, target_provider=provider,
        participation=1., max_weekly_turnover=max_turnover, relative_tolerance=.9, cost_aware_policy=p)
    return controller, broker, risk, strategy


@pytest.mark.parametrize("fail", [False, True])
def test_mandatory_or_failure_cash_sell_uses_real_broker_pending_capacity_and_account_bridge(fail):
    controller, broker, risk, strategy = cost_controller(max_turnover=.001, fail=fail)
    broker.commission_rate = .001
    opening = broker.submit_order(SYMBOL, "buy", 20, 100, timestamp=bars(-2)[SYMBOL].name,
                                  stop_loss=95, approved_risk_amount=100, strategy_id=strategy.name)
    broker.process_orders(bars(-1))
    assert opening.filled_qty == 20
    before = broker.portfolio.get_position(SYMBOL)["qty"]
    orders = process(controller, broker, risk)
    assert len(orders) == 1 and orders[0].exit_reason == "v3_risk_reduction"
    assert orders[0].qty > .001*10000/100
    assert broker.portfolio.get_position(SYMBOL)["qty"] == before
    assert process(controller, broker, risk) == []  # pending reduction is reserved
    broker.process_orders(bars(1, volume=2))
    assert orders[0].filled_qty == 2
    assert process(controller, broker, risk, 1) == []
    broker.process_orders(bars(2))
    held = broker.portfolio.get_position(SYMBOL)["qty"]
    assert held == pytest.approx(0 if fail else 9.998)
    cash_bridge = 10000-sum((1 if t["side"] == "buy" else -1)*t["qty"]*t["fill_price"]+
                           t["commission"] for t in broker.trades)
    assert broker.portfolio.cash == pytest.approx(cash_bridge)
    assert broker.portfolio.get_equity({SYMBOL: 100.}) == pytest.approx(cash_bridge+held*100)


def test_enabled_controller_requires_native_allocator_audit_and_checkpoint_policy():
    controller, broker, risk, _ = setup_controller()
    enabled = PortfolioTargetController(strategy=controller.strategy,
        target_provider=controller.target_provider, cost_aware_policy=policy())
    with pytest.raises(ValueError, match="matching allocator audit"):
        process(enabled, broker, risk)
    with pytest.raises(ValueError, match="policy changed"):
        enabled.restore(controller.checkpoint())


def test_native_cost_allocator_reaches_regular_engine_and_future_prefix_is_identical(monkeypatch):
    from backtest.engine import BacktestEngine
    from composition.factory import build_strategy_registry
    from config.config import config
    from scripts.run_trend_portfolio_v3 import effective_config
    from tests.test_v3_engine_integration import synthetic_market, spec

    frames, meta = synthetic_market(length=224, count=4)
    p = policy(max_turnover_weight=.15, rebalance="partial")
    settings = effective_config(deepcopy(config._config), spec(financing="verified_only"))
    settings["account"]["mode"] = "spot"
    settings["execution"]["fee_schedule"]["market_type"] = "spot"
    monkeypatch.setattr(config, "_config", settings)

    def run(market):
        registry = build_strategy_registry(config)
        strategy = registry["TrendPortfolioV3"]
        def provider(**kwargs):
            chosen = select_all_qualified(kwargs["event"].histories, meta, as_of=kwargs["as_of"],
                held_symbols=kwargs["held_symbols"], policy=strategy.selection_policy)
            return size_portfolio_targets(chosen, existing_weights=kwargs["existing_weights"],
                                          cost_aware_policy=p)
        controller = PortfolioTargetController(strategy=strategy, target_provider=provider,
                                               metadata=meta, cost_aware_policy=p)
        engine = BacktestEngine(initial_capital=10000, warmup_period=0, timeframe="1d", account_mode="spot",
            trading_start=pd.Timestamp("2023-07-19"), portfolio_controller=controller,
            terminal_policy="valuation_only", calculate_benchmarks=False)
        return engine.run(market, strategies=registry, routing_log_enabled=False)

    cutoff = pd.Timestamp("2023-08-05")
    past = run({s: f.loc[:cutoff].copy() for s, f in frames.items()})
    for f in frames.values():
        f.loc[f.index > cutoff, ["open", "high", "low", "close"]] *= 1.1
    future = run(frames)
    columns = ["symbol", "side", "qty", "fill_price", "fill_time", "commission", "exit_reason"]
    before = pd.DataFrame(past["trades"])[columns]
    after = pd.DataFrame(future["trades"])[columns]
    after = after.loc[pd.to_datetime(after.fill_time).dt.tz_localize(None) <= cutoff].reset_index(drop=True)
    assert not before.empty
    pd.testing.assert_frame_equal(before, after)
    assert past["accounting_check"]["ok"] and future["accounting_check"]["ok"]
    assert past["terminal_valuation"]["synthetic_fill_count"] == 0
    audit = past["portfolio_controller"]["audit"]
    assert any(row.get("weekly_snapshot", {}).get("cost_aware_audit", {}).get("enabled")
               for row in audit if row.get("weekly_snapshot"))
