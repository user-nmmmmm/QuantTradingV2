"""Read-only input readiness and descriptive serving drift reports.

No model is loaded into a runtime, fitted, or selected here. Claimed identities,
verified bytes, market watermarks, label watermarks, and missing evidence are
reported separately. A readiness report is never a production admission.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any
from types import SimpleNamespace

import numpy as np
import pandas as pd
import yaml

from core.reproducibility import canonical_json, sha256_file, sha256_frame

ROOT = Path(__file__).resolve().parents[2]


def _stamp(value):
    if value is None or isinstance(value, (bool, int, float, list, dict)):
        return None
    try:
        stamp = pd.to_datetime(value, utc=True, errors="coerce")
        return None if not isinstance(stamp, pd.Timestamp) or pd.isna(stamp) else stamp
    except (TypeError, ValueError, OverflowError):
        return None


def _finite(value):
    if isinstance(value, (bool, np.bool_)):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _read_mapping(path):
    source = Path(path)
    content = source.read_text(encoding="utf-8")
    # JSON exponent values (e.g. 1e-06) must remain floats for model identity.
    value = json.loads(content) if source.suffix.lower() == ".json" else yaml.safe_load(content)
    if not isinstance(value, dict):
        raise ValueError("document must be a mapping")
    return value


def _resolve(value, root):
    path = Path(value)
    return path.resolve() if path.is_absolute() else (Path(root) / path).resolve()


def _model_identity(payload):
    body = {key: value for key, value in payload.items() if key not in {"model_id", "rng_state"}}
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def audit_readiness(settings_path, bundle_path, *, root=ROOT, protocol_path=None,
                    serving_account_mode=None, as_of=None):
    """Inspect existing registrations and frozen JSON; never regenerate evidence.

    Relative registration paths resolve against ``root`` (the repository by
    default); bundled artifacts resolve against the bundle's own directory.
    ``protocol_path`` supplies the original hash anchor and account contract.
    Absent originals remain pending even if the portable model files verify.
    """
    root, settings_path, bundle_path = Path(root).resolve(), Path(settings_path), Path(bundle_path)
    clock = _stamp(as_of or datetime.now(timezone.utc))
    if clock is None:
        raise ValueError("as_of must be a valid timestamp")
    checks = []
    models = {}
    watermarks: dict[str, Any] = {}

    def check(name, status, reason, **details):
        checks.append({"name": name, "status": status, "reason": reason, **details})

    def document(path, name):
        if not path.is_file():
            check(name, "pending", "missing_external_artifact", path=str(path))
            return None
        try:
            data = _read_mapping(path)
        except (OSError, ValueError, TypeError, yaml.YAMLError) as exc:
            check(name, "failed", "invalid_document", path=str(path), detail=str(exc))
            return None
        check(name, "verified", "document_parsed_only", path=str(path), sha256=sha256_file(path))
        return data

    settings = document(settings_path, "settings") or {}
    bundle = document(bundle_path, "frozen_bundle") or {}
    protocol = document(Path(protocol_path), "source_protocol") if protocol_path else None
    if protocol is None:
        check("source_protocol_identity", "pending", "original_frozen_protocol_not_available")
    else:
        body = {key: value for key, value in protocol.items() if key != "protocol_id"}
        digest = hashlib.sha256(canonical_json(body).encode()).hexdigest()
        matches = (digest == protocol.get("protocol_id") == bundle.get("source_protocol_id")
                   and sha256_file(protocol_path) == bundle.get("source_protocol_sha256"))
        check("source_protocol_identity", "verified" if matches else "failed",
              "protocol_and_bundle_anchor_match" if matches else "protocol_identity_mismatch")
        if protocol.get("settings") != settings:
            check("settings_identity", "pending", "settings_differ_from_original_frozen_protocol")
    if not bundle:
        check("bundle_schema", "pending", "bundle_schema_not_available")
    elif bundle.get("schema") != "frozen-coin-selector/v1":
        check("bundle_schema", "failed", "unsupported_or_missing_bundle_schema")
    artifacts = bundle.get("files", {})
    for name in ("model", "policy"):
        entry = artifacts.get(name)
        if entry is None and name == "policy" and bundle.get("candidate", {}).get("selected_candidate") != "rl":
            continue
        if not isinstance(entry, dict) or not entry.get("path"):
            check(f"{name}_identity", "pending", "model_artifact_not_declared")
            continue
        path = (bundle_path.parent / entry["path"]).resolve()
        if not path.is_relative_to(bundle_path.parent.resolve()):
            check(f"{name}_identity", "failed", "artifact_path_escapes_bundle")
            continue
        payload = document(path, f"{name}_artifact")
        if payload is None:
            continue
        try:
            matches = (sha256_file(path) == entry.get("sha256")
                       and payload.get("model_id") == entry.get("model_id") == _model_identity(payload)
                       and payload.get("artifact_version") == 1)
        except (ValueError, TypeError):
            matches = False
        check(f"{name}_identity", "verified" if matches else "failed",
              "frozen_bytes_and_internal_identity_match" if matches else "artifact_identity_mismatch")
        models[name] = {"model_id": payload.get("model_id"), "kind": payload.get("kind"),
                        "features": payload.get("features", []), "metadata": payload.get("metadata", {})}
    candidate = bundle.get("candidate", {})
    if models:
        selected = "policy" if candidate.get("selected_candidate") == "rl" else "model"
        lineage = (selected in models and candidate.get("model_id") == models.get(selected, {}).get("model_id")
                   and (selected != "policy" or
                        candidate.get("parent_model_id") == models.get("model", {}).get("model_id")
                        == models["policy"]["metadata"].get("parent_model_id")))
        check("candidate_lineage", "verified" if lineage else "failed",
              "candidate_parent_match" if lineage else "candidate_or_parent_model_mismatch")
    evidence = protocol.get("data_evidence", {}) if protocol else {}
    registrations = {}
    market_ends = []
    for name in ("data_registration", "baseline_registration"):
        value = settings.get(name)
        if not value:
            check(name, "pending", "registration_not_declared")
            continue
        path = _resolve(value, root)
        registration = document(path, name)
        if registration is None:
            continue
        registrations[name] = registration
        expected = evidence.get(f"{name}_sha256")
        check(f"{name}_identity", "pending" if expected is None else
              "verified" if sha256_file(path) == expected else "failed",
              "original_hash_anchor_missing" if expected is None else "registration_hash_checked")
        if name != "data_registration":
            continue
        hashes = registration.get("input_files", {})
        if not isinstance(hashes, dict) or not hashes:
            check("input_inventory", "pending", "registered_file_inventory_missing")
            continue
        for relative, digest in sorted(hashes.items()):
            source = (path.parent / relative).resolve()
            if not source.is_relative_to(path.parent.resolve()):
                check("input_file", "failed", "registered_path_escapes_root", path=str(source))
            elif not source.is_file():
                check("input_file", "pending", "registered_input_missing", path=str(source))
            else:
                match = sha256_file(source) == digest
                check("input_file", "verified" if match else "failed", "input_hash_checked", path=str(source))
        symbols = registration.get("symbols", [])
        if (not symbols or len(set(symbols)) != len(symbols)
                or set(symbols) != set(registration.get("engine_frame_hashes", {}))):
            check("engine_inventory", "failed", "symbol_and_frame_inventory_incomplete")
        for symbol in symbols:
            relative = "input/engine/" + symbol.replace("/", "_") + ".csv"
            source = (path.parent / relative).resolve()
            if relative not in hashes:
                check("engine_frame", "failed", "engine_input_not_registered", symbol=symbol)
                continue
            if not source.is_relative_to(path.parent.resolve()) or not source.is_file():
                continue
            try:
                frame = pd.read_csv(source, index_col="timestamp", parse_dates=True,
                                    float_precision="round_trip")
                frame.index = pd.to_datetime(frame.index, utc=True).tz_convert(None)
                valid = (not frame.empty and not frame.index.hasnans and not frame.index.has_duplicates
                         and frame.index.is_monotonic_increasing
                         and (frame.index == frame.index.normalize()).all()
                         and {"open", "high", "low", "close", "volume"} <= set(frame.columns))
                matched = valid and sha256_frame(frame) == registration.get("engine_frame_hashes", {}).get(symbol)
                check("engine_frame", "verified" if matched else "failed", "daily_frame_identity_checked", symbol=symbol)
                if valid:
                    market_ends.append((symbol, frame.index[-1].tz_localize("UTC")))
            except (OSError, ValueError, TypeError, KeyError) as exc:
                check("engine_frame", "failed", "invalid_registered_frame", symbol=symbol, detail=str(exc))
    training = bundle.get("training_end_exclusive")
    validation = bundle.get("validation_end_exclusive")
    metadata = models.get("model", {}).get("metadata", {})
    label_watermark = metadata.get("train_latest_label_available_at")
    watermarks.update(training_end_exclusive=training, validation_end_exclusive=validation,
                      latest_training_feature_at=metadata.get("train_max_as_of"),
                      latest_training_label_available_at=label_watermark,
                      latest_market_bar_by_symbol={symbol: stamp.isoformat() for symbol, stamp in market_ends},
                      market_common_end=min((stamp for _, stamp in market_ends), default=None))
    if watermarks["market_common_end"] is not None:
        watermarks["market_common_end"] = watermarks["market_common_end"].isoformat()
    boundaries = (_stamp(training), _stamp(validation), _stamp(label_watermark))
    if any(stamp is None for stamp in boundaries):
        check("training_label_maturity", "pending", "training_or_label_watermark_missing")
    else:
        ordered = boundaries[2] < boundaries[0] < boundaries[1]
        check("training_label_maturity", "verified" if ordered else "failed",
              "metadata_label_before_training_boundary" if ordered else "label_or_split_boundary_violation",
              scope="artifact_metadata_only_raw_training_rows_not_reverified")
    if _stamp(training) is not None:
        watermarks["training_age_days"] = (clock - _stamp(training)).total_seconds() / 86400
    source_mode = protocol.get("parameters", {}).get("account", {}).get("mode") if protocol else None
    contracts = [value for value in (bundle.get("account_contract"),
        models.get("model", {}).get("metadata", {}).get("account_contract"),
        models.get("policy", {}).get("metadata", {}).get("account_contract")) if value is not None]
    declared_mode = contracts[0].get("training_account_mode") if contracts and isinstance(contracts[0], dict) else None
    account_mode = source_mode or declared_mode or settings.get("account_mode")
    target_mode = serving_account_mode or account_mode
    account_status = ("pending" if source_mode is None else
                      "verified" if source_mode == target_mode else "failed")
    check("account_mode", account_status, "training_source_account_unknown" if source_mode is None else
          "matching_frozen_account" if account_status == "verified" else "training_serving_account_mismatch",
          declared_training_mode=account_mode, verified_training_mode=source_mode, serving_mode=target_mode)
    # Reuse the serving loader's contract enforcement without constructing a
    # model or selector. Frozen metadata and protocol account claims must agree.
    from backtest.coin_selector import _account_compatibility
    try:
        contract = _account_compatibility(bundle,
            SimpleNamespace(metadata=models.get("model", {}).get("metadata", {})),
            SimpleNamespace(metadata=models.get("policy", {}).get("metadata", {})), target_mode)
    except ValueError as exc:
        contract = {"status": "invalid", "compatibility_verified": False}
        check("account_compatibility", "failed", "frozen_account_contract_invalid", detail=str(exc))
    else:
        verified = contract.get("compatibility_verified") is True
        check("account_compatibility", "verified" if verified else "pending",
              "frozen_account_contract_supported" if verified else "legacy_account_compatibility_unverified",
              contract=contract)
        if source_mode is not None and declared_mode is not None and source_mode != declared_mode:
            check("account_training_provenance", "failed", "frozen_contract_training_account_disagrees_with_protocol")
    baseline = registrations.get("baseline_registration", {}).get("arms", {}).get(settings.get("baseline_arm", "smart"), {})
    baseline_mode = baseline.get("parameters", {}).get("account", {}).get("mode")
    if baseline_mode and baseline_mode != account_mode:
        check("baseline_account_adaptation", "pending", "baseline_account_differs_requires_explicit_paired_contract",
              baseline_mode=baseline_mode, research_mode=account_mode)
    pit_verified = evidence.get("historical_universe_verified")
    check("historical_pit", "pending", "historical_pit_sources_and_coverage_not_reverified",
          declared_verified=pit_verified, declared_basis=evidence.get("membership_basis"),
          scope="registration_claims_are_not_source_publication_evidence")
    final = settings.get("evaluation_protocol", {}).get("final_sample",
                         settings.get("next_research", {}).get("final_sample", {}))
    opened = protocol.get("final_holdout_opened") if protocol else final.get("opened")
    if final.get("opened") is True:
        opened = True
    final_status = "verified" if opened is False else "pending" if opened is None else "failed"
    check("final_unopened", final_status, "declared_unopened_no_final_results_read" if opened is False
          else "final_open_state_unknown" if opened is None else "final_already_opened",
          scope="declared_state_only", start=final.get("start"), end=final.get("end"))
    check("economic_reproduction", "pending", "original_account_ledgers_not_compared_by_readiness_audit")
    status = "blocked" if any(item["status"] == "failed" for item in checks) else "pending_external_evidence"
    return {"schema": "ml-selection-readiness/v1", "status": status, "as_of": clock.isoformat(),
            "checks": checks, "watermarks": watermarks, "models": models,
            "account_contract": contract,
            "evaluation_scope": models.get("model", {}).get("metadata", {}).get("evaluation_scope"),
            "candidate_id": candidate.get("model_id"), "source_protocol_id": bundle.get("source_protocol_id"),
            "pending_reasons": sorted({item["reason"] for item in checks if item["status"] == "pending"}),
            "failed_reasons": sorted({item["reason"] for item in checks if item["status"] == "failed"}),
            "training_performed": False, "backtest_performed": False, "independent_holdout": False,
            "formal_admission": False}


def describe_serving_drift(reference, observations, *, features, activity=None, stale_after_days=1.,
                           training_domain=None, serving_domain=None):
    """Describe existing rows; missing activity cannot imply zero actual fills.

    Reference rows may be a DataFrame or records; features may be nested under
    ``features`` in observations. Observation/feature cutoffs use ``as_of`` and
    ``information_cutoff``. Verified account activity must be supplied separately
    as dated records with ``fills`` and ``status='completed'``. Selector rejection
    and actual no-trade streaks remain different statistics.
    """
    if not _finite(stale_after_days) or float(stale_after_days) < 0:
        raise ValueError("stale_after_days must be finite and nonnegative")
    features = list(features)
    if not features or len(set(features)) != len(features):
        raise ValueError("features must be nonempty and unique")
    records = observations.to_dict("records") if isinstance(observations, pd.DataFrame) else list(observations)
    prior = reference.to_dict("records") if isinstance(reference, pd.DataFrame) else list(reference)
    ref = pd.DataFrame([{**row, **row.get("features", {})} for row in prior])
    rows = [{**row, **row.get("features", {})} for row in records]
    missingness, shifts = {}, {}
    for name in features:
        values = np.asarray([float(row[name]) for row in rows if _finite(row.get(name))])
        baseline = pd.to_numeric(ref.get(name, pd.Series(dtype=float)), errors="coerce")
        baseline = baseline.replace([np.inf, -np.inf], np.nan).dropna()
        scale = float(baseline.std(ddof=0)) if len(baseline) else 0.
        missingness[name] = (1 - len(values) / len(rows)) if rows else None
        shifts[name] = {"reference_rows": len(baseline), "serving_rows": len(values),
                        "standardized_mean_shift": (float((values.mean() - baseline.mean()) / scale)
                            if len(values) and scale > 0 else None),
                        "reference_missing_fraction": (1 - len(baseline) / len(prior)) if prior else None}
    stale, future, unknown = 0, 0, 0
    ages = []
    for row in rows:
        as_of, cutoff = _stamp(row.get("as_of")), _stamp(row.get("information_cutoff"))
        if as_of is None or cutoff is None:
            unknown += 1
            continue
        age = (cutoff - as_of).total_seconds() / 86400
        ages.append(age)
        future += int(age < 0)
        stale += int(age > stale_after_days)
    known_eligible = [row["eligible"] for row in rows if type(row.get("eligible")) is bool]
    # Reuse the forward evidence's statistics without importing the episodic
    # engine diagnostics. Missing thresholds remain unknown, with no fallback
    # action rule manufactured by this report.
    from research.ml_selection.forward_evidence import _summary
    decisions = [row for row in rows if _finite(row.get("selection_probability"))]
    probabilities = np.asarray([float(row["selection_probability"]) for row in decisions])
    if np.any((probabilities < 0) | (probabilities > 1)):
        raise ValueError("selection probabilities must be in [0, 1]")
    threshold_rows = [row for row in decisions if _finite(row.get("policy_threshold"))]
    if any(not 0 <= float(row["policy_threshold"]) <= 1 for row in threshold_rows):
        raise ValueError("saved policy thresholds must be in [0, 1]")
    probability = {**_summary(probabilities),
        "meaning": "Probability of the policy accept action; not probability of profit.",
        "observed_probabilities": len(probabilities), "unknown_probability_rows": len(rows) - len(probabilities),
        "quantiles": {str(q): float(np.quantile(probabilities, q)) for q in (0, .1, .5, .9, 1)} if len(probabilities) else {},
        "at_or_above_frozen_threshold": sum(float(row["selection_probability"]) >= float(row["policy_threshold"]) for row in threshold_rows),
        "below_frozen_threshold": sum(float(row["selection_probability"]) < float(row["policy_threshold"]) for row in threshold_rows),
        "unknown_threshold_rows": len(decisions) - len(threshold_rows),
        "threshold_source": "saved_decisions_only_no_default_or_search", "economic_result": None}
    known_selected = [row["selected"] for row in rows if type(row.get("selected")) is bool]
    dates: dict[pd.Timestamp, list[Any]] = {}
    for row in rows:
        stamp = _stamp(row.get("as_of", row.get("bar_time")))
        if stamp is not None:
            dates.setdefault(stamp.normalize(), []).append(row.get("selected"))
    max_rejection, rejection_run, previous = 0, 0, None
    for date, selections in sorted(dates.items()):
        consecutive = previous is None or date - previous == pd.Timedelta(days=1)
        all_rejected = bool(selections) and all(value is False for value in selections)
        rejection_run = rejection_run + 1 if consecutive and all_rejected else int(all_rejected)
        max_rejection, previous = max(max_rejection, rejection_run), date
    activity_rows = list(activity) if activity is not None else []
    seen, no_fill_run, max_no_fill, previous = set(), 0, 0, None
    for row in sorted(activity_rows, key=lambda item: str(item.get("date", ""))):
        date = _stamp(row.get("date"))
        if date is None or date != date.normalize() or date in seen:
            raise ValueError("activity requires unique valid UTC daily dates")
        seen.add(date)
        fills = row.get("fills")
        known = row.get("status") == "completed" and type(fills) is int and fills >= 0
        consecutive = previous is None or date - previous == pd.Timedelta(days=1)
        no_fill_run = no_fill_run + 1 if known and fills == 0 and consecutive else int(known and fills == 0)
        max_no_fill, previous = max(max_no_fill, no_fill_run), date
    domains_known = training_domain is not None and serving_domain is not None
    return {"schema": "ml-selection-serving-drift/v1", "status": "descriptive_only" if rows else "pending_observations",
            "reference_rows": len(prior), "serving_rows": len(rows), "feature_missingness": missingness,
            "feature_mean_shift": shifts, "staleness": {"observed_ages_days": ages, "stale_rows": stale,
                "future_information_rows": future, "unknown_rows": unknown, "threshold_days": stale_after_days},
            "eligibility": {"known_rows": len(known_eligible), "unknown_rows": len(rows) - len(known_eligible),
                "coverage": sum(known_eligible) / len(known_eligible) if known_eligible else None},
            "selection": {"known_rows": len(known_selected), "unknown_rows": len(rows) - len(known_selected),
                "rejection_fraction": 1 - sum(known_selected) / len(known_selected) if known_selected else None,
                "maximum_consecutive_all_rejected_dates": max_rejection},
            "action_probability": probability,
            "actual_activity": {"status": "observed" if activity_rows else "pending_account_activity",
                "maximum_consecutive_no_fill_dates": max_no_fill if activity_rows else None,
                "unknown_rows": sum(row.get("status") != "completed" or type(row.get("fills")) is not int
                    or row.get("fills", -1) < 0 for row in activity_rows)},
            "domain_comparison": {"training": training_domain, "serving": serving_domain,
                "matched": training_domain == serving_domain if domains_known else None},
            "drift_pass": None, "formal_admission": False, "training_performed": False,
            "interpretation": "Statistical descriptions are not profitability estimates or admission gates."}
