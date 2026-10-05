"""Dataset/serving checks only: no model fitting, updates or training jobs."""
from copy import deepcopy
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from backtest.coin_selector import _account_compatibility, create_selector, restore_selector, write_selector_report
from research.ml_selection import dataset as data
from research.ml_selection.membership import audit_membership
from tests.test_ml_selection_dataset import candles


def market():
    frames = {"BTC/USDT": candles(180, trend=.4), "A/USDT": candles(180, trend=.1)}
    for frame in frames.values():
        frame["quote_volume"] = frame.volume * frame.close
    return frames


def test_inference_never_builds_outcomes_and_keeps_the_exact_serving_inputs(monkeypatch):
    frames = market()
    frames["A/USDT"]["available_at"] = frames["A/USDT"].index + data.DAY
    frames["A/USDT"].loc[frames["A/USDT"].index[80], "available_at"] += data.DAY
    old = data.build_dataset(frames, horizon_bars=20, min_quote_volume=1e6)
    monkeypatch.setattr(data, "_shadow_labels", lambda *args, **kwargs: pytest.fail("inference traversed future labels"))
    inferred = data.build_inference_dataset(frames, horizon_bars=20, min_quote_volume=1e6)
    pd.testing.assert_frame_equal(inferred, old.loc[:, list(inferred.columns)])
    assert not any(column.startswith("label_") for column in inferred)
    assert inferred.attrs["future_labels_computed"] is False
    assert inferred.attrs["data_identity"] == old.attrs["data_identity"]
    assert inferred.loc[inferred.as_of == frames["A/USDT"].index[-1] + data.DAY, "eligible"].all()


def test_benchmark_rolling_features_and_availability_are_built_once(monkeypatch):
    frames = market()
    frames.update({f"ALT{i}": candles(180, trend=i / 10) for i in range(8)})
    feature_calls, benchmark_availability_calls = [], []
    original_features, original_availability = data._feature_table, data._history_available

    def count_features(frame):
        feature_calls.append(id(frame))
        return original_features(frame)

    def count_availability(frame, window):
        if window == 21:
            benchmark_availability_calls.append(id(frame))
        return original_availability(frame, window)

    monkeypatch.setattr(data, "_feature_table", count_features)
    monkeypatch.setattr(data, "_history_available", count_availability)
    result = data.build_inference_dataset(frames)
    assert len(result) == 180 * len(frames)
    assert len(feature_calls) == len(set(feature_calls)) == len(frames)
    assert len(benchmark_availability_calls) == 1


@pytest.mark.parametrize("as_mapping", [False, True])
def test_relisting_intervals_match_source_audit_and_respect_publication(as_mapping):
    frame = candles(220)
    intervals = [
        {"listed_at": frame.index[0], "available_at": frame.index[0], "source": "exchange:listing1",
         "delisted_at": frame.index[110], "delisting_available_at": frame.index[105]},
        {"listed_at": frame.index[130], "available_at": frame.index[133], "source": "exchange:listing2"},
    ]
    facts = {"A": intervals} if as_mapping else pd.DataFrame([{"symbol": "A", **item} for item in intervals])
    rows = data.build_inference_dataset({"A": frame}, membership=facts, require_verified_membership=True)
    audit, report = audit_membership(facts, rows[["symbol", "as_of"]])
    mature = rows.contiguous_history >= 61
    assert rows.loc[mature, "eligible"].tolist() == audit.loc[mature, "membership_eligible"].tolist()
    assert report["valid_evidence_rows"] == 2 and not report["full_pit_coverage"]
    indexed = rows.set_index("as_of")
    assert indexed.loc[frame.index[109], "eligible"]
    assert not indexed.loc[frame.index[110], "eligible"]
    assert not indexed.loc[frame.index[132], "eligible"]
    assert indexed.loc[frame.index[133], "eligible"]


def test_unknown_sources_retain_legacy_weak_basis_but_fail_strict_membership():
    frame = candles(100)
    facts = {"A": {"listed_at": frame.index[0], "available_at": frame.index[0]}}
    legacy = data.build_inference_dataset({"A": frame}, membership=facts)
    strict = data.build_inference_dataset({"A": frame}, membership=facts, require_verified_membership=True)
    assert legacy.eligible.sum() == 40
    assert legacy.membership_basis.eq("supplied_point_in_time_facts").all()
    assert not strict.eligible.any()
    assert not data.build_inference_dataset({"A": frame}, require_verified_membership=True).eligible.any()


def test_mixed_utc_serialization_and_missing_availability_fail_closed():
    frame = candles(100)
    known = frame.index + data.DAY
    # Three serialized formats describe exactly the same UTC cutoff.
    frame["available_at"] = [stamp.isoformat() if i % 3 == 0 else
        stamp.tz_convert("Asia/Singapore").isoformat() if i % 3 == 1 else
        stamp.strftime("%Y-%m-%dT%H:%M:%SZ") for i, stamp in enumerate(known)]
    rows = data.build_inference_dataset({"A": frame})
    assert rows.eligible.sum() == 40
    pd.testing.assert_series_equal(rows.available_at, pd.Series(known, name="available_at"), check_freq=False)
    frame.loc[frame.index[65], "available_at"] = None
    unavailable = data.build_inference_dataset({"A": frame})
    assert not unavailable.loc[unavailable.bar_time == frame.index[65], "eligible"].iloc[0]
    assert unavailable.attrs["data_identity"]["symbols"]["A"]["missing_availability_rows"] == 1


def test_quote_volume_provenance_is_per_row_and_strict_policy_rejects_proxies():
    frame = candles(100)
    legacy = data.build_inference_dataset({"A": frame}, min_quote_volume=1e6)
    assert legacy.eligible.sum() == 40
    assert legacy.quote_volume_basis.eq("close_times_base_volume_proxy").all()
    strict = data.build_inference_dataset({"A": frame}, require_exchange_quote_volume=True)
    assert not strict.eligible.any()
    assert strict.iloc[65].exclusion_reason == "quote_volume_source_unverified"
    frame["quote_volume"] = frame.volume * frame.close
    undeclared = data.build_inference_dataset({"A": frame}, require_exchange_quote_volume=True)
    assert not undeclared.eligible.any()
    assert undeclared.quote_volume_basis.eq("supplied_quote_volume_unverified").all()
    frame["quote_volume_basis"] = "exchange_quote_volume"
    frame.loc[frame.index[65], "quote_volume_basis"] = "close_times_base_volume_proxy"
    rows = data.build_inference_dataset({"A": frame}, require_exchange_quote_volume=True)
    assert rows.eligible.sum() == 39
    counts = rows.attrs["data_identity"]["symbols"]["A"]["quote_volume_basis_counts"]
    assert counts == {"exchange_quote_volume": 99, "close_times_base_volume_proxy": 1}


def test_data_identity_changes_for_revised_prices_not_only_for_a_new_date():
    frames = market()
    first = data.build_inference_dataset(frames)
    changed = deepcopy(frames)
    changed["A/USDT"].loc[changed["A/USDT"].index[-1], "volume"] *= 2
    revised = data.build_inference_dataset(changed)
    left = first.attrs["data_identity"]
    right = revised.attrs["data_identity"]
    assert left["symbols"]["A/USDT"]["latest_bar_time"] == right["symbols"]["A/USDT"]["latest_bar_time"]
    assert left["data_identity_sha256"] != right["data_identity_sha256"]
    assert left["symbols"]["BTC/USDT"]["frame_sha256"] == right["symbols"]["BTC/USDT"]["frame_sha256"]


def test_empty_inference_universe_has_a_stable_identity_and_no_labels():
    empty = data.build_inference_dataset({})
    assert empty.empty and not any(column.startswith("label_") for column in empty)
    assert empty.attrs["data_identity"]["symbols"] == {}
    assert data.build_inference_dataset({"A": candles(0)}).empty


def test_legacy_account_identity_remains_loadable_but_does_not_claim_compatibility():
    model = SimpleNamespace(metadata={})
    result = _account_compatibility({}, model, model, "spot_margin")
    assert result["training_account_mode"] is None
    assert result["deployment_account_mode"] == "spot_margin"
    assert result["compatibility_verified"] is False


def test_explicit_account_contract_rejects_unverified_transfer_and_contradiction():
    contract = {"training_account_mode": "spot", "deployment_account_modes": ["spot"],
                "compatibility_verified": True, "cross_account_transfer_verified": False}
    model = SimpleNamespace(metadata={"account_contract": contract})
    assert _account_compatibility({}, model, model, "spot")["compatibility_verified"]
    with pytest.raises(ValueError, match="does not support"):
        _account_compatibility({}, model, model, "spot_margin")
    with pytest.raises(ValueError, match="requires a deployment"):
        _account_compatibility({}, model, model, None)
    policy = SimpleNamespace(metadata={"account_contract": {**contract, "training_account_mode": "spot_margin"}})
    with pytest.raises(ValueError, match="disagree"):
        _account_compatibility({}, model, policy, "spot")


def test_frozen_loader_has_no_label_traversal_and_replay_checks_market_identity(monkeypatch, tmp_path):
    pytest.importorskip("lightgbm")
    monkeypatch.setattr(data, "_shadow_labels", lambda *args, **kwargs: pytest.fail("frozen loader built labels"))
    frames = market()
    selector, identity = create_selector(frames, initial_capital=100000, account_mode="spot_margin")
    assert identity["future_labels_computed"] is False
    assert identity["training_latest_label_available_at"] < identity["training_end_exclusive"]
    assert identity["market_watermarks"]["A/USDT"]["latest_bar_time"].endswith("Z")
    assert identity["account_contract"]["status"] == "legacy_account_compatibility_unverified"
    timings = identity["timing_seconds"]
    assert timings["total_seconds"] >= timings["model_load_seconds"] + timings["feature_build_seconds"]
    report = write_selector_report(tmp_path, selector, identity)
    assert report["selector_funnel"]["selected_count_semantics"] == "selector_acceptance_not_order_or_fill"
    execution = {"coin_selector": report, "capital": 100000, "account_mode": "spot_margin"}
    restored = restore_selector(execution, frames, tmp_path)
    pd.testing.assert_frame_equal(restored.table, selector.table)
    frames["A/USDT"].loc[frames["A/USDT"].index[-1], "volume"] *= 2
    with pytest.raises(ValueError, match="data_identity_sha256"):
        restore_selector(execution, frames, tmp_path)
