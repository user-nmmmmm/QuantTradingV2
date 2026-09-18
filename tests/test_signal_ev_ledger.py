"""Hand-checkable causal and dependence-aware P1 ledger contracts."""
from copy import deepcopy
from dataclasses import replace
import math

import pandas as pd
import pytest

from core.signal_ev_ledger import EVLedger
from core.signal_ev_types import EVPolicy


def candidate(number=0, *, day=0, symbol="BTC/USDT", strategy="Trend",
              direction="long", signal_version="v1", snapshot_version="costs-v1",
              timeframe="1d"):
    stamp = pd.Timestamp("2020-01-01", tz="UTC") + pd.Timedelta(days=day)
    return {"candidate_id": f"candidate-{number}", "symbol": symbol,
            "strategy": strategy, "direction": direction, "signal_version": signal_version,
            "context": {"available_at": stamp.isoformat(), "timeframe": timeframe,
                        "snapshot_version": snapshot_version}}


def outcome(c, value=100.0, *, horizon=1, label_at=None):
    stamp = pd.Timestamp(c["context"]["available_at"]) + pd.Timedelta(days=horizon)
    return {"candidate_id": c["candidate_id"], "symbol": c["symbol"],
            "strategy": c["strategy"], "direction": c["direction"],
            "horizon_bars": horizon, "status": "matured",
            "scope": "independent_fixed_notional_signal_diagnostic",
            "execution_flags": [], "net_return_bps": value,
            "available_at": label_at or stamp.isoformat()}


def policy(**kwargs):
    defaults = {"half_life_days": 90.0, "prior_strength": 20.0,
                "min_effective_samples": 2.0, "min_effective_blocks": 2.0,
                "min_weight_mass": 0.00001, "block_days": 1}
    return EVPolicy(**(defaults | kwargs))


def append(ledger, c, value=100.0, memberships=None, horizon=1, cutoff="2025-01-01"):
    memberships = memberships or {"trend": {"high": 1.0}}
    return ledger.add(c, outcome(c, value, horizon=horizon), memberships, cutoff=cutoff)


def query(ledger, *, day=20, memberships=None, horizon=1, **kwargs):
    c = candidate("query", day=day, **kwargs)
    return ledger.query(c, memberships or {"trend": {"high": 1.0}}, horizon,
                        as_of=c["context"]["available_at"])


def test_decay_is_from_entry_not_label_arrival_and_counts_are_distinct():
    ledger = EVLedger(policy(half_life_days=10.0, prior_strength=0.0))
    append(ledger, candidate(0, day=0), 100)
    delayed = candidate(1, day=10)
    delayed_outcome = outcome(delayed, 200, label_at="2020-01-20T00:00:00Z")
    ledger.add(delayed, delayed_outcome, {"trend": {"high": 1.0}}, cutoff="2020-02-01")
    result = query(ledger, day=20)
    assert result["weight_mass"] == pytest.approx(.25 + .5)
    assert result["raw_count"] == 2
    assert result["effective_samples"] == pytest.approx(.75 ** 2 / (.25 ** 2 + .5 ** 2))
    assert result["effective_blocks"] == pytest.approx(result["effective_samples"])
    assert result["estimate_bps"] == pytest.approx((.25 * 100 + .5 * 200) / .75)
    assert result["prior"]["variance_bps2"] == pytest.approx(5000)


def test_soft_memberships_shrink_to_causal_prior_using_mass_not_sample_count():
    ledger = EVLedger(policy(half_life_days=10.0, prior_strength=3.0))
    append(ledger, candidate(0, day=0), 100, {"trend": {"high": .5, "low": .5}})
    append(ledger, candidate(1, day=10), -100, {"trend": {"high": 0., "low": 1.}})
    result = query(ledger)
    cell = result["axes"]["trend"]["states"]["high"]
    expected_prior = (.25 * 100 - .5 * 100) / .75
    alpha = .125 / (3 + .125)
    assert cell["weight_mass"] == pytest.approx(.125)
    assert cell["effective_samples"] == 1.0
    assert cell["shrinkage_alpha"] == pytest.approx(alpha)
    assert result["estimate_bps"] == pytest.approx(alpha * 100 + (1 - alpha) * expected_prior)
    assert result["stderr_bps"] == pytest.approx(
        alpha * cell["stderr_bps"] + (1 - alpha) * result["prior"]["stderr_bps"])


def test_entry_memberships_are_frozen_and_exact_replay_is_idempotent():
    ledger = EVLedger(policy())
    c = candidate()
    o = outcome(c)
    memberships = {"trend": {"high": 1.0, "low": 0.0}}
    original = deepcopy(memberships)
    assert ledger.add(c, o, memberships, cutoff="2020-02-01") is True
    assert ledger.add(c, o, original, cutoff="2020-02-02") is False
    memberships["trend"] = {"high": 0.0, "low": 1.0}
    c["context"]["available_at"] = "2019-01-01T00:00:00Z"
    o["net_return_bps"] = -1000
    result = query(ledger)
    assert result["estimate_bps"] == 100
    assert result["raw_count"] == 1
    assert result["axes"]["trend"]["states"]["high"]["first_entry_at"].startswith("2020-01-01")
    with pytest.raises(ValueError, match="conflicting replay"):
        ledger.add(candidate(), outcome(candidate()), memberships, cutoff="2020-02-01")


@pytest.mark.parametrize("field,value", [
    ("strategy", "Other"), ("direction", "short"),
    ("signal_version", "v2"), ("snapshot_version", "costs-v2"),
    ("timeframe", "12h"),
])
def test_books_never_pool_versions_directions_costs_or_timeframes(field, value):
    ledger = EVLedger(policy())
    append(ledger, candidate(), 100)
    result = query(ledger, **{field: value})
    assert result["status"] == "abstain"
    assert result["reason"] == "cold_start"
    assert result["prior"]["raw_count"] == 0


def test_horizons_are_separate_books_but_each_can_be_added_for_same_candidate():
    ledger = EVLedger(policy())
    append(ledger, candidate(), 100, horizon=1)
    assert query(ledger, horizon=3)["reason"] == "cold_start"
    append(ledger, candidate(), -100, horizon=3)
    assert query(ledger, horizon=1)["estimate_bps"] == 100
    assert query(ledger, horizon=3)["estimate_bps"] == -100


def test_future_records_cannot_affect_query_prior_or_cell_export():
    ledger = EVLedger(policy())
    append(ledger, candidate(), 100)
    baseline = query(ledger, day=10)
    cells = ledger.cell_rows(as_of="2020-01-11")
    future = candidate(1, day=8)
    ledger.add(future, outcome(future, -1e9, label_at="2020-01-20T00:00:00Z"),
               {"future_axis": {"future_state": 1.0}}, cutoff="2020-02-01")
    assert query(ledger, day=10) == baseline
    assert ledger.cell_rows(as_of="2020-01-11") == cells
    at_boundary = ledger.query(future, {"future_axis": {"future_state": 1.0}}, 1,
                               as_of="2020-01-20T00:00:00Z")
    assert at_boundary["prior"]["raw_count"] == 1
    after_boundary = ledger.query(future, {"future_axis": {"future_state": 1.0}}, 1,
                                  as_of="2020-01-20T00:00:00.000001Z")
    assert after_boundary["prior"]["raw_count"] == 2


def test_sixty_same_day_symbols_are_one_time_block_not_sixty_independent_bets():
    ledger = EVLedger(EVPolicy())
    for i in range(60):
        append(ledger, candidate(i, symbol=f"COIN{i}/USDT"), 1000)
    result = query(ledger, day=2)
    assert result["effective_samples"] == pytest.approx(60)
    assert result["effective_blocks"] == pytest.approx(1)
    assert result["status"] == "abstain"
    assert result["reason"] == "insufficient_blocks"
    assert result["prior"]["floor_stderr_bps"] == pytest.approx(50)
    assert result["would_allow"] is None


def test_block_length_is_at_least_outcome_horizon():
    ledger = EVLedger(policy(block_days=1))
    for i in range(5):
        append(ledger, candidate(i, day=i), 100, horizon=20)
    result = query(ledger, day=30, horizon=20)
    assert result["prior"]["block_days"] == 20
    assert result["prior"]["block_count"] == 1
    assert result["effective_blocks"] == pytest.approx(1)


@pytest.mark.parametrize("net_bps,status", [(500, "allow"), (-500, "veto")])
def test_supported_positive_and_negative_books(net_bps, status):
    ledger = EVLedger(EVPolicy(half_life_days=365.0))
    for i in range(40):
        append(ledger, candidate(i, day=i * 6), net_bps)
    result = query(ledger, day=240)
    assert result["status"] == status
    assert result["would_allow"] is (status == "allow")
    assert result["estimate_bps"] == pytest.approx(net_bps)
    assert result["stderr_bps"] > 0  # identical outcomes cannot imply certainty
    assert result["effective_blocks"] >= 8
    assert result["effective_samples"] >= 20


def test_sparse_state_cannot_pass_using_a_large_positive_prior():
    ledger = EVLedger(EVPolicy(half_life_days=365.0))
    for i in range(40):
        append(ledger, candidate(i, day=i * 6), 1000, {"trend": {"high": 1.0}})
    append(ledger, candidate(99, day=236), 1000, {"trend": {"low": 1.0}})
    result = query(ledger, day=240, memberships={"trend": {"high": .999, "low": .001}})
    assert result["estimate_bps"] == pytest.approx(1000)
    assert result["lower_bound_bps"] > 0
    assert result["status"] == "abstain"
    assert result["reason"] == "insufficient_samples"
    assert result["effective_samples"] == pytest.approx(1)
    assert result["would_allow"] is None


def test_decayed_stale_mass_is_not_confused_with_effective_sample_size():
    ledger = EVLedger(policy(half_life_days=1, min_effective_samples=20,
                             min_weight_mass=.1))
    for i in range(30):
        append(ledger, candidate(i, day=i % 3), 1000)
    result = query(ledger, day=20)
    assert result["effective_samples"] == pytest.approx(10 * 7 ** 2 / 21)
    assert result["weight_mass"] < .1
    assert result["reason"] == "stale_weight_mass"
    assert result["would_allow"] is None


def test_unknown_missing_or_unseen_contexts_never_silently_use_global_prior():
    ledger = EVLedger(policy())
    assert query(ledger)["reason"] == "cold_start"
    append(ledger, candidate())
    c = candidate("query", day=20)
    for memberships in ({}, {"trend": {"unknown": 1}}, {"absent_axis": {"high": 1}}):
        result = ledger.query(c, memberships, 1, as_of=c["context"]["available_at"])
        assert result["would_allow"] is None
        assert result["reason"] == "unknown_context"
    result = query(ledger, memberships={"trend": {"unseen_state": 1}})
    assert result["reason"] == "cold_start"


def test_axes_use_uniform_additive_means_and_linear_error_bound():
    ledger = EVLedger(policy(prior_strength=2))
    append(ledger, candidate(0, day=0), 200,
           {"trend": {"up": 1}, "vol": {"high": 1}})
    append(ledger, candidate(1, day=3), -100,
           {"trend": {"up": 1}, "vol": {"low": 1}})
    result = query(ledger, memberships={"trend": {"up": 1}, "vol": {"high": .6, "low": .4}})
    trend, vol = result["axes"]["trend"], result["axes"]["vol"]
    expected_vol = .6 * vol["states"]["high"]["estimate_bps"] + .4 * vol["states"]["low"]["estimate_bps"]
    assert vol["estimate_bps"] == pytest.approx(expected_vol)
    assert result["estimate_bps"] == pytest.approx((trend["estimate_bps"] + expected_vol) / 2)
    assert result["stderr_bps"] == pytest.approx((trend["stderr_bps"] + vol["stderr_bps"]) / 2)
    assert result["axis_weighting"] == "uniform_additive"


def test_cluster_sandwich_matches_hand_calculation_and_dominates_individual_se():
    ledger = EVLedger(policy(half_life_days=1e12, block_days=5, prior_strength=0))
    # Equal returns within each timestamp: 4 rows but only 2 independent groups.
    for i, (day, value) in enumerate([(0, 100), (0, 100), (10, -100), (10, -100)]):
        append(ledger, candidate(i, day=day), value)
    result = query(ledger, day=20)
    stats = result["prior"]
    assert stats["effective_blocks"] == pytest.approx(2)
    # G/(G-1) * ((2*100)^2 + (-2*100)^2) / 4^2 = 10000.
    assert stats["cluster_stderr_bps"] == pytest.approx(100)
    assert stats["individual_stderr_bps"] == pytest.approx(math.sqrt(10000 / 3))
    assert result["stderr_bps"] == pytest.approx(100)


@pytest.mark.parametrize("memberships", [
    {"trend": {"up": -.1, "down": 1.1}}, {"trend": {"up": .9}},
    {"trend": {"up": float("nan")}}, {"trend": {"up": float("inf")}},
    {"trend": {"up": True}}, {"trend": {}}, {"": {"up": 1}},
])
def test_invalid_memberships_are_rejected(memberships):
    ledger = EVLedger(policy())
    with pytest.raises(ValueError):
        ledger.add(candidate(), outcome(candidate()), memberships, cutoff="2020-02-01")
    with pytest.raises(ValueError):
        ledger.query(candidate(), memberships, 1, as_of="2020-02-01")


@pytest.mark.parametrize("changes", [
    {"status": "censored_end_of_data"}, {"scope": "ghost"},
    {"execution_flags": ["borrow_limit"]}, {"execution_flags": None},
    {"net_return_bps": None}, {"net_return_bps": float("nan")},
    {"net_return_bps": True}, {"horizon_bars": True}, {"horizon_bars": 0},
    {"candidate_id": "other"}, {"direction": "short"},
    {"available_at": "2020-01-01T00:00:00Z"},
    {"available_at": "2020-01-01T12:00:00Z"},
    {"available_at": "2020-02-01T00:00:00Z"},
])
def test_inadmissible_or_noncausal_labels_are_rejected(changes):
    ledger = EVLedger(policy())
    c = candidate()
    with pytest.raises(ValueError):
        ledger.add(c, outcome(c) | changes, {"trend": {"high": 1}}, cutoff="2020-02-01")


def test_missing_flags_are_not_treated_as_known_executable():
    ledger = EVLedger(policy())
    c, o = candidate(), outcome(candidate())
    del o["execution_flags"]
    with pytest.raises(ValueError, match="execution_flags"):
        ledger.add(c, o, {"trend": {"high": 1}}, cutoff="2020-02-01")


def test_query_cannot_precede_candidate_availability():
    ledger = EVLedger(policy())
    with pytest.raises(ValueError, match="cannot precede"):
        ledger.query(candidate(day=10), {"trend": {"high": 1}}, 1, as_of="2020-01-01")


def test_cell_rows_are_detached_causal_and_export_ready():
    ledger = EVLedger(policy())
    append(ledger, candidate())
    rows = ledger.cell_rows(as_of="2020-01-03")
    assert len(rows) == 1
    assert rows[0]["snapshot_version"] == "costs-v1"
    assert rows[0]["horizon_bars"] == 1
    assert rows[0]["raw_count"] == 1
    rows[0]["estimate_bps"] = -10000
    assert query(ledger)["estimate_bps"] == 100
    assert ledger.cell_rows(as_of="2020-01-02") == []


def test_no_ledger_operation_mutates_the_policy():
    original = EVPolicy()
    ledger = EVLedger(original)
    append(ledger, candidate())
    query(ledger)
    ledger.cell_rows(as_of="2020-02-01")
    assert original == replace(original)


def test_conflicting_outcome_or_book_identity_is_rejected_not_counted_twice():
    ledger = EVLedger(policy())
    c = candidate()
    append(ledger, c, 100)
    with pytest.raises(ValueError, match="conflicting replay"):
        append(ledger, c, 101)
    with pytest.raises(ValueError, match="conflicting replay"):
        append(ledger, candidate(signal_version="v2"), 100)
    assert query(ledger)["raw_count"] == 1


def test_positive_mean_does_not_override_the_predeclared_ev_threshold():
    ledger = EVLedger(EVPolicy(half_life_days=365, min_ev_bps=600))
    for i in range(40):
        append(ledger, candidate(i, day=i * 6), 500)
    result = query(ledger, day=240)
    assert result["estimate_bps"] > 0
    assert result["status"] == "veto"
    assert result["reason"] == "lower_bound_not_positive"


def test_complete_weight_underflow_is_unknown_not_an_export_error():
    ledger = EVLedger(policy(half_life_days=.001, prior_strength=0))
    append(ledger, candidate(), 100)
    result = query(ledger, day=20)
    rows = ledger.cell_rows(as_of="2020-01-21")
    assert result["would_allow"] is None
    assert result["estimate_bps"] == 100
    assert rows[0]["weight_mass"] == 0
    assert rows[0]["estimate_bps"] == 100


@pytest.mark.parametrize("memberships", [
    {"trend": {"up": .5, 1: .5}}, {"trend": {None: 1}},
    {"trend": {"up": 1}, 1: {"down": 1}},
])
def test_invalid_mixed_label_types_produce_a_contract_error(memberships):
    ledger = EVLedger(policy())
    with pytest.raises(ValueError):
        append(ledger, candidate(), memberships=memberships)


def test_common_aging_preserves_effective_counts_despite_squared_weight_underflow():
    ledger = EVLedger(policy(half_life_days=10))
    for i, day in enumerate((0, 2, 4)):
        append(ledger, candidate(i, day=day), i * 100)
    recent = query(ledger, day=10)
    # 2^-600 remains finite, but its square underflows to zero in float64.
    aged = query(ledger, day=6010)
    assert aged["weight_mass"] > 0
    assert aged["weight_mass"] ** 2 == 0
    assert aged["weight_mass"] == pytest.approx(recent["weight_mass"] * 2 ** -600, rel=1e-12, abs=0)
    assert aged["effective_samples"] == pytest.approx(recent["effective_samples"])
    assert aged["effective_blocks"] == pytest.approx(recent["effective_blocks"])
    assert aged["prior"]["mean_bps"] == pytest.approx(recent["prior"]["mean_bps"])
    assert aged["prior"]["stderr_bps"] == pytest.approx(recent["prior"]["stderr_bps"])
    assert aged["status"] == "abstain"
    assert aged["reason"] == "stale_weight_mass"


@pytest.mark.parametrize("half_life", [.001, 1e-300, 5e-324])
def test_tiny_positive_half_life_abstains_without_numerical_failure(half_life):
    ledger = EVLedger(policy(half_life_days=half_life))
    for i in range(3):
        append(ledger, candidate(i, day=i), 100)
    result = query(ledger, day=20)
    rows = ledger.cell_rows(as_of="2020-01-21")
    assert result["status"] == "abstain"
    assert result["would_allow"] is None
    assert result["weight_mass"] == 0
    assert math.isfinite(result["stderr_bps"])
    assert rows[0]["raw_count"] == 3
