from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from config.config import config
from core.allocation import (
    EntryCandidate,
    PortfolioSignalAllocator,
    joint_entry_exit_attribution,
    state_duration_and_transition_matrix,
)
from core.state import MarketState
from composition.factory import build_router, build_strategy_registry


@dataclass
class _Accepted:
    accepted: bool = True


class _CandidateStrategy:
    def __init__(self, name, sink):
        self.name = name
        self.sink = sink

    def submit_entry_candidate(self, candidate, **_kwargs):
        self.sink.append(candidate.symbol)
        return _Accepted()


def _candidate(symbol, score, sink):
    frame = pd.DataFrame(
        {"close": [100.0]}, index=pd.to_datetime(["2024-01-01"], utc=True)
    )
    strategy = _CandidateStrategy("S", sink)
    return EntryCandidate(symbol, strategy, 0, frame, MarketState.TREND_UP,
                          {"action": "buy"}, score)


def test_t4_9_allocator_ranks_all_same_day_signals_before_submission():
    submitted = []
    candidates = [_candidate("CCC", 0.2, submitted),
                  _candidate("AAA", 0.9, submitted),
                  _candidate("BBB", 0.5, submitted)]
    allocator = PortfolioSignalAllocator()
    allocator.allocate(candidates, portfolio=object(), broker=object(),
                       risk_manager=object(), current_prices={})
    assert submitted == ["AAA", "BBB", "CCC"]


def test_t4_10_allocator_is_invariant_to_symbol_input_order():
    outputs = []
    for order in (["C", "A", "B"], ["B", "C", "A"], ["A", "B", "C"]):
        submitted = []
        candidates = [_candidate(symbol, 1.0, submitted) for symbol in order]
        PortfolioSignalAllocator().allocate(
            candidates, portfolio=object(), broker=object(), risk_manager=object(),
            current_prices={},
        )
        outputs.append(submitted)
    assert outputs == [["A", "B", "C"]] * 3


def test_t4_2_state_analysis_exposes_durations_and_switch_matrix():
    result = state_duration_and_transition_matrix([
        MarketState.SIDEWAYS, MarketState.SIDEWAYS, MarketState.TREND_UP,
        MarketState.TREND_UP, MarketState.VOLATILE,
    ])
    assert result["durations"]["SIDEWAYS"] == [2]
    assert result["transition_matrix"]["SIDEWAYS"]["TREND_UP"] == 1
    assert result["switches"] == 2


def test_t4_12_joint_attribution_separates_entry_and_exit_owners():
    result = joint_entry_exit_attribution([
        {"strategy": "TrendBreakout", "exit_strategy": "TrendBreakout", "net_pnl": 4},
        {"strategy": "TrendBreakout", "exit_strategy": "MaxHoldingPeriod", "net_pnl": -1},
    ])
    assert result["matrix"]["TrendBreakout"]["TrendBreakout"] == {
        "trades": 1, "net_pnl": 4.0,
    }
    assert result["matrix"]["TrendBreakout"]["MaxHoldingPeriod"]["net_pnl"] == -1.0


def test_t4_5_to_t4_7_unadmitted_strategies_are_not_in_portfolio_routes():
    router = build_router(build_strategy_registry(), config, allow_short=True)
    assert router.regime_map["TREND_DOWN"] == "Cash"
    assert router.regime_map["SIDEWAYS"] == "Cash"
    assert router.regime_map["VOLATILE"] == "Cash"
    governance = config.require("strategy_governance")
    assert governance["TrendBreakdown"] == "paused_redesign"
    assert governance["RangeMeanReversion"] == "paused_redesign"
    assert governance["VolatilityReversion"] == "isolated_research"


def test_t4_1_and_t4_11_router_has_explicit_production_contract():
    router = build_router(build_strategy_registry(), config)
    assert callable(router.collect_candidate)
    assert router.max_holding_days == 365.0
    assert config.require("router", "transition_action") == "stop_new_entries"
    assert config.require("allocation", "order") == "score_strategy_symbol"
    assert config.require("state", "stability_candidates") == [2, 3, 5, 10]
