"""Pure decision logic of the batch-2 strategy remediation studies."""

from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.run_health_power_study import cooldown_day_count, simulate
from scripts.run_health_power_study import verdict as power_verdict
from scripts.run_live_window_study import aggregate, compare_symbol
from scripts.run_live_window_study import verdict as window_verdict
from scripts.run_time_exit_rebuy_study import rebuy_pairs, summarize, verdict


def _trade(time, side, reason, strategy, price, qty=1.0, cost=1.0, symbol="A/USDT"):
    return {"fill_time": time, "side": side, "exit_reason": reason, "strategy_id": strategy,
            "fill_price": price, "qty": qty, "symbol": symbol,
            "costs": str({"commission": cost / 2, "modeled_total": cost})}


def _trades(*rows):
    return pd.DataFrame(list(rows))


def test_rebuy_is_paired_within_k_days_and_measured():
    trades = _trades(
        _trade("2020-01-01", "sell", "MaxHoldingPeriod", "MaxHoldingPeriod", 100.0, 2.0, cost=2.0),
        _trade("2020-01-03", "buy", "signal", "TrendBreakout", 103.0, 2.0, cost=1.5),
    )
    (pair,) = rebuy_pairs(trades, 3)
    assert pair["days"] == 2
    assert pair["round_trip_cost"] == 3.5
    assert pair["price_gap"] == 6.0
    assert pair["impact"] == 9.5
    assert rebuy_pairs(trades, 1) == []


def test_price_gap_uses_pre_cost_prices_so_slippage_is_not_counted_twice():
    sold = _trade("2020-01-01", "sell", "MaxHoldingPeriod", "MaxHoldingPeriod", 99.0, 1.0, cost=1.0)
    bought = _trade("2020-01-02", "buy", "signal", "TrendBreakout", 101.0, 1.0, cost=1.0)
    sold["theoretical_price"] = bought["theoretical_price"] = 100.0  # same market, adverse fills
    (pair,) = rebuy_pairs(_trades(sold, bought), 3)
    assert pair["price_gap"] == 0.0
    assert pair["impact"] == pair["round_trip_cost"] == 2.0


def test_only_first_buy_of_the_same_symbol_after_the_exit_counts():
    trades = _trades(
        _trade("2020-01-01", "buy", "signal", "TrendBreakout", 90.0),
        _trade("2020-01-05", "sell", "MaxHoldingPeriod", "MaxHoldingPeriod", 100.0),
        _trade("2020-01-06", "buy", "signal", "TrendBreakout", 101.0, symbol="B/USDT"),
        _trade("2020-01-07", "buy", "signal", "TrendBreakout", 99.0),
        _trade("2020-01-08", "buy", "signal", "TrendBreakout", 98.0),
    )
    (pair,) = rebuy_pairs(trades, 5)
    assert pair["rebuy_fill_time"].startswith("2020-01-07")
    assert pair["price_gap"] == -1.0


def test_verdict_needs_every_window_within_both_limits():
    quiet = summarize(_trades(_trade("2020-01-01", "buy", "signal", "TrendBreakout", 100.0)))
    assert quiet["time_exits"] == 0
    assert verdict({"train60": quiet, "validation20": quiet})["negligible"]
    costly = _trades(
        _trade("2020-01-01", "sell", "MaxHoldingPeriod", "MaxHoldingPeriod", 100.0, 10.0),
        _trade("2020-01-02", "buy", "signal", "TrendBreakout", 103.0, 10.0),
    )
    result = verdict({"train60": quiet, "validation20": summarize(costly)})
    assert not result["negligible"]
    # one re-buy, but 2 USDT of costs + 3 USDT x 10 of price gap > the 25 USDT limit
    assert result["checks"]["validation20"] == {"count_le_limit": True, "impact_le_limit": False}
    assert result["conclusion"] == "candidate_for_stage_C"


# ---- SR-DATA-001 (B2) ------------------------------------------------------

def _walk(periods=160, seed=7):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0.003, 0.03, periods)))
    index = pd.date_range("2020-01-01", periods=periods, freq="1D")
    return pd.DataFrame({"open": close, "high": close * 1.02, "low": close * 0.98,
                         "close": close, "volume": 1e6}, index=index)


def test_a_window_spanning_the_whole_history_matches_the_reference():
    frame = _walk()
    result = compare_symbol("A/USDT", frame, frame.index[0], frame.index[-1], [len(frame)])
    row = result[str(len(frame))]
    assert row["compared_bars"] == 1
    assert row["state_mismatches"] == 0
    assert row["only_reference"] == row["only_window"] == 0


def test_window_verdict_picks_the_smallest_passing_window():
    def row(compared, state, ref, diff):
        return {"compared_bars": compared, "state_mismatches": state, "reference_candidates": ref,
                "only_reference": diff, "only_window": 0, "window_candidates": ref,
                "max_score_abs_diff": 0.0, "candidate_mismatches": []}
    per_symbol = {"A": {"100": row(10_000, 70, 300, 9), "200": row(10_000, 5, 300, 2),
                        "400": row(10_000, 0, 300, 0)}}
    totals = aggregate(per_symbol, [100, 200, 400])
    result = window_verdict(totals)
    assert result["selected_closed_bars"] == 200 and result["live_lookback"] == 201
    assert result["checks"]["100"]["state_rate_ok"] is False
    failing = {"A": {"100": row(10_000, 70, 300, 9)}}
    assert window_verdict(aggregate(failing, [100]))["conclusion"] == \
        "rolling_window_insufficient_seed_from_full_history"


# ---- SR1-HEALTH-007 (B5) ---------------------------------------------------


def test_cooldown_days_are_counted_from_transitions():
    transitions = pd.DataFrame([
        {"at": "2020-01-10T00:00:00+00:00", "to": "cooldown"},
        {"at": "2020-02-09T00:00:00+00:00", "to": "probation"},
        {"at": "2020-03-01T00:00:00+00:00", "to": "cooldown"},
    ])
    assert cooldown_day_count(transitions, pd.Timestamp("2020-01-01"), pd.Timestamp("2020-03-11")) == 40


def test_simulation_separates_a_losing_from_a_winning_stream():
    inputs = {"r": [-1.0, -1.0, -1.0, 3.0], "symbols": ["A", "B", "C", "D"], "p": 0.2}
    losing = simulate("current", -0.3, inputs, paths=20, years=2)
    winning = simulate("current", 0.3, inputs, paths=20, years=2)
    assert losing["first_year_trigger_rate"] > 0.5
    assert losing["mean_multiplier"] < winning["mean_multiplier"] <= 1.0
    assert 0.0 <= losing["cooldown_share"] <= 1.0


def test_power_verdict_rules():
    def row(arm, mu, m, f=0.0):
        return {"arm": arm, "mu": mu, "mean_multiplier": m, "first_year_trigger_rate": f}
    good = [row("current", 0.2, 0.7, 0.2), row("current", -0.3, 0.2), row("h3", 0.2, 0.8), row("h3", -0.3, 0.2)]
    assert power_verdict(good)["conclusion"] == "current_discrimination_adequate"
    weak = [row("current", 0.2, 0.3, 0.6), row("current", -0.3, 0.2), row("h3", 0.2, 0.45), row("h3", -0.3, 0.25)]
    assert power_verdict(weak)["conclusion"] == "h3_is_stage_C1_health_candidate"
    weak[2]["mean_multiplier"] = 0.35
    assert power_verdict(weak)["conclusion"] == "discrimination_weak_no_health_change_in_stage_C"


# ---- SR-ALPHA-004 (B3) -----------------------------------------------------


def test_each_entry_arm_changes_exactly_one_dimension():
    from config.config import config
    from scripts.run_entry_delay_study import ARMS, arm_parameters
    base = config._config
    assert arm_parameters(base, "b0_baseline")["state"] == base["state"]
    assert arm_parameters(base, "stab2")["state"]["stability_period"] == 2
    assert arm_parameters(base, "stab2")["router"] == base["router"]
    assert arm_parameters(base, "cool0")["router"]["cooldown_bars"] == 0
    assert arm_parameters(base, "obv_off")["research"]["strategy_ablation"] == "no_obv_confirmation"
    assert base["state"]["stability_period"] == 5 and base["router"]["cooldown_bars"] == 2
    assert set(ARMS) == {"b0_baseline", "obv_off", "stab2", "stab3", "stab10", "cool0", "cool1"}


def test_entry_decision_needs_both_windows_and_picks_one_dimension():
    from scripts.run_entry_delay_study import ARMS, decide

    def row(ret, dd=10.0, top5=100.0):
        return {"return_pct": ret, "max_drawdown_pct": dd, "net_closed_pnl_without_top_5": top5,
                "accounting_ok": True}
    rows = {w: {arm: row(10.0) for arm in ARMS} for w in ("train60", "validation20")}
    assert decide(rows)["conclusion"] == "retain_current_entry_gates"
    for w in rows:
        rows[w]["cool1"] = row(14.0, top5=120.0)
        rows[w]["obv_off"] = row(12.0, top5=110.0)
        rows[w]["stab10"] = row(30.0, top5=500.0)  # control only, never selected
    rows["train60"]["stab2"] = row(40.0, top5=900.0)  # fails validation below
    result = decide(rows)
    assert result["dimension_candidates"] == {"cooldown": "cool1", "obv": "obv_off"}
    assert result["stage_c1_arm"] == "cool1" and result["stage_c1_dimension"] == "cooldown"
    assert result["control_passed"] == {"stab10": True}
    assert result["verdicts"]["stab2"]["passed"] is False


# ---- SR5-SUCC-001 (D2) -----------------------------------------------------


def test_successor_comparison_isolates_c1():
    from config.config import config
    from scripts.run_successor_comparison import arm_parameters
    parent, successor = arm_parameters(config._config, "parent"), arm_parameters(config._config, "successor")
    assert parent["trend_breakout"] == {"use_obv": True}
    assert successor["trend_breakout"] == config._config["trend_breakout"] == {"use_obv": False}
    assert {k: v for k, v in parent.items() if k not in ("trend_breakout", "research")} == \
        {k: v for k, v in successor.items() if k not in ("trend_breakout", "research")}


def test_successor_verdict_rejects_unless_both_costs_hold():
    from scripts.run_successor_comparison import judge

    def row(ret, dd=10.0, top5=100.0):
        return {"return_pct": ret, "max_drawdown_pct": dd, "net_closed_pnl_without_top_5": top5, "accounting_ok": True}
    rows = {"parent@1": row(5), "successor@1": row(6), "parent@2": row(3), "successor@2": row(3)}
    result = judge(rows)
    assert result["conclusion"] == "not_rejected_retrospective"
    assert result["admission"] == "paused_revalidation"
    rows["successor@2"] = row(4, top5=90.0)
    assert judge(rows)["conclusion"] == "reject_successor"
    rows["successor@2"] = row(30, dd=14.0)  # drawdown above max(12.5, 13)
    assert judge(rows)["checks"]["2"]["drawdown_within_cap"] is False
