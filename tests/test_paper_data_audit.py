"""Audits distinguish genuine known input contracts from missing historical evidence."""
import hashlib
import json

import pandas as pd
import pytest

from analysis.paper_data_audit import audit_frame, audit_manifest, membership_at


IDENTITY = {"symbol": "BTC/USDT", "venue": "binance", "market_type": "spot"}
UNITS = {"timestamp": "iso8601", "price": "quote_per_base", "volume": "base"}
LISTING = {"kind": "spot_listed", "effective_at": "2019-01-01", "available_at": "2018-12-31",
           "source_status": "verified"}


def frame():
    index = pd.date_range("2020-01-01", periods=3, tz="UTC")
    return pd.DataFrame({"timestamp": index, "open": 100., "high": 102., "low": 98.,
                         "close": 101., "volume": 50., "available_at": index + pd.Timedelta(days=1)})


def audit(data=None, *, events=None, units=None, **kwargs):
    return audit_frame(frame() if data is None else data, identity=IDENTITY, timeframe="1d",
                       as_of="2020-01-10", units=UNITS if units is None else units,
                       membership_events=[LISTING] if events is None else events, **kwargs)


def codes(out):
    return {item["code"] for item in out["errors"] + out["warnings"]}


def test_complete_known_market_units_and_availability_pass():
    out = audit(expected_start="2020-01-01", expected_end="2020-01-04")
    assert out["quality_status"] == "pass" and out["research_ready"]
    assert out["coverage"]["missing_bars"] == 0
    assert out["membership"]["eligible_rows"] == 3
    assert out["availability"]["verified_rows"] == 3


def test_no_listing_evidence_does_not_infer_tradability_from_first_candle():
    out = audit(events=[])
    assert out["membership"]["unknown_rows"] == 3
    assert out["membership"]["eligible_rows"] == 0
    assert "historical_membership_unknown" in codes(out)
    assert not out["research_ready"]
    assert not out["membership"]["observed_bar_bounds_are_listing_facts"]


def test_future_membership_and_wrong_market_events_cannot_enter_historical_universe():
    future = {**LISTING, "available_at": "2020-01-05"}
    unknown = membership_at(IDENTITY, "2020-01-01", [future])
    assert unknown["status"] == "unknown" and unknown["eligible"] is None
    assert unknown["future_evidence_ignored"] == 1
    wrong = {**LISTING, "kind": "margin_listed"}
    assert audit(events=[future, wrong])["membership"]["unknown_rows"] == 3
    known = membership_at(IDENTITY, "2020-01-06", [future])
    assert known["eligible"] is True


def test_verified_delisting_censors_later_bars_without_dropping_old_history():
    event = {"kind": "spot_delisted", "effective_at": "2020-01-03", "available_at": "2020-01-02",
             "source_status": "verified"}
    out = audit(events=[LISTING, event])
    assert out["membership"] == {"eligible_rows": 2, "ineligible_rows": 1, "unknown_rows": 0,
                                 "observed_bar_bounds_are_listing_facts": False}
    assert "bars_outside_verified_membership" in codes(out)


def test_internal_leading_and_trailing_gaps_are_counted_without_fill():
    data = frame().drop(1)
    out = audit(data, expected_start="2019-12-31", expected_end="2020-01-05")
    assert out["coverage"]["missing_bars"] == 3
    assert out["coverage"]["internal_missing_bars"] == 1
    assert "missing_bar" in codes(out)
    assert len(data) == 2


def test_duplicate_and_unsorted_times_are_not_silently_normalized():
    data = pd.concat([frame(), frame().iloc[:1]], ignore_index=True)
    out = audit(data)
    assert {"duplicate_timestamp", "timestamps_not_strictly_increasing"} <= codes(out)


@pytest.mark.parametrize("column,value,code", [
    ("volume", -1, "invalid_volume"), ("close", 500, "invalid_ohlc"),
    ("low", float("nan"), "invalid_ohlc"), ("open", 0, "invalid_ohlc")])
def test_invalid_ohlcv_reports_quality_without_asserting_manipulation(column, value, code):
    data = frame()
    data.loc[1, column] = value
    out = audit(data)
    assert code in codes(out)
    assert out["manipulation_inference"] == "not_identifiable_from_OHLCV"


def test_numeric_timestamp_units_are_required_and_wrong_precision_fails():
    data = frame()
    data["timestamp"] = data["timestamp"].astype("int64") // 1_000_000
    units = {**UNITS, "timestamp": "ms"}
    assert audit(data, units=units)["quality_status"] == "pass"
    assert "invalid_timestamp_or_unit" in codes(audit(data, units={**units, "timestamp": "s"}))
    assert "timestamp_unit_or_parse_error" in codes(audit(data, units=UNITS))


def test_quote_volume_is_not_silently_reinterpreted_as_base_volume():
    out = audit(units={**UNITS, "volume": "quote"})
    assert "volume_not_base_units" in codes(out) and not out["research_ready"]
    missing = audit(units={"timestamp": "iso8601"})
    assert {"price_unit_unknown", "volume_unit_unknown"} <= codes(missing)


def test_identity_mismatch_and_settlement_suffix_are_preserved():
    data = frame()
    data["symbol"] = "BTC/USDT:USDT"
    data.attrs["provider"] = "okx"
    out = audit(data)
    assert sum(item["code"] == "identity_mismatch" for item in out["errors"]) == 2


def test_available_at_missing_future_or_before_bar_close_is_not_guessed():
    assert "available_at_unknown" in codes(audit(frame().drop(columns="available_at")))
    data = frame()
    data.loc[0, "available_at"] = pd.Timestamp("2020-01-01", tz="UTC")
    data.loc[1, "available_at"] = pd.Timestamp("2020-01-11", tz="UTC")
    assert {"invalid_available_at", "available_at_after_as_of"} <= codes(audit(data))


def bundle(tmp_path):
    source = tmp_path / "BTC.csv"
    frame().to_csv(source, index=False)
    item = {"path": source.name, "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            **IDENTITY, "timeframe": "1d", "units": UNITS, "membership_events": [LISTING]}
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"schema": "paper_data_inputs/v1", "inputs": [item]}), encoding="utf-8")
    return source, manifest, item


def test_manifest_binds_exact_file_and_manifest_hashes_and_is_json_serializable(tmp_path):
    source, manifest, item = bundle(tmp_path)
    out = audit_manifest(manifest, as_of="2020-01-10")
    assert out["summary"]["research_ready"] == 1
    assert out["inputs"][0]["sha256"] == item["sha256"]
    assert out["manifest_sha256"] == hashlib.sha256(manifest.read_bytes()).hexdigest()
    assert json.loads(json.dumps(out, allow_nan=False)) == out
    source.write_text("this is replacement data", encoding="utf-8")
    bad = audit_manifest(manifest, as_of="2020-01-10")
    assert bad["inputs"][0]["errors"] == [{"code": "sha256_mismatch"}]


def test_manifest_cannot_follow_traversal_or_open_final_samples(tmp_path):
    _, manifest, item = bundle(tmp_path)
    manifest.write_text(json.dumps({"split": "holdout", "inputs": [item]}), encoding="utf-8")
    with pytest.raises(ValueError, match="holdout"):
        audit_manifest(manifest, as_of="2020-01-10")
    manifest.write_text(json.dumps({"inputs": [{**item, "path": "../other.csv"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="bundle"):
        audit_manifest(manifest, as_of="2020-01-10")
    with pytest.raises(ValueError, match="adjudication"):
        audit_manifest(manifest, as_of="2020-01-10", split="final")
