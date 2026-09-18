"""P1 frozen walk-forward EV scoring over immutable P0 facts.

Pure post-processing: neither prediction nor evaluation can reach a broker.
Each test fold uses only labels matured strictly before its embargoed cutoff.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy

import pandas as pd

from core.signal_ev_types import EVPolicy, CONTEXT_DEFINITION, context_memberships, implementation_identity
from core.signal_ev_ledger import EVLedger
from core.signal_observation_types import finite, fingerprint, iso
from core.reproducibility import canonical_json, sha256_bytes
from core.timeframes import as_utc_timestamp, timeframe_delta


SCOPE = "independent_fixed_notional_signal_diagnostic"
EXCLUDED_GATES = {"warmup", "data", "universe"}


def _validate_input(payload):
    if payload.get("schema") != "signal_observation/v1" or payload.get("status") != "complete":
        raise ValueError("P1 requires a complete P0 signal_observation/v1 payload")
    horizons = tuple(payload["policy"]["horizons"])
    if not horizons or any(type(h) is not int or h < 1 for h in horizons) or len(set(horizons)) != len(horizons):
        raise ValueError("invalid P0 horizons")
    candidates = {}
    for candidate in payload["candidates"]:
        cid = candidate["candidate_id"]
        for value in (cid, candidate["strategy"], candidate["symbol"], candidate["signal_version"],
                      candidate["context"]["snapshot_version"]):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("candidate identity fields must be nonempty strings")
        if cid in candidates:
            raise ValueError("duplicate raw candidate identity")
        context = candidate["context"]
        if context.get("features") is not None and not isinstance(context["features"], dict):
            raise ValueError("candidate features must be a mapping or null")
        stamp = as_utc_timestamp(candidate["timestamp"])
        available = as_utc_timestamp(context["available_at"])
        if pd.isna(stamp) or pd.isna(available) or available != stamp + timeframe_delta(context["timeframe"]):
            raise ValueError("candidate availability must match its signal bar close")
        if candidate["direction"] not in {"long", "short"}:
            raise ValueError("invalid candidate direction")
        if context["snapshot_version"] != payload["snapshot_version"]:
            raise ValueError("candidate snapshot does not match P0 input identity")
        if candidate["signal_version"] != payload["strategy_versions"].get(candidate["strategy"]):
            raise ValueError("candidate strategy version does not match P0 identity")
        candidates[cid] = candidate
    decisions = {row["candidate_id"]: row for row in payload["decisions"]}
    if len(decisions) != len(payload["decisions"]) or set(decisions) != set(candidates):
        raise ValueError("P0 decision partition is incomplete or duplicated")
    if any(not isinstance(row.get("veto_stage"), str) for row in decisions.values()):
        raise ValueError("P0 decision requires an explicit gate stage")
    outcomes = {}
    for row in payload["outcomes"]:
        cid, horizon = row["candidate_id"], row["horizon_bars"]
        key = cid, horizon
        if cid not in candidates or type(horizon) is not int or horizon not in horizons or key in outcomes:
            raise ValueError("invalid or duplicate outcome identity")
        candidate = candidates[cid]
        if row.get("scope") != SCOPE:
            raise ValueError("unsupported outcome scope; never mix actual trades or ghost returns with fixed horizons")
        for name in ("strategy", "symbol", "direction"):
            if row.get(name) != candidate[name]:
                raise ValueError("outcome identity differs from candidate")
        if row["status"] == "matured":
            if not isinstance(row.get("execution_flags"), list):
                raise ValueError("matured outcome execution_flags must be an explicit list")
            available = as_utc_timestamp(row["available_at"])
            minimum = as_utc_timestamp(candidate["context"]["available_at"]) + horizon*timeframe_delta(candidate["context"]["timeframe"])
            if (pd.isna(available) or available < minimum or isinstance(row.get("net_return_bps"), bool)
                    or finite(row.get("net_return_bps")) is None):
                raise ValueError("premature or nonfinite matured outcome")
        elif not str(row["status"]).startswith("censored_"):
            raise ValueError("unknown outcome status")
        elif "execution_flags" in row and not isinstance(row["execution_flags"], list):
            raise ValueError("censored execution_flags must be a list when present")
        outcomes[key] = row
    if len(outcomes) != len(candidates)*len(horizons):
        raise ValueError("P0 outcome partition must include censored rows, not silently omit labels")
    return horizons, candidates, decisions, outcomes


def _exclusions(candidate_id, outcome, decisions):
    flags = list(outcome.get("execution_flags", []))
    stage = decisions[candidate_id]["veto_stage"]
    if stage in EXCLUDED_GATES:
        flags.append("p0_ineligible:" + stage)
    return flags


def _build_signal_meta_layer(payload, policy=None):
    policy = policy if isinstance(policy, EVPolicy) else EVPolicy.from_mapping(policy)
    if not policy.enabled:
        return None
    model_version = implementation_identity(policy)
    result = {"schema": policy.schema, "policy": policy.to_dict(), "model_version": model_version,
        "context_definition": deepcopy(CONTEXT_DEFINITION), "status": "complete",
        "evidence_status": "diagnostic_only_not_admission", "predictions": [], "evaluations": [],
        "folds": [], "cell_snapshots": [], "errors": [], "ingestion_audit": {}, "validation": {},
        "input_identity": {"p0_snapshot_version": payload.get("snapshot_version")},
        "protocol": {"training": "rolling fixed-duration; labels available strictly before cutoff; freeze within each test fold",
            "decay_origin": "entry context available_at; recomputed at query time without adding test labels",
            "label_availability": "no earlier than entry plus horizon; delayed availability is allowed and delays ingestion",
            "model_selection": "none; parameters and hard context thresholds predeclared, not selected on these results",
            "estimator": "uniform additive axis EV; direction/version/cost/timeframe/horizon books isolated",
            "exclusions": "censored, flagged execution, and P0 warmup/data/universe candidates never train or score realized evaluation means",
            "confidence": "approximate time-clustered diagnostic lower bound; no guaranteed coverage or significance claim",
            "actuation": "research scores only; no order, routing, health, risk or sizing changes",
            "economics": "overlapping fixed-horizon labels, NOT portfolio return or gate causal uplift"}}
    try:
        horizons, candidates, decisions, outcomes = _validate_input(payload)
        result["input_identity"].update(
            candidates_sha256=fingerprint([candidates[cid] for cid in sorted(candidates)]),
            outcomes_sha256=fingerprint([outcomes[key] for key in sorted(outcomes)]),
            decisions_sha256=sha256_bytes(canonical_json([decisions[cid] for cid in sorted(decisions)]).encode("utf-8")))
    except (KeyError, TypeError, ValueError) as exc:
        result["status"] = "incomplete"
        result["errors"].append({"reason": "invalid_p0_input", "message": str(exc)})
        return result
    ordered = sorted(candidates.values(), key=lambda c: (as_utc_timestamp(c["context"]["available_at"]), c["candidate_id"]))
    stamps = {cid: context_memberships(c) for cid, c in candidates.items()}
    eligible = [(candidates[cid], outcome) for (cid, _), outcome in outcomes.items()
                if outcome["status"] == "matured" and not _exclusions(cid, outcome, decisions)]
    eligible.sort(key=lambda pair: (as_utc_timestamp(pair[1]["available_at"]), pair[0]["candidate_id"], pair[1]["horizon_bars"]))
    result["ingestion_audit"] = {
        "candidate_count": len(candidates), "label_count": len(outcomes), "eligible_matured_labels": len(eligible),
        "censored_labels": sum(o["status"] != "matured" for o in outcomes.values()),
        "excluded_matured_labels": sum(o["status"] == "matured" and bool(_exclusions(cid, o, decisions))
            for (cid, _), o in outcomes.items()),
        "horizons": list(horizons)}
    if not ordered:
        result["validation"] = {"strict_training_cutoffs": True, "frozen_test_folds": True,
                                "prediction_partition_ok": True, "no_official_actuation": True}
        return result
    origin = as_utc_timestamp(ordered[0]["context"]["available_at"])
    result["protocol"]["anchor"] = iso(origin)
    first_test = origin + pd.Timedelta(days=policy.train_days + policy.embargo_days)
    fold_length = pd.Timedelta(days=policy.test_days)
    active_fold, ledger, fold = None, None, None
    for candidate in ordered:
        query_time = as_utc_timestamp(candidate["context"]["available_at"])
        index = int((query_time-first_test)//fold_length) if query_time >= first_test else None
        if index is not None and index != active_fold:
            start = first_test + index*fold_length
            cutoff = start - pd.Timedelta(days=policy.embargo_days)
            train_start = cutoff - pd.Timedelta(days=policy.train_days)
            training = [(c, o) for c, o in eligible
                        if train_start <= as_utc_timestamp(c["context"]["available_at"])
                        and as_utc_timestamp(o["available_at"]) < cutoff]
            ledger = EVLedger(policy)
            for c, o in training:
                ledger.add(c, o, stamps[c["candidate_id"]], cutoff=cutoff)
            training_digest = fingerprint([{"candidate": c, "outcome": o, "memberships": stamps[c["candidate_id"]]}
                                           for c, o in training])
            fold = {"fold_id": f"fold_{index:04d}", "start": iso(start), "end": iso(start+fold_length),
                "train_start": iso(train_start), "training_cutoff": iso(cutoff),
                "embargo_days": policy.embargo_days, "training_observations": len(training),
                "training_candidates": len({c["candidate_id"] for c, _ in training}),
                "training_digest": training_digest,
                "latest_label_available_at": max((o["available_at"] for _, o in training), default=None),
                "model_version": fingerprint({"implementation": model_version, "training_digest": training_digest,
                                               "cutoff": iso(cutoff), "train_start": iso(train_start)})}
            result["folds"].append(fold)
            result["cell_snapshots"].extend({**row, "fold_id": fold["fold_id"], "training_cutoff": iso(cutoff),
                                               "model_version": fold["model_version"]}
                                              for row in ledger.cell_rows(as_of=start))
            active_fold = index
        for horizon in sorted(horizons):
            if index is None:
                score = {"estimate_bps": None, "lower_bound_bps": None, "upper_bound_bps": None,
                    "stderr_bps": None, "effective_samples": 0.0, "effective_blocks": 0.0, "weight_mass": 0.0,
                    "raw_count": 0, "status": "abstain", "reason": "training_window", "would_allow": None,
                    "prior": {}, "axes": {}, "max_label_available_at": None}
            else:
                score = ledger.query(candidate, stamps[candidate["candidate_id"]], horizon, as_of=query_time)
            prediction = {**deepcopy(score), "candidate_id": candidate["candidate_id"],
                "strategy": candidate["strategy"], "direction": candidate["direction"], "symbol": candidate["symbol"],
                "horizon_bars": horizon, "available_at": iso(query_time),
                "memberships": deepcopy(stamps[candidate["candidate_id"]]),
                "fold_id": fold["fold_id"] if index is not None else None,
                "training_cutoff": fold["training_cutoff"] if index is not None else None,
                "model_version": fold["model_version"] if index is not None else model_version}
            result["predictions"].append(prediction)
            outcome = outcomes[candidate["candidate_id"], horizon]
            prior = score.get("prior") or {}
            result["evaluations"].append({**deepcopy(prediction),
                "label_status": outcome["status"], "label_available_at": outcome.get("available_at"),
                "realized_net_bps": outcome.get("net_return_bps") if outcome["status"] == "matured" else None,
                "baseline_ev_bps": prior.get("mean_bps"),
                "execution_flags": _exclusions(candidate["candidate_id"], outcome, decisions),
                "label_execution_flags": list(outcome.get("execution_flags", [])),
                "official_gate": decisions[candidate["candidate_id"]]["veto_stage"]})
    result["validation"] = {
        "strict_training_cutoffs": all(p["max_label_available_at"] is None
            or as_utc_timestamp(p["max_label_available_at"]) < as_utc_timestamp(p["training_cutoff"])
            <= as_utc_timestamp(p["available_at"]) for p in result["predictions"]),
        "frozen_test_folds": all(p["model_version"] == f["model_version"]
            and p["training_cutoff"] == f["training_cutoff"]
            and as_utc_timestamp(f["start"]) <= as_utc_timestamp(p["available_at"]) < as_utc_timestamp(f["end"])
            for f in result["folds"] for p in result["predictions"] if p["fold_id"] == f["fold_id"]),
        "prediction_partition_ok": len(result["predictions"]) == len(candidates)*len(horizons),
        "no_official_actuation": True}
    result["prediction_statuses"] = dict(Counter(p["status"] for p in result["predictions"]))
    if not all(result["validation"].values()):
        result["status"] = "incomplete"
        result["errors"].append({"reason": "temporal_or_partition_validation_failed"})
    return result


def build_signal_meta_layer(payload, policy=None):
    """Contain malformed research inputs; never discard the official result."""
    policy = policy if isinstance(policy, EVPolicy) else EVPolicy.from_mapping(policy)
    if not policy.enabled:
        return None
    try:
        return _build_signal_meta_layer(payload, policy)
    except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError) as exc:
        return {"schema": policy.schema, "policy": policy.to_dict(), "status": "incomplete",
                "model_version": implementation_identity(policy),
                "evidence_status": "invalid_research_input_or_estimation",
                "predictions": [], "evaluations": [], "folds": [], "cell_snapshots": [],
                "validation": {"research_estimation_complete": False},
                "errors": [{"reason": "meta_layer_failure", "message": str(exc)}]}
