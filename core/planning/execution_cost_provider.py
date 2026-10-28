"""Frozen research cost estimates; account charges remain Broker facts.

The current optimizer accepts a single nonnegative L1 cost. Domain-qualified
upper estimates are reduced to that scalar explicitly, without claiming that
this preserves direction-specific costs or proves executable venue capacity.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
import re
from typing import Mapping, Protocol

import numpy as np
import pandas as pd

_HASH = re.compile(r"[0-9a-f]{64}")


def strict_json(value):
    return json.loads(json.dumps(value, sort_keys=True, allow_nan=False, ensure_ascii=False))


def cost_digest(value):
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False, ensure_ascii=False
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def aware_time(value):
    if not isinstance(value, (str, datetime)):
        raise ValueError("aware ISO timestamp required")
    result = pd.Timestamp(value)
    if pd.isna(result) or result.tzinfo is None:
        raise ValueError("aware ISO timestamp required")
    return result.tz_convert("UTC")


def _number(value, *, positive=False):
    if isinstance(value, bool):
        raise ValueError("finite numeric value required")
    result = float(value)
    if not math.isfinite(result) or positive and result <= 0:
        raise ValueError("finite positive value required" if positive else "finite value required")
    return result


def cost_evaluation_mode(evaluation_kind=None, information_policy=None):
    """Resolve consumer mode without letting omitted metadata downgrade strict."""
    if information_policy is not None:
        if not isinstance(information_policy, Mapping) or information_policy.get("mode") not in {
            "strict",
            "retrospective",
        }:
            raise ValueError("explicit cost information_policy mode required")
        if information_policy.get("knowledge", "local") not in {
            "local",
            "published",
        } or information_policy.get("unknown", "exclude") not in {"exclude", "raise"}:
            raise ValueError("invalid cost information policy")
        mode = information_policy["mode"]
    else:
        mode = None
    kind = (
        evaluation_kind
        if evaluation_kind is not None
        else ("strict" if mode == "strict" else "retrospective")
    )
    if kind not in {"retrospective", "strict", "prospective"}:
        raise ValueError("unsupported cost evaluation kind")
    if (
        mode == "strict"
        and kind == "retrospective"
        or mode == "retrospective"
        and kind in {"strict", "prospective"}
    ):
        raise ValueError("cost evaluation kind conflicts with information policy")
    return kind


class ExecutionCostProvider(Protocol):
    def estimate(self, context: Mapping) -> dict:
        """Return an estimate with source identity, point-in-time and support."""


class FrozenExecutionCostProvider:
    """Use a byte-identified fitted artifact without training at prediction time."""

    def __init__(self, model: Mapping):
        self.model = strict_json(deepcopy(dict(model)))
        supplied = self.model.pop("model_sha256", None)
        self.model_sha256 = cost_digest(self.model)
        if supplied != self.model_sha256:
            raise ValueError("cost model hash mismatch")
        if self.model.get("schema") != "frozen-execution-cost-model/v1":
            raise ValueError("unsupported cost model schema")
        if self.model.get("evaluation_kind", "retrospective") != "retrospective":
            raise ValueError(
                "v1 cost artifact only supports retrospective research; actual publication adapter required"
            )
        if not _HASH.fullmatch(str(self.model.get("protocol_sha256", ""))):
            raise ValueError("cost protocol hash required")
        if aware_time(self.model["available_at"]) >= aware_time(self.model["expires_at"]):
            raise ValueError("cost model must have a positive validity window")
        if self.model.get("unit") != "bps" or self.model.get("target") != "execution_cost_bps":
            raise ValueError("explicit execution-cost bps target required")
        horizon, minimum = self.model.get("horizon_days"), self.model.get("minimum_stratum_samples")
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 1 for v in (horizon, minimum)):
            raise ValueError("positive integer horizon and stratum support required")
        names = self.model.get("feature_names")
        if not isinstance(names, list) or not names or len(set(names)) != len(names):
            raise ValueError("unique feature definitions required")
        n = len(names)
        for key in ("feature_mean", "feature_scale", "coefficients"):
            values = self.model.get(key)
            if not isinstance(values, list) or len(values) != n:
                raise ValueError("model feature dimensions mismatch")
            for value in values:
                _number(value, positive=key == "feature_scale")
        for key in ("intercept_bps", "residual_lower_bps", "residual_upper_bps"):
            _number(self.model[key])
        if self.model["residual_lower_bps"] > self.model["residual_upper_bps"]:
            raise ValueError("invalid frozen residual interval")

    def estimate(self, context: Mapping) -> dict:
        m = self.model
        result = {
            "schema": "execution-cost-prediction/v1",
            "status": "unsupported",
            "reason": None,
            "model_sha256": self.model_sha256,
            "protocol_sha256": m["protocol_sha256"],
            "target": m["target"],
            "anchor": m["anchor"],
            "horizon_days": m["horizon_days"],
            "unit": "bps",
            "available_at": m["available_at"],
            "expires_at": m["expires_at"],
            "support_domain": deepcopy(m["support_domain"]),
            "evidence_verification_level": m["evidence_verification_level"],
            "evaluation_kind": m.get("evaluation_kind", "retrospective"),
            "availability_semantics": m.get(
                "availability_semantics", "retrospective_asof_simulation"
            ),
            "total_cost_bps": None,
            "lower_bps": None,
            "upper_bps": None,
            "components_bps": {},
            "production_approved": False,
        }
        try:
            if cost_digest(m) != self.model_sha256:
                raise ValueError("cost model changed after freeze")
            requested_kind = cost_evaluation_mode(
                context.get("evaluation_kind"), context.get("information_policy")
            )
            result["requested_evaluation_kind"] = requested_kind
            if requested_kind != "retrospective":
                raise ValueError(
                    "retrospective_cost_model_requires_actual_publication_adapter_for_strict_or_prospective"
                )
            at = aware_time(context["decision_at"])
            if aware_time(m["available_at"]) > at:
                raise ValueError("model_unavailable_at_decision")
            if at >= aware_time(m["expires_at"]):
                raise ValueError("expired_model")
            if (
                context.get("horizon_days") != m["horizon_days"]
                or context.get("anchor", "decision_mid") != m["anchor"]
            ):
                raise ValueError("target_anchor_or_horizon_mismatch")
            domain = m["support_domain"]
            for field in ("symbol", "side", "market_identity"):
                if context.get(field) not in domain[field]:
                    raise ValueError("unsupported_" + field)
            if aware_time(context["features_available_at"]) > at:
                raise ValueError("future_feature")
            features = context["features"]
            values = []
            for name in m["feature_names"]:
                value = _number(features[name])
                bounds = domain["feature_ranges"][name]
                if not bounds[0] <= value <= bounds[1]:
                    raise ValueError("feature_outside_support:" + name)
                values.append(value)
            strata = domain["strata"]
            key = "|".join(str(context[f]) for f in ("market_identity", "symbol", "side"))
            if strata.get(key, 0) < m["minimum_stratum_samples"]:
                raise ValueError("insufficient_stratum")
            vector = (np.asarray(values) - np.asarray(m["feature_mean"])) / np.asarray(
                m["feature_scale"]
            )
            predicted = _number(m["intercept_bps"] + np.dot(vector, m["coefficients"]))
            lower = min(predicted, _number(predicted + m["residual_lower_bps"]))
            upper = max(predicted, _number(predicted + m["residual_upper_bps"]))
            if lower > upper:
                raise ValueError("invalid_prediction_interval")
            result.update(
                status="supported",
                total_cost_bps=predicted,
                lower_bps=lower,
                upper_bps=upper,
                components_bps={"execution_price_and_explicit_fee_target": predicted},
                predicted_at=at.isoformat(),
                symbol=context["symbol"],
                side=context["side"],
                market_identity=context["market_identity"],
                interval_interpretation="training residual range; held-out coverage must be evaluated",
            )
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            result["reason"] = str(exc)
        result["prediction_sha256"] = cost_digest(result)
        return result

    predict = estimate


class ExecutionCostPublicationVerifier(Protocol):
    def resolve_model(self, publication: Mapping, *, as_of, information_policy=None) -> dict:
        """Resolve only against independently configured publication authority."""

    def resolve_features(self, observation: Mapping, *, as_of) -> dict:
        """Resolve an actual local input observation from the trusted authority."""


def _published_prediction_context(context, kind, policy):
    prediction_context = {
        key: deepcopy(context[key])
        for key in (
            "decision_at",
            "features_available_at",
            "features",
            "symbol",
            "side",
            "market_identity",
            "horizon_days",
            "feature_observation",
        )
    }
    for key in ("decision_at", "features_available_at"):
        prediction_context[key] = aware_time(prediction_context[key]).isoformat()
    prediction_context.update(
        anchor=context.get("anchor", "decision_mid"),
        evaluation_kind=kind,
        information_policy=deepcopy(policy),
    )
    return prediction_context


class PublishedExecutionCostProvider:
    """Explicit v2 adapter authenticated by a trusted local publication resolver.

    The verifier is application trust configuration, never taken from model or
    context metadata. A local release proves availability, not venue provenance,
    interval accuracy, capacity or permission to trade.
    """

    def __init__(
        self, artifact: Mapping, *, publication_verifier: ExecutionCostPublicationVerifier
    ):
        self.artifact = strict_json(deepcopy(dict(artifact)))
        supplied = self.artifact.pop("model_sha256", None)
        self.model_sha256 = cost_digest(self.artifact)
        self.artifact["model_sha256"] = supplied
        if (
            supplied != self.model_sha256
            or self.artifact.get("schema") != "published-execution-cost-model/v2"
        ):
            raise ValueError("published cost artifact schema or hash mismatch")
        if publication_verifier is None or any(
            not callable(getattr(publication_verifier, method, None))
            for method in ("resolve_model", "resolve_features")
        ):
            raise ValueError("independently configured publication verifier required")
        self.publication_verifier = publication_verifier
        self._base = FrozenExecutionCostProvider(self.artifact["fitted_model"])

    def estimate(self, context: Mapping) -> dict:
        a = self.artifact
        result = {
            "schema": "execution-cost-prediction/v2",
            "status": "unsupported",
            "reason": None,
            "model_sha256": self.model_sha256,
            "protocol_sha256": a["fitted_model"]["protocol_sha256"],
            "target": a["fitted_model"]["target"],
            "unit": "bps",
            "anchor": a["fitted_model"]["anchor"],
            "horizon_days": a["fitted_model"]["horizon_days"],
            "available_at": a["available_at"],
            "expires_at": a["expires_at"],
            "availability_semantics": a["publication_semantics"],
            "publication": deepcopy(a["publication"]),
            "training_evidence": deepcopy(a["training_evidence"]),
            "total_cost_bps": None,
            "lower_bps": None,
            "upper_bps": None,
            "production_approved": False,
            "real_venue_calibration": False,
        }
        try:
            unsigned = {key: value for key, value in a.items() if key != "model_sha256"}
            if cost_digest(unsigned) != self.model_sha256:
                raise ValueError("published_cost_artifact_changed")
            kind = cost_evaluation_mode(
                context.get("evaluation_kind"), context.get("information_policy")
            )
            result["evaluation_kind"] = result["requested_evaluation_kind"] = kind
            at = aware_time(context["decision_at"])
            policy = context.get("information_policy")
            authenticated = self.publication_verifier.resolve_model(
                a["publication"], as_of=at, information_policy=policy
            )
            if authenticated != a:
                raise ValueError("published_cost_artifact_does_not_match_receipt")
            observation = self.publication_verifier.resolve_features(
                context["feature_observation"], as_of=at
            )
            if observation["inputs"] != {
                key: context[key] for key in ("symbol", "market_identity", "features")
            }:
                raise ValueError("feature_inputs_do_not_match_local_observation")
            # Reuse the frozen numerical/domain engine, then replace only its
            # retrospective availability claim with the verified v2 release.
            known_at = max(
                aware_time(context["features_available_at"]), aware_time(observation["observed_at"])
            )
            base_context = {
                **dict(context),
                "features_available_at": known_at.isoformat(),
                "evaluation_kind": "retrospective",
                "information_policy": None,
            }
            base = self._base.estimate(base_context)
            if base["status"] != "supported":
                raise ValueError(base["reason"])
            prediction_context = _published_prediction_context(context, kind, policy)
            result.update(
                {
                    key: value
                    for key, value in base.items()
                    if key
                    in {
                        "support_domain",
                        "evidence_verification_level",
                        "total_cost_bps",
                        "lower_bps",
                        "upper_bps",
                        "components_bps",
                        "predicted_at",
                        "symbol",
                        "side",
                        "market_identity",
                        "interval_interpretation",
                    }
                }
            )
            result.update(
                status="supported",
                prediction_context=strict_json(prediction_context),
                evidence_verification_level="authenticated_local_publication_not_venue_authentication",
            )
        except (ValueError, TypeError, KeyError, OverflowError) as exc:
            result["reason"] = str(exc)
        result["prediction_sha256"] = cost_digest(result)
        return result

    predict = estimate


def _verify_published_prediction(row, *, publication_verifier, at, information_policy):
    if publication_verifier is None:
        raise ValueError("independently_configured_publication_verifier_required")
    artifact = publication_verifier.resolve_model(
        row["publication"], as_of=at, information_policy=information_policy
    )
    context = row["prediction_context"]
    if not isinstance(context, Mapping):
        raise ValueError("published_prediction_context_must_be_mapping")
    # Resolve knowledge with the consumer's own policy, then reproduce every
    # signed-content field. Rehashing an altered point, interval, support domain,
    # evaluation mode, publication reference or context does not authenticate it.
    if cost_evaluation_mode(
        context.get("evaluation_kind"), context.get("information_policy")
    ) != row.get("evaluation_kind"):
        raise ValueError("published_prediction_context_mode_mismatch")
    expected = PublishedExecutionCostProvider(
        artifact, publication_verifier=publication_verifier
    ).estimate(context)
    candidate = deepcopy(dict(row))
    candidate["prediction_sha256"] = cost_digest(row)
    if candidate != expected:
        raise ValueError("published_prediction_does_not_match_authenticated_model")


def conservative_trading_cost(
    predictions,
    *,
    as_of,
    horizon_days,
    model_sha256=None,
    protocol_sha256=None,
    anchor="decision_mid",
    evaluation_kind=None,
    information_policy=None,
    publication_verifier=None,
):
    """Validate actual forecast artifacts and map their maximum upper to scalar.

    Negative realized rebates remain account facts. This nonnegative reduction
    is for the optimizer objective only; it never writes cash, fills or fees.
    """
    output: dict = {
        "status": "unsupported",
        "trading_cost_bps": None,
        "reason": None,
        "aggregation": "max(0, maximum domain-qualified prediction upper bps)",
        "information_loss": "one scalar discards asset, direction, size and state differences",
        "actual_account_costs_changed": False,
        "production_approved": False,
    }
    try:
        requested_kind = cost_evaluation_mode(evaluation_kind, information_policy)
        output["requested_evaluation_kind"] = requested_kind
        at = aware_time(as_of)
        rows = list(predictions)
        if not rows:
            raise ValueError("no_cost_predictions")
        upper = []
        for original in rows:
            row = deepcopy(dict(original))
            expected = row.pop("prediction_sha256", None)
            if expected != cost_digest(row):
                raise ValueError("prediction_hash_mismatch")
            if (
                row.get("schema")
                not in {"execution-cost-prediction/v1", "execution-cost-prediction/v2"}
                or row.get("status") != "supported"
            ):
                raise ValueError("unsupported_prediction:" + str(row.get("reason")))
            if row["schema"] == "execution-cost-prediction/v1":
                if requested_kind != "retrospective":
                    raise ValueError(
                        "v1_cost_predictions_require_actual_publication_adapter_for_strict_or_prospective"
                    )
                if row.get("evaluation_kind", "retrospective") != "retrospective":
                    raise ValueError("v1_prediction_cannot_relabel_actual_publication_mode")
            else:
                if requested_kind in {"strict", "prospective"} and row.get(
                    "evaluation_kind"
                ) not in {"strict", "prospective"}:
                    raise ValueError("retrospective_prediction_not_a_strict_publication_request")
                _verify_published_prediction(
                    row,
                    publication_verifier=publication_verifier,
                    at=at,
                    information_policy=information_policy,
                )
            for field, expected_hash in (
                ("model_sha256", model_sha256),
                ("protocol_sha256", protocol_sha256),
            ):
                if (
                    not _HASH.fullmatch(str(row.get(field, "")))
                    or expected_hash is not None
                    and row[field] != expected_hash
                ):
                    raise ValueError(field + "_mismatch")
            if row.get("unit") != "bps" or row.get("target") != "execution_cost_bps":
                raise ValueError("cost_unit_or_target_mismatch")
            if row.get("anchor") != anchor or row.get("horizon_days") != horizon_days:
                raise ValueError("cost_anchor_or_horizon_mismatch")
            if aware_time(row["available_at"]) > at or aware_time(row["predicted_at"]) > at:
                raise ValueError("cost_prediction_not_available")
            if at >= aware_time(row["expires_at"]):
                raise ValueError("expired_cost_prediction")
            low, point, high = (
                _number(row[k]) for k in ("lower_bps", "total_cost_bps", "upper_bps")
            )
            if low > high or not low <= point <= high:
                raise ValueError("inconsistent_cost_prediction_interval")
            upper.append(high)
        output.update(
            status="supported",
            trading_cost_bps=max(0.0, max(upper)),
            predictions=len(rows),
            prediction_hashes=[r["prediction_sha256"] for r in rows],
            model_hashes=sorted({r["model_sha256"] for r in rows}),
            protocol_hashes=sorted({r["protocol_sha256"] for r in rows}),
        )
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        output["reason"] = str(exc)
    return output


class FixedExecutionCostProvider:
    """Declared constant assumption for compatibility, not a fitted venue model."""

    def __init__(
        self,
        bps,
        *,
        horizon_days=7,
        available_at="1970-01-01T00:00:00Z",
        expires_at="2100-01-01T00:00:00Z",
        protocol_sha256="0" * 64,
        anchor="decision_mid",
    ):
        bps = _number(bps)
        if not isinstance(horizon_days, int) or isinstance(horizon_days, bool) or horizon_days < 1:
            raise ValueError("positive horizon required")
        if not _HASH.fullmatch(str(protocol_sha256)):
            raise ValueError("protocol hash required")
        if aware_time(available_at) >= aware_time(expires_at):
            raise ValueError("positive validity window required")
        self.parameters = {
            "bps": bps,
            "horizon_days": horizon_days,
            "available_at": available_at,
            "expires_at": expires_at,
            "protocol_sha256": protocol_sha256,
            "anchor": anchor,
        }
        self.model_sha256 = cost_digest(self.parameters)

    def estimate(self, context: Mapping) -> dict:
        p = self.parameters
        row = {
            "schema": "execution-cost-prediction/v1",
            "status": "unsupported",
            "reason": None,
            "model_sha256": self.model_sha256,
            "protocol_sha256": p["protocol_sha256"],
            "target": "execution_cost_bps",
            "anchor": p["anchor"],
            "horizon_days": p["horizon_days"],
            "unit": "bps",
            "available_at": p["available_at"],
            "expires_at": p["expires_at"],
            "total_cost_bps": None,
            "lower_bps": None,
            "upper_bps": None,
            "support_domain": {"scope": "declared_constant_assumption_not_empirical_capacity"},
            "components_bps": {},
            "evidence_verification_level": "declared_assumption",
            "evaluation_kind": "retrospective",
            "availability_semantics": "declared_research_assumption_not_actual_model_publication",
            "production_approved": False,
        }
        try:
            if cost_digest(p) != self.model_sha256:
                raise ValueError("constant policy changed after freeze")
            requested_kind = cost_evaluation_mode(
                context.get("evaluation_kind"), context.get("information_policy")
            )
            row["requested_evaluation_kind"] = requested_kind
            if requested_kind != "retrospective":
                raise ValueError(
                    "declared_constant_requires_actual_publication_adapter_for_strict_or_prospective"
                )
            at = aware_time(context["decision_at"])
            if not aware_time(p["available_at"]) <= at < aware_time(p["expires_at"]):
                raise ValueError("constant_policy_unavailable_or_expired")
            if (
                context.get("horizon_days") != p["horizon_days"]
                or context.get("anchor", "decision_mid") != p["anchor"]
            ):
                raise ValueError("constant_policy_anchor_or_horizon_mismatch")
            if not context.get("symbol") or context.get("side") not in {
                "buy",
                "sell",
                "cover",
                "short",
            }:
                raise ValueError("explicit_instrument_and_direction_required")
            row.update(
                status="supported",
                total_cost_bps=p["bps"],
                lower_bps=p["bps"],
                upper_bps=p["bps"],
                predicted_at=at.isoformat(),
                symbol=context["symbol"],
                side=context["side"],
                market_identity=context.get("market_identity"),
                components_bps={"declared_total_execution_assumption": p["bps"]},
            )
        except (ValueError, KeyError, TypeError) as exc:
            row["reason"] = str(exc)
        row["prediction_sha256"] = cost_digest(row)
        return row

    predict = estimate
