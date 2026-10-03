"""Read-only audits of explicitly manifested OHLCV research inputs.

No discovery, downloads, trading, or inferred listing dates. Missing historical
membership/availability evidence remains unknown. OHLCV anomalies are quality
observations and cannot establish wash trading or market manipulation.
"""
from __future__ import annotations

from collections import Counter
import hashlib
import io
import json
import math
from pathlib import Path
from typing import Mapping

import pandas as pd

from core.timeframes import as_utc_timestamp, timeframe_delta
from core.universe import normalize_symbol


def _time(value):
    point = as_utc_timestamp(value)
    if pd.isna(point):
        raise ValueError("timestamp is required")
    return point


def _identity(value):
    symbol = normalize_symbol(value.get("symbol", ""))
    venue = str(value.get("venue") or value.get("provider") or "").strip().lower()
    market = str(value.get("market_type") or "").strip().lower()
    if not venue or market not in {"spot", "spot_margin", "perpetual", "swap", "future", "futures"}:
        raise ValueError("explicit symbol, venue/provider and market_type are required")
    return {"symbol": symbol, "venue": venue, "market_type": market}


def _issue(code, *, rows=None, detail=None):
    result = {"code": code}
    if rows is not None:
        result["rows"] = list(rows)[:20]
        result["count"] = len(rows)
    if detail is not None:
        result["detail"] = detail
    return result


def _timestamps(values, unit):
    numeric = pd.api.types.is_numeric_dtype(values)
    if numeric:
        if unit not in {"s", "ms", "us", "ns"}:
            raise ValueError("numeric timestamps require an explicit s/ms/us/ns unit")
        return pd.DatetimeIndex(pd.to_datetime(values, unit=unit, utc=True, errors="coerce"))
    if unit not in {None, "iso8601"}:
        raise ValueError("ISO timestamps disagree with the declared numeric timestamp unit")
    # Reject numeric-looking strings instead of letting them become calendar dates.
    if any(str(value).strip().replace(".", "", 1).lstrip("-").isdigit() for value in values):
        raise ValueError("numeric timestamp strings require numeric conversion with explicit units")
    return pd.DatetimeIndex(pd.to_datetime(values, utc=True, format="mixed", errors="coerce"))


def membership_at(identity: Mapping, at, events=()) -> dict:
    """Tri-state membership using verified, contemporaneously available facts."""
    identity, point = _identity(identity), _time(at)
    relevant, unknown, future = [], 0, 0
    prefix = "spot" if identity["market_type"] in {"spot", "spot_margin"} else "perpetual"
    for raw in events:
        event = dict(raw)
        if event.get("symbol") and normalize_symbol(event["symbol"]) != identity["symbol"]:
            continue
        if event.get("venue") and str(event["venue"]).lower() != identity["venue"]:
            continue
        if event.get("market_type") and event["market_type"] != identity["market_type"]:
            continue
        kind = event.get("kind")
        if kind not in {f"{prefix}_listed", f"{prefix}_delisted", "listed", "delisted"}:
            continue
        if event.get("source_status") != "verified":
            unknown += 1
            continue
        try:
            available, effective = _time(event["available_at"]), _time(event["effective_at"])
        except (KeyError, TypeError, ValueError):
            unknown += 1
            continue
        if available > point:
            future += 1
            continue
        if effective <= point:
            relevant.append((effective, kind.endswith("listed") and not kind.endswith("delisted")))
    if not relevant:
        return {"status": "unknown", "eligible": None, "evidence_count": 0,
                "unknown_evidence": unknown, "future_evidence_ignored": future}
    # At coincident boundaries a delisting is the conservative terminal fact.
    relevant.sort(key=lambda pair: (pair[0], not pair[1]))
    eligible = relevant[-1][1]
    return {"status": "eligible" if eligible else "ineligible", "eligible": eligible,
            "evidence_count": len(relevant), "unknown_evidence": unknown,
            "future_evidence_ignored": future}


def audit_frame(frame: pd.DataFrame, *, identity: Mapping, timeframe: str, as_of,
                units: Mapping | None = None, membership_events=(),
                expected_start=None, expected_end=None) -> dict:
    """Audit without sorting, filling gaps, changing units or inferring tradability.

    expected_end is exclusive. Missing leading/trailing bars are reported as
    requested coverage gaps, never filled or treated as evidence of a listing.
    Available_at must be explicit and on/after nominal bar close to be verified.
    """
    declared, cutoff = _identity(identity), _time(as_of)
    delta = pd.Timedelta(timeframe_delta(timeframe))
    units = dict(units or {})
    errors, warnings = [], []
    report = {"schema": "paper_ohlcv_audit/v1", "identity": declared,
              "timeframe": timeframe, "as_of": cutoff.isoformat(), "units": units,
              "rows": len(frame), "errors": errors, "warnings": warnings,
              "research_ready": False, "quality_status": "fail",
              "manipulation_inference": "not_identifiable_from_OHLCV",
              "coverage": {}, "membership": {}, "availability": {}}
    report["dataframe_sha256"] = hashlib.sha256(
        frame.to_csv(index=True, lineterminator="\n").encode("utf-8")).hexdigest()
    if units.get("price") not in {"quote_per_base"}:
        (warnings if units.get("price") is None else errors).append(_issue(
            "price_unit_unknown" if units.get("price") is None else "unsupported_price_unit"))
    if units.get("volume") not in {"base"}:
        (warnings if units.get("volume") is None else errors).append(_issue(
            "volume_unit_unknown" if units.get("volume") is None else "volume_not_base_units"))
    for key in ("symbol", "venue", "market_type"):
        attr = frame.attrs.get(key, frame.attrs.get("provider") if key == "venue" else None)
        if attr is not None:
            actual = normalize_symbol(attr) if key == "symbol" else str(attr).lower()
            if actual != declared[key]:
                errors.append(_issue("identity_mismatch", detail=f"attrs.{key}"))
        if key in frame.columns:
            bad = []
            for i, value in enumerate(frame[key]):
                try:
                    actual = normalize_symbol(value) if key == "symbol" else str(value).lower()
                except ValueError:
                    actual = None
                if actual != declared[key]:
                    bad.append(i)
            if bad:
                errors.append(_issue("identity_mismatch", rows=bad, detail=key))
    if "timeframe" in frame.attrs and frame.attrs["timeframe"] != timeframe:
        errors.append(_issue("timeframe_identity_mismatch"))
    values = frame["timestamp"] if "timestamp" in frame.columns else frame.index
    try:
        index = _timestamps(values, units.get("timestamp"))
    except (ValueError, TypeError, OverflowError) as exc:
        errors.append(_issue("timestamp_unit_or_parse_error", detail=str(exc)))
        return report
    invalid = [i for i, value in enumerate(index) if pd.isna(value)]
    if invalid:
        errors.append(_issue("invalid_timestamp_or_unit", rows=invalid))
        return report
    if index.has_duplicates:
        errors.append(_issue("duplicate_timestamp", rows=
                             [i for i, duplicate in enumerate(index.duplicated(keep=False)) if duplicate]))
    if not index.is_monotonic_increasing:
        errors.append(_issue("timestamps_not_strictly_increasing"))
    off_grid = [i for i, stamp in enumerate(index) if stamp.value % delta.value]
    if off_grid:
        errors.append(_issue("timestamp_off_timeframe_grid", rows=off_grid))
    future = [i for i, stamp in enumerate(index) if stamp + delta > cutoff]
    if future:
        errors.append(_issue("unclosed_or_future_bar", rows=future))
    if "timestamp_unit" in frame.columns and units.get("timestamp") in {"s", "ms", "us", "ns"}:
        bad = [i for i, value in enumerate(frame["timestamp_unit"]) if value != units["timestamp"]]
        if bad:
            errors.append(_issue("mixed_timestamp_units", rows=bad))
    required = {"open", "high", "low", "close", "volume"}
    if not required <= set(frame.columns):
        errors.append(_issue("missing_ohlcv_columns", detail=sorted(required - set(frame.columns))))
    else:
        invalid_prices, invalid_volume = [], []
        for i, row in enumerate(frame.to_dict("records")):
            try:
                open_, high, low, close = [float(row[key]) for key in ("open", "high", "low", "close")]
                valid = all(math.isfinite(x) and x > 0 for x in (open_, high, low, close))
                valid = valid and low <= min(open_, close) <= max(open_, close) <= high
            except (ValueError, TypeError):
                valid = False
            if not valid:
                invalid_prices.append(i)
            try:
                volume = float(row["volume"])
                valid_volume = math.isfinite(volume) and volume >= 0
            except (ValueError, TypeError):
                valid_volume = False
            if not valid_volume:
                invalid_volume.append(i)
        if invalid_prices:
            errors.append(_issue("invalid_ohlc", rows=invalid_prices))
        if invalid_volume:
            errors.append(_issue("invalid_volume", rows=invalid_volume))
    valid_available, availability_invalid, availability_future = [], [], []
    if "available_at" not in frame.columns:
        warnings.append(_issue("available_at_unknown", detail="nominal bar close is not historical version evidence"))
        report["availability"] = {"status": "unknown", "verified_rows": 0, "unknown_rows": len(frame)}
    else:
        for i, (stamp, raw) in enumerate(zip(index, frame["available_at"])):
            try:
                available = _time(raw)
            except (ValueError, TypeError):
                availability_invalid.append(i)
                continue
            if available < stamp + delta:
                availability_invalid.append(i)
            elif available > cutoff:
                availability_future.append(i)
            else:
                valid_available.append(i)
        if availability_invalid:
            errors.append(_issue("invalid_available_at", rows=availability_invalid))
        if availability_future:
            errors.append(_issue("available_at_after_as_of", rows=availability_future))
        report["availability"] = {"status": "verified" if len(valid_available) == len(frame) else "invalid",
                                  "verified_rows": len(valid_available), "unknown_rows": 0}
    members = Counter(membership_at(declared, stamp, membership_events)["status"] for stamp in index)
    report["membership"] = {"eligible_rows": members["eligible"], "ineligible_rows": members["ineligible"],
                            "unknown_rows": members["unknown"],
                            "observed_bar_bounds_are_listing_facts": False}
    if members["unknown"]:
        warnings.append(_issue("historical_membership_unknown", detail="missing contemporaneous verified listing evidence"))
    if members["ineligible"]:
        errors.append(_issue("bars_outside_verified_membership", detail=members["ineligible"]))
    unique = index.unique().sort_values()
    start = _time(expected_start) if expected_start is not None else (unique[0] if len(unique) else None)
    end = _time(expected_end) if expected_end is not None else (unique[-1] + delta if len(unique) else None)
    if start is not None and end is not None:
        if start >= end or end > cutoff or start.value % delta.value or end.value % delta.value:
            raise ValueError("expected interval must be aligned, ordered and closed by as_of")
        # Count gaps arithmetically rather than materializing years of tick grids.
        expected = int((end - start) / delta)
        within = unique[(unique >= start) & (unique < end)]
        missing = expected - sum(stamp.value % delta.value == 0 for stamp in within)
        outside = sum((stamp < start or stamp >= end) for stamp in index)
        internal_missing = sum(max(0, int((b - a) / delta) - 1) for a, b in zip(unique[:-1], unique[1:]))
        report["coverage"] = {"start": start.isoformat(), "end_exclusive": end.isoformat(),
                              "expected_rows": expected, "unique_rows_in_interval": len(within),
                              "missing_bars": missing, "internal_missing_bars": internal_missing,
                              "outside_requested_interval_rows": outside,
                              "first": unique[0].isoformat() if len(unique) else None,
                              "last": unique[-1].isoformat() if len(unique) else None}
        if missing:
            errors.append(_issue("missing_bar", detail=missing))
        if outside:
            errors.append(_issue("timestamp_outside_requested_interval", detail=outside))
    if frame.empty:
        errors.append(_issue("empty_data"))
    report["quality_status"] = "fail" if errors else ("warning" if warnings else "pass")
    report["research_ready"] = not errors and not warnings and bool(len(frame))
    return report


def _entries(manifest):
    if isinstance(manifest.get("inputs"), list):
        return manifest["inputs"]
    key = "symbols" if "symbols" in manifest else "markets"
    if not isinstance(manifest.get(key), Mapping):
        raise ValueError("manifest requires explicit inputs, symbols or markets")
    return [{"symbol": symbol, **record} for symbol, record in manifest[key].items()]


def audit_manifest(path: str | Path, *, as_of, split: str = "retrospective") -> dict:
    """Read only files listed in a manifest and bind each report to exact bytes.

    Preferred inputs: [{path, sha256, symbol, venue, market_type, timeframe,
    units:{timestamp:'iso8601', price:'quote_per_base', volume:'base'},
    membership_events:[{kind, effective_at, available_at, source_status}],
    expected_start, expected_end}]. Binance cache/V3 manifests are also accepted;
    missing old metadata is reported unknown, never guessed from their filenames.
    """
    if split not in {"train", "validation", "retrospective"}:
        raise ValueError("final/holdout samples require the existing adjudication entrypoint")
    manifest_path = Path(path).resolve()
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw)
    if manifest.get("split") in {"final", "holdout"}:
        raise ValueError("this auditor does not open declared final/holdout inputs")
    reports, seen = [], set()
    for record in _entries(manifest):
        declared = {key: record.get(key, manifest.get(key)) for key in
                    ("symbol", "venue", "provider", "market_type")}
        identity = _identity(declared)
        timeframe = record.get("timeframe", manifest.get("timeframe"))
        if not timeframe:
            raise ValueError("input timeframe is required")
        key = tuple(identity.values()) + (timeframe,)
        if key in seen:
            raise ValueError("duplicate manifested market identity")
        seen.add(key)
        relative = record.get("path") or record.get("file") or record.get("csv_path")
        if not relative:
            reports.append({"identity": identity, "quality_status": "fail", "research_ready": False,
                            "errors": [_issue("manifest_input_path_missing")]})
            continue
        source = (manifest_path.parent / relative).resolve()
        # A frozen bundle never follows traversal/absolute paths into other data.
        if not source.is_relative_to(manifest_path.parent):
            raise ValueError("manifest input must stay inside its explicit bundle directory")
        expected_hash = record.get("sha256") or record.get("csv_sha256")
        if not source.is_file():
            reports.append({"identity": identity, "path": str(source), "quality_status": "fail",
                            "research_ready": False, "errors": [_issue("manifest_input_missing")]})
            continue
        data = source.read_bytes()
        actual_hash = hashlib.sha256(data).hexdigest()
        if expected_hash != actual_hash:
            reports.append({"identity": identity, "path": str(source), "sha256": actual_hash,
                            "quality_status": "fail", "research_ready": False,
                            "errors": [_issue("sha256_missing" if expected_hash is None else "sha256_mismatch")]})
            continue  # Never parse unauthenticated replacement bytes.
        try:
            frame = pd.read_csv(io.BytesIO(data))  # Parse the exact bytes whose digest was checked.
            report = audit_frame(frame, identity=identity, timeframe=timeframe, as_of=as_of,
                                 units=record.get("units", manifest.get("units")),
                                 membership_events=record.get("membership_events", record.get("events", ())),
                                 expected_start=record.get("expected_start"), expected_end=record.get("expected_end"))
        except (pd.errors.ParserError, pd.errors.EmptyDataError, ValueError, TypeError) as exc:
            report = {"identity": identity, "quality_status": "fail", "research_ready": False,
                      "errors": [_issue("input_parse_or_contract_error", detail=str(exc))]}
        report.update(path=str(source), sha256=actual_hash, expected_sha256=expected_hash)
        reports.append(report)
    return {"schema": "paper_data_manifest_audit/v1", "manifest_path": str(manifest_path),
            "manifest_sha256": hashlib.sha256(raw).hexdigest(), "as_of": _time(as_of).isoformat(),
            "split": split, "inputs": reports, "summary": {
                "inputs": len(reports), "research_ready": sum(row["research_ready"] for row in reports),
                "failed": sum(row["quality_status"] == "fail" for row in reports),
                "warning": sum(row["quality_status"] == "warning" for row in reports),
                "missing_bars": sum(row.get("coverage", {}).get("missing_bars", 0) for row in reports),
                "membership_unknown_rows": sum(row.get("membership", {}).get("unknown_rows", 0) for row in reports)}}
