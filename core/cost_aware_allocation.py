"""Explicit research-only cost-aware allocation; targets never imply fills.

The legacy one-period solver is shared with paper experiments unchanged. The
bounded allocator below also solves a hard discretionary L1 turnover budget.
All inputs are completed-history estimates supplied by the native selector.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Mapping

import numpy as np


def cost_aware_target(mu, covariance, existing, *, gross_cap, trading_cost,
                      holding_cost=0., risk_aversion=10., impact_penalty=.01):
    """Convex one-period objective solved by coordinate descent and a dual cap.

    Returns (weights, status). Bad forecasts or solver failures fall to a cash
    target, which still requires real, capacity-limited sells in the replay.
    """
    mu, covariance, existing = np.asarray(mu, float), np.asarray(covariance, float), np.asarray(existing, float)
    n = len(mu)
    if (covariance.shape != (n, n) or existing.shape != (n,) or
            not np.isfinite(np.r_[mu, covariance.ravel(), existing]).all()):
        return np.zeros(n), "invalid_input_cash_fallback"
    covariance = (covariance+covariance.T)/2
    covariance += np.eye(n)*max(1e-10, -float(np.linalg.eigvalsh(covariance).min())+1e-10)
    cost = np.broadcast_to(np.asarray(trading_cost, float), (n,))
    carry = np.broadcast_to(np.asarray(holding_cost, float), (n,))
    if not np.isfinite(np.r_[cost, carry]).all() or (cost < 0).any() or (carry < 0).any():
        return np.zeros(n), "invalid_cost_cash_fallback"
    if not 0 <= gross_cap <= 1 or risk_aversion <= 0 or impact_penalty < 0:
        return np.zeros(n), "invalid_constraint_cash_fallback"
    if gross_cap == 0:
        return np.zeros(n), "optimal"
    # Minimise .5*w'Q*w - b'w + cost*abs(w-existing), w>=0,
    # sum(w)<=gross_cap. The multiplier has a monotone mass response.
    quadratic = risk_aversion*covariance+2*impact_penalty*np.eye(n)
    linear = mu-carry+2*impact_penalty*existing
    def coordinate(multiplier):
        weights = np.zeros(n)
        for _ in range(1000):
            previous = weights.copy()
            for j in range(n):
                diagonal = quadratic[j, j]
                optimum = (linear[j]-multiplier-quadratic[j]@weights+diagonal*weights[j])/diagonal
                centered = optimum-existing[j]
                proximal = existing[j]+np.sign(centered)*max(abs(centered)-cost[j]/diagonal, 0.)
                weights[j] = max(float(proximal), 0.)
            if np.max(np.abs(weights-previous)) < 1e-10:
                return weights
        raise RuntimeError("coordinate descent did not converge")
    try:
        weights = coordinate(0.)
        if weights.sum() > gross_cap:
            lower, upper = 0., max(1., float(np.max(np.abs(linear)))+float(cost.max()))
            for _ in range(60):
                middle = (lower+upper)/2
                candidate = coordinate(middle)
                if candidate.sum() > gross_cap:
                    lower = middle
                else:
                    upper, weights = middle, candidate
            weights = coordinate(upper)
    except (ValueError, RuntimeError, FloatingPointError):
        return np.zeros(n), "solver_error_cash_fallback"
    if not np.isfinite(weights).all() or weights.sum() > gross_cap+1e-7:
        return np.zeros(n), "solver_failure_cash_fallback"
    return weights, "optimal"


@dataclass(frozen=True)
class CostAwareAllocationPolicy:
    """Research assumptions, expressed in decimal equity weights and bps.

    Holding cost is an objective estimate, not an invented account debit. The
    Broker remains the authority for actual fees, carry, fills and reservations.
    ``max_gross_weight`` is deliberately cash-funded (at most one).
    """
    enabled: bool = False
    horizon_days: int = 7
    trading_cost_bps: float = 20.
    holding_cost_bps_per_day: float = 0.
    turnover_penalty_bps: float = 0.
    risk_aversion: float = 10.
    impact_penalty: float = .01
    max_turnover_weight: float = .25
    max_gross_weight: float = 1.
    rebalance: str = "immediate"
    partial_fraction: float = .5
    no_trade_band: float = .02

    def __post_init__(self):
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be boolean")
        if isinstance(self.horizon_days, bool) or not isinstance(self.horizon_days, int) or self.horizon_days < 1:
            raise ValueError("horizon_days must be a positive integer")
        for name in ("trading_cost_bps", "holding_cost_bps_per_day", "turnover_penalty_bps",
                     "impact_penalty", "max_turnover_weight", "no_trade_band"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        for name in ("risk_aversion", "max_gross_weight", "partial_fraction"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.max_gross_weight > 1 or self.partial_fraction > 1:
            raise ValueError("gross weight and partial fraction cannot exceed one")
        if self.rebalance not in {"immediate", "partial", "band"}:
            raise ValueError("rebalance must be immediate, partial or band")

    @classmethod
    def from_mapping(cls, value: Mapping | None):
        return cls(**dict(value or {}))


def allocate_cost_aware(mu, covariance, existing, upper_weights, *, policy: CostAwareAllocationPolicy):
    """Solve inside a pre-approved risk envelope with an internal turnover cap.

    Objective: .5*risk_aversion*w'Cov*w - mu'w + holding*w
    + (trading_cost + turnover_penalty)*|w-existing|
    + impact_penalty*||w-existing||^2. Forecast/covariance are for the declared
    horizon. Upper weights already satisfy native per-symbol/cluster/stop caps.

    Holdings above that envelope must reduce before discretionary adjustment.
    Only turnover around ``min(existing, upper)`` consumes this optimization's
    budget. These mandatory reductions are separately audited and still need
    real Broker executions. Failure requests cash, never fabricates liquidation.
    """
    mu, covariance = np.asarray(mu, float), np.asarray(covariance, float)
    existing, upper = np.asarray(existing, float), np.asarray(upper_weights, float).copy()
    n = len(mu)
    audit = {"enabled": True, "policy": asdict(policy), "research_only": True,
             "forecast_method": "completed_trailing_arithmetic_mean_times_horizon",
             "cost_source": "declared_research_assumptions",
             "holding_cost_is_objective_estimate_only": True,
             "turnover_constraint": "inside_convex_optimization_after_mandatory_envelope_reduction"}

    def fail(status):
        return np.zeros(n), {**audit, "status": status, "cash_fallback": True}

    if (mu.shape != (n,) or existing.shape != (n,) or upper.shape != (n,) or
            covariance.shape != (n, n) or
            not np.isfinite(np.r_[mu, covariance.ravel(), existing, upper]).all() or
            (existing < 0).any() or (upper < 0).any()):
        return fail("invalid_input_cash_fallback")
    if not n:
        return np.zeros(0), {**audit, "status": "empty_cash_target", "cash_fallback": False,
                             "discretionary_turnover_weight": 0., "mandatory_turnover_weight": 0.}
    if upper.sum() > policy.max_gross_weight:
        upper *= policy.max_gross_weight / upper.sum()
    anchor = np.minimum(existing, upper)
    covariance = (covariance + covariance.T)/2
    try:
        covariance += np.eye(n)*max(1e-10, -float(np.linalg.eigvalsh(covariance).min())+1e-10)
        quadratic = policy.risk_aversion*covariance + 2*policy.impact_penalty*np.eye(n)
        carry = policy.holding_cost_bps_per_day*policy.horizon_days/10000.
        cost = (policy.trading_cost_bps + policy.turnover_penalty_bps)/10000.
        linear = mu-carry+2*policy.impact_penalty*existing

        def coordinate(multiplier):
            weights = anchor.copy()
            for _ in range(1500):
                previous = weights.copy()
                for j in range(n):
                    diagonal = quadratic[j, j]
                    optimum = (linear[j]-quadratic[j]@weights+diagonal*weights[j])/diagonal
                    centered = optimum-anchor[j]
                    proximal = anchor[j]+np.sign(centered)*max(abs(centered)-(cost+multiplier)/diagonal, 0.)
                    weights[j] = np.clip(proximal, 0., upper[j])
                if np.max(np.abs(weights-previous)) < 1e-10:
                    return weights
            raise RuntimeError("bounded coordinate descent did not converge")

        if policy.max_turnover_weight == 0:
            optimized = anchor.copy()
        else:
            optimized = coordinate(0.)
            if np.abs(optimized-anchor).sum() > policy.max_turnover_weight:
                low, high = 0., max(1., float(np.max(np.abs(linear)))+cost)
                # For a sufficiently large L1 multiplier anchor is optimal.
                while np.abs(coordinate(high)-anchor).sum() > policy.max_turnover_weight:
                    high *= 2
                    if high > 1e12:
                        raise RuntimeError("turnover dual did not bracket")
                for _ in range(55):
                    middle = (low+high)/2
                    candidate = coordinate(middle)
                    if np.abs(candidate-anchor).sum() > policy.max_turnover_weight:
                        low = middle
                    else:
                        high, optimized = middle, candidate
                optimized = coordinate(high)
        weights = optimized.copy()
        if policy.rebalance == "partial":
            weights = anchor + policy.partial_fraction*(weights-anchor)
        elif policy.rebalance == "band":
            weights = np.where(np.abs(weights-anchor) <= policy.no_trade_band, anchor, weights)
    except (ValueError, RuntimeError, FloatingPointError, np.linalg.LinAlgError):
        return fail("solver_error_cash_fallback")
    turnover = float(np.abs(weights-anchor).sum())
    if (not np.isfinite(weights).all() or (weights < -1e-10).any() or
            (weights > upper+1e-8).any() or turnover > policy.max_turnover_weight+1e-7):
        return fail("solver_failure_cash_fallback")
    audit.update(status="optimal", cash_fallback=False,
                 risk_envelope_weights=upper.tolist(), anchor_weights=anchor.tolist(),
                 optimized_weights=optimized.tolist(), final_weights=weights.tolist(),
                 mandatory_turnover_weight=float(np.sum(existing-anchor)),
                 discretionary_turnover_weight=turnover,
                 optimized_discretionary_turnover_weight=float(np.abs(optimized-anchor).sum()),
                 estimated_transaction_cost_weight=float(cost*np.abs(weights-existing).sum()),
                 estimated_holding_cost_weight=float(carry*weights.sum()))
    return weights, audit
