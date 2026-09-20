"""P2 nested chronological regime fitting, entry stamping and attribution.

Regime prototypes are fitted on an earlier subwindow. The later subwindow is
replayed sequentially to build the EV book and genuinely frozen axis forecasts.
Every outer test fold freezes both this book and the attribution weights.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import math

import pandas as pd

from core.reproducibility import canonical_json, sha256_bytes
from core.signal_adaptive_types import AdaptiveEVPolicy, adaptive_implementation_identity
from core.signal_axis_attribution import combine_axis_score, fit_axis_weights
from core.signal_ev_ledger import EVLedger
from core.signal_meta_layer import _exclusions, _validate_input
from core.signal_observation_types import fingerprint, iso
from core.signal_regime_model import AXIS_FEATURES, RegimeModel
from core.timeframes import as_utc_timestamp


def _book(candidate, horizon):
    ctx = candidate["context"]
    return (candidate["strategy"], candidate["signal_version"], candidate["direction"],
            ctx["timeframe"], ctx["snapshot_version"], horizon)


def _book_dict(book):
    return dict(zip(("strategy", "signal_version", "direction", "timeframe", "snapshot_version", "horizon_bars"), book))


def _stamp(candidate):
    return as_utc_timestamp(candidate["context"]["available_at"])


def _unknown():
    return {axis: {"unknown": 1.0} for axis in AXIS_FEATURES}


def _base_result(payload, policy):
    return {"schema": policy.schema, "policy": policy.to_dict(), "status": "complete",
        "model_version": adaptive_implementation_identity(policy),
        "evidence_status": "diagnostic_only_not_admission", "predictions": [], "evaluations": [],
        "folds": [], "regime_models": [], "attribution": [], "calibration_predictions": [],
        "cell_snapshots": [], "errors": [], "validation": {},
        "input_identity": {"p0_snapshot_version": payload.get("snapshot_version")},
        "protocol": {
            "regime_fit": "earlier subwindow only; feature-neighbor empirical outcome quantiles; leave-self-out fit distributions",
            "posterior": "W2 quantile geometry soft membership, not calibrated class probabilities",
            "population": "later subwindow; process labels strictly before each entry; freeze entry posteriors and axis forecasts",
            "attribution": "mature prequential forecasts; positive Spearman squared, clipped, EMA, bounded-simplex projection",
            "test": "outer frozen folds; no test labels in prototypes, EV books or axis weights",
            "directions": "all directions, strategy versions, horizons and cost snapshots isolated",
            "actuation": "research predictions only; P3 has separate Broker accounts",
            "model_selection": "predeclared policy; no holdout selection, no automatic state-count growth",
            "uncertainty": "conservative linear mixture; time blocks reduce dependence but do not guarantee CI coverage"}}


def _prepare_fold(ordered, eligible, outcomes, stamps, *, start, cutoff, train_start, policy,
                  index, result, previous_weights):
    fit_cutoff = train_start + pd.Timedelta(days=policy.regime_fit_days)
    fit_groups = defaultdict(list)
    population_labels = []
    for c, o in eligible:
        entry, label = _stamp(c), as_utc_timestamp(o["available_at"])
        if train_start <= entry < fit_cutoff and label < fit_cutoff:
            fit_groups[_book(c, o["horizon_bars"])].append({"candidate": c, "outcome": o})
        elif fit_cutoff <= entry and label < cutoff:
            population_labels.append((c, o))
    models = {book: RegimeModel.fit(rows, cutoff=fit_cutoff,
                clusters=policy.regime_clusters, neighbors=policy.regime_neighbors,
                quantiles=policy.regime_quantiles, min_samples=policy.min_regime_samples,
                n_init=policy.regime_restarts, max_iter=policy.regime_max_iter, seed=policy.seed)
              for book, rows in sorted(fit_groups.items())}
    ledger = EVLedger(policy.ev_policy)
    pending = sorted(population_labels, key=lambda pair: (
        as_utc_timestamp(pair[1]["available_at"]), pair[0]["candidate_id"], pair[1]["horizon_bars"]))
    position = 0
    entry_memberships, calibration = {}, defaultdict(list)
    label_keys = {(c["candidate_id"], o["horizon_bars"]) for c, o in population_labels}
    fold_id = f"fold_{index:04d}"
    inner_candidates = [c for c in ordered if fit_cutoff <= stamps[c["candidate_id"]] < cutoff]

    def drain(before):
        nonlocal position
        while position < len(pending) and as_utc_timestamp(pending[position][1]["available_at"]) < before:
            c, o = pending[position]
            ledger.add(c, o, entry_memberships[c["candidate_id"], o["horizon_bars"]], cutoff=before)
            position += 1

    horizons = result["ingestion_audit"]["horizons"]
    for candidate in inner_candidates:
        at, cid = _stamp(candidate), candidate["candidate_id"]
        drain(at)
        for horizon in horizons:
            book = _book(candidate, horizon)
            model = models.get(book)
            memberships = model.memberships(candidate, as_of=at) if model is not None else _unknown()
            entry_memberships[cid, horizon] = deepcopy(memberships)
            score = ledger.query(candidate, memberships, horizon, as_of=at)
            outcome = outcomes[cid, horizon]
            # These forecasts were computed before the current outcome entered
            # the ledger. Labels are attached only for the later cutoff audit.
            record = {"candidate_id": cid, "available_at": iso(at),
                "label_available_at": outcome.get("available_at"),
                "net_return_bps": outcome.get("net_return_bps") if (cid, horizon) in label_keys else None,
                "label_eligible": (cid, horizon) in label_keys,
                "axes": deepcopy(score["axes"]), "memberships": deepcopy(memberships),
                "max_training_label_at": score["max_label_available_at"],
                "model_version": model.manifest()["model_id"] if model is not None else "unavailable",
                "fold_id": fold_id, **_book_dict(book)}
            result["calibration_predictions"].append(record)
            if record["label_eligible"]:
                calibration[book].append(record)
    drain(cutoff)
    # Freeze the known book universe too. A strategy/direction first appearing
    # later in this test fold must not change earlier model identities.
    used_books = sorted({_book(c, h) for c in ordered if _stamp(c) < start for h in horizons})
    weights = {}
    for book in used_books:
        weights[book] = fit_axis_weights(calibration[book], list(AXIS_FEATURES), policy,
            cutoff=cutoff, previous=previous_weights.get(book), horizon_bars=book[-1], timeframe=book[3])
        previous_weights[book] = deepcopy(weights[book]["weights"])
    training_digest = fingerprint([{"candidate": c, "outcome": o,
        "memberships": entry_memberships[c["candidate_id"], o["horizon_bars"]]} for c, o in pending])
    model_manifests = [{**model.manifest(), "book": _book_dict(book)} for book, model in sorted(models.items())]
    fold = {"fold_id": fold_id, "start": iso(start),
        "end": iso(start+pd.Timedelta(days=policy.ev_policy.test_days)),
        "train_start": iso(train_start), "regime_fit_cutoff": iso(fit_cutoff), "training_cutoff": iso(cutoff),
        "training_observations": len(pending), "regime_training_observations": sum(map(len, fit_groups.values())),
        "training_digest": training_digest,
        "latest_label_available_at": max((iso(o["available_at"]) for _, o in pending), default=None),
        "model_version": fingerprint({"implementation": result["model_version"], "training": training_digest,
            "models": model_manifests, "weights": [(list(book), weights[book]) for book in used_books],
            "train_start": iso(train_start), "cutoff": iso(cutoff), "fit_cutoff": iso(fit_cutoff)})}
    result["folds"].append(fold)
    result["regime_models"].extend({**manifest, "fold_id": fold_id} for manifest in model_manifests)
    result["attribution"].extend({**deepcopy(value), **_book_dict(book), "fold_id": fold_id,
                                  "model_version": fold["model_version"]} for book, value in sorted(weights.items()))
    result["cell_snapshots"].extend({**row, "fold_id": fold_id, "training_cutoff": iso(cutoff),
                                    "model_version": fold["model_version"]} for row in ledger.cell_rows(as_of=start))
    return fold, ledger, models, weights


def _build(payload, policy):
    result = _base_result(payload, policy)
    horizons, candidates, decisions, outcomes = _validate_input(payload)
    horizons = sorted(horizons)
    result["input_identity"].update(
        candidates_sha256=fingerprint([candidates[cid] for cid in sorted(candidates)]),
        outcomes_sha256=fingerprint([outcomes[key] for key in sorted(outcomes)]),
        decisions_sha256=sha256_bytes(canonical_json([decisions[cid] for cid in sorted(decisions)]).encode("utf-8")))
    ordered = sorted(candidates.values(), key=lambda c: (_stamp(c), c["candidate_id"]))
    stamps = {cid: _stamp(c) for cid, c in candidates.items()}
    eligible = [(candidates[cid], o) for (cid, _), o in sorted(outcomes.items())
                if o["status"] == "matured" and not _exclusions(cid, o, decisions)]
    result["ingestion_audit"] = {"candidate_count": len(candidates), "label_count": len(outcomes),
        "eligible_matured_labels": len(eligible), "horizons": horizons,
        "censored_labels": sum(o["status"] != "matured" for o in outcomes.values()),
        "excluded_matured_labels": sum(o["status"] == "matured" for o in outcomes.values())-len(eligible)}
    if not ordered:
        result["validation"] = {"prediction_partition_ok": True, "strict_training_cutoffs": True,
            "frozen_test_folds": True, "prequential_forecasts_causal": True, "no_official_actuation": True}
        return result
    ev = policy.ev_policy
    anchor = _stamp(ordered[0])
    result["protocol"]["anchor"] = iso(anchor)
    first = anchor + pd.Timedelta(days=ev.train_days+ev.embargo_days)
    length = pd.Timedelta(days=ev.test_days)
    active, previous_weights = None, {}
    fold, ledger, models, weights = None, None, {}, {}
    for candidate in ordered:
        at, cid = _stamp(candidate), candidate["candidate_id"]
        index = int((at-first)//length) if at >= first else None
        if index is not None and index != active:
            start = first + index*length
            cutoff = start - pd.Timedelta(days=ev.embargo_days)
            fold, ledger, models, weights = _prepare_fold(ordered, eligible, outcomes, stamps,
                start=start, cutoff=cutoff, train_start=cutoff-pd.Timedelta(days=ev.train_days),
                policy=policy, index=index, result=result, previous_weights=previous_weights)
            active = index
        for horizon in horizons:
            book = _book(candidate, horizon)
            model = models.get(book) if index is not None else None
            memberships = model.memberships(candidate, as_of=at) if model is not None else _unknown()
            if index is None:
                score = {"estimate_bps": None, "lower_bound_bps": None, "upper_bound_bps": None,
                    "stderr_bps": None, "effective_samples": 0., "effective_blocks": 0., "weight_mass": 0.,
                    "raw_count": 0, "status": "abstain", "reason": "training_window", "would_allow": None,
                    "prior": {}, "axes": {}, "max_label_available_at": None,
                    "axis_weights": {axis: 1/len(AXIS_FEATURES) for axis in AXIS_FEATURES}}
            else:
                base = ledger.query(candidate, memberships, horizon, as_of=at)
                attribution = weights.get(book)
                if attribution is None:
                    attribution = fit_axis_weights([], list(AXIS_FEATURES), policy,
                        cutoff=fold["training_cutoff"], horizon_bars=horizon, timeframe=book[3])
                score = combine_axis_score(base, attribution, ev)
            prediction = {**score, "candidate_id": cid, "symbol": candidate["symbol"], **_book_dict(book),
                "available_at": iso(at), "memberships": memberships,
                "posterior_entropy": {axis: None if "unknown" in states else
                                      -sum(p*math.log(p) for p in states.values() if p > 0)
                                      for axis, states in memberships.items()},
                "regime_model_id": model.manifest()["model_id"] if model is not None else None,
                "fold_id": fold["fold_id"] if index is not None else None,
                "training_cutoff": fold["training_cutoff"] if index is not None else None,
                "model_version": fold["model_version"] if index is not None else result["model_version"]}
            result["predictions"].append(deepcopy(prediction))
            outcome = outcomes[cid, horizon]
            result["evaluations"].append({**deepcopy(prediction), "label_status": outcome["status"],
                "label_available_at": outcome.get("available_at"),
                "realized_net_bps": outcome.get("net_return_bps") if outcome["status"] == "matured" else None,
                "baseline_ev_bps": score.get("prior", {}).get("mean_bps"),
                "execution_flags": _exclusions(cid, outcome, decisions),
                "official_gate": decisions[cid]["veto_stage"]})
    result["validation"] = {
        "prediction_partition_ok": len(result["predictions"]) == len(candidates)*len(horizons),
        "strict_training_cutoffs": all(p["max_label_available_at"] is None
            or as_utc_timestamp(p["max_label_available_at"]) < as_utc_timestamp(p["training_cutoff"])
            <= as_utc_timestamp(p["available_at"]) for p in result["predictions"]),
        "frozen_test_folds": all(p["model_version"] == f["model_version"] and p["training_cutoff"] == f["training_cutoff"]
            for f in result["folds"] for p in result["predictions"] if p["fold_id"] == f["fold_id"]),
        "prequential_forecasts_causal": all(p["max_training_label_at"] is None
            or as_utc_timestamp(p["max_training_label_at"]) < as_utc_timestamp(p["available_at"])
            for p in result["calibration_predictions"]),
        "no_official_actuation": True}
    result["prediction_statuses"] = dict(Counter(p["status"] for p in result["predictions"]))
    if not all(result["validation"].values()):
        raise ValueError("adaptive chronological validation failed")
    return result


def build_adaptive_signal_meta(payload, policy=None):
    policy = AdaptiveEVPolicy.from_mapping(policy)
    if not policy.enabled:
        return None
    try:
        return _build(payload, policy)
    except (KeyError, TypeError, ValueError, AttributeError, ArithmeticError) as exc:
        return {"schema": policy.schema, "policy": policy.to_dict(), "status": "incomplete",
            "predictions": [], "evaluations": [], "folds": [], "regime_models": [], "attribution": [],
            "calibration_predictions": [], "cell_snapshots": [], "validation": {"research_complete": False},
            "errors": [{"reason": "adaptive_estimation_failed", "message": str(exc)}]}
