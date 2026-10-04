"""Late source corrections must not rewrite earlier P1/P2 research forecasts."""
from copy import deepcopy
from dataclasses import asdict

import pandas as pd
import pytest

from core.signal_adaptive import build_adaptive_signal_meta
from core.signal_label_versions import OutcomeRevisionBook, PROTOCOL
from core.signal_meta_layer import build_signal_meta_layer, _validate_input
from core.signal_observation_types import fingerprint
from core.signal_outcomes import ObservationCosts
from tests.test_signal_meta_layer import facts, policy
from tests.test_signal_adaptive import adaptive_policy


def seal(row):
    row.pop("revision_id", None)
    row.pop("content_sha256", None)
    row["content_sha256"] = fingerprint(row)
    row["revision_id"] = "label_" + row["content_sha256"]
    return row


def versioned_facts(days=range(110)):
    payload = facts(days=days, horizons=(1,))
    payload["costs"] = asdict(ObservationCosts(account_mode="spot"))
    cost_hash = fingerprint(payload["costs"])
    payload["temporal_label_protocol"] = {"schema": PROTOCOL, "knowledge": "local", "cost_policy_sha256": cost_hash}
    payload["temporal_scope"] = {"decision_mode": "strict"}
    payload["outcome_revisions"] = []
    for c, o in zip(payload["candidates"], payload["outcomes"]):
        record = {"record_id": c["context"]["available_at"], "revision_id": "first",
            "event_time": c["context"]["available_at"], "observed_at": o["available_at"],
            "available_at": o["available_at"], "availability_evidence": {
                "kind": "local_receipt", "reference": "synthetic:test-fixture"},
            "data": {"open": 100., "high": 120., "low": 90.,
                     "close": 100 * (1 + o["net_return_bps"] / 10000), "volume": 10000.}}
        o.update(label_protocol=PROTOCOL, knowledge="local", observed_at=o["available_at"],
            account_mode="spot", cost_policy_sha256=cost_hash,
            training_eligible=True, source_versions=[{"dataset_id": "binance:spot:BTC/USDT:1d",
                "record_sha256": fingerprint(record), "record": record}])
        signal_record = deepcopy(record)
        signal_record.update(record_id=c["timestamp"], event_time=c["timestamp"],
            observed_at=c["context"]["available_at"], available_at=c["context"]["available_at"])
        o["candidate_source_version"] = {"dataset_id": "binance:spot:BTC/USDT:1d",
            "record_sha256": fingerprint(signal_record), "record": signal_record}
        seal(o)
        payload["outcome_revisions"].append(deepcopy(o))
    return payload


def correct(payload, cid, at, invalid=False):
    row = next(o for o in payload["outcomes"] if o["candidate_id"] == cid)
    row.update(available_at=at, observed_at=at, net_return_bps=-400.)
    record = row["source_versions"][0]["record"]
    record.update(revision_id="correction", available_at=at, observed_at=at)
    record["data"]["close"] = 96.
    row["source_versions"][0]["record_sha256"] = fingerprint(record)
    if invalid:
        row.update(status="censored_invalid_bar", training_eligible=False,
                   execution_flags=["invalid_bar"])
    seal(row)
    payload["outcome_revisions"].append(deepcopy(row))


def test_old_version_survives_until_correction_and_exact_cutoff_is_excluded():
    payload = versioned_facts(range(20))
    old = deepcopy(payload["outcomes"][1])
    correct(payload, "c001", "2020-01-15T00:00:00+00:00")
    h, c, _, o = _validate_input(payload)
    book = OutcomeRevisionBook(payload, c, h, o)
    assert book.as_of("2020-01-15T00:00:00Z")["c001", 1] == old
    assert book.as_of("2020-01-16T00:00:00Z")["c001", 1]["net_return_bps"] == -400.
    selected = book.as_of("2020-01-16T00:00:00Z")
    selected["c001", 1]["net_return_bps"] = 1e9
    assert book.as_of("2020-01-16T00:00:00Z")["c001", 1]["net_return_bps"] == -400.


def test_label_revision_cannot_rewrite_the_frozen_candidate_source():
    payload = versioned_facts(range(20))
    correct(payload, "c001", "2020-01-15T00:00:00+00:00")
    row = payload["outcome_revisions"][-1]
    source = row["candidate_source_version"]
    source["record"]["data"]["high"] += 7
    source["record_sha256"] = fingerprint(source["record"])
    seal(row)
    payload["outcomes"][1] = deepcopy(row)
    result = build_signal_meta_layer(payload, policy())
    assert result["status"] == "incomplete"
    assert "frozen candidate source" in result["errors"][0]["message"]


def test_p1_revision_does_not_change_earlier_frozen_fold():
    payload = versioned_facts(range(25))
    original = build_signal_meta_layer(payload, policy())
    correct(payload, "c010", "2020-01-14T00:00:00+00:00")
    changed = build_signal_meta_layer(payload, policy())
    assert changed["status"] == "complete", changed.get("errors")
    assert changed["folds"][:2] == original["folds"][:2]
    assert changed["folds"][2]["training_digest"] != original["folds"][2]["training_digest"]
    historical = lambda r: [p for p in r["predictions"] if p["fold_id"] in (None, "fold_0000", "fold_0001")]
    assert historical(changed) == historical(original)


def test_invalid_new_version_stops_training_without_erasing_old_evidence():
    payload = versioned_facts(range(25))
    original = build_signal_meta_layer(payload, policy())
    correct(payload, "c010", "2020-01-14T00:00:00+00:00", invalid=True)
    changed = build_signal_meta_layer(payload, policy())
    assert changed["status"] == "complete", changed.get("errors")
    assert changed["folds"][:2] == original["folds"][:2]
    assert changed["folds"][2]["training_observations"] == original["folds"][2]["training_observations"] - 1


def test_p2_inner_forecasts_use_the_version_known_at_their_own_instant():
    payload = versioned_facts()
    original = build_adaptive_signal_meta(payload, adaptive_policy())
    correct(payload, "c025", "2020-02-20T00:00:00+00:00")
    changed = build_adaptive_signal_meta(payload, adaptive_policy())
    assert changed["status"] == "complete", changed.get("errors")
    # Correction is known to the outer cutoff, but not to earlier forecasts.
    forecast = lambda r: next(p for p in r["calibration_predictions"]
        if p["candidate_id"] == "c045" and p["fold_id"] == "fold_0000")
    assert forecast(changed) == forecast(original)
    assert changed["folds"][0]["training_digest"] != original["folds"][0]["training_digest"]
    assert changed["regime_models"][:1] == original["regime_models"][:1]


def test_p2_future_correction_preserves_whole_earlier_fold():
    payload = versioned_facts()
    original = build_adaptive_signal_meta(payload, adaptive_policy())
    correct(payload, "c045", "2020-03-21T00:00:00+00:00")
    changed = build_adaptive_signal_meta(payload, adaptive_policy())
    assert changed["status"] == "complete", changed.get("errors")
    assert changed["folds"][0] == original["folds"][0]
    assert [p for p in changed["predictions"] if p["fold_id"] == "fold_0000"] == [
        p for p in original["predictions"] if p["fold_id"] == "fold_0000"]


@pytest.mark.parametrize("mutation", ["label_hash", "source_hash", "unknown", "missing_bar", "missing_protocol", "missing_revisions"])
def test_strict_training_rejects_unproven_or_mutated_evidence(mutation):
    payload = versioned_facts(range(20))
    row = payload["outcome_revisions"][0]
    if mutation == "label_hash":
        row["net_return_bps"] += 1
    elif mutation == "source_hash":
        row["source_versions"][0]["record"]["data"]["close"] += 1
        seal(row)
    elif mutation == "unknown":
        record = row["source_versions"][0]["record"]
        record.update(available_at=None, availability_evidence=None)
        row["source_versions"][0]["record_sha256"] = fingerprint(record)
        seal(row)
    elif mutation == "missing_bar":
        row["source_versions"] = []
        seal(row)
    elif mutation == "missing_protocol":
        payload.pop("temporal_label_protocol")
    elif mutation == "missing_revisions":
        payload["outcome_revisions"] = []
    result = build_signal_meta_layer(payload, policy())
    assert result["status"] == "incomplete"
    assert not result["predictions"]


def test_normal_engine_runs_strict_p0_through_p3_without_changing_official_account(tmp_path, monkeypatch):
    from backtest.engine import BacktestEngine
    from core.reproducibility import deterministic_result_digest
    from tests.engine_baseline_harness import build_synthetic_data_map
    from config.config import config
    settings_config = deepcopy(config._config)
    settings_config["account"]["mode"] = "spot"
    settings_config["execution"]["fee_schedule"]["market_type"] = "spot"
    monkeypatch.setattr(config, "_config", settings_config)
    frames = build_synthetic_data_map(symbols=("A", "M", "Z"), bars=110)
    for frame in frames.values():
        frame["available_at"] = pd.to_datetime(frame.index, utc=True) + pd.Timedelta(days=1)
        frame["observed_at"] = frame["available_at"]
        frame["revision_id"] = "synthetic-original"
        frame.attrs["availability_evidence"] = {"kind": "source_publication", "reference": "synthetic:engine-contract"}
    settings = {"initial_capital": 10000, "timeframe": "1d", "account_mode": "spot",
        "temporal_policy": {"mode": "strict", "store_path": str(tmp_path / "versions")},
        "signal_observation": {"enabled": True, "horizons": [1, 3], "ghost_horizon": 3}}
    baseline = BacktestEngine(**settings).run(frames, routing_log_enabled=False)
    active = BacktestEngine(**settings, signal_adaptive=adaptive_policy(),
        signal_meta_replay={"enabled": True, "horizon_bars": 3}).run(frames, routing_log_enabled=False)
    assert deterministic_result_digest(baseline) == deterministic_result_digest(active)
    p0 = active["signal_observation"]
    assert p0["status"] == "complete", p0["errors"]
    assert p0["outcome_revisions"]
    assert p0["temporal_scope"]["strict_training_eligible"]
    assert not p0["temporal_scope"]["outcomes_point_in_time_certified"]
    for name in ("signal_meta_layer", "signal_adaptive", "signal_meta_replay"):
        assert active[name]["status"] == "complete", active[name].get("errors")
    assert active["signal_adaptive"]["folds"]


def test_unknown_historical_inputs_do_not_become_strict_training_examples(tmp_path):
    from backtest.engine import BacktestEngine
    from tests.engine_baseline_harness import build_synthetic_data_map
    result = BacktestEngine(temporal_policy={"mode": "strict", "store_path": str(tmp_path / "versions")},
        signal_meta_layer={"enabled": True}).run(build_synthetic_data_map(bars=45), routing_log_enabled=False)
    assert result["signal_observation"]["candidates"] == []
    assert result["signal_observation"]["outcome_revisions"] == []
    assert result["signal_meta_layer"]["predictions"] == []
