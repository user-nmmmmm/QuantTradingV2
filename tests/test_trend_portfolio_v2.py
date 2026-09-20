"""V2 changes ranking/regime scaling/stops while retaining shared governance."""

from dataclasses import asdict
import math
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pandas as pd
import pytest

from core.allocation import PortfolioSignalAllocator
from core.candidate_scoring import CandidateScorePolicy
from core.portfolio import Portfolio
from core.protective_stops import ProtectiveStopPolicy
from core.risk import RiskManager
from core.signal_observation import strategy_identity
from core.state import MarketState
from core.strategy_health import HealthStatus, StrategyHealthPolicy
from strategies.trend_breakout import TrendBreakoutStrategy
from strategies.trend_portfolio_v2 import TrendPortfolioV2Strategy


SYMBOL = "BTC/USDT"


def _prices(length=150, *, slope=0.5, atr=2.0):
    close = 100.0 + np.arange(length) * slope
    frame = pd.DataFrame({
        "open": close - 0.1,
        "high": close + 0.2,
        "low": close - 0.2,
        "close": close,
        "volume": 1_000.0,
    }, index=pd.date_range("2024-01-01", periods=length, tz="UTC"))
    if atr is not None:
        frame["ATR_14"] = atr
    return frame


def _signal(frame, i=120, strategy=None, state=MarketState.TREND_UP):
    strategy = strategy or TrendPortfolioV2Strategy()
    return strategy.should_enter(SYMBOL, i, frame, state, Portfolio(100_000.0))


def test_default_score_matches_hand_computed_weighted_horizons():
    signal = _signal(_prices())
    expected = {f"trend_{h}": 1.0 for h in (20, 60, 120)}
    assert signal["score_components"] == pytest.approx(expected)
    assert signal["score"] == 1.0
    assert TrendPortfolioV2Strategy().weights == (0.5, 0.3, 0.2)
    assert signal["market_risk_multiplier"] == 1.0


def test_custom_horizons_and_weights_are_normalized_and_components_are_signs():
    strategy = TrendPortfolioV2Strategy(horizons=(20, 60), weights=(1, 3))
    signal = _signal(_prices(slope=5.0), strategy=strategy)
    assert strategy.weights == (0.25, 0.75)
    assert signal["score_components"] == {"trend_20": 1.0, "trend_60": 1.0}
    assert signal["score"] == 1.0
    frame = _prices(slope=-0.5)
    breakdown = strategy._score_signal(frame, 120, channel_level=1.0, side="buy")
    assert breakdown.total == -1.0
    negative = _prices(slope=-0.5, atr=0.01)
    assert strategy._score_signal(negative, 120, channel_level=1.0, side="buy").total == -1.0


def test_multi_horizon_score_drives_existing_allocator_ranking():
    strategy = TrendPortfolioV2Strategy()
    portfolio = Portfolio(100_000.0)
    # Stronger trend deliberately has the later alphabetical symbol.
    weak_frame = _prices()
    weak_frame.loc[weak_frame.index[0], "close"] = 180.0
    weak = strategy.build_entry_candidate(
        "AAA/USDT", 120, weak_frame, MarketState.TREND_UP, portfolio,
    )
    strong = strategy.build_entry_candidate(
        "ZZZ/USDT", 120, _prices(slope=0.5), MarketState.TREND_UP, portfolio,
    )
    assert weak is not None and strong is not None
    assert strong.score > weak.score
    assert PortfolioSignalAllocator.rank([weak, strong]) == [strong, weak]


def test_future_perturbation_and_prefix_have_identical_scores_and_stops():
    original = _prices(180, atr=None)
    prefix = original.iloc[:131].copy()
    altered = original.copy()
    altered.loc[altered.index[131:], ["open", "high", "low", "close"]] *= 10.0
    altered.loc[altered.index[131:], "volume"] *= 25.0
    signals = [_signal(frame, i=130) for frame in (original, prefix, altered)]
    assert all(signal is not None for signal in signals)
    for signal in signals[1:]:
        assert signal["score"] == pytest.approx(signals[0]["score"])
        assert signal["score_components"] == pytest.approx(signals[0]["score_components"])
        assert signal["stop_loss"] == pytest.approx(signals[0]["stop_loss"])
        assert signal["realized_volatility"] == pytest.approx(signals[0]["realized_volatility"])
        assert signal["target_weight"] == pytest.approx(signals[0]["target_weight"])


@pytest.mark.parametrize("scale", [0.001, 1_000.0])
def test_scores_are_price_scale_invariant(scale):
    frame = _prices(atr=None)
    scaled = frame.copy()
    scaled.loc[:, ["open", "high", "low", "close"]] *= scale
    reference, candidate = _signal(frame), _signal(scaled)
    assert reference is not None and candidate is not None
    assert candidate["score"] == pytest.approx(reference["score"])
    assert candidate["score_components"] == pytest.approx(reference["score_components"])
    assert candidate["stop_loss"] == pytest.approx(reference["stop_loss"] * scale)


def test_requires_full_longest_horizon_even_when_shorter_breakout_is_ready():
    strategy = TrendPortfolioV2Strategy()
    frame = _prices()
    for i in (20, 60, 119):
        assert _signal(frame, i=i, strategy=strategy) is None
    assert strategy.raw_setup_count == 0
    assert strategy.raw_entry_signal(SYMBOL, 120, frame) is not None
    assert strategy.raw_setup_count == 0  # passive observation has no health side effects
    assert _signal(frame, strategy=strategy) is not None
    assert strategy.raw_setup_count == 1


def test_atr_warmup_is_required_even_with_short_custom_horizons():
    strategy = TrendPortfolioV2Strategy(entry_window=2, exit_window=2, horizons=(2, 3), volatility_window=5)
    frame = _prices(30, atr=None)
    assert strategy.raw_entry_signal(SYMBOL, 12, frame) is None
    assert strategy.raw_entry_signal(SYMBOL, 13, frame) is not None


@pytest.mark.parametrize("value", [np.nan, np.inf, -np.inf, 0.0, -1.0])
@pytest.mark.parametrize("column,index", [("close", 20), ("high", 30), ("low", 40), ("ATR_14", 120)])
def test_invalid_data_inside_required_window_never_becomes_partial_score(column, index, value):
    frame = _prices()
    frame.loc[frame.index[index], column] = value
    strategy = TrendPortfolioV2Strategy()
    assert _signal(frame, strategy=strategy) is None
    assert strategy.raw_setup_count == 0


def test_invalid_current_price_is_rejected_before_a_candidate_is_emitted():
    frame = _prices()
    frame.loc[frame.index[120], "close"] = np.inf
    assert _signal(frame) is None


@pytest.mark.parametrize("column,index", [("close", 20), ("close", 120), ("ATR_14", 120)])
@pytest.mark.parametrize("value", [pd.NA, "invalid"])
def test_malformed_prices_and_atr_reject_without_indicator_exceptions(column, index, value):
    frame = _prices()
    frame[column] = frame[column].astype(object)
    frame.loc[frame.index[index], column] = value
    assert _signal(frame) is None


@pytest.mark.parametrize("column", ["high", "low", "close"])
def test_missing_required_price_columns_reject_without_partial_score(column):
    assert _signal(_prices().drop(columns=column)) is None


def test_obv_gate_and_donchian_breakout_gate_are_preserved():
    frame = _prices()
    frame["OBV"] = -np.arange(len(frame), dtype=float)
    assert _signal(frame) is None
    assert _signal(frame.copy(), strategy=TrendPortfolioV2Strategy(use_obv=False)) is not None
    no_breakout = _prices()
    no_breakout.loc[no_breakout.index[119], "high"] = 200.0
    assert _signal(no_breakout) is None


def test_legacy_score_policy_cannot_silently_disable_v2_horizon_ranking():
    strategy = TrendPortfolioV2Strategy()
    strategy.configure_score_policy(CandidateScorePolicy(enabled=False))
    signal = _signal(_prices(), strategy=strategy)
    assert len(signal["score_components"]) == 3
    assert signal["score"] > 0


@pytest.mark.parametrize("state,multiplier", [
    (MarketState.TREND_UP, 1.0),
    (MarketState.SIDEWAYS, 0.25),
    (MarketState.VOLATILE, 0.5),
])
def test_market_states_scale_entry_risk_without_forcing_regime_exit(state, multiplier):
    strategy = TrendPortfolioV2Strategy()
    frame, portfolio = _prices(), Portfolio(100_000.0)
    frame.loc[frame.index[120], ["close", "high"]] += 2.0
    candidate = strategy.build_entry_candidate(SYMBOL, 120, frame, state, portfolio)
    assert candidate is not None
    assert candidate.signal["market_risk_multiplier"] == multiplier
    assert strategy.entry_risk_multiplier(state) == multiplier
    assert strategy.health_risk_multiplier() == 1.0
    assert strategy.should_exit(SYMBOL, 120, frame, state, portfolio) is None


def test_no_trade_remains_nontradable_and_preserves_defensive_exit():
    strategy = TrendPortfolioV2Strategy()
    frame, portfolio = _prices(), Portfolio(100_000.0)
    assert strategy.entry_risk_multiplier(MarketState.NO_TRADE) == 0.0
    assert strategy.build_entry_candidate(SYMBOL, 120, frame, MarketState.NO_TRADE, portfolio) is None
    assert strategy.should_exit(SYMBOL, 120, frame, MarketState.NO_TRADE, portfolio)["action"] == "sell"


def test_initial_stop_is_pure_two_atr_even_with_tighter_donchian_level():
    signal = _signal(_prices(atr=4.0))
    # close = 160; ATR stop = 152; structural low = 154.8 would win a hybrid.
    assert signal["stop_loss"] == pytest.approx(152.0)
    assert signal["stop_plan"]["structural_stop"] == pytest.approx(154.8)
    assert signal["stop_plan"]["method"] == "atr"
    assert signal["stop_plan"]["resolved_initial_stop_mode"] == "atr"


def test_injected_stop_policy_retains_every_unrelated_safeguard():
    supplied = ProtectiveStopPolicy(
        atr_period=21, initial_stop_mode="hybrid", initial_atr_multiple=3.0,
        trailing_atr_multiple=4.0, min_stop_distance_pct=0.02,
        max_stop_distance_pct=0.15, breakeven_after_r=3.0, breakeven_cost_buffer=0.4,
    )
    strategy = TrendPortfolioV2Strategy()
    strategy.configure_stop_policy(supplied)
    expected = asdict(supplied) | {
        "initial_stop_mode": "atr", "initial_atr_multiple": 2.0,
        "trailing_atr_multiple": 2.5, "use_atr_initial_stop": True,
        "use_trailing_stop": True,
    }
    assert asdict(strategy.stop_policy) == expected
    assert strategy.stop_policy is not supplied
    assert supplied.initial_stop_mode == "hybrid"
    assert TrendBreakoutStrategy().stop_policy == ProtectiveStopPolicy()


def test_trailing_stop_uses_two_point_five_atr_and_never_loosens():
    strategy = TrendPortfolioV2Strategy()
    frame = _prices()
    strategy.context[SYMBOL] = {"stop_loss": 140.0, "initial_stop": 140.0, "entry_price": 145.0}
    strategy.should_exit(SYMBOL, 120, frame, MarketState.SIDEWAYS, Portfolio(100_000.0))
    assert strategy.context[SYMBOL]["stop_loss"] == pytest.approx(160.2 - 2.5 * 2.0)
    frame.loc[frame.index[121], "ATR_14"] = 20.0
    strategy.should_exit(SYMBOL, 121, frame, MarketState.TREND_DOWN, Portfolio(100_000.0))
    assert strategy.context[SYMBOL]["stop_loss"] == pytest.approx(155.2)


def test_inherited_exit_flow_checks_old_stop_before_raising_next_bar_trail():
    strategy = TrendPortfolioV2Strategy()
    frame = _prices()
    # Low 159.8 clears the old stop of 150 but breaches today's new 170 trail.
    frame.loc[frame.index[120], "high"] = 175.0
    strategy.context[SYMBOL] = {"stop_loss": 150.0, "entry_price": 152.0, "entry_bar": 115}
    portfolio = SimpleNamespace(get_position=lambda symbol: {"qty": 1.0})
    broker = SimpleNamespace(event_pipeline=None, submit_order=Mock(return_value=SimpleNamespace(accepted=True)))
    strategy._consume_execution_trades = Mock()
    assert strategy.process_exit_only(SYMBOL, 120, frame, MarketState.TREND_UP, portfolio, broker) is None
    assert strategy.context[SYMBOL]["stop_loss"] == pytest.approx(170.0)
    broker.submit_order.assert_not_called()
    strategy.process_exit_only(SYMBOL, 121, frame, MarketState.TREND_UP, portfolio, broker)
    assert broker.submit_order.call_args.kwargs["exit_reason"] == "hard_stop"


def test_baseline_ablation_preserves_ten_bar_donchian_exit():
    frame = _prices()
    frame.loc[frame.index[120], "close"] = 150.0
    exit_signal = TrendPortfolioV2Strategy(exit_mode="baseline").should_exit(
        SYMBOL, 120, frame, MarketState.SIDEWAYS, Portfolio(100_000.0),
    )
    assert exit_signal["reason"] == "Breakout Exit (Below Low10)"


def test_health_identity_is_independent_and_lifecycle_policy_is_unchanged():
    baseline, strategy = TrendBreakoutStrategy(), TrendPortfolioV2Strategy()
    assert strategy.health_state_key == "strategy_health:TrendPortfolioV2"
    assert strategy.health_state_key != baseline.health_state_key
    assert strategy.health.policy == baseline.health.policy
    policy = StrategyHealthPolicy(consecutive_negative_cohorts=2, cooldown_days=2)
    for instance in (baseline, strategy):
        instance.configure_health_policy(policy)
        for day in (1, 2):
            instance.on_trade_closed(SYMBOL, -10.0, {
                "close_event_id": f"loss-{day}", "initial_risk": 100.0,
                "exit_reason": "signal", "timestamp": f"2024-04-{day:02d}T00:00:00Z",
            }, day)
        assert not instance.check_health("2024-04-02T00:00:00Z")
        assert instance.health.status == HealthStatus.COOLDOWN
        assert instance.check_health("2024-04-05T00:00:00Z")
        assert instance.health.status == HealthStatus.PROBATION
        assert instance.health_risk_multiplier() == policy.probation_risk_multiplier
    strategy.health.manual_lock("test lock", at="2024-04-05T00:00:00Z")
    assert _signal(_prices(), strategy=strategy) is None
    assert strategy.raw_setup_count == strategy.suppressed_setup_count == 1
    assert baseline.health.status == HealthStatus.PROBATION
    assert strategy.raw_entry_signal(SYMBOL, 120, _prices()) is not None
    assert strategy.raw_setup_count == 1


def test_strategy_identity_includes_custom_horizons_and_weights():
    baseline = strategy_identity(TrendPortfolioV2Strategy())
    assert baseline == strategy_identity(TrendPortfolioV2Strategy())
    assert baseline != strategy_identity(TrendPortfolioV2Strategy(horizons=(10, 30, 60)))
    assert baseline != strategy_identity(TrendPortfolioV2Strategy(weights=(1, 2, 3)))


@pytest.mark.parametrize("kwargs", [
    {"horizons": ()}, {"horizons": (0, 20)}, {"horizons": (-1, 20)},
    {"horizons": (20, 20)}, {"horizons": (1.5, 20)}, {"horizons": (True, 20)},
    {"weights": (1, 2)}, {"weights": (0, 1, 2)}, {"weights": (-1, 1, 2)},
    {"weights": (np.nan, 1, 2)}, {"weights": (np.inf, 1, 2)},
    {"weights": (True, 1, 2)}, {"weights": (1e308, 1e308, 1e308)},
])
def test_invalid_horizon_configuration_fails_explicitly(kwargs):
    with pytest.raises(ValueError):
        TrendPortfolioV2Strategy(**kwargs)


@pytest.mark.parametrize("sixty_prior,slow_prior,expected,allowed", [
    (130.0, 100.0, 1.0, True),
    (200.0, 100.0, 0.4, True),
    (160.0, 200.0, 0.3, False),
    (200.0, 200.0, 0.0, False),
])
def test_trend_gate_is_strict_and_uses_weighted_signs(sixty_prior, slow_prior, expected, allowed):
    frame = _prices()
    frame.loc[frame.index[60], "close"] = sixty_prior
    frame.loc[frame.index[0], "close"] = slow_prior
    strategy = TrendPortfolioV2Strategy(use_obv=False)
    score = strategy._score_signal(frame, 120, channel_level=None, side="buy")
    assert score.total == expected
    assert (strategy.raw_entry_signal(SYMBOL, 120, frame) is not None) is allowed


def test_no_new_longs_in_downtrend_and_hard_gate_is_a_separate_ablation():
    frame = _prices()
    frame.loc[frame.index[120], ["close", "high"]] += 2.0
    strategy = TrendPortfolioV2Strategy()
    assert strategy.entry_risk_multiplier(MarketState.TREND_DOWN) == 0.0
    assert _signal(frame, state=MarketState.TREND_DOWN, strategy=strategy) is None
    hard = TrendPortfolioV2Strategy(market_state_mode="hard_gate")
    for state in (MarketState.SIDEWAYS, MarketState.VOLATILE, MarketState.TREND_DOWN):
        assert _signal(frame, state=state, strategy=hard) is None
    assert _signal(frame, state=MarketState.TREND_UP, strategy=hard) is not None


def test_sideways_requires_both_strong_breakout_and_score_confirmation():
    frame = _prices()
    assert _signal(frame, state=MarketState.SIDEWAYS) is None  # only .15 ATR above channel
    frame.loc[frame.index[120], ["close", "high"]] += 2.0
    assert _signal(frame.copy(), state=MarketState.SIDEWAYS) is not None
    frame.loc[frame.index[60], "close"] = 200.0  # score=.4 still long-eligible
    assert _signal(frame.copy(), state=MarketState.TREND_UP) is not None
    assert _signal(frame.copy(), state=MarketState.SIDEWAYS) is None


def test_realized_volatility_uses_completed_log_returns_and_explicit_annualization():
    frame = _prices()
    expected = np.std(np.diff(np.log(frame["close"].iloc[100:121])), ddof=1) * math.sqrt(365)
    strategy = TrendPortfolioV2Strategy()
    assert strategy._realized_volatility(frame, 120) == pytest.approx(expected)
    four_hour = TrendPortfolioV2Strategy(periods_per_year=2190)
    assert four_hour._realized_volatility(frame, 120) == pytest.approx(expected * math.sqrt(6))


def test_volatility_target_decreases_weight_in_higher_volatility(monkeypatch):
    frame = _prices()
    strategy = TrendPortfolioV2Strategy()
    monkeypatch.setattr(strategy, "_realized_volatility", lambda df, i: 0.50)
    low = _signal(frame.copy(), strategy=strategy)
    monkeypatch.setattr(strategy, "_realized_volatility", lambda df, i: 1.0)
    high = _signal(frame.copy(), strategy=strategy)
    assert low["target_weight"] == pytest.approx(0.10)
    assert high["target_weight"] == pytest.approx(0.05)
    monkeypatch.setattr(strategy, "_realized_volatility", lambda df, i: 0.01)
    assert _signal(frame.copy(), strategy=strategy)["target_weight"] == 0.25


def test_volatility_weight_includes_trend_confidence_and_base_asset_budget(monkeypatch):
    frame = _prices()
    frame.loc[frame.index[60], "close"] = 200.0  # score .4
    strategy = TrendPortfolioV2Strategy(asset_base_weight=1 / 6)
    monkeypatch.setattr(strategy, "_realized_volatility", lambda df, i: 0.5)
    signal = _signal(frame, strategy=strategy)
    assert signal["target_weight"] == pytest.approx((1 / 6) * .4 * .10 / .5)


def test_zero_realized_volatility_rejects_instead_of_dividing_or_leveraging():
    frame = _prices()
    close = 100 * np.exp(np.arange(len(frame)) * .01)
    frame.loc[:, ["open", "high", "low", "close"]] = np.column_stack((close, close + .2, close - .2, close))
    strategy = TrendPortfolioV2Strategy()
    assert strategy._realized_volatility(frame, 120) is None
    assert _signal(frame, strategy=strategy) is None
    assert _signal(frame, strategy=TrendPortfolioV2Strategy(volatility_sizing=False)) is not None


@pytest.mark.parametrize("stop,weight,expected", [(99.0, .8, 25.0), (90.0, .25, 10.0), (99.0, .05, 5.0)])
def test_sizing_enforces_weight_and_one_percent_initial_risk(stop, weight, expected):
    strategy = TrendPortfolioV2Strategy()
    quantity = strategy.initial_entry_quantity(
        signal={"target_weight": weight}, equity=10000, current_price=100,
        stop_loss=stop, risk_manager=RiskManager(risk_per_trade=.02),
    )
    assert quantity == pytest.approx(expected)


def test_sizing_respects_tighter_shared_risk_limit():
    quantity = TrendPortfolioV2Strategy().initial_entry_quantity(
        signal={"target_weight": .25}, equity=10000, current_price=100,
        stop_loss=90, risk_manager=RiskManager(risk_per_trade=.005),
    )
    assert quantity == 5.0


def test_default_exit_uses_score_reversal_instead_of_short_channel():
    frame = _prices()
    frame.loc[frame.index[120], "close"] = 150.0  # r20=0; medium+slow still positive
    strategy = TrendPortfolioV2Strategy()
    assert strategy.should_exit(SYMBOL, 120, frame, MarketState.TREND_UP, Portfolio(10000)) is None
    frame.loc[frame.index[120], "close"] = 110.0  # -.5-.3+.2 = -.6
    assert strategy.should_exit(SYMBOL, 120, frame, MarketState.TREND_UP, Portfolio(10000))["reason"] == "TrendScoreReversal"


def test_medium_channel_exit_operates_even_with_positive_ensemble():
    frame = _prices()
    frame.loc[frame.index[0], "close"] = 110.0
    frame.loc[frame.index[100], "close"] = 120.0
    frame.loc[frame.index[120], "close"] = 125.0
    # Positive score .5-.3+.2=.4, but close below prior60 lowest low129.8.
    strategy = TrendPortfolioV2Strategy()
    assert strategy.should_exit(SYMBOL, 120, frame, MarketState.TREND_UP, Portfolio(10000))["reason"] == "MediumChannelExit60"


def test_hybrid_stop_arm_uses_tighter_structural_level():
    signal = _signal(_prices(atr=4), strategy=TrendPortfolioV2Strategy(exit_mode="hybrid"))
    assert signal["stop_plan"]["method"] == "hybrid_max"
    assert signal["stop_loss"] == pytest.approx(154.8)


@pytest.mark.parametrize("kwargs", [
    {"market_state_mode": "automatic"}, {"exit_mode": "best"},
    {"volatility_window": 1}, {"periods_per_year": 0},
    {"target_annual_volatility": float("nan")}, {"max_asset_weight": 2},
    {"max_initial_risk": 0}, {"long_score_threshold": 1},
    {"sideways_breakout_atr": -1}, {"volatility_sizing": "false"},
])
def test_invalid_research_settings_fail_explicitly(kwargs):
    with pytest.raises(ValueError):
        TrendPortfolioV2Strategy(**kwargs)
