"""Offline execution-cost research: strict facts, frozen fits and whole-day tests.

These tools never send orders, mutate a venue ledger, infer hidden real search
history or approve capacity. Price, fee, residual inventory and financing remain
separate facts. Statistical precision is independent of engineering coverage.
"""

from __future__ import annotations

import math
from typing import Mapping

import numpy as np
import pandas as pd

from core.planning.execution_cost_provider import (
    FrozenExecutionCostProvider,
    aware_time,
    cost_digest,
    strict_json,
)


def _positive(value):
    if isinstance(value, bool):
        raise ValueError("finite positive value required")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("finite positive value required")
    return value


def _known_intent_fills(intent, fills, cutoff, anchor_at):
    seen, known, unavailable = {}, [], []
    for raw in fills:
        row = strict_json(dict(raw))
        fid = row.get("fill_id")
        if not isinstance(fid, str) or not fid:
            raise ValueError("fill identity required")
        if fid in seen:
            if seen[fid] != row:
                raise ValueError("conflicting duplicate fill identity")
            continue
        seen[fid] = row
        if row.get("client_order_id") != intent["client_order_id"]:
            raise ValueError("fill order identity mismatch")
        if row.get("symbol", intent.get("symbol")) != intent.get("symbol"):
            raise ValueError("fill instrument mismatch")
        event_at, available = aware_time(row["occurred_at"]), aware_time(row["available_at"])
        if available < event_at or event_at < anchor_at:
            raise ValueError("inconsistent fill event/availability clock")
        if available > cutoff:
            unavailable.append(fid)
            continue
        _positive(row["qty"])
        _positive(row["price"])
        known.append(row)
    return known, unavailable


def _terminal_opportunity(
    intent, terminal, known, residual, requested, reference, sign, currency, anchor_at, cutoff
):
    terminal_amount, terminal_reason = None, None
    if residual <= max(1e-10, requested * 1e-10):
        terminal_amount = 0.0
    elif terminal is None:
        terminal_reason = "unfilled_inventory_requires_terminal_valuation"
    else:
        if (
            terminal.get("symbol") != intent.get("symbol")
            or terminal.get("quote_currency") != currency
        ):
            raise ValueError("terminal instrument/currency mismatch")
        occurred, available = (
            aware_time(terminal["occurred_at"]),
            aware_time(terminal["available_at"]),
        )
        if terminal.get("independent") is not True or not terminal.get("source_id"):
            raise ValueError("independent terminal source required")
        if (
            occurred < anchor_at
            or available < occurred
            or any(occurred < aware_time(r["occurred_at"]) for r in known)
        ):
            raise ValueError("invalid terminal clock")
        if available > cutoff:
            terminal_reason = "terminal_valuation_unavailable"
        else:
            terminal_amount = sign * residual * (_positive(terminal["price"]) - reference)
    return terminal_amount, terminal_reason


def _explicit_fill_fees(known, fee_fx, currency, cutoff):
    fees, fee_gaps = 0.0, []
    for row in known:
        fid = row["fill_id"]
        try:
            if row.get("fee_status") != "recorded":
                raise ValueError("fee_not_explicitly_final")
            fee = float(row["fee"])
            if not math.isfinite(fee):
                raise ValueError("nonfinite_fee")
            fee_at = aware_time(row["fee_available_at"])
            if fee_at < aware_time(row["occurred_at"]) or fee_at > cutoff:
                raise ValueError("fee_unavailable_at_cutoff")
            fee_currency = row["fee_currency"]
            if fee_currency != currency:
                matches = [
                    fx
                    for fx in fee_fx
                    if fx.get("fill_id") == fid
                    and fx.get("currency") == fee_currency
                    and fx.get("quote_currency") == currency
                ]
                if len(matches) != 1:
                    raise ValueError("missing_or_ambiguous_fee_fx")
                fx = matches[0]
                event, available = aware_time(fx["occurred_at"]), aware_time(fx["available_at"])
                if (
                    not fx.get("source_id")
                    or event > aware_time(row["occurred_at"])
                    or available < event
                    or available > cutoff
                ):
                    raise ValueError("fee_fx_not_point_in_time")
                fee *= _positive(fx["rate"])
            fees += fee
        except (ValueError, TypeError, KeyError) as exc:
            fee_gaps.append({"fill_id": fid, "reason": str(exc)})
    return fees, fee_gaps


def _financing_total(financing, full_cost, currency, cutoff):
    financed, financing_reason = None, "financing_not_supplied"
    if financing is not None:
        try:
            components = []
            for name in ("borrow", "funding"):
                item = financing[name]
                if item.get("applicable") is False:
                    if not item.get("reason"):
                        raise ValueError("not_applicable_requires_reason")
                    components.append(0.0)
                else:
                    available = aware_time(item["available_at"])
                    amount = float(item["amount_quote"])
                    if (
                        available > cutoff
                        or item.get("quote_currency") != currency
                        or not math.isfinite(amount)
                    ):
                        raise ValueError("financing_not_available_in_quote_currency")
                    components.append(amount)
            if full_cost is not None:
                financed = full_cost + sum(components)
                financing_reason = None
        except (ValueError, KeyError, TypeError) as exc:
            financing_reason = str(exc)
    return financed, financing_reason


def _training_fit_matrix(rows, feature_names, method, constant_bps, ridge_alpha):
    x = np.asarray([[r["features"][name] for name in feature_names] for r in rows], float)
    y = np.asarray([r["execution_cost_bps"] for r in rows], float)
    day_parents = {
        day: {r["parent_order_id"] for r in rows if r["day"] == day}
        for day in {r["day"] for r in rows}
    }
    parent_notional = {}
    for row in rows:
        key = (row["day"], row["parent_order_id"])
        parent_notional[key] = parent_notional.get(key, 0.0) + row["requested_notional"]
    weights = np.asarray(
        [
            r["requested_notional"]
            / (len(day_parents[r["day"]]) * parent_notional[r["day"], r["parent_order_id"]])
            for r in rows
        ]
    )
    means = np.average(x, axis=0, weights=weights)
    scales = np.sqrt(np.average((x - means) ** 2, axis=0, weights=weights))
    scales = np.where(scales > 1e-12, scales, 1.0)
    normalized = (x - means) / scales
    if method == "constant":
        intercept, coefficients = float(constant_bps), np.zeros(len(feature_names))
    elif method == "daily_robust":
        frame = pd.DataFrame({"day": [r["day"] for r in rows], "target": y, "weight": weights})
        daily = frame.groupby("day").apply(
            lambda g: np.average(g["target"], weights=g["weight"]), include_groups=False
        )
        intercept, coefficients = float(daily.median()), np.zeros(len(feature_names))
    else:
        design = np.column_stack((np.ones(len(x)), normalized))
        penalty = np.diag([0.0, *([float(ridge_alpha)] * len(feature_names))])
        theta = np.linalg.solve(
            design.T @ (weights[:, None] * design) + penalty, design.T @ (weights * y)
        )
        intercept, coefficients = float(theta[0]), theta[1:]
    if not math.isfinite(intercept):
        raise ValueError("finite fitted intercept required")
    residual = y - (intercept + normalized @ coefficients)
    return x, means, scales, intercept, coefficients, residual, day_parents


def _evaluate_heldout_model(model, test, report_cutoff, horizon_days, minimum_stratum_samples):
    provider, evaluated, unsupported = FrozenExecutionCostProvider(model), [], []
    for r in test:
        if aware_time(r["target_available_at"]) > report_cutoff:
            unsupported.append(
                {"sample_id": r["sample_id"], "reason": "test_target_not_yet_mature"}
            )
            continue
        prediction = provider.estimate({**r, "horizon_days": horizon_days})
        if prediction["status"] != "supported":
            unsupported.append({"sample_id": r["sample_id"], "reason": prediction["reason"]})
            continue
        error = float(r["execution_cost_bps"]) - prediction["total_cost_bps"]
        evaluated.append(
            {
                "sample_id": r["sample_id"],
                "parent_order_id": r["parent_order_id"],
                "day": r["day"],
                "symbol": r["symbol"],
                "side": r["side"],
                "notional": r["requested_notional"],
                "error_bps": error,
                "covered": prediction["lower_bps"]
                <= r["execution_cost_bps"]
                <= prediction["upper_bps"],
                "prediction": prediction,
            }
        )
    out = {
        "model": model,
        "status": "diagnostic" if evaluated else "insufficient",
        "evaluated": evaluated,
        "unsupported": unsupported,
        "metrics": None,
    }
    if evaluated:
        frame = pd.DataFrame(evaluated)
        days = frame.groupby("day")
        out["metrics"] = {
            "equal_day_bias_bps": float(days["error_bps"].mean().mean()),
            "equal_day_mae_bps": float(days["error_bps"].apply(lambda s: s.abs().mean()).mean()),
            "notional_weighted_mae_bps": float(
                np.average(frame["error_bps"].abs(), weights=frame["notional"])
            ),
            "underestimation_fraction": float((frame["error_bps"] > 0).mean()),
            "held_out_interval_coverage": float(frame["covered"].mean()),
            "independent_test_days": frame["day"].nunique(),
            "strata": [
                {
                    "symbol": key[0],
                    "side": key[1],
                    "samples": len(group),
                    "days": group["day"].nunique(),
                    "status": "diagnostic"
                    if len(group) >= minimum_stratum_samples and group["day"].nunique() >= 2
                    else "insufficient",
                    "mae_bps": float(group["error_bps"].abs().mean()),
                }
                for key, group in frame.groupby(["symbol", "side"])
            ],
        }
    return out


def _qualified_cost_sample(qualified, fills, orders, decisions, qmap, cutoff):
    matches = [
        r
        for (account, fid), r in fills.items()
        if fid == qualified["fill_id"]
        and r["client_order_id"] == qualified["client_order_id"]
        and (qualified.get("account_id") is None or account == qualified["account_id"])
    ]
    if len(matches) != 1:
        raise ValueError("ambiguous_or_missing_qualified_fill")
    fill = matches[0]
    key = (fill["account_id"], fill["client_order_id"])
    order, decision, quote = orders[key], decisions[key], qmap[qualified["quote_id"]]
    actual_hash = cost_digest({"fill": fill, "order": order, "quote": quote})
    if qualified.get("execution_fact_sha256") != actual_hash:
        raise ValueError("qualified_execution_facts_changed")
    if (
        order["evidence_class"] != "live"
        or fill["kind"] != "venue_trade"
        or fill.get("fee_evidence_status") != "recorded"
        or fill.get("fee_qualification_error")
    ):
        raise ValueError("complete_cost_provenance_missing")
    if not fill.get("parent_order_id"):
        raise ValueError("actual_parent_identity_missing")
    if not decision.get("source_id"):
        raise ValueError("decision_source_missing")
    at, features_at = (
        aware_time(decision["decision_at"]),
        aware_time(decision["features_available_at"]),
    )
    if max(aware_time(quote["available_at"]), aware_time(quote["observed_at"]), features_at) > at:
        raise ValueError("quote_or_features_unavailable_at_actual_decision")
    if at > aware_time(fill["occurred_at"]):
        raise ValueError("decision_follows_execution")
    available = max(aware_time(fill["available_at"]), aware_time(fill["fee_available_at"]))
    if available > cutoff:
        raise ValueError("cost_target_not_mature")
    reference = (_positive(quote["bid"]) + _positive(quote["ask"])) / 2
    quantity, price = _positive(fill["qty"]), _positive(fill["price"])
    direction = 1 if order["side"] in {"buy", "cover"} else -1
    fee = float(fill["fee_quote_value"])
    if not math.isfinite(fee):
        raise ValueError("finite_explicit_fee_required")
    requested_notional = _positive(order["requested_qty"]) * reference
    features = {
        **decision.get("features", {}),
        "spread_bps": (quote["ask"] - quote["bid"]) / reference * 10000,
        "requested_notional": requested_notional,
    }
    sample = {
        "sample_id": "/".join((fill["exchange_id"], fill["account_id"], fill["fill_id"])),
        "parent_order_id": "/".join(
            (fill["exchange_id"], fill["account_id"], str(fill["parent_order_id"]))
        ),
        "client_order_id": fill["client_order_id"],
        "symbol": order["symbol"],
        "side": order["side"],
        "market_identity": ":".join(
            (fill["exchange_id"], order["market_type"], "live", quote["quote_currency"])
        ),
        "decision_at": at.isoformat(),
        "features_available_at": at.isoformat(),
        "target_available_at": available.isoformat(),
        "requested_notional": requested_notional,
        "features": features,
        "execution_cost_bps": (direction * (price - reference) * quantity + fee)
        / (quantity * reference)
        * 10000,
        "evidence_verification_level": "verified_live_complete_cost",
        "source_evidence_sha256": cost_digest(
            {
                "qualification": qualified,
                "fill": fill,
                "order": order,
                "quote": quote,
                "decision": decision,
            }
        ),
    }
    return _validated_samples([sample])


def implementation_shortfall(
    intent: Mapping, fills, *, as_of, terminal=None, fee_fx=(), financing=None
):
    """Hand-calculable all-intent shortfall with partial/zero fills retained.

    ``fee_fx`` is an explicit point-in-time currency/quote conversion receipt.
    Financing is optional but its absence keeps the financing-inclusive total
    unknown; it is never manufactured as zero. Negative fees are real rebates.
    """
    cutoff = aware_time(as_of)
    side = intent.get("side")
    if side not in {"buy", "sell", "cover", "short"}:
        raise ValueError("explicit side required")
    sign = 1 if side in {"buy", "cover"} else -1
    requested, reference = _positive(intent["qty"]), _positive(intent["reference_price"])
    currency = intent["quote_currency"]
    anchor_at = aware_time(intent["decision_at"])
    if anchor_at > cutoff:
        raise ValueError("intent unavailable at report cutoff")
    known, unavailable = _known_intent_fills(intent, fills, cutoff, anchor_at)
    quantity = sum(float(r["qty"]) for r in known)
    if quantity > requested + max(1e-10, requested * 1e-10):
        raise ValueError("overfilled intent")
    residual = max(0.0, requested - quantity)
    price_amount = sum(sign * float(r["qty"]) * (float(r["price"]) - reference) for r in known)
    if not all(math.isfinite(v) for v in (quantity, price_amount, requested * reference)):
        raise ValueError("execution arithmetic overflow")
    terminal_amount, terminal_reason = _terminal_opportunity(
        intent, terminal, known, residual, requested, reference, sign, currency, anchor_at, cutoff
    )
    fees, fee_gaps = _explicit_fill_fees(known, fee_fx, currency, cutoff)
    complete_price = terminal_amount is not None and not unavailable
    full_price = price_amount + terminal_amount if complete_price else None
    full_cost = full_price + fees if full_price is not None and not fee_gaps else None
    financed, financing_reason = _financing_total(financing, full_cost, currency, cutoff)
    basis = requested * reference
    return {
        "schema": "intent-implementation-shortfall/v1",
        "status": "complete" if full_cost is not None else "insufficient",
        "client_order_id": intent["client_order_id"],
        "final_order_status": intent.get("status", "unknown"),
        "requested_qty": requested,
        "filled_qty": quantity,
        "residual_qty": residual,
        "quote_currency": currency,
        "reference_notional": basis,
        "executed_price_amount": price_amount,
        "executed_price_shortfall_bps": price_amount / (quantity * reference) * 10000
        if quantity
        else None,
        "opportunity_amount": terminal_amount,
        "opportunity_reason": terminal_reason,
        "order_price_shortfall_bps": full_price / basis * 10000 if full_price is not None else None,
        "fee_quote_amount": fees if not fee_gaps else None,
        "fee_gaps": fee_gaps,
        "order_execution_cost_bps": full_cost / basis * 10000 if full_cost is not None else None,
        "financing_inclusive_cost_bps": financed / basis * 10000 if financed is not None else None,
        "financing_reason": financing_reason,
        "unavailable_fill_ids": unavailable,
        "account_mutation": False,
        "production_approved": False,
        "interpretation": "price + explicit fees; residual opportunity is separate from actual cash charges",
    }


def _validated_samples(samples):
    seen, result = {}, []
    for original in samples:
        r = strict_json(dict(original))
        for key in (
            "sample_id",
            "parent_order_id",
            "client_order_id",
            "symbol",
            "side",
            "market_identity",
        ):
            if not isinstance(r.get(key), str) or not r[key].strip():
                raise ValueError("sample identity missing: " + key)
        if r["side"] not in {"buy", "sell", "cover", "short"}:
            raise ValueError("invalid sample side")
        if r["sample_id"] in seen:
            if seen[r["sample_id"]] != r:
                raise ValueError("conflicting sample identity")
            continue
        seen[r["sample_id"]] = r
        decision = aware_time(r["decision_at"])
        feature_at, mature = (
            aware_time(r["features_available_at"]),
            aware_time(r["target_available_at"]),
        )
        if feature_at > decision or mature < decision:
            raise ValueError("future feature or target clock before decision")
        if r.get("evidence_verification_level") not in {
            "verified_live_complete_cost",
            "simulation_fixture",
            "sandbox_fixture",
        }:
            raise ValueError("unverified complete-cost target provenance")
        if not math.isfinite(float(r["execution_cost_bps"])):
            raise ValueError("finite execution cost target required")
        _positive(r["requested_notional"])
        r["features"] = {k: float(v) for k, v in r["features"].items()}
        if not all(math.isfinite(v) for v in r["features"].values()):
            raise ValueError("finite features required")
        r["day"] = decision.date().isoformat()
        result.append(r)
    return result


def _training_support_domain(rows, feature_names, x):
    return {
        "symbol": sorted({r["symbol"] for r in rows}),
        "side": sorted({r["side"] for r in rows}),
        "market_identity": sorted({r["market_identity"] for r in rows}),
        "feature_ranges": {
            name: [float(x[:, j].min()), float(x[:, j].max())]
            for j, name in enumerate(feature_names)
        },
        "strata": {
            key: len(
                {
                    r["parent_order_id"]
                    for r in rows
                    if "|".join(r[f] for f in ("market_identity", "symbol", "side")) == key
                }
            )
            for key in {"|".join(r[f] for f in ("market_identity", "symbol", "side")) for r in rows}
        },
    }


def fit_frozen_cost_model(
    samples,
    *,
    method,
    training_cutoff,
    expires_at,
    protocol_sha256,
    feature_names=("spread_bps", "volatility", "requested_notional"),
    constant_bps=20.0,
    ridge_alpha=1.0,
    interval_coverage=0.95,
    minimum_train_days=2,
    minimum_stratum_samples=3,
    horizon_days=7,
    anchor="decision_mid",
):
    """Fit only supplied mature training rows; no test-day fitting is possible.

    Training day/parent weights prevent bursts of child fills dominating fit.
    The interval is a training-residual envelope, not a guaranteed calibrated
    prediction interval; held-out empirical coverage is reported separately.
    """
    if method not in {"constant", "daily_robust", "ridge"}:
        raise ValueError("unsupported cost model method")
    cutoff, expiry = aware_time(training_cutoff), aware_time(expires_at)
    if expiry <= cutoff or not 0 < interval_coverage < 1 or ridge_alpha <= 0:
        raise ValueError("valid model window, interval coverage and ridge penalty required")
    rows = _validated_samples(samples)
    if not rows or len({r["day"] for r in rows}) < minimum_train_days:
        raise ValueError("insufficient independent training days")
    if any(
        aware_time(r["decision_at"]) >= cutoff or aware_time(r["target_available_at"]) > cutoff
        for r in rows
    ):
        raise ValueError("training row or fee target unavailable before frozen cutoff")
    for r in rows:
        if "requested_notional" in r["features"] and r["features"]["requested_notional"] != float(
            r["requested_notional"]
        ):
            raise ValueError("notional support feature differs from actual requested notional")
    feature_names = list(feature_names)
    if not feature_names or len(set(feature_names)) != len(feature_names):
        raise ValueError("unique nonempty feature names required")
    x, means, scales, intercept, coefficients, residual, day_parents = _training_fit_matrix(
        rows, feature_names, method, constant_bps, ridge_alpha
    )
    tail = (1 - interval_coverage) / 2
    model = {
        "schema": "frozen-execution-cost-model/v1",
        "algorithm_version": "pr25-cost-fit/v1",
        "method": method,
        "target": "execution_cost_bps",
        "unit": "bps",
        "anchor": anchor,
        "evaluation_kind": "retrospective",
        "availability_semantics": "retrospective_asof_simulation_not_actual_model_publication",
        "horizon_days": horizon_days,
        "protocol_sha256": protocol_sha256,
        "training_cutoff": cutoff.isoformat(),
        "available_at": cutoff.isoformat(),
        "expires_at": expiry.isoformat(),
        "feature_names": feature_names,
        "feature_mean": means.tolist(),
        "feature_scale": scales.tolist(),
        "intercept_bps": intercept,
        "coefficients": coefficients.tolist(),
        "residual_lower_bps": float(np.quantile(residual, tail)),
        "residual_upper_bps": float(np.quantile(residual, 1 - tail)),
        "interval_coverage": interval_coverage,
        "interval_method": "training empirical residual envelope; not calibrated coverage guarantee",
        "minimum_stratum_samples": minimum_stratum_samples,
        "training_samples": len(rows),
        "training_days": len(day_parents),
        "weighting": "equal UTC day, equal parent within day, requested-notional-weighted child rows; weighted scaler",
        "training_parent_orders": len({r["parent_order_id"] for r in rows}),
        "training_manifest_sha256": cost_digest(rows),
        "training_sample_ids": [r["sample_id"] for r in rows],
        "evidence_verification_level": "verified_live_complete_cost"
        if all(r["evidence_verification_level"] == "verified_live_complete_cost" for r in rows)
        else "simulation_fixture",
        "support_domain": _training_support_domain(rows, feature_names, x),
        "production_approved": False,
    }
    model["model_sha256"] = cost_digest(model)
    FrozenExecutionCostProvider(model)  # Refuse malformed identities at creation.
    return model


def evaluate_cost_models(
    samples,
    *,
    test_start,
    expires_at,
    protocol_sha256,
    as_of,
    feature_names=("spread_bps", "volatility", "requested_notional"),
    constant_bps=20.0,
    minimum_train_days=2,
    minimum_test_days=2,
    minimum_stratum_samples=3,
    ridge_alpha=1.0,
    horizon_days=7,
):
    """Three predeclared arms; split full UTC days and never split a parent.

    Cross-boundary parents and immature training targets are retained in the
    exclusions table, rather than randomly mixing children into train/test.
    """
    boundary, report_cutoff = aware_time(test_start), aware_time(as_of)
    if boundary.time().isoformat() != "00:00:00":
        raise ValueError("test boundary must be a whole UTC day")
    rows = _validated_samples(samples)
    parents = {}
    for r in rows:
        parents.setdefault(r["parent_order_id"], []).append(r)
    train, test, excluded = [], [], []
    for pid, group in parents.items():
        before = [aware_time(r["decision_at"]) < boundary for r in group]
        if all(before) and all(aware_time(r["target_available_at"]) < boundary for r in group):
            train.extend(group)
        elif not any(before):
            test.extend(group)
        else:
            excluded.extend(
                {
                    "sample_id": r["sample_id"],
                    "parent_order_id": pid,
                    "reason": "cross_boundary_parent_or_immature_training_target",
                }
                for r in group
            )
    result = {
        "schema": "cost-model-later-day-evaluation/v1",
        "status": "insufficient",
        "as_of": report_cutoff.isoformat(),
        "test_start": boundary.isoformat(),
        "train_sample_ids": [r["sample_id"] for r in train],
        "test_sample_ids": [r["sample_id"] for r in test],
        "excluded": excluded,
        "train_days": len({r["day"] for r in train}),
        "test_days": len({r["day"] for r in test}),
        "models": {},
        "production_approved": False,
        "real_venue_calibration": False,
        "interpretation": "Coverage and held-out diagnostics are not capacity or precision approval.",
    }
    if result["train_days"] < minimum_train_days or result["test_days"] < minimum_test_days:
        result["reason"] = "insufficient_disjoint_mature_training_or_test_days"
        return result
    cutoff = (pd.Timestamp(boundary) - pd.Timedelta(nanoseconds=1)).isoformat()
    for method in ("constant", "daily_robust", "ridge"):
        model = fit_frozen_cost_model(
            train,
            method=method,
            training_cutoff=cutoff,
            expires_at=expires_at,
            protocol_sha256=protocol_sha256,
            feature_names=feature_names,
            constant_bps=constant_bps,
            minimum_train_days=minimum_train_days,
            minimum_stratum_samples=minimum_stratum_samples,
            ridge_alpha=ridge_alpha,
            horizon_days=horizon_days,
        )
        out = _evaluate_heldout_model(
            model, test, report_cutoff, horizon_days, minimum_stratum_samples
        )
        result["models"][method] = out
    result["status"] = "diagnostic"
    return result


def cost_samples_from_qualified_execution(
    evidence_report, ledgers, quotes, decision_receipts, *, as_of
):
    """Materialize model inputs only from independently joined complete facts.

    A submission clock cannot stand in for a strategy decision: a separate
    decision receipt supplies that clock and feature availability. If the
    readiness-selected quote is later than that decision, the sample is a gap.
    Missing actual parent IDs remain gaps rather than invented singleton groups.
    """
    from analysis.execution_calibration import validate_execution_quote

    cutoff = aware_time(as_of)
    if evidence_report.get("schema") != "strict-execution-evidence/v1" or evidence_report.get(
        "errors"
    ):
        raise ValueError("valid strict execution evidence report required")
    if aware_time(evidence_report["as_of"]) != cutoff:
        raise ValueError("evidence report cutoff mismatch")
    orders, fills, qmap, decisions = {}, {}, {}, {}
    for ledger in ledgers:
        for row in ledger["orders"]:
            key = (row["account"], row["client_order_id"])
            if key in orders and orders[key] != row:
                raise ValueError("conflicting order projection")
            orders[key] = row
        for row in ledger["fills"]:
            key = (row["account_id"], row["fill_id"])
            if key in fills and fills[key] != row:
                raise ValueError("conflicting fill projection")
            fills[key] = row
    for raw_quote in quotes:
        q = validate_execution_quote(raw_quote)
        if q["quote_id"] in qmap and qmap[q["quote_id"]] != q:
            raise ValueError("conflicting quote projection")
        qmap[q["quote_id"]] = q
    for receipt in decision_receipts:
        key = (receipt["account_id"], receipt["client_order_id"])
        if key in decisions and decisions[key] != receipt:
            raise ValueError("conflicting decision receipt")
        decisions[key] = receipt
    samples, excluded = [], []
    for qualified in evidence_report.get("qualified_fills", []):
        try:
            samples.extend(
                _qualified_cost_sample(qualified, fills, orders, decisions, qmap, cutoff)
            )
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            excluded.append(
                {
                    "fill_id": qualified.get("fill_id"),
                    "client_order_id": qualified.get("client_order_id"),
                    "reason": str(exc),
                }
            )
    return {
        "schema": "qualified-execution-cost-samples/v1",
        "status": "diagnostic" if samples else "insufficient",
        "samples": samples,
        "excluded": excluded,
        "as_of": cutoff.isoformat(),
        "source_report_sha256": cost_digest(evidence_report),
        "production_approved": False,
    }
