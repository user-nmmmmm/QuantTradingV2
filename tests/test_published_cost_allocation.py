"""Explicit publication trust across selector/optimizer and mandatory reductions."""

from copy import deepcopy
from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from analysis.execution_cost_publication import LocalExecutionCostPublicationAuthority
from core.planning.cost_aware_allocation import CostAwareAllocationPolicy, allocate_cost_aware
from core.planning.execution_cost_provider import PublishedExecutionCostProvider, cost_digest
from core.planning.selection_v2 import (
    SelectionPolicyV2,
    SelectionResultV2,
    SelectionRowV2,
    size_portfolio_targets,
)
from tests.test_execution_cost_publication import POLICY, context, publication as publication
from tests.test_trend_portfolio_v3 import selection

SYMBOL = "BTC/USDT"


def _chosen(artifact, *, as_of=None):
    at = pd.Timestamp(as_of or artifact["available_at"])
    index = pd.date_range(end=at.normalize() - pd.Timedelta(days=1), periods=60, tz="UTC")
    returns = pd.DataFrame(
        {SYMBOL: np.random.default_rng(32).normal(0.002, 0.006, len(index))}, index=index
    )
    row = SelectionRowV2(
        SYMBOL,
        True,
        (),
        True,
        score=0.1,
        close=100.0,
        stop_price=95.0,
        observed_at=index[-1].isoformat(),
        available_at=index[-1].isoformat(),
    )
    return SelectionResultV2(
        at.isoformat(), {SYMBOL: row}, (SYMBOL,), returns, SelectionPolicyV2(volatility_window=60)
    )


def _case(publication, *, as_of=None, provider=None, verifier=True, **changes):
    authority, _, artifact = publication
    chosen = _chosen(artifact, as_of=as_of)
    supplied = context(
        artifact, decision_at=chosen.as_of, feature_observation=authority.test_feature_receipt
    )
    supplied.update(changes)
    actual_provider = provider or PublishedExecutionCostProvider(
        artifact, publication_verifier=authority.verifier()
    )
    args = {
        "cost_aware_policy": CostAwareAllocationPolicy(enabled=True),
        "execution_cost_provider": actual_provider,
        "execution_cost_contexts": {SYMBOL: supplied},
        "evaluation_kind": supplied["evaluation_kind"],
        "information_policy": supplied["information_policy"],
    }
    if verifier is not False:
        args["publication_verifier"] = authority.verifier() if verifier is True else verifier
    return chosen, args


def _predictions(publication, *, kind="strict"):
    authority, _, artifact = publication
    provider = PublishedExecutionCostProvider(artifact, publication_verifier=authority.verifier())
    return [
        provider.estimate(
            context(
                artifact,
                side=side,
                evaluation_kind=kind,
                feature_observation=authority.test_feature_receipt,
            )
        )
        for side in ("buy", "sell")
    ]


def _wrong_verifier(tmp_path):
    authority = LocalExecutionCostPublicationAuthority(
        tmp_path / "other.sqlite",
        authority_id="another-configured-authority",
        signing_key=b"other-test-key-not-a-real-secret" * 2,
    )
    return authority.verifier()


@pytest.mark.parametrize("kind", ["strict", "prospective"])
def test_actual_local_release_reaches_allocator_and_selector(publication, kind):
    authority, _, artifact = publication
    rows = _predictions(publication, kind=kind)
    weights, audit = allocate_cost_aware(
        [0.03],
        [[0.02]],
        [0.0],
        [0.3],
        policy=CostAwareAllocationPolicy(enabled=True),
        cost_predictions=rows,
        decision_at=artifact["available_at"],
        evaluation_kind=kind,
        information_policy=POLICY,
        publication_verifier=authority.verifier(),
    )
    assert audit["status"] == "optimal" and audit["cost_provider"]["status"] == "supported"
    assert 0 < weights[0] <= 0.3
    assert audit["cost_provider"]["predictions"] == 2
    assert audit["holding_cost_is_objective_estimate_only"]
    assert audit["cost_provider"]["actual_account_costs_changed"] is False
    chosen, args = _case(publication, evaluation_kind=kind)
    result = size_portfolio_targets(chosen, **args)
    assert result.cost_aware_audit["status"] == "optimal"
    assert result.cost_aware_audit["cost_provider"]["status"] == "supported"
    assert (
        0 < result.target_weights[SYMBOL] <= result.cost_aware_audit["native_risk_envelope"][SYMBOL]
    )
    assert result.cost_aware_audit["research_only"]
    assert not artifact["production_approved"] and not artifact["real_venue_calibration"]


@pytest.mark.parametrize("trust", ["missing", "other_authority"])
@pytest.mark.parametrize("rebalance", ["immediate", "partial", "band"])
def test_no_or_wrong_consumer_verifier_retains_mandatory_reduction(
    publication, tmp_path, trust, rebalance
):
    authority, _, artifact = publication
    verifier = None if trust == "missing" else _wrong_verifier(tmp_path)
    policy = CostAwareAllocationPolicy(
        enabled=True, rebalance=rebalance, max_turnover_weight=0.0, no_trade_band=1.0
    )
    kwargs = {} if verifier is None else {"publication_verifier": verifier}
    weights, audit = allocate_cost_aware(
        [0.03],
        [[0.02]],
        [0.6],
        [0.2],
        policy=policy,
        cost_predictions=_predictions(publication),
        decision_at=artifact["available_at"],
        evaluation_kind="strict",
        information_policy=POLICY,
        **kwargs,
    )
    assert weights == pytest.approx([0.2])
    assert audit["status"] == "unsupported_cost_provider_mandatory_only"
    assert audit["mandatory_turnover_weight"] == pytest.approx(0.4)
    assert audit["discretionary_turnover_weight"] == 0 and not audit["cash_fallback"]
    chosen, args = _case(publication, verifier=False if verifier is None else verifier)
    args["cost_aware_policy"] = policy
    result = size_portfolio_targets(chosen, existing_weights={SYMBOL: 0.6}, **args)
    assert result.cost_aware_audit["status"] == "unsupported_cost_provider_mandatory_only"
    envelope = result.cost_aware_audit["native_risk_envelope"][SYMBOL]
    assert result.target_weights[SYMBOL] == envelope < 0.6
    assert result.mandatory_reduction_weights == {SYMBOL: envelope}


def test_provider_verifier_and_context_metadata_cannot_supply_consumer_trust(publication):
    authority, _, _ = publication
    chosen, args = _case(publication, verifier=False)
    args["execution_cost_contexts"][SYMBOL]["publication_verifier"] = authority.verifier()
    result = size_portfolio_targets(chosen, existing_weights={SYMBOL: 0.6}, **args)
    assert result.cost_aware_audit["status"] == "unsupported_cost_provider_mandatory_only"
    assert (
        result.cost_aware_audit["cost_provider"]["reason"]
        == "independently_configured_publication_verifier_required"
    )


def test_rehashing_changed_forecast_is_refused_after_cross_layer_request_binding(publication):
    authority, _, artifact = publication
    real = PublishedExecutionCostProvider(artifact, publication_verifier=authority.verifier())

    class TamperedProvider:
        def estimate(self, request):
            row = deepcopy(real.estimate(request))
            for field in ("total_cost_bps", "lower_bps", "upper_bps"):
                row[field] += 1.0
            row.pop("prediction_sha256")
            row["prediction_sha256"] = cost_digest(row)
            return row

    chosen, args = _case(publication, provider=TamperedProvider())
    result = size_portfolio_targets(chosen, existing_weights={SYMBOL: 0.6}, **args)
    assert result.cost_aware_audit["status"] == "unsupported_cost_provider_mandatory_only"
    assert "authenticated_model" in result.cost_aware_audit["cost_provider"]["reason"]


def test_legitimate_cached_buy_prediction_cannot_replace_the_sell_request(publication):
    row = _predictions(publication)[0]
    assert row["status"] == "supported" and row["side"] == "buy"

    class CachedBuy:
        def estimate(self, request):
            return deepcopy(row)

    chosen, args = _case(publication, provider=CachedBuy())
    result = size_portfolio_targets(chosen, existing_weights={SYMBOL: 0.6}, **args)
    assert result.cost_aware_audit["status"] == "unsupported_cost_provider_mandatory_only"
    assert (
        result.cost_aware_audit["cost_provider"]["reason"]
        == "unsupported_prediction:published_cost_prediction_request_mismatch"
    )


def test_legitimate_older_input_receipt_cannot_replace_requested_inputs(publication):
    authority, _, artifact = publication
    real = PublishedExecutionCostProvider(artifact, publication_verifier=authority.verifier())
    old_inputs = context(artifact, feature_observation=authority.test_feature_receipt)
    new_features = {"spread_bps": 1.0, "volatility": 0.01, "requested_notional": 100.0}
    receipt = authority.observe_features(
        {
            "symbol": SYMBOL,
            "market_identity": "fixture:spot:simulation:USDT",
            "features": new_features,
        }
    )
    at = pd.Timestamp.now(tz="UTC").isoformat()

    class OldInputs:
        def estimate(self, request):
            substituted = {
                **request,
                **{
                    key: old_inputs[key]
                    for key in ("features", "feature_observation", "features_available_at")
                },
            }
            row = real.estimate(substituted)
            assert row["status"] == "supported"
            assert row["predicted_at"] == request["decision_at"]
            return row

    chosen, args = _case(
        publication,
        as_of=at,
        provider=OldInputs(),
        features=new_features,
        feature_observation=receipt,
        features_available_at=at,
    )
    result = size_portfolio_targets(chosen, existing_weights={SYMBOL: 0.6}, **args)
    assert result.cost_aware_audit["status"] == "unsupported_cost_provider_mandatory_only"
    assert "request_mismatch" in result.cost_aware_audit["cost_provider"]["reason"]


@pytest.mark.parametrize("cutoff", ["before_release", "expired", "published"])
def test_unavailable_or_external_knowledge_never_authorizes_discretionary_risk(publication, cutoff):
    artifact = publication[2]
    at, policy = artifact["available_at"], POLICY
    if cutoff == "before_release":
        at = (pd.Timestamp(at) - pd.Timedelta(nanoseconds=1)).isoformat()
    elif cutoff == "expired":
        at = artifact["expires_at"]
    else:
        policy = {**POLICY, "knowledge": "published"}
    chosen, args = _case(publication, as_of=at, information_policy=policy)
    result = size_portfolio_targets(chosen, existing_weights={SYMBOL: 0.6}, **args)
    assert result.cost_aware_audit["status"] == "unsupported_cost_provider_mandatory_only"
    assert result.target_weights[SYMBOL] <= 0.6
    assert result.mandatory_reduction_weights


def test_default_and_empty_selector_keep_legacy_allocator_kwargs_shape(monkeypatch):
    import core.planning.selection_v2 as module

    chosen = selection(2)
    default = size_portfolio_targets(chosen).to_dict()
    assert size_portfolio_targets(chosen, publication_verifier=object()).to_dict() == default
    calls = []
    original = module.allocate_cost_aware

    def allocator(mu, covariance, existing, upper, *, policy):
        calls.append(policy)
        return original(mu, covariance, existing, upper, policy=policy)

    monkeypatch.setattr(module, "allocate_cost_aware", allocator)
    policy = CostAwareAllocationPolicy(enabled=True)
    size_portfolio_targets(chosen, cost_aware_policy=policy, publication_verifier=object())
    size_portfolio_targets(
        replace(chosen, selected_symbols=()),
        cost_aware_policy=policy,
        publication_verifier=object(),
    )
    assert calls == [policy, policy]


def test_legitimate_btc_prediction_cannot_replace_a_different_selected_asset(publication):
    authority, _, artifact = publication
    real = PublishedExecutionCostProvider(artifact, publication_verifier=authority.verifier())

    class BtcOnlyCache:
        def estimate(self, request):
            row = real.estimate({**request, "symbol": SYMBOL})
            assert row["status"] == "supported"
            return row

    chosen, args = _case(publication, provider=BtcOnlyCache())
    other = "ETH/USDT"
    chosen = replace(
        chosen,
        rows={**chosen.rows, other: replace(chosen.rows[SYMBOL], symbol=other)},
        selected_symbols=(SYMBOL, other),
        returns=chosen.returns.assign(**{other: chosen.returns[SYMBOL] * 1.1}),
    )
    args["execution_cost_contexts"][other] = {
        **args["execution_cost_contexts"][SYMBOL],
        "symbol": other,
    }
    result = size_portfolio_targets(chosen, existing_weights={SYMBOL: 0.5, other: 0.5}, **args)
    assert result.cost_aware_audit["status"] == "unsupported_cost_provider_mandatory_only"
    assert "request_mismatch" in result.cost_aware_audit["cost_provider"]["reason"]
    assert set(result.mandatory_reduction_weights) == {SYMBOL, other}


def test_legacy_prediction_aggregator_kwargs_remain_unchanged_without_verifier(monkeypatch):
    import core.planning.execution_cost_provider as module

    calls = []

    def aggregate(rows, *, as_of, horizon_days, evaluation_kind=None, information_policy=None):
        calls.append((rows, as_of, horizon_days, evaluation_kind, information_policy))
        return {"status": "supported", "trading_cost_bps": 12.0}

    monkeypatch.setattr(module, "conservative_trading_cost", aggregate)
    rows = [{"legacy-fixture": True}]
    _, audit = allocate_cost_aware(
        [0.03],
        [[0.02]],
        [0.0],
        [0.3],
        policy=CostAwareAllocationPolicy(enabled=True),
        cost_predictions=rows,
        decision_at="2024-01-01T00:00:00Z",
    )
    assert audit["status"] == "optimal"
    assert calls == [(rows, "2024-01-01T00:00:00Z", 7, None, None)]


def test_valid_numeric_cached_features_cannot_substitute_boolean_input(publication):
    authority, _, artifact = publication
    features = {"spread_bps": 1.0, "volatility": 0.01, "requested_notional": 100.0}
    receipt = authority.observe_features(
        {"symbol": SYMBOL, "market_identity": "fixture:spot:simulation:USDT", "features": features}
    )
    at = pd.Timestamp.now(tz="UTC").isoformat()
    real = PublishedExecutionCostProvider(artifact, publication_verifier=authority.verifier())
    rows = {
        side: real.estimate(
            context(
                artifact,
                decision_at=at,
                features_available_at=at,
                features=features,
                feature_observation=receipt,
                side=side,
            )
        )
        for side in ("buy", "sell")
    }
    assert all(row["status"] == "supported" for row in rows.values())
    invalid_features = {**features, "spread_bps": True}
    assert (
        real.estimate(
            context(
                artifact,
                decision_at=at,
                features_available_at=at,
                features=invalid_features,
                feature_observation=receipt,
            )
        )["status"]
        == "unsupported"
    )

    class CachedNumeric:
        def estimate(self, request):
            return deepcopy(rows[request["side"]])

    chosen, args = _case(
        publication,
        as_of=at,
        provider=CachedNumeric(),
        features=invalid_features,
        feature_observation=receipt,
        features_available_at=at,
    )
    result = size_portfolio_targets(chosen, existing_weights={SYMBOL: 0.6}, **args)
    assert result.cost_aware_audit["status"] == "unsupported_cost_provider_mandatory_only"
    assert "request_mismatch" in result.cost_aware_audit["cost_provider"]["reason"]


@pytest.mark.parametrize("failure", ["missing_context", "invalid_provider_payload"])
def test_missing_or_invalid_forecast_inputs_never_restore_discretionary_risk(publication, failure):
    class InvalidProvider:
        def estimate(self, request):
            if failure == "missing_context":
                raise AssertionError("missing context must abstain before calling provider")
            return []

    chosen, args = _case(publication, provider=InvalidProvider())
    if failure == "missing_context":
        args["execution_cost_contexts"] = {}
    result = size_portfolio_targets(chosen, existing_weights={SYMBOL: 0.6}, **args)
    assert result.cost_aware_audit["status"] == "unsupported_cost_provider_mandatory_only"
    assert result.mandatory_reduction_weights
    assert result.target_weights[SYMBOL] == result.mandatory_reduction_weights[SYMBOL]


@pytest.mark.parametrize("malformed", [True, False])
def test_verifier_configuration_or_runtime_errors_preserve_mandatory_anchor(publication, malformed):
    class UnavailableVerifier:
        def resolve_model(self, *args, **kwargs):
            raise RuntimeError("fixture verifier unavailable")

        def resolve_features(self, *args, **kwargs):
            raise RuntimeError("fixture verifier unavailable")

    verifier = object() if malformed else UnavailableVerifier()
    artifact = publication[2]
    weights, audit = allocate_cost_aware(
        [0.03],
        [[0.02]],
        [0.6],
        [0.2],
        policy=CostAwareAllocationPolicy(enabled=True, max_turnover_weight=0.0),
        cost_predictions=_predictions(publication),
        decision_at=artifact["available_at"],
        evaluation_kind="strict",
        information_policy=POLICY,
        publication_verifier=verifier,
    )
    assert weights == pytest.approx([0.2])
    assert audit["status"] == "unsupported_cost_provider_mandatory_only"
    assert audit["discretionary_turnover_weight"] == 0.0
    assert audit["cost_provider"]["reason"].startswith("cost_prediction_validation_error:")
    assert not audit["cost_provider"]["actual_account_costs_changed"]
    chosen, args = _case(publication, verifier=verifier)
    result = size_portfolio_targets(chosen, existing_weights={SYMBOL: 0.6}, **args)
    assert result.cost_aware_audit["status"] == "unsupported_cost_provider_mandatory_only"
    assert result.mandatory_reduction_weights
