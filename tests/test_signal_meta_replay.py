"""P3 policy replays must make their own causal orders and reconcile accounts."""
from copy import deepcopy
import random

import pandas as pd
import pytest

from backtest.signal_meta_replay import replay_signal_meta
from core.broker import Broker
from core.market_data import HistoricalMarketDataAdapter
from core.portfolio import Portfolio
from core.signal_adaptive_types import MetaReplayPolicy
from core.signal_observation_types import fingerprint, iso


def _frame(n=8, *, price=100, volume=10000):
    return pd.DataFrame({"open": [price+i for i in range(n)],
        "high": [price+i+1 for i in range(n)], "low": [price+i-1 for i in range(n)],
        "close": [price+i for i in range(n)], "volume": volume},
        index=pd.date_range("2020-01-01", periods=n))


def _candidate(cid="c", *, day=0, symbol="X", direction="long", price=None, score=1):
    timestamp = pd.Timestamp("2020-01-01") + pd.Timedelta(days=day)
    return {"candidate_id": cid, "timestamp": iso(timestamp), "symbol": symbol,
        "strategy": "Test", "direction": direction, "native_score": score,
        "reference_price": price if price is not None else 100+day, "signal_version": "s1",
        "signal": {"action": "buy" if direction == "long" else "short", "order_type": "market"},
        "context": {"snapshot_version": "p0v1", "available_at": iso(timestamp+pd.Timedelta(days=1)),
                    "timeframe": "1d"}}


def _payload(candidates, *, statuses=None, horizon=1, lower=50):
    statuses = statuses or {}
    p0 = {"schema": "signal_observation/v1", "status": "complete", "snapshot_version": "p0v1",
        "strategy_versions": {"Test": "s1"}, "policy": {"horizons": [horizon]},
        "candidates": candidates, "decisions": [{"candidate_id": c["candidate_id"],
            "accepted": False, "veto_stage": "regime"} for c in candidates],
        "outcomes": []}
    fold = {"fold_id": "f1", "model_version": "model1", "start": iso("2020-01-01"),
            "end": iso("2021-01-01"), "training_cutoff": iso("2019-12-31")}
    predictions = [{"candidate_id": c["candidate_id"], "horizon_bars": horizon,
        "available_at": c["context"]["available_at"], "model_version": "model1", "fold_id": "f1",
        "training_cutoff": fold["training_cutoff"], "status": statuses.get(c["candidate_id"], "allow"),
        "lower_bound_bps": lower, "estimate_bps": lower+50,
        "strategy": c["strategy"], "symbol": c["symbol"], "direction": c["direction"],
        "signal_version": c["signal_version"], "snapshot_version": c["context"]["snapshot_version"],
        "timeframe": c["context"]["timeframe"], "memberships": {}}
        for c in candidates]
    p2 = {"schema": "signal_adaptive_ev/v1", "status": "complete", "model_version": "implementation",
        "input_identity": {"p0_snapshot_version": "p0v1",
            "candidates_sha256": fingerprint(sorted(candidates, key=lambda c: c["candidate_id"]))},
        "folds": [fold], "predictions": predictions}
    return p0, p2


def _run(candidates=None, *, frames=None, statuses=None, horizon=1, template=None, lower=50, **policy):
    candidates = [_candidate()] if candidates is None else candidates
    p0, p2 = _payload(candidates, statuses=statuses, horizon=horizon, lower=lower)
    frames = frames or {s: _frame() for s in {c["symbol"] for c in candidates}}
    return replay_signal_meta(HistoricalMarketDataAdapter(frames, timeframe="1d"), p0, p2,
        template or Broker(Portfolio(10000), commission_rate=0, slippage=0),
        MetaReplayPolicy(enabled=True, horizon_bars=horizon, **policy))


def _rows(result, arm="baseline"):
    return [row for row in result["rows"] if row["arm"] == arm]


def test_p0_decision_audit_accepts_real_engine_timestamps():
    p0, p2 = _payload([_candidate()])
    p0["decisions"][0]["audit"] = {"timestamp": pd.Timestamp("2020-01-01")}
    result = replay_signal_meta(HistoricalMarketDataAdapter({"X": _frame()}, timeframe="1d"),
        p0, p2, Broker(Portfolio(10000), commission_rate=0, slippage=0),
        MetaReplayPolicy(enabled=True, horizon_bars=1))
    assert result["status"] == "complete"
    assert len(result["input_identity"]["decisions_sha256"]) == 64


def _account(result, arm="baseline"):
    return next(row for row in result["accounts"] if row["arm"] == arm)


def test_all_arms_execute_next_actual_open_on_both_legs_and_reconcile_net_costs():
    template = Broker(Portfolio(10000), commission_rate=.001, slippage=.0005)
    result = _run(template=template)
    assert result["status"] == "complete"
    assert all(result["validation"].values())
    row = _rows(result)[0]
    assert row["status"] == "closed"
    assert row["first_fill_time"] == iso("2020-01-02")
    assert row["last_exit_time"] == iso("2020-01-03")
    assert row["entry_commission"] > 0 and row["exit_commission"] > 0 and row["slippage_cost"] > 0
    assert _account(result)["net_pnl"] == pytest.approx(row["net_pnl"])
    assert len(result["equity"]) == 8*3
    assert result["equity"][0]["pending_reserved"] == pytest.approx(1000)
    assert not template.trades and template.portfolio.cash == 10000


def test_gate_abstains_but_sizing_uses_preregistered_minimum_and_veto_always_zero():
    cs = [_candidate("positive", symbol="A"), _candidate("negative", symbol="B"),
          _candidate("unknown", symbol="C")]
    result = _run(cs, statuses={"positive": "allow", "negative": "veto", "unknown": "abstain"})
    assert [r["status"] for r in _rows(result, "baseline")] == ["closed", "closed", "closed"]
    assert [r["status"] for r in _rows(result, "gate")] == ["closed", "meta_blocked", "meta_blocked"]
    sizing = _rows(result, "sizing")
    assert [r["multiplier"] for r in sizing] == [.5, 0, .25]
    assert sizing[-1]["reason"] == "fixed_horizon_exit_completed"
    assert sizing[-1]["prediction_status"] == "abstain"
    assert sizing[-1]["research_notional"] == 250


@pytest.mark.parametrize("lower, multiplier", [(-10, .25), (5, .25), (75, .75), (150, 1)])
def test_sizing_lower_bound_clips_at_declared_bounds(lower, multiplier):
    result = _run(lower=lower)
    assert _rows(result, "sizing")[0]["multiplier"] == multiplier


def test_pending_reservations_compete_and_veto_releases_a_later_opportunity():
    result = _run([_candidate("a", symbol="A"), _candidate("b", symbol="B")],
        statuses={"a": "veto"}, initial_capital=1000, reference_notional=600)
    assert [row["status"] for row in _rows(result)] == ["closed", "capital_blocked"]
    assert [row["status"] for row in _rows(result, "gate")] == ["meta_blocked", "closed"]
    assert _rows(result)[1]["decision_pending_reserved"] == 600
    assert _rows(result, "gate")[1]["decision_pending_reserved"] == 0


def test_low_limit_price_reserves_quantity_at_current_mark_before_submission():
    candidate = _candidate("limit", price=50)
    candidate["signal"]["order_type"] = "limit"
    result = _run([candidate], initial_capital=1000, reference_notional=600)
    baseline = _rows(result)[0]
    assert baseline["status"] == "capital_blocked"
    assert baseline["research_notional"] == 600
    assert baseline["decision_required_reservation"] == 1200
    assert baseline["decision_remaining_budget"] == 1000
    assert "opening_order_id" not in baseline
    assert result["validation"]["pretrade_budget_ok"]
    # The half-size account may queue its smaller limit order. Both admitted
    # and blocked arms reconcile their actual reservation against the budget.
    sizing = _rows(result, "sizing")[0]
    assert sizing["decision_required_reservation"] == 600
    first = next(r for r in result["equity"] if r["arm"] == "sizing")
    assert first["pending_reserved"] == sizing["decision_required_reservation"]
    assert all(row["gross_used"]+row["pending_reserved"] <= row["gross_budget"]
               for row in result["equity"])


@pytest.mark.parametrize("other_symbol_present", [True, False])
def test_missing_candidate_bar_halts_before_later_orders_or_fills(other_symbol_present):
    frames = {"X": _frame().drop(pd.Timestamp("2020-01-01"))}
    if other_symbol_present:
        frames["Y"] = _frame()
    result = _run([_candidate("missing"), _candidate("later", day=2)], frames=frames)
    assert result["status"] == "incomplete"
    assert result["validation"]["candidate_event_partition_ok"]
    assert not result["validation"]["candidate_bars_complete"]
    assert result["validation"]["arm_row_partition_ok"]
    assert not result["fills"]
    for arm in ("baseline", "gate", "sizing"):
        assert [r["status"] for r in _rows(result, arm)] == [
            "unresolved_missing_bar", "unresolved_account_error"]
        account = _account(result, arm)
        assert account["status"] == "unresolved_execution_error"
        assert account["halt_time"] == iso("2020-01-01")
        assert account["net_pnl"] is None and account["return_fraction"] is None


def test_missing_decision_event_between_real_events_prevents_pending_entry_fill():
    frame = _frame().drop(pd.Timestamp("2020-01-02"))
    result = _run([_candidate("pending"), _candidate("missing", day=1), _candidate("later", day=3)],
                  frames={"X": frame})
    assert result["status"] == "incomplete"
    assert not result["fills"]
    assert [r["status"] for r in _rows(result)] == [
        "unresolved_execution_error", "unresolved_missing_bar", "unresolved_account_error"]
    assert _account(result)["return_fraction"] is None


def test_native_score_order_and_input_permutations_are_deterministic():
    cs = [_candidate("a", symbol="A", score=1), _candidate("z", symbol="Z", score=5)]
    first = _run(cs, initial_capital=1000, reference_notional=600)
    second = _run(list(reversed(cs)), initial_capital=1000, reference_notional=600)
    assert first == second
    assert [row["candidate_id"] for row in _rows(first)] == ["z", "a"]
    assert [row["status"] for row in _rows(first)] == ["closed", "capital_blocked"]


def test_symbol_occupancy_includes_pending_and_partial_orders():
    cs = [_candidate(str(i), day=i) for i in range(4)]
    result = _run(cs, horizon=2, frames={"X": _frame(volume=1)},
        template=Broker(Portfolio(10000), commission_rate=0, slippage=0, max_participation_rate=1))
    rows = _rows(result)
    assert [r["status"] for r in rows] == ["closed", "busy_blocked", "busy_blocked", "busy_blocked"]
    assert rows[0]["filled_quantity"] == 2
    assert rows[0]["closed_quantity"] == 2
    assert rows[0]["last_exit_time"] == iso("2020-01-05")
    assert any(a["reason"] == "participation_limit" for a in result["execution_audit"])


def test_unfilled_opening_order_ttl_releases_symbol_and_budget():
    first = _candidate("limit", price=50)
    first["signal"]["order_type"] = "limit"
    result = _run([first, _candidate("later", day=3)],
        template=Broker(Portfolio(10000), commission_rate=0, slippage=0, opening_order_ttl_bars=1))
    assert [r["status"] for r in _rows(result)] == ["unfilled_expired", "closed"]
    assert _rows(result)[1]["decision_pending_reserved"] == 0


def test_tail_position_is_marked_but_never_given_an_invented_exit():
    result = _run(horizon=5, frames={"X": _frame(n=3)})
    row, account = _rows(result)[0], _account(result)
    assert row["status"] == "censored_end_of_data" and row["net_pnl"] is None
    assert account["open_positions"] == 1 and account["closed_candidates"] == 0
    assert account["ending_positions"][0]["mark_price"] == 102
    assert account["net_pnl"] == pytest.approx(10)
    assert len([f for f in result["fills"] if f["arm"] == "baseline"]) == 1


def test_missing_funding_preserves_entry_facts_halts_once_and_returns_unknown():
    cs = [_candidate("first"), _candidate("later", day=3)]
    template = Broker(Portfolio(10000, account_mode="perpetual"),
                      commission_rate=0, slippage=0, funding_rate_required=True)
    result = _run(cs, template=template)
    assert result["status"] == "incomplete"
    assert len(result["errors"]) == 3
    assert [r["status"] for r in _rows(result)] == ["unresolved_execution_error", "unresolved_account_error"]
    assert len(result["fills"]) == 3
    assert all(a["return_fraction"] is None and a["net_pnl"] is None for a in result["accounts"])
    assert _account(result)["ending_marked_equity"] > 10000
    assert result["equity"][-1]["status"] == "diagnostic_after_halt"


def test_short_borrow_financing_reconciles_and_flat_gap_does_not_accrue():
    cs = [_candidate("first", direction="short"), _candidate("later", day=5, direction="short")]
    template = Broker(Portfolio(10000, account_mode="spot_margin"),
        commission_rate=0, slippage=0, default_borrow_rate_annual=.365)
    result = _run(cs, horizon=2, frames={"X": _frame(n=11)}, template=template)
    costs = [r["carry"] for r in _rows(result)]
    # Two holding intervals, valued at each interval end; the exit fill
    # settles the final day before the borrowed quantity becomes zero.
    assert costs[0] == pytest.approx(10*(102+103)*.001)
    assert costs[1] == pytest.approx((1000/105)*(107+108)*.001)
    assert _account(result)["financing_cost"] == pytest.approx(sum(costs))
    assert _account(result)["net_pnl"] == pytest.approx(sum(r["net_pnl"] for r in _rows(result)))


def test_maintenance_breach_halts_with_position_facts_and_no_capital_restart():
    frame = _frame(n=8)
    frame.loc[frame.index[2]:, ["open", "high", "low", "close"]] = 400
    cs = [_candidate("first", direction="short"), _candidate("later", day=4, direction="short")]
    template = Broker(Portfolio(1000, account_mode="spot_margin", initial_margin_rate=.1),
        commission_rate=0, slippage=0, default_borrow_rate_annual=0)
    result = _run(cs, frames={"X": frame}, template=template, horizon=5, initial_capital=1000)
    assert result["status"] == "incomplete"
    assert _account(result)["return_fraction"] is None
    assert _account(result)["ending_marked_equity"] < 0
    assert [r["status"] for r in _rows(result)] == ["unresolved_execution_error", "unresolved_account_error"]
    assert len([f for f in result["fills"] if f["arm"] == "baseline"]) == 1
    assert any("margin breach" in e["reason"] for e in result["errors"])


def test_missing_calendar_bars_count_only_real_bars_and_keep_stale_marks_explicit():
    frame = _frame(n=8).drop(pd.Timestamp("2020-01-03"))
    result = _run(frames={"X": frame, "Y": _frame(n=8)}, horizon=2)
    row = _rows(result)[0]
    assert row["holding_bars"] == 2
    assert row["last_exit_time"] == iso("2020-01-05")
    stale = next(r for r in result["equity"] if r["arm"] == "baseline" and r["timestamp"] == iso("2020-01-03"))
    assert stale["stale_mark_symbols"] == ["X"]


def test_one_preselected_horizon_is_used_even_when_other_predictions_disagree():
    p0, p2 = _payload([_candidate()], horizon=1)
    p0["policy"]["horizons"] = [1, 5]
    p2["predictions"].append({**p2["predictions"][0], "horizon_bars": 5, "status": "veto"})
    result = replay_signal_meta(HistoricalMarketDataAdapter({"X": _frame()}, timeframe="1d"), p0, p2,
        Broker(Portfolio(10000)), MetaReplayPolicy(enabled=True, horizon_bars=5))
    assert result["status"] == "complete"
    assert _rows(result, "gate")[0]["status"] == "meta_blocked"
    assert _rows(result)[0]["horizon_bars"] == 5
    assert _rows(result)[0]["last_exit_time"] == iso("2020-01-07")


def test_future_outcomes_and_official_acceptance_cannot_select_earlier_orders():
    p0, p2 = _payload([_candidate()])
    template = Broker(Portfolio(10000), commission_rate=0, slippage=0)
    data = HistoricalMarketDataAdapter({"X": _frame()}, timeframe="1d")
    policy = MetaReplayPolicy(enabled=True, horizon_bars=1)
    first = replay_signal_meta(data, p0, p2, template, policy)
    p0["outcomes"] = [{"candidate_id": "c", "net_return_bps": -999999,
        "status": "future_unavailable", "execution_flags": ["future_bad_execution"]}]
    p0["decisions"][0]["accepted"] = True
    second = replay_signal_meta(data, p0, p2, template, policy)
    for name in ("rows", "fills", "financing", "equity", "accounts"):
        assert first[name] == second[name]


@pytest.mark.parametrize("stage", ["warmup", "data", "universe"])
def test_p0_ineligible_candidates_are_reported_in_each_arm(stage):
    p0, p2 = _payload([_candidate()])
    p0["decisions"][0]["veto_stage"] = stage
    result = replay_signal_meta(HistoricalMarketDataAdapter({"X": _frame()}, timeframe="1d"), p0, p2,
        Broker(Portfolio(10000)), MetaReplayPolicy(enabled=True, horizon_bars=1))
    assert all(row["status"] == "p0_ineligible" for row in result["rows"])
    assert all(a["activity"] == "inactive" and a["return_fraction"] == 0 for a in result["accounts"])


@pytest.mark.parametrize("mutation", ["duplicate", "missing", "extra", "future", "cutoff", "version",
                                     "signal_version", "snapshot", "direction", "untrained_allow", "nan"])
def test_invalid_or_unavailable_prediction_fails_closed_before_broker_orders(mutation):
    p0, p2 = _payload([_candidate()])
    pred = p2["predictions"][0]
    if mutation == "duplicate":
        p2["predictions"].append(deepcopy(pred))
    elif mutation == "missing":
        p2["predictions"].clear()
    elif mutation == "extra":
        p2["predictions"].append({**pred, "candidate_id": "unknown"})
    elif mutation == "future":
        pred["available_at"] = iso("2020-01-03")
    elif mutation == "cutoff":
        pred["training_cutoff"] = iso("2020-01-03")
    elif mutation == "version":
        pred["model_version"] = "another_model"
    elif mutation == "signal_version":
        pred["signal_version"] = "another_strategy"
    elif mutation == "snapshot":
        p2["input_identity"]["p0_snapshot_version"] = "another_snapshot"
    elif mutation == "direction":
        pred["direction"] = "short"
    elif mutation == "untrained_allow":
        pred.update(fold_id=None, training_cutoff=None, model_version=p2["model_version"])
    else:
        pred["lower_bound_bps"] = float("nan")
    result = replay_signal_meta(HistoricalMarketDataAdapter({"X": _frame()}, timeframe="1d"), p0, p2,
        Broker(Portfolio(10000)), MetaReplayPolicy(enabled=True, horizon_bars=1))
    assert result["status"] == "incomplete"
    assert not result["fills"] and not result["accounts"]
    assert result["errors"][0]["reason"] == "invalid_replay_input"


def test_cold_start_can_abstain_without_a_fitted_fold():
    p0, p2 = _payload([_candidate()], statuses={"c": "abstain"})
    p2["folds"] = []
    p2["predictions"][0].update(fold_id=None, training_cutoff=None, model_version=p2["model_version"],
                                estimate_bps=None, lower_bound_bps=None)
    result = replay_signal_meta(HistoricalMarketDataAdapter({"X": _frame()}, timeframe="1d"), p0, p2,
        Broker(Portfolio(10000)), MetaReplayPolicy(enabled=True, horizon_bars=1))
    assert result["status"] == "complete"
    assert _account(result, "gate")["return_fraction"] == 0
    assert _rows(result, "sizing")[0]["multiplier"] == .25


def test_replay_does_not_mutate_inputs_template_or_random_generator():
    p0, p2 = _payload([_candidate()])
    original = deepcopy((p0, p2))
    template = Broker(Portfolio(10000), random_slip=True)
    data = HistoricalMarketDataAdapter({"X": _frame()}, timeframe="1d")
    data_before = data.data_map["X"].copy(deep=True)
    state = random.getstate()
    replay_signal_meta(data, p0, p2, template, MetaReplayPolicy(enabled=True, horizon_bars=1))
    assert random.getstate() == state
    assert (p0, p2) == original
    pd.testing.assert_frame_equal(data.data_map["X"], data_before)
    assert template.portfolio.cash == 10000 and not template.trades and not template.pending_orders


def test_disabled_replay_is_noop_even_without_inputs():
    assert replay_signal_meta(None, None, None, None) is None


def test_empty_complete_input_returns_three_inactive_accounts():
    p0, p2 = _payload([])
    result = replay_signal_meta(HistoricalMarketDataAdapter({"X": _frame()}, timeframe="1d"), p0, p2,
        Broker(Portfolio(10000)), MetaReplayPolicy(enabled=True, horizon_bars=1))
    assert result["status"] == "complete"
    assert not result["rows"]
    assert all(a["activity"] == "inactive" and a["return_fraction"] == 0 for a in result["accounts"])
