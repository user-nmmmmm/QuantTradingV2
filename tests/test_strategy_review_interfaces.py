"""Review research arms keep explicit stop semantics and cannot route live risk."""

from __future__ import annotations

import pandas as pd
import pytest

from composition.factory import build_strategy_registry
from core.broker import Broker
from core.candidate_scoring import CandidateScorePolicy
from core.portfolio import Portfolio
from core.protective_stops import (
    ProtectiveStopPolicy, plan_initial_stop, update_trailing_stop,
)
from core.state import MarketState
from core.strategy_governance import GovernanceError, assert_live_admission
from strategies.trend_breakout import TrendBreakoutStrategy
from strategies.volatility import VolatilityReversionStrategy


class _Configuration:
    def __init__(self, **sections):
        self.sections = sections

    def get(self, section, key=None):
        value = self.sections.get(section)
        return value if key is None else (value or {}).get(key)

    def require(self, section, key=None):
        value = self.sections[section]
        return value if key is None else value[key]


def _stop(policy, *, side="long", structural=98.0, atr=3.0):
    return plan_initial_stop(
        side=side, reference_price=100.0, structural_stop=structural,
        atr=atr, policy=policy,
    )


@pytest.mark.parametrize("legacy,explicit", [(False, "structural_donchian"), (True, "hybrid")])
def test_legacy_stop_policy_retains_its_exact_behavior(legacy, explicit):
    old = ProtectiveStopPolicy(use_atr_initial_stop=legacy)
    new = ProtectiveStopPolicy(initial_stop_mode=explicit)
    assert old.resolved_initial_stop_mode == explicit
    assert _stop(old).to_dict() == _stop(new).to_dict()


@pytest.mark.parametrize("side,structural", [("long", 98.0), ("short", 102.0)])
@pytest.mark.parametrize("mode,expected_long,expected_short", [
    ("structural_donchian", 98.0, 102.0),
    ("atr", 94.0, 106.0),
    ("hybrid", 98.0, 102.0),
])
@pytest.mark.parametrize("legacy", [False, True])
def test_explicit_mode_wins_boolean_and_atr_does_not_use_structural_leg(
    side, structural, mode, expected_long, expected_short, legacy,
):
    result = _stop(
        ProtectiveStopPolicy(initial_stop_mode=mode, use_atr_initial_stop=legacy),
        side=side, structural=structural,
    )
    assert result.stop_price == (expected_long if side == "long" else expected_short)
    assert result.to_dict()["resolved_initial_stop_mode"] == mode


@pytest.mark.parametrize("side,structural", [("long", 90.0), ("short", 110.0)])
@pytest.mark.parametrize("atr", [None, 0.0, -1.0, float("nan"), float("inf")])
def test_atr_only_rejects_missing_or_invalid_atr_with_valid_structural_stop(side, structural, atr):
    result = _stop(
        ProtectiveStopPolicy(initial_stop_mode="atr"),
        side=side, structural=structural, atr=atr,
    )
    assert not result.accepted
    assert result.reject_reason == "no_valid_stop_level"
    assert result.resolved_initial_stop_mode == "atr"


@pytest.mark.parametrize("mode", ["structural_donchian", "atr", "hybrid"])
def test_all_stop_modes_share_risk_distance_limits(mode):
    policy = ProtectiveStopPolicy(
        initial_stop_mode=mode, min_stop_distance_pct=0.01, max_stop_distance_pct=0.20,
    )
    assert not _stop(policy, structural=99.8, atr=0.1).accepted
    far = _stop(policy, structural=50.0, atr=25.0)
    assert far.accepted and far.stop_price == 80.0
    assert far.method == "clamped_max_distance"


def test_invalid_initial_stop_mode_fails_before_execution():
    with pytest.raises(ValueError, match="initial_stop_mode"):
        ProtectiveStopPolicy.from_mapping({"initial_stop_mode": "typo"})


def _frame():
    frame = pd.DataFrame(
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 1000.0},
        index=pd.date_range("2024-01-01", periods=40, freq="D", tz="UTC"),
    )
    frame.loc[frame.index[-1], ["high", "low", "close"]] = [121.0, 119.0, 120.0]
    return frame


def test_explicit_atr_mode_computes_atr_without_legacy_switch_or_scoring():
    strategy = TrendBreakoutStrategy(use_obv=False)
    strategy.configure_score_policy(CandidateScorePolicy(enabled=False))
    strategy.configure_stop_policy(ProtectiveStopPolicy(initial_stop_mode="atr"))
    frame = _frame()
    signal = strategy.should_enter("BTC/USDT", 39, frame, MarketState.TREND_UP, Portfolio())
    assert signal is not None
    expected = 120.0 - 2 * frame["ATR_14"].iat[39]
    assert signal["stop_loss"] == pytest.approx(expected)
    assert signal["stop_plan"]["resolved_initial_stop_mode"] == "atr"


@pytest.mark.parametrize("mode", ["structural_donchian", "atr", "hybrid"])
def test_initial_stop_modes_preserve_donchian_and_regime_exits(mode):
    strategy = TrendBreakoutStrategy(use_obv=False)
    strategy.configure_stop_policy(ProtectiveStopPolicy(initial_stop_mode=mode))
    frame = _frame()
    frame.loc[frame.index[-1], "close"] = 90.0
    signal = strategy.should_exit("BTC/USDT", 39, frame, MarketState.TREND_UP, Portfolio())
    assert signal["action"] == "sell" and "Below Low10" in signal["reason"]
    frame.loc[frame.index[-1], "close"] = 120.0
    signal = strategy.should_exit("BTC/USDT", 39, frame, MarketState.SIDEWAYS, Portfolio())
    assert signal["action"] == "sell" and "Not Allowed" in signal["reason"]


@pytest.mark.parametrize("side,initial,extreme,expected", [
    ("long", 90.0, 130.0, 118.0), ("short", 110.0, 70.0, 82.0),
])
def test_trailing_stop_ratchets_and_does_not_give_back_on_wider_atr(side, initial, extreme, expected):
    policy = ProtectiveStopPolicy(initial_stop_mode="atr", use_trailing_stop=True)
    level = update_trailing_stop(
        side=side, current_stop=initial, initial_stop=initial,
        extreme_since_fill=extreme, atr=4.0, policy=policy,
    )
    assert level == expected
    assert update_trailing_stop(
        side=side, current_stop=level, initial_stop=initial,
        extreme_since_fill=extreme, atr=20.0, policy=policy,
    ) == expected


@pytest.mark.parametrize("qty,expected_side", [(1.0, "sell"), (-1.0, "cover")])
def test_volatility_regime_exit_passes_base_validation_and_fills_next_bar(qty, expected_side):
    symbol = "BTC/USDT"
    strategy = VolatilityReversionStrategy()
    portfolio = Portfolio(account_mode="spot_margin")
    frame = _frame()
    portfolio.update_position(
        symbol, qty, 100.0, strategy_id=strategy.name, time=frame.index[0],
    )
    broker = Broker(portfolio, commission_rate=0.0)
    result = strategy.process_exit_only(
        symbol, 30, frame, MarketState.SIDEWAYS, portfolio, broker,
    )
    assert result is not None and result.accepted
    order, = broker.pending_orders
    assert order.side == expected_side
    assert order.qty == abs(qty)
    assert strategy.get_context(symbol)["exit_pending"]
    assert broker.process_orders({symbol: frame.iloc[30]}) == []
    assert portfolio.get_position(symbol)["qty"] == qty
    broker.process_orders({symbol: frame.iloc[31]})
    assert portfolio.get_position(symbol)["qty"] == 0.0
    assert broker.close_events[-1].opening_strategy_id == strategy.name


@pytest.mark.parametrize("research", [
    {"experiment_id": "review/arm_01"}, {"experiment_id": ""},
    {"review_overrides": {}}, {"strategy_review": {}},
    {"trend_breakout_parameters": {"entry_window": 10, "exit_window": 5}},
    {"strategy_ablation": "no_health"},
])
def test_admitted_strategy_still_cannot_route_research_configuration(research):
    configuration = _Configuration(
        research=research, strategy_governance={"TrendBreakout": "admitted"},
    )
    with pytest.raises(GovernanceError, match="research-only"):
        assert_live_admission(configuration, ["TrendBreakout"])


def test_production_admission_without_research_identity_stays_available():
    configuration = _Configuration(strategy_governance={"TrendBreakout": "admitted"})
    assert assert_live_admission(configuration, ["TrendBreakout"]) == {
        "TrendBreakout": "admitted",
    }


def test_research_windows_are_set_before_indicator_columns_and_defaults_stay_unchanged():
    baseline = build_strategy_registry()["TrendBreakout"]
    assert (baseline.entry_window, baseline.exit_window) == (20, 10)
    research = build_strategy_registry(_Configuration(research={
        "experiment_id": "review/breakout_10_5",
        "trend_breakout_parameters": {"entry_window": 10, "exit_window": 5},
    }))["TrendBreakout"]
    frame = _frame()
    research._ensure_indicators(frame)
    assert (research.col_high_max, research.col_low_min) == ("HIGH_MAX_10", "LOW_MIN_5")
    assert frame["HIGH_MAX_10"].iat[10] == 101.0
    assert frame["LOW_MIN_5"].iat[5] == 99.0


@pytest.mark.parametrize("parameters", [
    {"entry_window": 10, "exit_window": 10},
    {"entry_window": 5, "exit_window": 10},
    {"entry_window": 10, "exit_window": 0},
    {"entry_window": 10, "exit_window": True},
    {"entry_window": 10.0, "exit_window": 5},
    {"entry_window": 10},
    {"entry_window": 10, "exit_window": 5, "unknown": 1},
])
def test_research_windows_reject_invalid_or_ambiguous_parameters(parameters):
    with pytest.raises(ValueError):
        build_strategy_registry(_Configuration(research={
            "experiment_id": "review/invalid", "trend_breakout_parameters": parameters,
        }))


@pytest.mark.parametrize("experiment_id", [None, "", " ", 1])
def test_research_windows_require_explicit_experiment_identity(experiment_id):
    with pytest.raises(ValueError, match="experiment_id"):
        build_strategy_registry(_Configuration(research={
            "experiment_id": experiment_id,
            "trend_breakout_parameters": {"entry_window": 10, "exit_window": 5},
        }))
