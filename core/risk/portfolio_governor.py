"""Correlation-aware portfolio risk budgets (SR3-2).

STR-P1-04: a 30% per-symbol cap and 3x gross leverage look diversified on
paper, but fifteen crypto majors in a 2021-05 style drawdown are one position
with fifteen tickers. Per-trade risk of 2% is only additive across *independent*
bets; across a correlated cluster it compounds.

This module adds the missing budgets:

* ``max_cluster_exposure_pct`` - gross notional of one correlation cluster
  against equity;
* ``max_crypto_beta_exposure`` - gross notional of everything that shares the
  crypto beta factor against equity;
* ``max_same_session_entry_risk`` - initial risk opened by one session's
  entries against equity (the batch that a single gap can take out together);
* ``max_correlated_stop_risk`` - open initial risk inside one cluster, i.e.
  what a correlated move through every stop in that cluster would cost.

The first two are notional caps and live in the risk manager's single
``_entry_notional_caps`` source of truth, so the clamp and the gate cannot
drift apart. The last two are risk caps and are enforced by
:class:`PortfolioRiskGovernor` at allocation time, where every same-timestamp
candidate is visible at once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple

DEFAULT_CLUSTER = "crypto_beta"


def base_asset(symbol: str) -> str:
    """``BTC/USDT`` / ``BTC-USDT`` -> ``BTC``."""
    text = str(symbol).upper()
    for separator in ("/", "-", "_"):
        if separator in text:
            return text.split(separator, 1)[0]
    return text


@dataclass(frozen=True)
class CorrelationClusterPolicy:
    """Which symbols move together, and how much of that is allowed."""

    #: base asset -> cluster name. Anything unmapped falls into
    #: ``default_cluster``: an unknown coin is assumed correlated, never
    #: assumed independent.
    clusters: Mapping[str, str] = field(default_factory=dict)
    default_cluster: str = DEFAULT_CLUSTER
    #: Gross notional of one cluster / equity. ``None`` disables the cap.
    max_cluster_exposure_pct: Optional[float] = None
    #: Gross notional of every crypto-correlated position / equity.
    max_crypto_beta_exposure: Optional[float] = None
    #: Initial risk opened by one session's entries / equity.
    max_same_session_entry_risk: Optional[float] = None
    #: Open initial risk inside one cluster / equity.
    max_correlated_stop_risk: Optional[float] = None
    enabled: bool = True

    def __post_init__(self) -> None:
        for name in (
            "max_cluster_exposure_pct", "max_crypto_beta_exposure",
            "max_same_session_entry_risk", "max_correlated_stop_risk",
        ):
            value = getattr(self, name)
            if value is not None and float(value) <= 0:
                raise ValueError(f"{name} must be positive or None")

    @classmethod
    def from_mapping(
        cls, mapping: Optional[Mapping[str, Any]],
    ) -> "CorrelationClusterPolicy":
        data = dict(mapping or {})
        clusters = data.pop("clusters", None) or {}
        default_cluster = str(clusters.pop("default", DEFAULT_CLUSTER)) if isinstance(
            clusters, dict
        ) else DEFAULT_CLUSTER
        kwargs = {
            key: value for key, value in data.items()
            if key in cls.__dataclass_fields__
        }
        kwargs["clusters"] = {
            str(key).upper(): str(value) for key, value in dict(clusters).items()
        }
        kwargs["default_cluster"] = default_cluster
        return cls(**kwargs)

    def cluster_for(self, symbol: str) -> str:
        return self.clusters.get(base_asset(symbol), self.default_cluster)

    @property
    def has_notional_caps(self) -> bool:
        return self.enabled and (
            self.max_cluster_exposure_pct is not None
            or self.max_crypto_beta_exposure is not None
        )

    @property
    def has_risk_caps(self) -> bool:
        return self.enabled and (
            self.max_same_session_entry_risk is not None
            or self.max_correlated_stop_risk is not None
        )


def exposure_by_cluster(
    policy: CorrelationClusterPolicy,
    portfolio: Any,
    current_prices: Optional[Mapping[str, float]],
    reserved_by_symbol: Optional[Mapping[str, float]] = None,
) -> Dict[str, float]:
    """Gross notional per cluster, including notional already reserved."""
    prices = dict(current_prices or {})
    reserved = dict(reserved_by_symbol or {})
    totals: Dict[str, float] = {}
    for symbol, position in portfolio.positions.items():
        qty = abs(float(position.get("qty", 0.0)))
        if qty == 0:
            continue
        price = float(prices.get(symbol, position.get("avg_price", 0.0)))
        cluster = policy.cluster_for(symbol)
        totals[cluster] = totals.get(cluster, 0.0) + qty * price
    for symbol, notional in reserved.items():
        value = max(float(notional), 0.0)
        if value == 0:
            continue
        cluster = policy.cluster_for(symbol)
        totals[cluster] = totals.get(cluster, 0.0) + value
    return totals


def open_risk_by_cluster(
    policy: CorrelationClusterPolicy, portfolio: Any,
) -> Dict[str, float]:
    """Sum of open lots' initial risk per cluster.

    This is what a correlated move that takes out every stop in the cluster
    costs - the number that a per-trade 2% budget silently multiplies.
    """
    totals: Dict[str, float] = {}
    for symbol, lot_book in getattr(portfolio, "lot_books", {}).items():
        cluster = policy.cluster_for(symbol)
        for lot in lot_book.open_lots:
            risk = lot.initial_risk
            if risk is None or lot.qty_original in (0, None):
                continue
            share = float(risk) * (float(lot.qty_open) / float(lot.qty_original))
            totals[cluster] = totals.get(cluster, 0.0) + max(share, 0.0)
    return totals


@dataclass(frozen=True)
class RiskBudgetDecision:
    """What the correlated-risk budget allows for one candidate."""

    allowed: bool
    scale: float
    reason: str
    cluster: str
    planned_risk: float
    allowed_risk: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "allowed": self.allowed,
            "scale": self.scale,
            "reason": self.reason,
            "cluster": self.cluster,
            "planned_risk": self.planned_risk,
            "allowed_risk": self.allowed_risk,
        }


class PortfolioRiskGovernor:
    """Enforces the session-level and cluster-level *risk* budgets.

    One instance lives for the whole run; ``begin_session`` resets the
    per-session accumulator when the timestamp changes, so a batch of entries
    at the same bar competes for one shared budget instead of each one
    independently claiming the full per-trade risk.
    """

    def __init__(self, policy: Optional[CorrelationClusterPolicy] = None) -> None:
        self.policy = policy or CorrelationClusterPolicy(enabled=False)
        self._session: Any = None
        self._session_risk = 0.0
        self.audit: list[Dict[str, Any]] = []

    def begin_session(self, session: Any) -> None:
        if session != self._session:
            self._session = session
            self._session_risk = 0.0

    def evaluate(
        self, *, symbol: str, planned_risk: float, equity: float,
        portfolio: Any,
    ) -> RiskBudgetDecision:
        """Scale or reject one candidate against the correlated-risk budgets."""
        cluster = self.policy.cluster_for(symbol)
        if not self.policy.has_risk_caps or planned_risk <= 0 or equity <= 0:
            return RiskBudgetDecision(
                True, 1.0, "no_risk_cap", cluster, planned_risk, planned_risk
            )
        headrooms: list[Tuple[str, float]] = []
        if self.policy.max_same_session_entry_risk is not None:
            headrooms.append((
                "same_session_entry_risk",
                equity * float(self.policy.max_same_session_entry_risk)
                - self._session_risk,
            ))
        if self.policy.max_correlated_stop_risk is not None:
            open_risk = open_risk_by_cluster(self.policy, portfolio).get(cluster, 0.0)
            headrooms.append((
                "correlated_stop_risk",
                equity * float(self.policy.max_correlated_stop_risk) - open_risk,
            ))
        binding, headroom = min(headrooms, key=lambda item: item[1])
        if headroom <= 0:
            return RiskBudgetDecision(
                False, 0.0, f"{binding}_exhausted", cluster, planned_risk, 0.0
            )
        if planned_risk <= headroom:
            return RiskBudgetDecision(
                True, 1.0, "within_budget", cluster, planned_risk, planned_risk
            )
        return RiskBudgetDecision(
            True, headroom / planned_risk, f"{binding}_scaled",
            cluster, planned_risk, headroom,
        )

    def commit(self, decision: RiskBudgetDecision, *, symbol: str) -> None:
        """Record risk that was actually opened against the session budget."""
        if decision.allowed and decision.allowed_risk > 0:
            self._session_risk += decision.allowed_risk
        self.audit.append({
            "session": str(self._session),
            "symbol": symbol,
            "session_risk_after": self._session_risk,
            **decision.to_dict(),
        })
