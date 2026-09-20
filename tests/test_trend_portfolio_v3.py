"""V3 causal selection, covariance targets, stop/health and opt-in contracts."""

from dataclasses import replace
import math

import numpy as np
import pandas as pd
import pytest

from composition.factory import build_strategy_registry
from core.portfolio import Portfolio
from core.protective_stops import ProtectiveStopPolicy
from core.selection_v2 import (
    SelectionPolicyV2, completed_history, select_all_qualified, signal_facts,
    size_portfolio_targets,
)
from core.state import MarketState
from core.strategy_health import HealthStatus
from strategies.trend_portfolio_v3 import TrendPortfolioV3Strategy


def frame(length=240, phase=0.0, start="2023-01-01"):
    days = np.arange(length)
    close = 100 * np.exp(np.cumsum(.004 + .009 * np.sin(days / 6 + phase)))
    return pd.DataFrame({"open": close * .999, "high": close * 1.004,
                         "low": close * .996, "close": close, "volume": 100000.,
                         "quote_volume": 8_000_000.},
                        index=pd.date_range(start, periods=length, tz="UTC"))


def metadata(symbols):
    return {symbol: {"listing_effective_at": "2022-01-01T00:00:00Z",
                     "listing_available_at": "2022-01-01T00:00:00Z",
                     "classification": "crypto", "classification_available_at": "2022-01-01T00:00:00Z",
                     "source_status": "verified", "cluster": f"cluster-{i % 3}"}
            for i, symbol in enumerate(symbols)}


def selection(count=12, *, policy=None, held=()):
    histories = {f"COIN{i}/USDT": frame(phase=i / 5) for i in range(count)}
    as_of = next(iter(histories.values())).index[-1] + pd.Timedelta(days=1)
    return select_all_qualified(histories, metadata(histories), as_of=as_of,
                                held_symbols=held, policy=policy)


@pytest.mark.parametrize("kwargs", [{"top_n": 3}, {"buffer_ranks": 0}, {"selection_mode": "top_n"},
                                    {"horizons": (60, 60)}, {"horizons": (True, 120)},
                                    {"variant": "best"}, {"min_quote_volume": float("nan")}])
def test_all_qualified_contract_forbids_rank_truncation_and_invalid_rules(kwargs):
    with pytest.raises(ValueError):
        SelectionPolicyV2(**kwargs)


def test_all_qualified_means_more_than_ten_symbols_get_positive_targets():
    result = selection(19)
    assert len(result.selected_symbols) == 19
    targets = size_portfolio_targets(result)
    assert set(targets.target_weights) == set(result.selected_symbols)
    assert all(value > 0 for value in targets.unconstrained_weights.values())
    assert all(value > 0 for value in targets.target_weights.values())


@pytest.mark.parametrize("length,qualified", [(120, False), (121, True)])
def test_120_day_return_requires_121_complete_closes(length, qualified):
    history = frame(length)
    facts = signal_facts(history, as_of=history.index[-1] + pd.Timedelta(days=1))
    assert facts["qualified"] is qualified


def test_neighbor_160_day_horizon_requires_161_closes():
    policy = SelectionPolicyV2(horizons=(80, 160))
    history = frame(160)
    assert "insufficient_contiguous_history" in signal_facts(
        history, as_of=history.index[-1] + pd.Timedelta(days=1), policy=policy)["reasons"]
    assert policy.required_closes == 161


def test_last_twenty_actual_quote_volumes_are_median_filtered_without_substitution():
    history = frame()
    now = history.index[-1] + pd.Timedelta(days=1)
    history.loc[history.index[-11:], "quote_volume"] = 4_999_999
    assert "insufficient_quote_volume" in signal_facts(history, as_of=now)["reasons"]
    history = history.drop(columns="quote_volume")
    assert "missing_actual_quote_volume" in signal_facts(history, as_of=now)["reasons"]


@pytest.mark.parametrize("kind", ["stablecoin", "fiat", "leveraged", "unknown"])
def test_excluded_and_unknown_classifications_fail_closed(kind):
    histories = {"BTC/USDT": frame()}
    meta = metadata(histories)
    meta["BTC/USDT"]["classification"] = kind
    result = select_all_qualified(histories, meta, as_of="2023-08-29")
    assert not result.selected_symbols
    assert "excluded_or_unknown_classification" in result.rows["BTC/USDT"].reasons


def test_listing_age_and_classification_require_available_evidence():
    histories = {"BTC/USDT": frame()}
    meta = metadata(histories)
    meta["BTC/USDT"]["listing_effective_at"] = "2023-08-01"
    meta["BTC/USDT"]["classification_available_at"] = "2024-01-01"
    result = select_all_qualified(histories, meta, as_of="2023-08-29")
    assert {"insufficient_listing_age", "classification_not_yet_available"} <= set(result.rows["BTC/USDT"].reasons)


def test_announced_spot_delisting_exits_before_halt_but_margin_delisting_does_not():
    histories = {"BTC/USDT": frame()}
    meta = metadata(histories)
    event = {"kind": "spot_delisted", "effective_at": "2023-09-01T08:00:00Z",
             "available_at": "2023-08-28T12:00:00Z", "source_status": "verified"}
    meta["BTC/USDT"]["events"] = [event]
    known = select_all_qualified(histories, meta, as_of="2023-08-29", held_symbols=("BTC/USDT",))
    assert known.rows["BTC/USDT"].force_exit
    assert "announced_spot_delisting" in known.rows["BTC/USDT"].reasons
    event["kind"] = "margin_delisted"
    margin = select_all_qualified(histories, meta, as_of="2023-08-29")
    assert margin.rows["BTC/USDT"].qualified
    assert not margin.rows["BTC/USDT"].force_exit
    event["kind"], event["available_at"] = "spot_delisted", "2023-08-30"
    future = select_all_qualified(histories, meta, as_of="2023-08-29")
    assert future.rows["BTC/USDT"].qualified


def test_classification_revisions_are_causal_and_missing_future_version_cannot_exclude_past():
    histories = {"BTC/USDT": frame()}
    meta = metadata(histories)
    events = [{"classification": "crypto", "source_status": "verified", "available_at": "2022-01-01"},
              {"classification": "stablecoin", "source_status": "verified", "available_at": "2023-08-30"}]
    meta["BTC/USDT"]["classification_events"] = events
    assert select_all_qualified(histories, meta, as_of="2023-08-29").rows["BTC/USDT"].qualified
    events[1]["available_at"] = "2023-08-28"
    row = select_all_qualified(histories, meta, as_of="2023-08-29").rows["BTC/USDT"].reasons
    assert "excluded_or_unknown_classification" in row


def test_missing_or_stale_day_is_not_filled_and_old_holdings_are_reported():
    histories = {"BTC/USDT": frame().drop(frame().index[-60])}
    result = select_all_qualified(histories, metadata(histories), as_of="2023-08-29", held_symbols=("GONE/USDT",))
    assert not result.selected_symbols
    assert "missing_or_stale_daily_close" in result.rows["BTC/USDT"].reasons
    assert result.rows["GONE/USDT"].held
    assert result.rows["GONE/USDT"].reasons == ("missing_history",)


def test_future_candles_future_announcements_and_input_order_do_not_change_targets():
    histories = {f"COIN{i}/USDT": frame(300, phase=i / 3) for i in range(4)}
    meta = metadata(histories)
    past = {symbol: history.iloc[:240] for symbol, history in histories.items()}
    now = next(iter(past.values())).index[-1] + pd.Timedelta(days=1)
    original = select_all_qualified(past, meta, as_of=now)
    for symbol, history in histories.items():
        history.iloc[240:, :5] *= 100
        meta[symbol]["events"] = [{"action": "delist", "effective_at": "2023-10-01", "available_at": "2023-09-01"}]
    augmented = select_all_qualified(dict(reversed(list(histories.items()))), meta, as_of=now)
    assert original.to_dict() == augmented.to_dict()
    assert size_portfolio_targets(original).to_dict() == size_portfolio_targets(augmented).to_dict()


def test_stated_close_time_controls_availability_and_source_frame_is_unchanged():
    history = frame()
    history["close_time"] = history.index + pd.Timedelta(days=1) - pd.Timedelta(milliseconds=1)
    original = history.copy(deep=True)
    now = history.index[-1] + pd.Timedelta(hours=12)
    assert len(completed_history(history, now)) == len(history) - 1
    completed_history(history, now)
    pd.testing.assert_frame_equal(history, original)


def test_later_publication_and_future_candle_corrections_do_not_rewrite_past():
    history = frame()
    history["available_at"] = history.index + pd.Timedelta(days=1)
    now = history.index[-1] + pd.Timedelta(days=1)
    correction = history.iloc[[-20]].copy()
    correction["available_at"] = now + pd.Timedelta(days=2)
    correction.loc[:, ["open", "high", "low", "close"]] *= 4
    amended = pd.concat([history, correction])
    pd.testing.assert_frame_equal(completed_history(amended, now), completed_history(history, now), check_freq=False)
    assert signal_facts(amended, as_of=now) == signal_facts(history, as_of=now)
    assert completed_history(amended, now + pd.Timedelta(days=2)).loc[correction.index[0], "close"] == correction.close.iloc[0]
    history.loc[history.index[-1], "available_at"] = now + pd.Timedelta(days=1)
    assert len(completed_history(history, now)) == len(history) - 1


def test_covariance_uses_60_simple_returns_ddof1_365_and_twenty_percent_diagonal():
    chosen = selection(5)
    returns = chosen.returns.to_numpy()
    covariance = np.cov(returns, rowvar=False, ddof=1) * 365
    covariance = .8 * covariance + .2 * np.diag(np.diag(covariance))
    inverse = 1 / np.sqrt(np.diag(covariance))
    base = inverse / inverse.sum()
    expected = base * .10 / math.sqrt(base @ covariance @ base)
    targets = size_portfolio_targets(chosen)
    assert list(targets.unconstrained_weights.values()) == pytest.approx(expected)
    assert targets.estimated_volatility_before == pytest.approx(.10)
    assert sum(targets.target_weights.values()) <= sum(targets.unconstrained_weights.values())


def test_more_assets_than_returns_require_no_covariance_inverse_or_topn():
    chosen = selection(65)
    assert len(size_portfolio_targets(chosen).target_weights) == 65


def test_health_and_account_multipliers_are_applied_once_and_removed_cap_is_not_refilled():
    chosen = selection(7)
    loose = replace(chosen.policy, max_symbol_initial_risk=.9, max_parent_initial_risk=.9)
    chosen = replace(chosen, policy=loose)
    normal = size_portfolio_targets(chosen, daily_new_risk_budget=1)
    reduced = size_portfolio_targets(chosen, health_multiplier=.5, account_multiplier=.4, daily_new_risk_budget=1)
    assert reduced.target_weights == pytest.approx({s: w * .2 for s, w in normal.target_weights.items()})
    assert reduced.applied_multipliers == {"health": .5, "account": .4}
    assert all("health_multiplier" in reasons and "account_multiplier" in reasons
               for reasons in reduced.binding_constraints.values())


def test_stop_risk_and_daily_increment_budget_are_shared_across_symbols():
    chosen = selection(15)
    targets = size_portfolio_targets(chosen, stop_distances={s: .2 for s in chosen.selected_symbols})
    assert all(weight * .2 <= .01 + 1e-12 for weight in targets.target_weights.values())
    assert sum(targets.target_weights.values()) * .2 == pytest.approx(.02)
    held = {symbol: value for symbol, value in targets.target_weights.items()}
    again = size_portfolio_targets(chosen, stop_distances={s: .2 for s in chosen.selected_symbols}, existing_weights=held)
    assert sum(again.target_weights.values()) * .2 <= .03 + 1e-12
    assert sum(max(again.target_weights[s] - held[s], 0) * .2 for s in held) <= .02 + 1e-12


def test_breakout_absence_prevents_new_and_added_risk_without_liquidating_held():
    histories = {"BTC/USDT": frame()}
    histories["BTC/USDT"].loc[histories["BTC/USDT"].index[-2], "high"] *= 2
    chosen = select_all_qualified(histories, metadata(histories), as_of="2023-08-29",
                                  policy=SelectionPolicyV2(variant="breakout"), held_symbols=("BTC/USDT",))
    assert chosen.selected_symbols == ("BTC/USDT",)
    assert not chosen.rows["BTC/USDT"].add_allowed
    assert size_portfolio_targets(chosen).target_weights["BTC/USDT"] == 0
    retained = size_portfolio_targets(chosen, existing_weights={"BTC/USDT": .03})
    assert retained.target_weights["BTC/USDT"] == pytest.approx(.03)


def test_strategy_continuous_score_and_normal_states_do_not_require_breakout():
    history = frame()
    history.loc[history.index[-2], "high"] *= 1.1
    strategy = TrendPortfolioV3Strategy()
    i = len(history) - 1
    score = .5 * (history.close.iloc[i] / history.close.iloc[i - 60] - 1
                  + history.close.iloc[i] / history.close.iloc[i - 120] - 1)
    for state in (MarketState.TREND_UP, MarketState.TREND_DOWN, MarketState.SIDEWAYS, MarketState.VOLATILE):
        signal = strategy.should_enter("BTC/USDT", i, history, state, Portfolio(10000))
        assert signal is not None
        assert signal["score"] == pytest.approx(score)
        assert signal["market_risk_multiplier"] == 1
    assert strategy.should_enter("BTC/USDT", i, history, MarketState.NO_TRADE, Portfolio(10000)) is None


def test_breakout_strategy_retains_held_positions_without_a_new_breakout():
    history = frame()
    history.loc[history.index[-2], "high"] *= 1.1
    strategy = TrendPortfolioV3Strategy(variant="breakout")
    assert strategy.raw_entry_signal("BTC/USDT", len(history) - 1, history) is None
    assert strategy.should_exit("BTC/USDT", len(history) - 1, history, MarketState.TREND_DOWN, Portfolio(10000)) is None


def test_strategy_health_identity_stop_policy_and_causal_facts_are_shared():
    strategy = TrendPortfolioV3Strategy()
    assert strategy.health_state_key == "strategy_health:TrendPortfolioV3"
    assert strategy.stop_policy.initial_atr_multiple == 2
    assert strategy.stop_policy.trailing_atr_multiple == 2.5
    assert strategy.stop_policy.resolved_initial_stop_mode == "atr"
    history = frame()
    assert strategy.raw_entry_signal("BTC/USDT", len(history) - 1, history) is not None
    strategy.health.manual_lock("test", at="2023-08-29T00:00:00Z")
    assert strategy.health.status == HealthStatus.MANUAL_LOCK
    assert strategy.should_enter("BTC/USDT", len(history) - 1, history, MarketState.SIDEWAYS, Portfolio(10000)) is None
    assert strategy.raw_setup_count == strategy.suppressed_setup_count == 1


def test_selection_stop_planning_keeps_injected_shared_distance_safeguards():
    strategy = TrendPortfolioV3Strategy()
    strategy.configure_stop_policy(ProtectiveStopPolicy(atr_period=21, min_stop_distance_pct=.10))
    history = frame()
    facts = strategy.signal_facts(history, history.index[-1] + pd.Timedelta(days=1))
    assert not facts["qualified"]
    assert "invalid_atr_stop" in facts["reasons"]
    assert strategy.selection_policy.atr_period == 21
    assert strategy.selection_policy.min_stop_distance_pct == .10


def test_daily_exit_does_not_recompute_entry_liquidity_obv_or_atr(monkeypatch):
    strategy = TrendPortfolioV3Strategy()
    history = frame()
    history.loc[history.index[-1], "close"] = 50.
    strategy._ensure_indicators(history)
    def unexpected(*args, **kwargs):
        raise AssertionError("daily exit must not recompute an entry signal")
    monkeypatch.setattr(strategy, "signal_facts", unexpected)
    monkeypatch.setattr("core.selection_v2.Indicators.ATR", unexpected)
    result = strategy.should_exit("BTC/USDT", len(history) - 1, history, MarketState.TREND_UP, Portfolio(10000))
    assert result["reason"] == "MomentumReversal"


def test_daily_exit_respects_unpublished_candles_and_missing_score_history():
    strategy = TrendPortfolioV3Strategy()
    history = frame()
    now = history.index[-1] + pd.Timedelta(days=1)
    history["available_at"] = history.index + pd.Timedelta(days=1)
    history.loc[history.index[-1], "close"] = 50.
    history.loc[history.index[-1], "available_at"] = now + pd.Timedelta(hours=1)
    assert strategy.should_exit("BTC/USDT", len(history) - 1, history, MarketState.TREND_UP, Portfolio(10000)) is None
    history.loc[history.index[-1], "available_at"] = now
    assert strategy.should_exit("BTC/USDT", len(history) - 1, history, MarketState.TREND_UP, Portfolio(10000))["reason"] == "MomentumReversal"


def test_optimized_exit_matches_full_entry_fact_reference_and_trailing_stops():
    history = frame(180)
    history.loc[history.index[[119, 135, 150]], "close"] = [60., 50., 30.]
    history.loc[history.index[140:], "quote_volume"] = np.nan
    fast, reference = TrendPortfolioV3Strategy(), TrendPortfolioV3Strategy()
    for strategy in (fast, reference):
        strategy.context["BTC/USDT"] = {"stop_loss": 70., "initial_stop": 70., "entry_price": 100.}
    for i in (119, 120, 130, 135, 136, 145, 150, 151, 170):
        expected = None
        facts = reference.signal_facts(history.iloc[:i + 1], history.index[i] + pd.Timedelta(days=1))
        reference._ensure_indicators(history)
        reference._update_protective_stop("BTC/USDT", i, history, side="long")
        if facts.get("score") is not None and facts["score"] < 0:
            expected = {"action": "sell", "reason": "MomentumReversal", "trend_score": facts["score"]}
        elif i >= 60 and history.close.iloc[i] < history.low.iloc[i - 60:i].min():
            expected = {"action": "sell", "reason": "MediumChannelExit60"}
        assert fast.should_exit("BTC/USDT", i, history, MarketState.TREND_DOWN, Portfolio(10000)) == expected
        assert fast.context["BTC/USDT"] == reference.context["BTC/USDT"]


def test_strategy_targets_require_coordinator_installation_and_normal_factory_is_unchanged():
    strategy = TrendPortfolioV3Strategy()
    history = frame()
    assert strategy.raw_entry_signal("BTC/USDT", len(history) - 1, history)["target_weight"] == 0
    strategy.set_portfolio_targets({"BTC/USDT": .15})
    assert strategy.raw_entry_signal("BTC/USDT", len(history) - 1, history)["target_weight"] == .15
    assert "TrendPortfolioV3" not in build_strategy_registry()
    class Configuration:
        def get(self, key, subkey=None):
            value = {"routing": {"TREND_UP": "TrendPortfolioV3"},
                     "research": {"experiment_id": "v3-test", "trend_portfolio_v3": {"variant": "breakout"}}}.get(key)
            return value if subkey is None else (value or {}).get(subkey)
    assert build_strategy_registry(Configuration())["TrendPortfolioV3"].variant == "breakout"
