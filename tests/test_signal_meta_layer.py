"""P1 causal replay contracts, using deliberately small synthetic calendars."""
from copy import deepcopy
from dataclasses import replace

import pandas as pd
import pytest

from backtest.reporting.signal_meta_layer import signal_meta_layer_digest
from core.signal_ev_types import EVPolicy, context_memberships
from core.signal_meta_layer import build_signal_meta_layer


def facts(days=range(20), horizons=(1, 3), timeframe="1d"):
    result = {"schema": "signal_observation/v1", "status": "complete",
              "policy": {"horizons": list(horizons)}, "snapshot_version": "snapshot-v1",
              "strategy_versions": {"Trend": "trend-v1"},
              "candidates": [], "decisions": [], "outcomes": []}
    for index, day in enumerate(days):
        entry = pd.Timestamp("2020-01-01", tz="UTC") + pd.Timedelta(days=day)
        delta = pd.Timedelta(timeframe)
        c = {"candidate_id": f"c{index:03}", "timestamp": (entry-delta).isoformat(),
             "symbol": "BTC/USDT", "strategy": "Trend", "direction": "long", "signal_version": "trend-v1",
             "context": {"available_at": entry.isoformat(), "timeframe": timeframe,
                         "snapshot_version": "snapshot-v1", "market_state": "TREND_UP",
                         "features": {"efficiency_ratio_10": .6, "volatility_ratio_8_48": 1.0}}}
        result["candidates"].append(c)
        result["decisions"].append({"candidate_id": c["candidate_id"], "veto_stage": "routing"})
        for h in horizons:
            result["outcomes"].append({"candidate_id": c["candidate_id"], "symbol": c["symbol"],
                "strategy": c["strategy"], "direction": c["direction"], "horizon_bars": h,
                "scope": "independent_fixed_notional_signal_diagnostic", "status": "matured",
                "available_at": (entry+h*delta).isoformat(), "net_return_bps": 100.+index,
                "execution_flags": []})
    return result


def policy(**changes):
    return EVPolicy(**({"enabled": True, "train_days": 6, "test_days": 5, "embargo_days": 2,
                        "block_days": 1, "min_effective_samples": 2., "min_effective_blocks": 2.,
                        "min_weight_mass": .01} | changes))


def score(result, cid, horizon=1):
    return next(p for p in result["predictions"] if p["candidate_id"] == cid and p["horizon_bars"] == horizon)


def test_default_disabled_and_input_untouched():
    payload = facts()
    original = deepcopy(payload)
    assert build_signal_meta_layer(payload) is None
    result = build_signal_meta_layer(payload, policy())
    assert payload == original
    assert result["status"] == "complete"
    assert all(result["validation"].values())
    assert len(result["predictions"]) == len(result["evaluations"]) == 40
    assert result["protocol"]["anchor"] == "2020-01-01T00:00:00+00:00"


def test_cutoff_strict_boundary_and_fold_boundary():
    result = build_signal_meta_layer(facts(), policy())
    first = result["folds"][0]
    assert first["training_cutoff"] == "2020-01-07T00:00:00+00:00"
    assert first["train_start"] == "2020-01-01T00:00:00+00:00"
    assert first["training_observations"] == 8  # H1 entries 0..4; H3 entries 0..2
    assert score(result, "c007")["reason"] == "training_window"
    assert score(result, "c008")["prior"]["raw_count"] == 5
    assert score(result, "c008", 3)["prior"]["raw_count"] == 3
    assert score(result, "c012")["fold_id"] == "fold_0000"
    assert score(result, "c013")["fold_id"] == "fold_0001"
    assert result["folds"][1]["train_start"] == "2020-01-06T00:00:00+00:00"


def test_future_mutation_does_not_change_frozen_fold_predictions_or_model_id():
    payload = facts()
    baseline = build_signal_meta_layer(payload, policy())
    for o in payload["outcomes"]:
        if pd.Timestamp(o["available_at"]) >= pd.Timestamp("2020-01-07", tz="UTC"):
            o["net_return_bps"] = -1e8
    changed = build_signal_meta_layer(payload, policy())
    historical = lambda result: [p for p in result["predictions"] if p["fold_id"] in (None, "fold_0000")]
    assert historical(baseline) == historical(changed)
    assert baseline["folds"][0] == changed["folds"][0]
    assert score(baseline, "c013")["estimate_bps"] != score(changed, "c013")["estimate_bps"]


def test_same_fold_mature_labels_wait_until_a_later_fold():
    payload = facts()
    baseline = build_signal_meta_layer(payload, policy(embargo_days=0))
    for o in payload["outcomes"]:
        if o["candidate_id"] == "c006":
            o["net_return_bps"] = -1e6
    changed = build_signal_meta_layer(payload, policy(embargo_days=0))
    for cid in ("c006", "c008", "c010"):
        assert score(baseline, cid) == score(changed, cid)
    assert score(baseline, "c011")["estimate_bps"] != score(changed, "c011")["estimate_bps"]


def test_same_clock_input_permutation_has_identical_full_research_digest():
    payload = facts(days=[0, 0, 1, 1, 3, 5, 8, 8, 13, 13])
    baseline = build_signal_meta_layer(payload, policy())
    for name in ("candidates", "decisions", "outcomes"):
        payload[name].reverse()
    changed = build_signal_meta_layer(payload, policy())
    assert signal_meta_layer_digest(changed) == signal_meta_layer_digest(baseline)


def test_prefix_tail_censoring_preserves_previous_predictions():
    payload = facts()
    baseline = build_signal_meta_layer(payload, policy())
    payload["candidates"] = payload["candidates"][:11]
    ids = {c["candidate_id"] for c in payload["candidates"]}
    payload["decisions"] = [d for d in payload["decisions"] if d["candidate_id"] in ids]
    payload["outcomes"] = [o for o in payload["outcomes"] if o["candidate_id"] in ids]
    for o in payload["outcomes"]:
        if pd.Timestamp(o["available_at"]) > pd.Timestamp("2020-01-11", tz="UTC"):
            o.update(status="censored_tail", available_at=None, net_return_bps=None)
    prefix = build_signal_meta_layer(payload, policy())
    assert prefix["predictions"] == [p for p in baseline["predictions"] if p["candidate_id"] in ids]
    assert any(e["realized_net_bps"] is None for e in prefix["evaluations"])


def test_long_hourly_horizon_not_mature_by_cutoff_cannot_train():
    result = build_signal_meta_layer(facts(horizons=(1, 72), timeframe="1h"), policy(embargo_days=0))
    assert score(result, "c006", 1)["prior"]["raw_count"] == 6
    assert score(result, "c006", 72)["prior"]["raw_count"] == 3


def test_flagged_and_p0_ineligible_rows_never_train_or_become_valid_labels():
    payload = facts()
    for d in payload["decisions"]:
        if d["candidate_id"] == "c000":
            d["veto_stage"] = "warmup"
    for o in payload["outcomes"]:
        if o["candidate_id"] == "c001":
            o["execution_flags"] = ["participation_exceeded"]
    result = build_signal_meta_layer(payload, policy())
    assert result["ingestion_audit"]["excluded_matured_labels"] == 4
    assert score(result, "c008")["prior"]["raw_count"] == 3
    assert [e for e in result["evaluations"] if e["candidate_id"] == "c000"][0]["execution_flags"] == ["p0_ineligible:warmup"]


@pytest.mark.parametrize("bad", ["missing_label", "duplicate", "future_time", "nan", "flags", "version", "features", "gate", "incomplete"])
def test_invalid_p0_fails_closed_with_audit(bad):
    payload = facts()
    if bad == "missing_label":
        payload["outcomes"].pop()
    elif bad == "duplicate":
        payload["outcomes"].append(deepcopy(payload["outcomes"][0]))
    elif bad == "future_time":
        payload["outcomes"][0]["available_at"] = payload["candidates"][0]["context"]["available_at"]
    elif bad == "nan":
        payload["outcomes"][0]["net_return_bps"] = float("nan")
    elif bad == "flags":
        del payload["outcomes"][0]["execution_flags"]
    elif bad == "version":
        payload["candidates"][0]["signal_version"] = "unknown"
    elif bad == "features":
        payload["candidates"][0]["context"]["features"] = "not_a_mapping"
    elif bad == "gate":
        del payload["decisions"][0]["veto_stage"]
    else:
        payload["status"] = "incomplete"
    result = build_signal_meta_layer(payload, policy())
    assert result["status"] == "incomplete"
    assert result["errors"][0]["reason"] == "invalid_p0_input"
    assert not result["predictions"]


def test_empty_cold_all_censored_and_unknown_have_no_false_allow():
    for payload in (facts(days=[]), facts(days=range(3)), facts()):
        for o in payload["outcomes"]:
            o.update(status="censored_tail", available_at=None, net_return_bps=None)
            o.pop("execution_flags")  # P0 censored rows have no execution decision.
        result = build_signal_meta_layer(payload, policy())
        assert result["status"] == "complete"
        assert all(p["status"] == "abstain" for p in result["predictions"])
    payload = facts()
    payload["candidates"][8]["context"]["features"] = {}
    assert score(build_signal_meta_layer(payload, policy()), "c008")["reason"] == "unknown_context"


def test_unusable_payload_is_contained_and_decision_audit_timestamps_are_supported():
    assert build_signal_meta_layer(None, policy())["status"] == "incomplete"
    payload = facts()
    payload["decisions"][0]["audit"] = {"timestamp": pd.Timestamp("2020-01-01")}
    original = build_signal_meta_layer(payload, policy())
    assert original["status"] == "complete"
    payload["decisions"][0]["veto_stage"] = "warmup"
    changed = build_signal_meta_layer(payload, policy())
    assert changed["input_identity"] != original["input_identity"]


@pytest.mark.parametrize("eff,vol,expected", [(.249, .799, ("low", "compressed")),
    (.25, .8, ("medium", "normal")), (.5, 1.2, ("high", "expanded")),
    (None, None, ("unknown", "unknown")), (1.5, -1, ("unknown", "unknown"))])
def test_fixed_axis_boundaries(eff, vol, expected):
    c = facts(days=[0])["candidates"][0]
    c["context"]["features"].update(efficiency_ratio_10=eff, volatility_ratio_8_48=vol)
    assert context_memberships(c) == {"market_state": {"TREND_UP": 1.},
        "efficiency": {expected[0]: 1.}, "volatility": {expected[1]: 1.}}


@pytest.mark.parametrize("changes", [{"enabled": 1}, {"half_life_days": 0}, {"confidence_z": float("nan")},
    {"train_days": 1.5}, {"embargo_days": -1}, {"min_ev_bps": "0"}, {"min_effective_blocks": 1},
    {"prior_strength": -1}, {"min_weight_mass": False}])
def test_policy_rejects_invalid_or_ambiguous_values(changes):
    with pytest.raises(ValueError):
        replace(EVPolicy(), **changes)
