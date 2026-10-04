"""Same-batch changes in real allocation, with the live graph untouched."""
from types import SimpleNamespace

import pandas as pd
import pytest

from backtest.execution_adapter import SimulatedExecutionAdapter
from core.allocation import EntryCandidate, PortfolioSignalAllocator
from core.broker import Broker
from core.portfolio import Portfolio
from core.risk import RiskManager
from core.risk.portfolio_governor import CorrelationClusterPolicy, PortfolioRiskGovernor
from core.state import MarketState
from research.ml_selection.batch_comparison import BatchAllocationComparisonSelector, copy_allocation_graph
from strategies.base import Strategy


class OneUnit(Strategy):
    def __init__(self):
        super().__init__("Original", set(MarketState))

    def should_enter(self, *args):
        return {"action": "buy", "stop_loss": 90.}

    def should_exit(self, *args):
        return None

    def initial_entry_quantity(self, **kwargs):
        return 1.


class PassThrough:
    def __init__(self):
        self.received = []
        self.audit = ["original audit remains delegated"]

    def select(self, candidates, **context):
        self.received.append((list(candidates), context))
        return list(candidates)


def batch(*, constrained=True, wrapped=True):
    portfolio = Portfolio(initial_capital=1000.)
    broker = Broker(portfolio, commission_rate=0., commission_rate_maker=0., timeframe="1d")
    strategy = OneUnit()
    risk = RiskManager(max_leverage=1., max_pos_size_pct=1., min_entry_notional_pct=0.)
    policy = CorrelationClusterPolicy(max_same_session_entry_risk=.01 if constrained else None,
                                      enabled=constrained)
    allocator = PortfolioSignalAllocator(PortfolioRiskGovernor(policy))
    frame = pd.DataFrame({"open": [100.], "high": [102.], "low": [99.], "close": [100.], "volume": [1e6]},
                         index=pd.DatetimeIndex(["2024-01-01"]))
    candidates = [EntryCandidate(symbol, strategy, 0, frame, next(iter(MarketState)),
                                  {"action": "buy", "order_type": "market", "stop_loss": 90.}, score, audit={})
                  for symbol, score in [("A-USDT", 2.), ("B-USDT", 1.)]]
    context = {"portfolio": portfolio, "broker": SimulatedExecutionAdapter(broker) if wrapped else broker,
               "risk_manager": risk, "current_prices": {"A-USDT": 100., "B-USDT": 100.},
               "event": SimpleNamespace(timestamp=pd.Timestamp("2024-01-01"), timeframe="1d")}
    return candidates, context, allocator


def provider(candidates, context):
    return {"momentum": [1., 2.], "model": [1., 3.]}


@pytest.mark.parametrize("wrapped", [True, False])
def test_joint_graph_copy_preserves_shared_relations_and_event_reservations(wrapped):
    candidates, context, allocator = batch(wrapped=wrapped)
    # Existing pending reservations and their frozen published events must be
    # retained in the independent graph, without deepcopying mappingproxy.
    venue = context["broker"].broker if wrapped else context["broker"]
    venue.submit_order("OLD-USDT", "buy", .1, price=100., timestamp=pd.Timestamp("2023-12-31"),
                       strategy_id="Original", stop_loss=90., approved_risk_amount=1.)
    copied = copy_allocation_graph(candidates, context, allocator)
    cloned = copied["context"]["broker"].broker if wrapped else copied["context"]["broker"]
    assert cloned.portfolio is copied["context"]["portfolio"]
    assert copied["candidates"][0].strategy is copied["candidates"][1].strategy
    assert copied["candidates"][0].frame is copied["candidates"][1].frame
    assert copied["candidates"][0].strategy is not candidates[0].strategy
    assert copied["candidates"][0].frame is not candidates[0].frame
    assert cloned.reservation_projection is not venue.reservation_projection
    subscriber = cloned.event_pipeline._subscribers["*"][0]
    assert subscriber.__self__ is cloned.reservation_projection
    assert len(cloned.pending_orders) == 1
    assert len(cloned.event_pipeline.events) == len(venue.event_pipeline.events)


def test_real_shared_budget_allocation_changes_selection_without_mutating_live_graph():
    candidates, context, allocator = batch()
    original = PassThrough()
    wrapper = BatchAllocationComparisonSelector(original, score_provider=provider, allocator=allocator)
    returned = wrapper.select(candidates, **context)
    assert all(left is right for left, right in zip(returned, candidates))
    assert wrapper.audit is original.audit
    venue = context["broker"].broker
    assert venue.pending_orders == []
    assert venue.trades == []
    assert venue.event_pipeline.events == ()
    assert allocator.audit == []
    assert allocator.risk_governor.session_risk_used == 0.
    assert candidates[0].strategy.context == {}
    assert all(candidate.audit == {} for candidate in candidates)
    comparison = wrapper.comparisons[0]
    assert comparison["status"] == "compared"
    assert comparison["candidate_keys"] == ["Original|A-USDT", "Original|B-USDT"]
    assert comparison["arms"]["native"]["approved_quantities"] == pytest.approx({"Original|A-USDT": 1.})
    assert comparison["arms"]["model"]["approved_quantities"] == pytest.approx({"Original|B-USDT": 1.})
    assert comparison["arms"]["model"]["changed_selected_set_from_native"]
    assert comparison["ranking_changed_approval_or_quantity"]
    assert comparison["arms"]["native"]["budget_limited"]
    assert all(arm["additional_fill_count"] == 0 for arm in comparison["arms"].values())
    assert wrapper.comparison_summary()["portfolio_return_difference"] is None


def test_unconstrained_ranking_change_is_not_claimed_as_allocation_value():
    candidates, context, allocator = batch(constrained=False)
    wrapper = BatchAllocationComparisonSelector(PassThrough(), score_provider=provider, allocator=allocator)
    wrapper.select(candidates, **context)
    comparison = wrapper.comparisons[0]
    assert comparison["status"] == "compared"
    assert not comparison["ranking_changed_approval_or_quantity"]
    assert comparison["arms"]["native"]["approved_quantities"] == comparison["arms"]["model"]["approved_quantities"]
    assert not any(arm["budget_limited"] for arm in comparison["arms"].values())


def test_comparison_budget_is_frozen_and_failure_cannot_change_actual_selection():
    candidates, context, allocator = batch()
    delegate = PassThrough()
    wrapper = BatchAllocationComparisonSelector(delegate, score_provider=lambda *args: {"model": [1, 2]},
                                                 allocator=allocator, max_batches=1)
    for _ in range(3):
        assert wrapper.select(candidates, **context) == candidates
    assert len(delegate.received) == 3
    assert len(wrapper.comparisons) == 1
    assert wrapper.comparisons[0]["status"] == "unmeasured"
    assert wrapper.comparison_summary()["competitive_batches_seen"] == 3
    assert wrapper.comparison_summary()["matching_executed"] is False


def test_shared_eligibility_is_applied_once_without_arm_specific_gates():
    candidates, context, allocator = batch()
    wrapper = BatchAllocationComparisonSelector(PassThrough(), score_provider=provider, allocator=allocator,
                    qualification_provider=lambda candidates, context: candidates[:1])
    assert wrapper.select(candidates, **context) == candidates
    assert wrapper.comparisons == []
    with pytest.raises(ValueError, match="positive"):
        BatchAllocationComparisonSelector(PassThrough(), score_provider=provider, allocator=allocator, max_batches=0)


def test_live_engine_hook_comparison_preserves_baseline_fills_and_equity():
    from copy import deepcopy
    from config.config import config
    from research.ml_selection.environment import FullEngineEnvironment

    settings = deepcopy(config._config)
    settings["account"]["mode"] = "spot"
    settings["execution"]["fee_schedule"]["market_type"] = "spot"
    settings["router"]["cooldown_bars"] = 0
    settings["portfolio_targets"] = {"enabled": False}
    settings["strategy_health"]["enabled"] = False
    settings["portfolio_risk"]["enabled"] = False
    settings["drawdown_budget"]["enabled"] = False
    settings["risk"]["max_leverage"] = 1.
    settings["routing"] = {state.name: "Original" for state in MarketState}
    frame = pd.DataFrame({"open": [100., 100., 101.], "high": [102., 103., 105.], "low": [99., 99., 99.],
                          "close": [100., 101., 103.], "volume": [1e6] * 3},
                         index=pd.date_range("2024-01-01", periods=3))
    environment = FullEngineEnvironment({"A-USDT": frame, "B-USDT": frame.copy()},
        strategies={"Original": OneUnit()}, parameters=settings,
        engine_options={"initial_capital": 1000., "warmup_period": 0, "slippage": 0.})
    baseline = environment.run_episode(PassThrough())
    wrapper = BatchAllocationComparisonSelector(PassThrough(), score_provider=provider,
        allocator=PortfolioSignalAllocator(), max_batches=1)
    measured = environment.run_episode(wrapper)
    pd.testing.assert_frame_equal(measured.result["equity_curve"], baseline.result["equity_curve"])
    assert measured.result["trades"] == baseline.result["trades"]
    assert wrapper.comparisons[0]["status"] == "compared"
    assert wrapper.comparisons[0]["matching_executed"] is False
