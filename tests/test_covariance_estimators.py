"""Independent covariance identities and default-off portfolio compatibility."""

import math

import numpy as np
import pytest

from core.planning.covariance_estimators import ESTIMATORS, estimate_covariance
from core.planning.cost_aware_allocation import CostAwareAllocationPolicy
from core.planning.selection_v2 import size_portfolio_targets
from tests.test_trend_portfolio_v3 import selection


def _independent_lw_reference(values):
    # Literal Appendix B scalar sample-moment sums, deliberately independent
    # of the matrix-product implementation and its theta orientation.
    t, n = values.shape
    centered = values - values.mean(axis=0)
    sample = np.empty((n, n))
    for i in range(n):
        for j in range(n):
            sample[i, j] = sum(centered[k, i] * centered[k, j] for k in range(t)) / t
    r = sum(
        sample[i, j] / math.sqrt(sample[i, i] * sample[j, j])
        for i in range(n)
        for j in range(n)
        if i != j
    ) / (n * (n - 1))
    target = np.array(
        [
            [
                sample[i, i] if i == j else r * math.sqrt(sample[i, i] * sample[j, j])
                for j in range(n)
            ]
            for i in range(n)
        ]
    )
    pi = rho = 0.0
    for i in range(n):
        for j in range(n):
            products = [centered[k, i] * centered[k, j] - sample[i, j] for k in range(t)]
            pij = sum(v * v for v in products) / t
            pi += pij
            if i == j:
                rho += pij
            else:
                ti = sum((centered[k, i] ** 2 - sample[i, i]) * products[k] for k in range(t)) / t
                tj = sum((centered[k, j] ** 2 - sample[j, j]) * products[k] for k in range(t)) / t
                rho += (
                    r
                    / 2
                    * (
                        math.sqrt(sample[j, j] / sample[i, i]) * ti
                        + math.sqrt(sample[i, i] / sample[j, j]) * tj
                    )
                )
    gamma = sum((sample[i, j] - target[i, j]) ** 2 for i in range(n) for j in range(n))
    intensity = float(np.clip((pi - rho) / (gamma * t), 0, 1))
    return ((1 - intensity) * sample + intensity * target) * t / (t - 1), intensity


def heterogenous():
    values = np.random.default_rng(19).normal(0, 0.01, (80, 3))
    values[:, 1] = 0.5 * values[:, 0] + values[:, 1]
    values[:, 2] = -0.1 * values[:, 0] + 0.6 * values[:, 2]
    return values


def test_two_asset_hand_covariance_and_constant_target_degeneracy():
    values = np.array([[0.1, 0.2], [0.2, 0.4], [0.3, 0.3]])
    expected = np.array([[0.01, 0.005], [0.005, 0.01]])
    for arm in ("sample", "lw_constant_correlation"):
        result = estimate_covariance(values, estimator_id=arm)
        np.testing.assert_allclose(result.covariance, expected, rtol=1e-14)
    assert result.audit["shrinkage_intensity"] == 0
    assert result.audit["degeneracy"] == "constant_correlation_target_equals_sample"


def test_three_asset_intensity_matches_independent_scalar_moment_reference():
    values = heterogenous()
    expected, intensity = _independent_lw_reference(values)
    result = estimate_covariance(values, estimator_id="lw_constant_correlation")
    assert 0 < intensity < 1
    assert result.audit["shrinkage_intensity"] == pytest.approx(intensity, rel=1e-12)
    np.testing.assert_allclose(result.covariance, expected, rtol=1e-12, atol=1e-18)
    assert result.audit["ddof"] == 1 and result.audit["periods_per_year_applied"] == 0


@pytest.mark.parametrize("arm", ESTIMATORS)
def test_symmetry_psd_diagonal_and_permutation_equivariance(arm):
    values = heterogenous()
    estimate = estimate_covariance(values, estimator_id=arm)
    np.testing.assert_allclose(estimate.covariance, estimate.covariance.T, atol=1e-18)
    assert np.linalg.eigvalsh(estimate.covariance).min() > -1e-15
    np.testing.assert_allclose(np.diag(estimate.covariance), np.var(values, axis=0, ddof=1))
    permutation = [2, 0, 1]
    permuted = estimate_covariance(values[:, permutation], estimator_id=arm)
    np.testing.assert_allclose(
        permuted.covariance,
        estimate.covariance[np.ix_(permutation, permutation)],
        rtol=1e-13,
        atol=1e-18,
    )


@pytest.mark.parametrize("factor", [1e-4, 1e-1, 2.0])
def test_constant_correlation_intensity_is_scale_invariant(factor):
    values = heterogenous()
    base = estimate_covariance(values, estimator_id="lw_constant_correlation")
    scaled = estimate_covariance(values * factor, estimator_id="lw_constant_correlation")
    assert scaled.audit["shrinkage_intensity"] == pytest.approx(
        base.audit["shrinkage_intensity"], rel=1e-11
    )
    np.testing.assert_allclose(
        scaled.covariance, base.covariance * factor**2, rtol=1e-12, atol=1e-24
    )


@pytest.mark.parametrize(
    "values",
    [
        np.zeros((5, 1)),
        np.zeros((5, 3)),
        np.array([[0, 0.1, 0.2], [0, 0.2, 0.3], [0, 0.1, 0.4]]),
        np.tile(np.arange(6)[:, None] * 0.001, (1, 3)),
        np.random.default_rng(7).normal(0, 0.01, (3, 7)),
    ],
)
def test_single_zero_collinear_and_more_assets_than_observations(values):
    for arm in ESTIMATORS:
        result = estimate_covariance(values, estimator_id=arm)
        assert np.isfinite(result.covariance).all()
        assert np.linalg.eigvalsh(result.covariance).min() > -1e-14


@pytest.mark.parametrize("strength", [0.0, 1.0])
def test_fixed_shrinkage_endpoints_are_explicit(strength):
    values = heterogenous()
    result = estimate_covariance(values, estimator_id="fixed_diag_020", fixed_shrinkage=strength)
    sample = np.cov(values, rowvar=False, ddof=1)
    np.testing.assert_allclose(
        result.covariance, np.diag(np.diag(sample)) if strength else sample, rtol=1e-14, atol=1e-18
    )
    assert result.audit["shrinkage_intensity"] == strength


@pytest.mark.parametrize(
    "values",
    [
        np.array([0.1, 0.2]),
        np.zeros((1, 2)),
        np.array([[np.nan, 0], [0.1, 0.2]]),
        np.array([[-1.1, 0], [0.1, 0.2]]),
    ],
)
def test_bad_inputs_are_refused_without_dropping_rows(values):
    with pytest.raises(ValueError):
        estimate_covariance(values)


def test_default_selection_output_is_unchanged_and_explicit_estimator_is_audited():
    chosen = selection(3)
    original = size_portfolio_targets(chosen)
    assert "covariance_audit" not in original.to_dict()
    explicit = size_portfolio_targets(chosen, covariance_estimator="fixed_diag_020")
    np.testing.assert_allclose(
        list(explicit.target_weights.values()), list(original.target_weights.values()), rtol=1e-14
    )
    assert explicit.covariance_audit["annualization_factor"] == 365
    assert explicit.covariance_audit["annualized_once"]


def test_forecast_and_covariance_enter_optimizer_at_same_horizon_once(monkeypatch):
    import core.planning.selection_v2 as module

    chosen = selection(3)
    horizon = 7
    captured = {}

    def allocate(mu, covariance, existing, upper, *, policy, **kwargs):
        captured.update(mu=mu, covariance=covariance)
        return np.asarray(upper), {"cash_fallback": False, "status": "optimal"}

    monkeypatch.setattr(module, "allocate_cost_aware", allocate)
    size_portfolio_targets(
        chosen,
        covariance_estimator="sample",
        cost_aware_policy=CostAwareAllocationPolicy(enabled=True, horizon_days=horizon),
    )
    values = chosen.returns.reindex(columns=chosen.selected_symbols).tail(
        chosen.policy.volatility_window
    )
    np.testing.assert_allclose(captured["mu"], values.mean().to_numpy() * horizon)
    np.testing.assert_allclose(captured["covariance"], values.cov(ddof=1).to_numpy() * horizon)


def test_constant_provider_matches_old_optimizer_and_bad_context_is_refused():
    from core.planning.execution_cost_provider import FixedExecutionCostProvider

    chosen = selection(3)
    policy = CostAwareAllocationPolicy(enabled=True)
    contexts = {
        s: {
            "market_identity": "spot_assumed",
            "features": {},
            "features_available_at": chosen.as_of,
        }
        for s in chosen.selected_symbols
    }
    old = size_portfolio_targets(chosen, cost_aware_policy=policy)
    new = size_portfolio_targets(
        chosen,
        cost_aware_policy=policy,
        execution_cost_provider=FixedExecutionCostProvider(20.0),
        execution_cost_contexts=contexts,
    )
    np.testing.assert_allclose(
        list(old.target_weights.values()), list(new.target_weights.values()), rtol=1e-12, atol=1e-14
    )
    contexts[chosen.selected_symbols[0]]["decision_at"] = "2090-01-01T00:00:00Z"
    with pytest.raises(ValueError, match="decision_at"):
        size_portfolio_targets(
            chosen,
            cost_aware_policy=policy,
            execution_cost_provider=FixedExecutionCostProvider(20.0),
            execution_cost_contexts=contexts,
        )


def test_provider_exception_preserves_only_existing_native_envelope():
    class BrokenProvider:
        def estimate(self, context):
            raise RuntimeError("fixture provider failure")

    chosen = selection(3)
    existing = {s: 0.1 for s in chosen.selected_symbols}
    contexts = {
        s: {
            "market_identity": "spot_assumed",
            "features": {},
            "features_available_at": chosen.as_of,
        }
        for s in chosen.selected_symbols
    }
    result = size_portfolio_targets(
        chosen,
        existing_weights=existing,
        cost_aware_policy=CostAwareAllocationPolicy(enabled=True),
        execution_cost_provider=BrokenProvider(),
        execution_cost_contexts=contexts,
    )
    assert result.cost_aware_audit["status"] == "unsupported_cost_provider_mandatory_only"
    assert all(w <= existing[s] for s, w in result.target_weights.items())


@pytest.mark.parametrize("kind", ["strict", "prospective"])
def test_strict_consumers_cannot_use_retrospective_cost_artifacts(kind):
    from core.planning.execution_cost_provider import FixedExecutionCostProvider

    chosen = selection(3)
    existing = {s: 0.1 for s in chosen.selected_symbols}
    contexts = {
        s: {
            "market_identity": "spot_assumed",
            "features": {},
            "features_available_at": chosen.as_of,
        }
        for s in chosen.selected_symbols
    }
    result = size_portfolio_targets(
        chosen,
        existing_weights=existing,
        cost_aware_policy=CostAwareAllocationPolicy(enabled=True),
        execution_cost_provider=FixedExecutionCostProvider(20.0),
        execution_cost_contexts=contexts,
        evaluation_kind=kind,
    )
    assert result.cost_aware_audit["status"] == "unsupported_cost_provider_mandatory_only"
    assert all(w <= existing[s] for s, w in result.target_weights.items())


def test_symbol_only_strict_policy_is_propagated_to_producer_and_consumer():
    from core.planning.execution_cost_provider import FixedExecutionCostProvider

    chosen = selection(3)
    contexts = {
        s: {
            "market_identity": "spot_assumed",
            "features": {},
            "features_available_at": chosen.as_of,
            "information_policy": {
                "mode": "strict",
                "knowledge": "published",
                "unknown": "exclude",
            },
        }
        for s in chosen.selected_symbols
    }
    result = size_portfolio_targets(
        chosen,
        existing_weights={s: 0.1 for s in chosen.selected_symbols},
        cost_aware_policy=CostAwareAllocationPolicy(enabled=True),
        execution_cost_provider=FixedExecutionCostProvider(20.0),
        execution_cost_contexts=contexts,
    )
    assert result.cost_aware_audit["status"] == "unsupported_cost_provider_mandatory_only"


def test_conflicting_consumer_and_symbol_cost_modes_are_refused():
    from core.planning.execution_cost_provider import FixedExecutionCostProvider

    chosen = selection(3)
    contexts = {s: {"evaluation_kind": "strict"} for s in chosen.selected_symbols}
    with pytest.raises(ValueError, match="conflicting"):
        size_portfolio_targets(
            chosen,
            cost_aware_policy=CostAwareAllocationPolicy(enabled=True),
            execution_cost_provider=FixedExecutionCostProvider(20.0),
            execution_cost_contexts=contexts,
            evaluation_kind="retrospective",
        )


def test_strict_consumer_refuses_cached_retro_prediction_even_if_provider_ignores_context():
    from core.planning.execution_cost_provider import FixedExecutionCostProvider

    chosen = selection(3)
    constant = FixedExecutionCostProvider(20.0)

    class CachedRetrospective:
        def estimate(self, context):
            return constant.estimate(
                {**context, "evaluation_kind": "retrospective", "information_policy": None}
            )

    contexts = {
        s: {
            "market_identity": "spot_assumed",
            "features": {},
            "features_available_at": chosen.as_of,
        }
        for s in chosen.selected_symbols
    }
    result = size_portfolio_targets(
        chosen,
        existing_weights={s: 0.1 for s in chosen.selected_symbols},
        cost_aware_policy=CostAwareAllocationPolicy(enabled=True),
        execution_cost_provider=CachedRetrospective(),
        execution_cost_contexts=contexts,
        evaluation_kind="strict",
    )
    assert result.cost_aware_audit["status"] == "unsupported_cost_provider_mandatory_only"
