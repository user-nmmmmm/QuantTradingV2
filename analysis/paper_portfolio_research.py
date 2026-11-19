"""Finite portfolio research using explicit covariance and real Broker accounts.

Everything is retrospective and isolated. A correct account, a risk forecast,
and independent evidence of excess return are different results.
"""

from __future__ import annotations

from dataclasses import replace
import math

import numpy as np
import pandas as pd

from core.planning.covariance_estimators import ESTIMATORS, estimate_covariance
from core.planning.cost_aware_allocation import CostAwareAllocationPolicy, allocate_cost_aware


def _complete_panel(frame):
    if (
        not isinstance(frame, pd.DataFrame)
        or not isinstance(frame.index, pd.DatetimeIndex)
        or frame.empty
        or frame.index.hasnans
        or frame.index.has_duplicates
        or not frame.index.is_monotonic_increasing
        or frame.columns.has_duplicates
        or not np.isfinite(frame.to_numpy(dtype=float)).all()
    ):
        raise ValueError("finite common ordered complete panel with unique dates/assets required")


def _validate_attribution_inputs(
    returns,
    factors,
    ids,
    factor_versions,
    min_observations,
    iterations,
    block_length,
    seed,
    definition_scope,
    strategy_cost_basis,
    factor_cost_basis,
    financing_basis,
):
    _complete_panel(factors)
    if (
        not isinstance(returns, pd.Series)
        or not returns.index.equals(factors.index)
        or not np.isfinite(returns.to_numpy(dtype=float)).all()
    ):
        raise ValueError("strategy and factors must have the exact same finite time axis")
    if (
        not ids
        or len(set(ids)) != len(ids)
        or any(not isinstance(i, str) or i not in factors for i in ids)
    ):
        raise ValueError("unique declared factor IDs must exist in the panel")
    if set(factor_versions) != set(ids) or any(
        not isinstance(v, str) or not v for v in factor_versions.values()
    ):
        raise ValueError("an explicit version is required for each factor")
    if (
        isinstance(min_observations, bool)
        or not isinstance(min_observations, int)
        or min_observations < len(ids) + 2
        or isinstance(iterations, bool)
        or not isinstance(iterations, int)
        or iterations < 0
        or isinstance(block_length, bool)
        or not isinstance(block_length, int)
        or block_length < 1
        or isinstance(seed, bool)
        or not isinstance(seed, int)
    ):
        raise ValueError("invalid sample, resampling or seed declaration")
    if definition_scope not in {"proxy", "qualified_subset", "full_market_requested"}:
        raise ValueError("unknown factor definition scope")
    for basis in (strategy_cost_basis, factor_cost_basis, financing_basis):
        if not isinstance(basis, str) or not basis:
            raise ValueError("cost and financing bases must be explicit")


def _attribution_cutoff(factors, ids, as_of, factor_available_at):
    cutoff = None
    if as_of is not None:
        cutoff = pd.Timestamp(as_of)
        cutoff = cutoff.tz_localize("UTC") if cutoff.tzinfo is None else cutoff.tz_convert("UTC")
        if (pd.to_datetime(factors.index, utc=True) > cutoff).any():
            raise ValueError("observations extend beyond attribution cutoff")
    if factor_available_at is not None:
        if (
            as_of is None
            or not isinstance(factor_available_at, pd.DataFrame)
            or not factor_available_at.index.equals(factors.index)
        ):
            raise ValueError("factor availability requires matching index and explicit as_of")
        for key in ids:
            available = pd.to_datetime(factor_available_at[key], utc=True, errors="coerce")
            if available.isna().any() or (available > cutoff).any():
                raise ValueError("factor version was unavailable at attribution cutoff")
    return cutoff


def _factor_intervals(x, y, beta, residual, ids, hac_lags, iterations, block_length, seed):
    n = len(y)
    lag = min(n - 1, int(n ** (1 / 3)) if hac_lags is None else hac_lags)
    if isinstance(lag, bool) or not isinstance(lag, int) or lag < 0:
        raise ValueError("nonnegative integer HAC lag required")
    scores = x * residual[:, None]
    meat = scores.T @ scores
    for k in range(1, lag + 1):
        term = scores[k:].T @ scores[:-k]
        meat += (1 - k / (lag + 1)) * (term + term.T)
    bread = np.linalg.inv(x.T @ x)
    parameter_covariance = bread @ meat @ bread
    standard_errors = np.sqrt(np.maximum(np.diag(parameter_covariance), 0.0))
    ci = np.column_stack(
        (beta - 1.959963984540054 * standard_errors, beta + 1.959963984540054 * standard_errors)
    )
    rng = np.random.default_rng(seed)
    alpha_samples = []
    for _ in range(iterations):
        starts = rng.integers(0, n, size=math.ceil(n / block_length))
        indices = np.concatenate([(s + np.arange(block_length)) % n for s in starts])[:n]
        if np.linalg.matrix_rank(x[indices]) == x.shape[1]:
            alpha_samples.append(float(np.linalg.lstsq(x[indices], y[indices], rcond=None)[0][0]))
    return {
        "method": "Bartlett_Newey_West_normal_approximation",
        "hac_lags": lag,
        "parameter_ci95": dict(zip(("alpha",) + ids, ci.tolist())),
        "alpha_circular_block_ci95": np.quantile(alpha_samples, [0.025, 0.975]).tolist()
        if alpha_samples
        else None,
        "requested_block_samples": iterations,
        "valid_block_samples": len(alpha_samples),
        "block_length": block_length,
        "seed": seed,
    }


def factor_attribution_general(
    returns,
    factors,
    *,
    factor_ids,
    factor_versions,
    as_of=None,
    factor_available_at=None,
    min_observations=60,
    hac_lags=None,
    iterations=500,
    block_length=20,
    seed=42,
    definition_scope="proxy",
    strategy_cost_basis="net_Broker_costs",
    factor_cost_basis="not_declared",
    financing_basis="not_declared",
):
    """OLS exposure diagnostics with Newey-West and circular time-block intervals.

    Inputs must already be selected from their as-of revision stores. When
    availability is supplied, every used fact must be visible by ``as_of``;
    unavailable observations are refused, never silently dropped/reweighted.
    """
    ids = tuple(factor_ids)
    _validate_attribution_inputs(
        returns,
        factors,
        ids,
        factor_versions,
        min_observations,
        iterations,
        block_length,
        seed,
        definition_scope,
        strategy_cost_basis,
        factor_cost_basis,
        financing_basis,
    )
    cutoff = _attribution_cutoff(factors, ids, as_of, factor_available_at)
    n = len(returns)
    base = {
        "schema": "factor-attribution/v2",
        "status": "insufficient",
        "admission_eligible": False,
        "factor_ids": list(ids),
        "factor_versions": dict(factor_versions),
        "sample_size": n,
        "factor_definition_scope": definition_scope,
        "full_market_replication": False,
        "strategy_cost_basis": strategy_cost_basis,
        "factor_cost_basis": factor_cost_basis,
        "financing_basis": financing_basis,
        "as_of": None if as_of is None else cutoff.isoformat(),
        "interpretation": "retrospective common-exposure diagnostic, not causal alpha or an executable factor account",
    }
    x = np.column_stack([np.ones(n), factors[list(ids)].to_numpy(dtype=float)])
    y = returns.to_numpy(dtype=float)
    rank = int(np.linalg.matrix_rank(x))
    condition = float(np.linalg.cond(x))
    base.update(
        rank=rank,
        columns=x.shape[1],
        condition_number=condition if math.isfinite(condition) else None,
    )
    if n < min_observations or rank != x.shape[1]:
        return {
            **base,
            "reason": "sample_size_or_rank",
            "alpha_daily": None,
            "beta": None,
            "residuals": None,
            "intervals": None,
        }
    beta = np.linalg.lstsq(x, y, rcond=None)[0]
    residual = y - x @ beta
    intervals = _factor_intervals(
        x, y, beta, residual, ids, hac_lags, iterations, block_length, seed
    )
    return {
        **base,
        "status": "diagnostic",
        "reason": None,
        "alpha_daily": float(beta[0]),
        "alpha_annual_linear": float(beta[0] * 365),
        "beta": dict(zip(ids, map(float, beta[1:]))),
        "residuals": residual.tolist(),
        "fitted": (x @ beta).tolist(),
        "timestamps": [t.isoformat() for t in factors.index],
        "intervals": intervals,
        "r_squared": None if np.var(y) == 0 else float(1 - np.var(residual) / np.var(y)),
        "limitations": [
            "normal/HAC and block intervals require time dependence and stationarity assumptions",
            "versions identify supplied inputs; no implicit historical member/market-cap verification",
            "gross factors and net strategy returns have different economic cost bases",
        ],
    }


def covariance_risk_comparison(
    returns,
    *,
    lookback=60,
    horizon=20,
    target_annual_volatility=0.2,
    max_gross=0.9,
    evaluation_start=None,
):
    """Fixed three-arm risk forecast comparison on non-overlapping later blocks.

    Individual volatility and inverse-volatility base weights are held fixed
    across arms; changes in gross scaling are reported separately. These risk
    projections are not synthetic fills or account returns.
    """
    _complete_panel(returns)
    if (
        isinstance(lookback, bool)
        or not isinstance(lookback, int)
        or lookback < 2
        or isinstance(horizon, bool)
        or not isinstance(horizon, int)
        or horizon < 2
        or not math.isfinite(target_annual_volatility)
        or target_annual_volatility <= 0
        or not 0 < max_gross <= 1
    ):
        raise ValueError("invalid lookback, horizon or risk budget")
    rows = []
    first = lookback
    if evaluation_start is not None:
        cutoff = pd.Timestamp(evaluation_start)
        if returns.index.tz is not None and cutoff.tzinfo is None:
            cutoff = cutoff.tz_localize(returns.index.tz)
        first = max(first, int(returns.index.searchsorted(cutoff)))
    for offset in range(first, len(returns) - horizon + 1, horizon):
        train = returns.iloc[offset - lookback : offset]
        future = returns.iloc[offset : offset + horizon]
        sigma = np.sqrt(np.diag(estimate_covariance(train, estimator_id="sample").covariance))
        if (sigma <= 0).any():
            raise ValueError("individual volatilities must be positive for inverse-vol comparison")
        base = (1 / sigma) / (1 / sigma).sum()
        realized_variance = float(np.var(future.to_numpy() @ base, ddof=1))
        for arm in ESTIMATORS:
            estimated = estimate_covariance(train, estimator_id=arm)
            predicted_variance = float(base @ estimated.covariance @ base)
            gross = (
                min(max_gross, target_annual_volatility / math.sqrt(365 * predicted_variance))
                if predicted_variance > 0
                else 0.0
            )
            rows.append(
                {
                    "estimator_id": arm,
                    "decision_at": future.index[0].isoformat(),
                    "training_last": train.index[-1].isoformat(),
                    "later_block_end": future.index[-1].isoformat(),
                    "predicted_daily_variance": predicted_variance,
                    "realized_daily_variance": realized_variance,
                    "absolute_variance_error": abs(predicted_variance - realized_variance),
                    "gross_scale": gross,
                    "realized_annual_volatility": math.sqrt(365 * realized_variance) * gross,
                    "relative_base_weights": base.tolist(),
                    "shrinkage_intensity": estimated.audit["shrinkage_intensity"],
                }
            )
    summaries = {}
    for arm in ESTIMATORS:
        values = [r for r in rows if r["estimator_id"] == arm]
        summaries[arm] = {
            "blocks": len(values),
            "mean_absolute_variance_error": float(
                np.mean([r["absolute_variance_error"] for r in values])
            )
            if values
            else None,
            "average_gross": float(np.mean([r["gross_scale"] for r in values])) if values else None,
        }
    return {
        "schema": "covariance-risk-comparison/v1",
        "engineering": "pass" if rows else "pending",
        "research": "insufficient",
        "candidate_ids": list(ESTIMATORS),
        "rows": rows,
        "summary": summaries,
        "independent_later_blocks": len(rows) // len(ESTIMATORS),
        "lookback": lookback,
        "horizon": horizon,
        "portfolio_return": None,
        "admission_eligible": False,
        "limitations": [
            "inverse-vol relative weights are unchanged by targets that preserve diagonal variance",
            "descriptive retrospective error comparison; no preregistered inferential improvement decision",
            "later blocks do not increase independent assets; no account return is inferred from risk forecasts",
        ],
    }


def _replay_spec(name, lookback):
    from analysis.paper_portfolio import PaperSpec

    return PaperSpec(
        name=name,
        signal="equal_weight",
        lookback=lookback,
        vol_window=lookback,
        warmup_bars=lookback,
        rebalance_every=1,
        rebalance="immediate",
        max_gross=0.9,
    )


def run_covariance_portfolio_study(
    frames,
    *,
    lookback=60,
    target_annual_volatility=0.2,
    initial_capital=10000.0,
    start=None,
    end=None,
    on_arm=None,
):
    """Replay three fixed covariance arms with the existing Broker and fee ledger."""
    from analysis.paper_portfolio import _prepare_frames, run_paper_replay

    data = _prepare_frames(frames)
    close = pd.DataFrame({s: f.close for s, f in data.items()})
    returns = close.pct_change(fill_method=None)
    results = {}
    for arm in ESTIMATORS:
        if on_arm is not None:
            on_arm(arm, "started", None)
        try:
            # Target construction is computation by this candidate too; it
            # belongs inside the registered attempt, not before its start.
            target = pd.DataFrame(0.0, index=close.index, columns=close.columns)
            for i in range(lookback, len(close)):
                history = returns.iloc[i - lookback + 1 : i + 1]
                estimated = estimate_covariance(history, estimator_id=arm)
                sigma = np.sqrt(np.diag(estimated.covariance))
                if (sigma <= 0).any():
                    continue
                relative = (1 / sigma) / (1 / sigma).sum()
                volatility = math.sqrt(float(relative @ estimated.covariance @ relative) * 365)
                if volatility > 0:
                    target.iloc[i] = relative * min(0.9, target_annual_volatility / volatility)
            results[arm] = run_paper_replay(
                data,
                _replay_spec(arm, lookback),
                initial_capital=initial_capital,
                target_overrides=target,
                start=start,
                end=end,
            )
        except BaseException as error:
            if on_arm is not None:
                on_arm(
                    arm, "interrupted" if isinstance(error, KeyboardInterrupt) else "failed", error
                )
            raise
        if on_arm is not None:
            on_arm(arm, "completed", results[arm])
    return {
        "schema": "covariance-account-study/v1",
        "candidate_ids": list(ESTIMATORS),
        "arms": results,
        "engineering": "pass" if all(r["accounting"]["ok"] for r in results.values()) else "fail",
        "research": "insufficient",
        "admission_eligible": False,
        "limitations": [
            "retrospective fixed asset panel; historical membership is not proven",
            "bar liquidity and assumed fee/slippage model are not real venue calibration",
            "risk/gross exposures differ; raw return ordering is not a risk-matched superiority claim",
        ],
    }


def allocation_policy_ablation(
    mu, covariance, existing, upper, *, policy, cost_predictions=None, decision_at=None
):
    """Fixed two-stage target experiment: provider then rebalance; no new grid."""
    if not isinstance(policy, CostAwareAllocationPolicy) or not policy.enabled:
        raise ValueError("enabled fixed cost-aware policy required")
    rows = {}
    arms = [
        ("native_constant_immediate", replace(policy, rebalance="immediate"), None),
        ("provider_immediate", replace(policy, rebalance="immediate"), cost_predictions),
        ("provider_partial", replace(policy, rebalance="partial"), cost_predictions),
        ("provider_band", replace(policy, rebalance="band"), cost_predictions),
    ]
    for name, arm_policy, predictions in arms:
        kwargs = (
            {}
            if predictions is None
            else {"cost_predictions": predictions, "decision_at": decision_at}
        )
        weights, audit = allocate_cost_aware(
            mu, covariance, existing, upper, policy=arm_policy, **kwargs
        )
        rows[name] = {"target_weights": weights.tolist(), "audit": audit}
    return {
        "schema": "allocation-policy-ablation/v1",
        "candidate_ids": list(rows),
        "arms": rows,
        "engineering": "pass"
        if all(r["audit"]["status"] == "optimal" for r in rows.values())
        else "fail",
        "research": "not_evaluated",
        "actual_costs_posted": 0,
        "limitations": [
            "targets are not fills; only a Broker may post actual fees",
            "mandatory risk reductions are outside discretionary turnover",
            "changed exposure is not a return improvement; no multi-period trigger is inferred",
        ],
    }


def _allocation_target_provider(lookback, provider, policy, use_provider):
    """Build one fixed arm callback from its own policy and provider declaration."""

    def target_provider(context, policy=policy, use_provider=use_provider):
        returns = context["returns"].tail(lookback)
        if len(returns) != lookback or not np.isfinite(returns.to_numpy()).all():
            return np.zeros(len(context["symbols"])), {"status": "warmup"}
        covariance = estimate_covariance(returns, estimator_id="fixed_diag_020").covariance
        kwargs = {}
        if use_provider:
            predictions = [
                provider.estimate(
                    {
                        "decision_at": context["as_of"],
                        "symbol": s,
                        "side": side,
                        "horizon_days": 7,
                        "market_identity": "retrospective_spot_assumption",
                        "features": {},
                        "features_available_at": context["as_of"],
                        "anchor": "decision_mid",
                    }
                )
                for s in context["symbols"]
                for side in ("buy", "sell")
            ]
            kwargs = {"cost_predictions": predictions, "decision_at": context["as_of"]}
        weights, audit = allocate_cost_aware(
            returns.mean().to_numpy() * 7,
            covariance * 7,
            context["existing_weights"],
            np.full(len(context["symbols"]), 0.9 / len(context["symbols"])),
            policy=policy,
            **kwargs,
        )
        if audit.get("cash_fallback") or str(audit.get("status", "")).startswith("unsupported"):
            raise ValueError(
                "registered allocation arm has unsupported cost or solver failure: "
                + str(audit.get("status"))
            )
        envelope = audit.get("risk_envelope_weights", [])
        audit["mandatory_reduction_weights"] = {
            symbol: float(envelope[i])
            for i, symbol in enumerate(context["symbols"])
            if context["existing_weights"][i] > envelope[i] + 1e-12
        }
        return weights, audit

    return target_provider


def run_allocation_portfolio_study(
    frames,
    *,
    lookback=60,
    initial_capital=10000.0,
    provider=None,
    start=None,
    end=None,
    on_arm=None,
):
    """Four fixed controls with the same forecasts, risk and actual Broker costs."""
    from analysis.paper_portfolio import _prepare_frames, run_paper_replay
    from core.planning.execution_cost_provider import FixedExecutionCostProvider

    data = _prepare_frames(frames)
    provider = provider or FixedExecutionCostProvider(20.0, horizon_days=7)
    results = {}
    for name, mode, use_provider in (
        ("native_constant_immediate", "immediate", False),
        ("provider_immediate", "immediate", True),
        ("provider_partial", "partial", True),
        ("provider_band", "band", True),
    ):
        policy = CostAwareAllocationPolicy(
            enabled=True,
            horizon_days=7,
            trading_cost_bps=20.0,
            rebalance=mode,
            max_turnover_weight=0.25,
            max_gross_weight=0.9,
        )

        target_provider = _allocation_target_provider(lookback, provider, policy, use_provider)
        if on_arm is not None:
            on_arm(name, "started", None)
        try:
            results[name] = run_paper_replay(
                data,
                _replay_spec(name, lookback),
                initial_capital=initial_capital,
                target_provider=target_provider,
                start=start,
                end=end,
            )
        except BaseException as error:
            if on_arm is not None:
                on_arm(
                    name, "interrupted" if isinstance(error, KeyboardInterrupt) else "failed", error
                )
            raise
        if on_arm is not None:
            on_arm(name, "completed", results[name])
    return {
        "schema": "allocation-account-study/v1",
        "candidate_ids": list(results),
        "arms": results,
        "engineering": "pass" if all(r["accounting"]["ok"] for r in results.values()) else "fail",
        "research": "insufficient",
        "admission_eligible": False,
        "conditional_multi_period": {
            "task": "PR25-15",
            "status": "not_triggered",
            "reason": "No validated real execution-cost domain or demonstrated single-period decay/turnover bottleneck",
        },
        "limitations": [
            "fixed scalar provider is an assumption, not real venue calibration",
            "actual Broker cost parameters are identical in all four arms; provider estimates are not posted to cash",
            "gross and realized risks differ; descriptive net returns do not prove superiority",
        ],
    }
