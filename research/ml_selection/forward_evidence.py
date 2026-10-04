"""Read-only, identity-checked forward evidence and public-data intake.

Proxy labels, policy gate decisions and original-engine simulated fills are
separate evidence. A report cannot manufacture maturity or portfolio returns.
"""
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from core.reproducibility import canonical_json, sha256_file, sha256_frame
from research.ml_selection.dataset import FEATURE_COLUMNS
from research.ml_selection.protocol import save_json
from research.ml_selection.selector import utc


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _identity(value, key):
    payload = dict(value)
    identity = payload.pop(key, None)
    if not identity or hashlib.sha256(canonical_json(payload).encode()).hexdigest() != identity:
        raise ValueError(f"{key} changed after recording")
    return identity


def _finite(value):
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _summary(values):
    numbers = [float(value) for value in values if _finite(value)]
    return {"count": len(numbers), "mean": float(np.mean(numbers)) if numbers else None,
            "minimum": min(numbers) if numbers else None, "maximum": max(numbers) if numbers else None}


def load_public_forward_collection(folder, symbols, *, information_cutoff):
    """Validate the bytes and public current-membership receipt before use."""
    folder = Path(folder).resolve()
    cutoff = utc(information_cutoff)
    manifest = _read(folder / "collection.json")
    identifier = _identity(manifest, "collection_id")
    if (manifest.get("schema") != "ml-selection-public-forward-intake/v1"
            or manifest.get("source") != "binance_public_spot_api"
            or manifest.get("provider") != "binance" or manifest.get("market_type") != "spot"
            or manifest.get("timeframe") != "1d"):
        raise ValueError("forward collection requires identified public daily spot inputs")
    if utc(manifest["received_at"]) > cutoff or utc(manifest["started_at"]) > utc(manifest["received_at"]):
        raise ValueError("public collection was unavailable at the information cutoff")
    frames = {}
    for symbol in symbols:
        metadata = manifest.get("market_files", {}).get(symbol)
        if metadata is None:
            continue
        path = (folder / metadata["path"]).resolve()
        if path.parent != folder or not path.is_file() or sha256_file(path) != metadata.get("sha256"):
            raise ValueError("public forward market bytes changed after recording")
        if utc(metadata["received_at"]) > cutoff or utc(metadata["received_at"]) > utc(manifest["received_at"]):
            raise ValueError("public market response was unavailable at the information cutoff")
        frame = pd.read_csv(path, index_col="timestamp", parse_dates=True, float_precision="round_trip")
        times = pd.to_datetime(frame.index, utc=True)
        if (frame.empty or times.has_duplicates or not times.is_monotonic_increasing
                or not (times == times.normalize()).all()
                or not {"open", "high", "low", "close", "volume"} <= set(frame.columns)
                or (times + pd.Timedelta(days=1) > utc(metadata["received_at"])).any()
                or metadata.get("closed_rows") != len(frame)
                or utc(metadata["latest_close_available_at"]) != times[-1] + pd.Timedelta(days=1)
                or sha256_frame(frame) != metadata.get("frame_sha256")):
            raise ValueError("public forward bars are malformed, unclosed or changed")
        frames[symbol] = frame
    membership = manifest.get("membership_evidence", {})
    verified_symbols = []
    if membership.get("status") == "current_membership_observed":
        verified = utc(membership["verified_at"])
        raw_path = folder / "exchange_info_raw.json"
        if (verified > cutoff or verified > utc(manifest["received_at"])
                or not raw_path.exists() or sha256_file(raw_path) != membership.get("raw_sha256")):
            raise ValueError("public current-membership receipt is changed or unavailable")
        raw = _read(raw_path)
        lookup = {item["symbol"]: item for item in raw.get("symbols", [])}
        for symbol, recorded in membership.get("symbols", {}).items():
            row = lookup.get(symbol.replace("/", ""), {})
            allowed = row.get("status") == "TRADING" and row.get("isSpotTradingAllowed") is True
            if recorded != {"status": row.get("status"), "spot_trading_allowed": allowed}:
                raise ValueError("public membership facts disagree with their raw receipt")
            if allowed and verified >= cutoff.normalize():
                verified_symbols.append(symbol)
    evidence = {"basis": "current_trading_membership", "historical_pit_membership": False,
        "collection_id": identifier, "verified_at": membership.get("verified_at"),
        "verified_symbols": sorted(verified_symbols), "all_eligible_symbols_verified": False,
        "registered_symbol_count": len(symbols), "collected_registered_symbol_count": len(frames),
        "current_membership_verified_registered_symbol_count": len(set(symbols) & set(verified_symbols)),
        "full_registered_universe_covered": len(frames) == len(symbols)}
    return frames, evidence


def evaluate_forward_store(folder, protocol, *, as_of=None):
    """Evaluate preregistered dates and drift without changing recorded facts.

    Existing observations retain their recorded identities. The actual clock
    bounds the report; ``as_of`` can narrow this read to a historical cutoff.
    Missing membership, new data, RL state, mature labels or simulated account
    evidence remain explicit gaps even when diagnostic scores exist.
    """
    folder = Path(folder)
    clock = utc(datetime.now(timezone.utc))
    cutoff = utc(as_of or clock)
    if cutoff > clock:
        raise ValueError("forward evidence cutoff cannot be in the future")
    _identity(protocol, "protocol_id")
    options = protocol["settings"].get("next_research", {}).get("forward_observation", {})
    if not options.get("start"):
        raise ValueError("forward observation dates must be preregistered")
    start = utc(options["start"])
    end_value = options.get("end") or protocol["settings"].get("next_research", {}).get("final_sample", {}).get("end")
    end = utc(end_value) if end_value else None
    minimum = options.get("minimum_decision_dates", 30)
    if type(minimum) is not int or minimum <= 0 or end is not None and end <= start:
        raise ValueError("invalid preregistered forward observation window")
    frozen = _read(folder / "candidate.json")
    protocol_preregistered = utc(protocol["created_at"]) < start
    require_candidate_clock = "next_research" in protocol["settings"]
    candidate_clock = frozen.get("frozen_at")
    candidate_preregistered, candidate_clock_status = None, "legacy_not_required"
    if require_candidate_clock:
        if candidate_clock is None:
            candidate_clock_status = "missing"
        else:
            try:
                candidate_time = utc(candidate_clock)
            except (TypeError, ValueError, OverflowError):
                candidate_clock_status = "invalid"
            else:
                candidate_preregistered = candidate_time < start
                candidate_clock_status = "before_registered_start" if candidate_preregistered else "at_or_after_registered_start"
    preregistered = protocol_preregistered and (candidate_preregistered is True if require_candidate_clock else True)
    observations, qualifying, proxy = {}, [], {}
    gap_counts = {"retrospective": 0, "outside_registered_window": 0, "fresh_data_missing": 0,
                  "membership_unverified": 0, "frozen_rl_state_missing": 0, "no_eligible_candidates": 0}
    for path in sorted((folder / "shadow").glob("observation_*.json")):
        observation = _read(path)
        identity = _identity(observation, "observation_id")
        if identity in observations:
            raise ValueError("duplicate forward observation identity")
        if observation.get("protocol_id") != protocol["protocol_id"]:
            raise ValueError("forward observation protocol mismatch")
        if observation.get("frozen_candidate_id") != frozen["model_id"]:
            raise ValueError("forward observation frozen candidate mismatch")
        observed = utc(observation["observed_at"])
        information = utc(observation["information_cutoff"])
        if information > observed:
            raise ValueError("forward observation used information after its clock")
        if observed > cutoff:
            continue
        observations[identity] = observation
        retrospective = observation.get("retrospective_observation", True)
        within = observed >= start and (end is None or observed < end)
        fresh = (observation.get("source_kind") != "frozen_historical_data" and
                 any(item.get("eligible") is True and utc(item["as_of"]) >= information.normalize()
                     for item in observation["observations"]))
        membership = observation.get("membership_evidence", {})
        membership_ok = (membership.get("basis") == "current_trading_membership" and
                         membership.get("verified_at") is not None and
                         utc(membership["verified_at"]) <= observed and
                         utc(membership["verified_at"]) >= information.normalize() and
                         membership.get("all_eligible_symbols_verified") is True)
        rl = observation.get("rl_decision")
        candidate_decision = (frozen.get("selected_candidate") != "rl" or
                              isinstance(rl, dict) and rl.get("policy_id") == frozen["model_id"] and
                              rl.get("model_id") == frozen.get("parent_model_id") and
                              rl.get("snapshot_id") is not None)
        gaps = {"retrospective": bool(retrospective), "outside_registered_window": not within,
                "fresh_data_missing": not fresh, "membership_unverified": not membership_ok,
                "frozen_rl_state_missing": not candidate_decision,
                "no_eligible_candidates": not any(item.get("eligible") is True for item in observation["observations"])}
        for key, value in gaps.items():
            gap_counts[key] += int(value)
        if not any(gaps.values()) and preregistered:
            qualifying.append(identity)
    for path in sorted((folder / "shadow").glob("outcomes_*.json")):
        receipt = _read(path)
        if receipt.get("protocol_id") != protocol["protocol_id"]:
            raise ValueError("forward outcome protocol mismatch")
        received = utc(receipt["observed_at"])
        information = utc(receipt["information_cutoff"])
        if information > received:
            raise ValueError("forward outcome information exceeds receipt clock")
        if received > cutoff:
            continue
        for row in receipt.get("resolved", []):
            key = (row["observation_id"], row["symbol"])
            if row["observation_id"] not in observations:
                raise ValueError("forward outcome references an unknown observation")
            observation = observations[row["observation_id"]]
            matches = [item for item in observation["observations"] if item["symbol"] == row["symbol"]]
            if len(matches) != 1 or utc(matches[0]["as_of"]) != utc(row["as_of"]):
                raise ValueError("forward outcome candidate does not match its observation")
            if canonical_json(row.get("predicted_value")) != canonical_json(matches[0].get("predicted_value")):
                raise ValueError("forward outcome rewrites the original prediction")
            if row.get("label_basis") != "independent_shadow_proxy_not_portfolio":
                raise ValueError("forward proxy and simulated fills must remain separate")
            if not _finite(row.get("label_net_return")) or utc(row["label_available_at"]) > information:
                raise ValueError("forward proxy label is nonfinite or immature")
            if not observation.get("retrospective_observation", True):
                earliest = utc(observation["observed_at"]).ceil("D")
                entry = row.get("entry_time", row.get("entry_at"))
                if entry is None or utc(entry) < earliest:
                    raise ValueError("forward proxy used an entry before observation")
                if utc(row["label_available_at"]) <= utc(observation["observed_at"]):
                    raise ValueError("forward proxy label predates its decision")
            if key in proxy and canonical_json(proxy[key]) != canonical_json(row):
                raise ValueError("mature proxy outcome changed after append")
            proxy[key] = row
    items = [item for identity in qualifying for item in observations[identity]["observations"]]
    eligible_keys = {(identity, item["symbol"]) for identity in qualifying
                     for item in observations[identity]["observations"] if item.get("eligible") is True}
    mature_keys = eligible_keys & set(proxy)
    dates = sorted({utc(observations[key]["observed_at"]).normalize().isoformat() for key in qualifying})
    probability = [row.get("selection_probability") for key in qualifying
                   for row in observations[key].get("rl_decision", {}).get("decisions", [])]
    missing = {name: (sum(not _finite(item.get("features", {}).get(name)) for item in items) / len(items)
                      if items else None) for name in FEATURE_COLUMNS}
    feature_drift = {}
    dataset_path = folder / "dataset.csv"
    if dataset_path.exists() and items:
        baseline = pd.read_csv(dataset_path)
        available = pd.to_datetime(baseline["label_available_at"], utc=True)
        baseline = baseline.loc[available < utc(protocol["settings"]["splits"]["train_end"])]
        for name in FEATURE_COLUMNS:
            historical = pd.to_numeric(baseline[name], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
            observed = [float(item["features"][name]) for item in items if _finite(item.get("features", {}).get(name))]
            scale = float(historical.std(ddof=0)) if len(historical) else 0.
            feature_drift[name] = {"reference_rows": len(historical), "observed_rows": len(observed),
                "standardized_mean_shift": (float((np.mean(observed) - historical.mean()) / scale)
                    if observed and scale > 0 else None)}
    pending = []
    if not protocol_preregistered:
        pending.append("protocol_not_frozen_before_registered_start")
    if require_candidate_clock and candidate_preregistered is not True:
        pending.append({"missing": "candidate_freeze_time_missing",
                        "invalid": "candidate_freeze_time_invalid"}.get(candidate_clock_status,
                       "candidate_not_frozen_before_registered_start"))
    if cutoff < start:
        pending.append("registered_forward_window_not_started")
    if len(dates) < minimum:
        pending.append("insufficient_verified_forward_decision_dates")
    if not eligible_keys or len(mature_keys) < len(eligible_keys):
        pending.append("forward_proxy_labels_pending_maturity_or_future_data")
    # Original-engine fill/equity outcomes require a separate account trajectory.
    # Decision-only bridges and planned positions never satisfy this condition.
    pending.append("original_engine_simulated_fill_and_equity_evidence_pending")
    return {"schema": "ml-selection-forward-evidence/v1", "observed_at": clock.isoformat(),
        "information_cutoff": cutoff.isoformat(), "protocol_id": protocol["protocol_id"],
        "frozen_candidate_id": frozen["model_id"], "status": "pending_forward_evidence",
        "registered_start": start.isoformat(), "registered_end": end.isoformat() if end is not None else None,
        "minimum_decision_dates": minimum, "maturity_horizon_bars": options.get("maturity_horizon_bars", 20),
        "preregistered_before_start": preregistered, "recorded_observations": len(observations),
        "protocol_preregistered_before_start": protocol_preregistered,
        "candidate_frozen_at": candidate_clock,
        "candidate_frozen_before_start": candidate_preregistered,
        "candidate_freeze_time_status": candidate_clock_status,
        "preregistration_rule": "protocol_and_candidate_before_start" if require_candidate_clock else "legacy_protocol_before_start",
        "verified_forward_decision_dates": len(dates), "verified_forward_dates": dates,
        "qualifying_observation_ids": qualifying, "gaps": gap_counts, "pending_reasons": pending,
        "proxy_evidence": {"basis": "independent_shadow_proxy_not_portfolio", "eligible_labels": len(eligible_keys),
                           "mature_labels": len(mature_keys), "pending_labels": len(eligible_keys - mature_keys)},
        "simulated_account_evidence": {"status": "pending", "portfolio_return": None,
            "reason": "decision_only_bridge_does_not_supply_original_engine_fills_or_equity"},
        "drift": {"status": "measured" if items else "pending_verified_forward_inputs",
                  "feature_missingness": missing, "feature_standardized_mean_shift": feature_drift,
                  "selection_probability": _summary(probability),
                  "eligibility_coverage": (sum(item.get("eligible") is True for item in items) / len(items) if items else None),
                  "staleness_excluded_observations": gap_counts["fresh_data_missing"],
                  "drift_pass": None, "reason": "no_unregistered_drift_threshold_or_profit_inference"},
        "formal_admission": False, "real_orders": False}


def collect_public_forward(output_dir, symbols=("BTC/USDT", "ETH/USDT"), *, history_days=90, fetcher=None):
    """Collect a new immutable public Binance receipt, without credentials.

    Today's TRADING status supports current membership only. It supplies no
    past listing, delisting or point-in-time historical universe assertion.
    """
    from core.data_fetcher import DataFetcher
    if type(history_days) is not int or history_days < 61 or not symbols:
        raise ValueError("forward collection needs symbols and at least 61 warmup days")
    symbols = list(dict.fromkeys(symbols))
    if any(not isinstance(symbol, str) or symbol.count("/") != 1 or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/" for c in symbol)
           for symbol in symbols):
        raise ValueError("forward symbols must be uppercase public spot pairs")
    started = utc(datetime.now(timezone.utc))
    folder = Path(output_dir) / started.strftime("public_binance_%Y%m%dT%H%M%S%f")
    folder.mkdir(parents=True, exist_ok=False)
    owned = fetcher is None
    fetcher = fetcher or DataFetcher(data_timezone="UTC", request_timeout_ms=10000)
    collected, failures = {}, []
    try:
        for symbol in symbols:
            frame = fetcher.fetch_ccxt(symbol, timeframe="1d", exchange_id="binance", market_type="spot",
                start_date=(started.normalize() - pd.Timedelta(days=history_days)).strftime("%Y-%m-%d"),
                end_date=started.strftime("%Y-%m-%d"), max_retries=1)
            received = utc(datetime.now(timezone.utc))
            if frame.empty:
                failures.append({"symbol": symbol, "reason": "public_ohlcv_unavailable"})
                continue
            frame = frame.loc[pd.to_datetime(frame.index, utc=True) + pd.Timedelta(days=1) <= received].copy()
            target = folder / (symbol.replace("/", "_") + ".csv")
            frame.to_csv(target, index_label="timestamp")
            collected[symbol] = {"path": target.name, "sha256": sha256_file(target), "frame_sha256": sha256_frame(frame),
                "received_at": received.isoformat(), "closed_rows": len(frame),
                "latest_close_available_at": (utc(frame.index[-1]) + pd.Timedelta(days=1)).isoformat() if len(frame) else None}
        membership = {"basis": "current_trading_membership", "historical_pit_membership": False,
                      "status": "pending_public_membership_response", "symbols": {}}
        try:
            import ccxt
            exchange = fetcher._public_clients.get("binance", "spot", lambda: ccxt.binance({
                "enableRateLimit": True, "timeout": fetcher.request_timeout_ms,
                "proxies": fetcher._build_ccxt_proxies(), "options": {"defaultType": "spot"}}))
            raw = exchange.public_get_exchangeinfo()
            verified = utc(datetime.now(timezone.utc))
            save_json(folder / "exchange_info_raw.json", raw)
            lookup = {item["symbol"]: item for item in raw.get("symbols", [])}
            membership["verified_at"] = verified.isoformat()
            membership["status"] = "current_membership_observed"
            membership["raw_sha256"] = sha256_file(folder / "exchange_info_raw.json")
            for symbol in symbols:
                item = lookup.get(symbol.replace("/", ""), {})
                allowed = item.get("status") == "TRADING" and item.get("isSpotTradingAllowed") is True
                membership["symbols"][symbol] = {"status": item.get("status"), "spot_trading_allowed": allowed}
        except Exception as error:
            failures.append({"stage": "membership", "reason": type(error).__name__})
        manifest = {"schema": "ml-selection-public-forward-intake/v1", "source": "binance_public_spot_api",
            "provider": "binance", "market_type": "spot", "timeframe": "1d",
            "started_at": started.isoformat(), "received_at": utc(datetime.now(timezone.utc)).isoformat(),
            "source_kind": "immutable_public_forward_receipt", "symbols_requested": symbols,
            "market_files": collected, "membership_evidence": membership,
            "failures": failures, "historical_pit_membership": False, "real_orders": False}
        manifest["collection_id"] = hashlib.sha256(canonical_json(manifest).encode()).hexdigest()
        save_json(folder / "collection.json", manifest)
        return {"folder": str(folder.resolve()), "manifest": manifest,
                "status": "public_inputs_recorded" if collected else "pending_public_market_data"}
    finally:
        if owned:
            fetcher.close()
