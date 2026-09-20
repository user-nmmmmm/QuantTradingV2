"""Opt-in, pure target sizing and monotone tranche decisions (SYS-13).

No orders, account balances, reservations or health state are written here.
The caller supplies reconciled position/order facts and the existing risk
manager's remaining budgets, persists the frozen checkpoint before execution,
then uses the normal approval/reservation/execution path. Derivatives require
their own contract-aware research and are deliberately rejected in version 1.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
import hashlib
import json
import math
from statistics import stdev
from typing import Any, Mapping, Sequence

from core.domain import OrderStatus
from core.entry_risk import resolve_approved_risk
from core.exchange.metadata import MarketSpecification
from core.risk.portfolio_governor import CorrelationClusterPolicy
from core.universe import normalize_symbol


def _number(value: Any, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric")
    try:
        result = Decimal(str(value))
    except (ValueError, ArithmeticError) as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not result.is_finite() or result < 0 or (positive and result == 0):
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'nonnegative'}")
    return result


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class VolatilityPolicy:
    enabled: bool = False
    target_annual_volatility: float = 0.15
    periods_per_year: float | None = None
    window: int = 20
    minimum_samples: int = 20
    maximum_multiplier: float = 1.0
    maximum_annual_volatility: float = 10.0
    maximum_age_seconds: float = 172800.0
    rebalance_seconds: float = 86400.0
    observation_interval_seconds: float = 86400.0
    annualization_days: float = 365.0

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be boolean")
        for field in ("target_annual_volatility", "maximum_multiplier",
                      "maximum_annual_volatility", "maximum_age_seconds", "rebalance_seconds",
                      "observation_interval_seconds", "annualization_days"):
            number = float(_number(getattr(self, field), field, positive=True))
            if not math.isfinite(number):
                raise ValueError(f"{field} exceeds representable range")
            object.__setattr__(self, field, number)
        expected_periods = self.annualization_days * 86400 / self.observation_interval_seconds
        if self.periods_per_year is not None:
            supplied = float(_number(self.periods_per_year, "periods_per_year", positive=True))
            if not math.isclose(supplied, expected_periods, rel_tol=1e-12):
                raise ValueError("periods_per_year contradicts observation interval and year length")
        if not math.isfinite(expected_periods) or expected_periods <= 0:
            raise ValueError("annualization periods must be finite and positive")
        object.__setattr__(self, "periods_per_year", expected_periods)
        if (type(self.window) is not int or type(self.minimum_samples) is not int
                or not 2 <= self.minimum_samples <= self.window):
            raise ValueError("2 <= minimum_samples <= window is required")

    def rebalance_due(self, as_of: datetime, previous: datetime | None) -> bool:
        _aware(as_of)
        if previous is None:
            return True
        if _aware(previous) > as_of:
            raise ValueError("previous rebalance cannot be in the future")
        return as_of - previous >= timedelta(seconds=self.rebalance_seconds)


@dataclass(frozen=True)
class VolatilityTarget:
    weight: float
    multiplier: float
    annual_volatility: float | None
    sample_count: int
    status: str
    reason: str
    last_observation: str | None = None


def volatility_target_weight(
    base_weight: float, returns: Sequence[tuple[datetime, float]], *,
    as_of: datetime, policy: VolatilityPolicy = VolatilityPolicy(),
) -> VolatilityTarget:
    """Use completed, strictly earlier simple returns; invalid data buys nothing.

    Returns must describe the predeclared equally spaced return frequency.
    Missing observations never become zero returns. Duplicate past timestamps,
    stale data, nonfinite data and zero/divergent variance fail conservatively.
    """
    base = float(_number(base_weight, "base_weight"))
    _aware(as_of)
    if not policy.enabled:
        return VolatilityTarget(base, 1.0, None, 0, "disabled", "fixed_weight_unchanged")
    history = sorted(((_aware(moment), value) for moment, value in returns
                      if _aware(moment) < as_of), key=lambda item: item[0])
    if len({moment for moment, _ in history}) != len(history):
        return VolatilityTarget(0, 0, None, 0, "blocked", "duplicate_observation")
    samples = history[-policy.window:]
    last = samples[-1][0].isoformat() if samples else None
    def blocked(reason: str) -> VolatilityTarget:
        return VolatilityTarget(0, 0, None, len(samples), "blocked", reason, last)
    if len(samples) < policy.minimum_samples:
        return blocked("insufficient_history")
    if as_of - samples[-1][0] > timedelta(seconds=policy.maximum_age_seconds):
        return blocked("stale_history")
    interval = timedelta(seconds=policy.observation_interval_seconds)
    if any(current[0] - previous[0] != interval for previous, current in zip(samples, samples[1:])):
        return blocked("irregular_history")
    values = []
    for _, value in samples:
        if isinstance(value, bool):
            return blocked("invalid_return")
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return blocked("invalid_return")
        if not math.isfinite(number) or number < -1:
            return blocked("invalid_return")
        values.append(number)
    try:
        volatility = stdev(values) * math.sqrt(policy.periods_per_year)
    except (ArithmeticError, ValueError):
        return blocked("invalid_volatility")
    if not math.isfinite(volatility) or volatility <= 0 or volatility > policy.maximum_annual_volatility:
        return blocked("invalid_volatility")
    multiplier = min(policy.maximum_multiplier, policy.target_annual_volatility / volatility)
    return VolatilityTarget(base * multiplier, multiplier, volatility, len(samples),
                            "ok", "trailing_sample_volatility", last)


def constrain_target_weights(
    weights: Mapping[str, float], *, max_gross_weight: float, max_symbol_weight: float,
    cluster_policy: CorrelationClusterPolicy,
) -> dict[str, float]:
    """Proportional caps; unknown assets share the existing default risk cluster.

    These are gross, nonnegative exposures. No covariance diversification credit
    is claimed. Existing positions and pending reservations still have to pass
    the authoritative budget checks before any returned target is executed.
    """
    gross_cap = _number(max_gross_weight, "max_gross_weight")
    symbol_cap = _number(max_symbol_weight, "max_symbol_weight")
    normalized = {}
    for symbol in weights:
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("nonempty symbols are required")
        canonical = normalize_symbol(symbol)
        if canonical in normalized.values():
            raise ValueError(f"duplicate normalized target symbol: {canonical}")
        normalized[symbol] = canonical
    result = {symbol: min(_number(weight, "weight"), symbol_cap)
              for symbol, weight in sorted(weights.items())}
    if cluster_policy.enabled and cluster_policy.max_cluster_exposure_pct is not None:
        clusters: dict[str, list[str]] = {}
        for symbol in result:
            clusters.setdefault(cluster_policy.cluster_for(normalized[symbol]), []).append(symbol)
        cap = _number(cluster_policy.max_cluster_exposure_pct, "cluster_cap")
        for symbols in clusters.values():
            total = sum((result[symbol] for symbol in symbols), Decimal(0))
            scale = min(Decimal(1), cap / total) if total else Decimal(1)
            for symbol in symbols:
                result[symbol] *= scale
    if cluster_policy.enabled and cluster_policy.max_crypto_beta_exposure is not None:
        gross_cap = min(gross_cap, _number(cluster_policy.max_crypto_beta_exposure, "beta_cap"))
    total = sum(result.values(), Decimal(0))
    scale = min(Decimal(1), gross_cap / total) if total else Decimal(1)
    return {symbol: float(value * scale) for symbol, value in result.items()}


@dataclass(frozen=True)
class PositionIdentity:
    account: str
    symbol: str
    side: str
    position_id: str

    def __post_init__(self) -> None:
        if any(not isinstance(item, str) or not item for item in
               (self.account, self.symbol, self.position_id)):
            raise ValueError("account, symbol and position_id are required")
        if self.side not in {"long", "short"}:
            raise ValueError("side must be long or short")


@dataclass(frozen=True)
class FrozenTarget:
    plan_id: str
    identity: PositionIdentity
    original_qty: Decimal
    target_qty: Decimal
    reference_price: Decimal
    initial_stop: Decimal
    approved_risk_amount: Decimal
    maximum_tranche_qty: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.plan_id, str) or not self.plan_id:
            raise ValueError("plan_id is required")
        if not isinstance(self.identity, PositionIdentity):
            raise ValueError("position identity is required")
        for name in ("original_qty", "target_qty", "approved_risk_amount"):
            object.__setattr__(self, name, _number(getattr(self, name), name))
        for name in ("reference_price", "initial_stop", "maximum_tranche_qty"):
            object.__setattr__(self, name, _number(getattr(self, name), name, positive=True))
        if self.target_qty > self.original_qty:
            stop_valid = (self.initial_stop < self.reference_price if self.identity.side == "long"
                          else self.initial_stop > self.reference_price)
            if not stop_valid:
                raise ValueError("entry stop must protect the declared side")
            resolve_approved_risk({"approved_risk_amount": self.approved_risk_amount})
            if self.entry_capacity * self.unit_risk > self.approved_risk_amount:
                raise ValueError("target exceeds immutable approved risk")

    @property
    def unit_risk(self) -> Decimal:
        return abs(self.reference_price - self.initial_stop)

    @property
    def entry_capacity(self) -> Decimal:
        return max(Decimal(0), self.target_qty - self.original_qty)

    def checkpoint(self) -> dict[str, Any]:
        payload = {name: str(getattr(self, name)) for name in (
            "original_qty", "target_qty", "reference_price", "initial_stop",
            "approved_risk_amount", "maximum_tranche_qty")}
        payload.update(plan_id=self.plan_id, identity=asdict(self.identity), version=1)
        return {"payload": payload, "sha256": _digest(payload)}

    @classmethod
    def restore(cls, checkpoint: Mapping[str, Any]) -> FrozenTarget:
        payload = dict(checkpoint["payload"])
        if checkpoint.get("sha256") != _digest(payload):
            raise ValueError("target checkpoint digest mismatch")
        if payload.pop("version") != 1:
            raise ValueError("unsupported target checkpoint version")
        payload["identity"] = PositionIdentity(**payload["identity"])
        return cls(**payload)


def freeze_target(
    *, plan_id: str, identity: PositionIdentity, target_weight: float, equity: float,
    current_qty: float, reference_price: float, initial_stop: float,
    approved_risk_amount: float, remaining_entry_notional: float,
    maximum_position_notional: float, maximum_tranche_qty: float,
    market: MarketSpecification,
) -> FrozenTarget:
    """Freeze one monotone plan under already-computed account/cluster/cash caps.

    ``approved_risk_amount`` is the original approval for all new tranches in
    this plan, never a percentage recomputed from future equity. The remaining
    notional is the minimum headroom from cash, leverage, cluster and turnover
    constraints, with existing order reservations already subtracted.
    """
    if market.symbol != identity.symbol or market.is_derivative or market.contract_size != 1:
        raise ValueError("version 1 requires matching unit-based spot/margin market")
    if not market.active:
        raise ValueError("market is inactive")
    price = _number(reference_price, "reference_price", positive=True)
    stop = _number(initial_stop, "initial_stop", positive=True)
    current = _number(current_qty, "current_qty")
    approval = _number(approved_risk_amount, "approved_risk_amount")
    entry_cap = _number(remaining_entry_notional, "remaining_entry_notional") / price
    total_cap = _number(maximum_position_notional, "maximum_position_notional") / price
    desired = (_number(target_weight, "target_weight") * _number(equity, "equity") / price)
    target = min(desired, total_cap)
    if target > current:
        if price == stop:
            raise ValueError("entry stop distance must be positive")
        target = min(target, current + entry_cap, current + approval / abs(price - stop))
        # An entry capped to zero headroom must not accidentally become a dust
        # reduction when an existing quantity is off the current venue grid.
        target = max(current, _floor(target, market.amount_step))
    else:
        target = _floor(target, market.amount_step)
    return FrozenTarget(plan_id, identity, current, target, price, stop, approval,
                        _number(maximum_tranche_qty, "maximum_tranche_qty", positive=True))


def _floor(value: Decimal, step: Decimal | None) -> Decimal:
    return value if step is None else (value / step).to_integral_value(rounding=ROUND_DOWN) * step


@dataclass(frozen=True)
class TrancheFact:
    """Authoritative facts for a previously emitted decision, including failures."""
    decision_id: str
    sequence: int
    requested_qty: Decimal
    filled_qty: Decimal
    status: OrderStatus

    def __post_init__(self) -> None:
        if type(self.sequence) is not int or self.sequence < 0:
            raise ValueError("sequence must be a nonnegative integer")
        object.__setattr__(self, "requested_qty", _number(self.requested_qty, "requested_qty", positive=True))
        object.__setattr__(self, "filled_qty", _number(self.filled_qty, "filled_qty"))
        object.__setattr__(self, "status", OrderStatus(self.status))
        if self.filled_qty > self.requested_qty:
            raise ValueError("filled quantity exceeds request")
        if self.status is OrderStatus.FILLED and self.filled_qty != self.requested_qty:
            raise ValueError("filled status requires full quantity")


@dataclass(frozen=True)
class TrancheDecision:
    action: str
    quantity: Decimal
    target_qty: Decimal
    reason: str
    decision_id: str | None = None
    approved_risk_amount: Decimal = Decimal(0)
    reduce_only: bool = False


def _decision_id(plan: FrozenTarget, sequence: int) -> str:
    return "target-" + _digest({"plan": plan.checkpoint()["sha256"], "sequence": sequence})[:32]


def next_tranche(
    plan: FrozenTarget, *, identity: PositionIdentity, current_qty: float,
    current_price: float, market: MarketSpecification,
    order_facts: Sequence[TrancheFact] = (), facts_reconciled: bool = False,
    has_unresolved_orders: bool = False, lifecycle_closed: bool = False,
    allows_new_risk: bool = False, remaining_entry_notional: float = 0,
    remaining_entry_risk: float = 0, effective_stop: float | None = None,
) -> TrancheDecision:
    """Emit one deterministic slice or a reasoned hold; never submit or reserve.

    Every emitted slice must be durably registered by its decision_id in the
    existing order ledger before send. Supply *all* this plan's order facts,
    including terminal cancellations/rejections. Unknown and cancel-pending
    facts prevent retry. A missing ledger must set facts_reconciled=False.
    """
    def hold(reason: str) -> TrancheDecision:
        return TrancheDecision("HOLD", Decimal(0), plan.target_qty, reason)
    if any(type(value) is not bool for value in
           (facts_reconciled, has_unresolved_orders, lifecycle_closed, allows_new_risk)):
        raise ValueError("reconciliation, lifecycle and risk gates must be booleans")
    if not facts_reconciled:
        return hold("facts_not_reconciled")
    if identity != plan.identity:
        return hold("position_identity_changed")
    if lifecycle_closed:
        return hold("position_lifecycle_closed")
    if market.symbol != identity.symbol or market.is_derivative or market.contract_size != 1:
        return hold("unsupported_market")
    if not market.active:
        return hold("market_inactive")
    current = _number(current_qty, "current_qty")
    price = _number(current_price, "current_price", positive=True)
    ordered = sorted(order_facts, key=lambda item: item.sequence)
    if [item.sequence for item in ordered] != list(range(len(ordered))):
        return hold("order_history_incomplete_or_duplicate")
    if any(item.decision_id != _decision_id(plan, item.sequence) for item in ordered):
        return hold("order_identity_mismatch")
    terminal = {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.EXPIRED,
                OrderStatus.REJECTED, OrderStatus.EXPIRED_UNSUBMITTED, OrderStatus.NO_POSITION}
    if has_unresolved_orders or any(item.status not in terminal for item in ordered):
        return hold("orders_unresolved")
    opening = plan.target_qty > plan.original_qty
    consumed = sum((item.filled_qty for item in ordered), Decimal(0))
    capacity = (plan.entry_capacity if opening else plan.original_qty - plan.target_qty)
    if consumed > capacity:
        return hold("fills_exceed_frozen_capacity")
    quantity = min((plan.target_qty - current if opening else current - plan.target_qty),
                   capacity - consumed, plan.maximum_tranche_qty)
    if quantity <= 0:
        return hold("target_satisfied_or_capacity_consumed")
    if opening:
        if not allows_new_risk:
            return hold("new_risk_blocked")
        if effective_stop is None:
            return hold("protective_stop_unverified")
        stop = _number(effective_stop, "effective_stop", positive=True)
        if ((identity.side == "long" and stop < plan.initial_stop)
                or (identity.side == "short" and stop > plan.initial_stop)):
            return hold("protective_stop_loosened")
        if ((identity.side == "long" and price <= stop)
                or (identity.side == "short" and price >= stop)):
            return hold("price_crossed_protective_stop")
        if abs(price - stop) > plan.unit_risk:
            return hold("current_price_exceeds_approved_stop_risk")
        quantity = min(quantity,
                       _number(remaining_entry_notional, "remaining_entry_notional") / price,
                       _number(remaining_entry_risk, "remaining_entry_risk") / plan.unit_risk,
                       (plan.approved_risk_amount - consumed * plan.unit_risk) / plan.unit_risk)
    if market.max_amount is not None:
        quantity = min(quantity, market.max_amount)
    if market.max_notional is not None:
        quantity = min(quantity, market.max_notional / price)
    quantity = _floor(quantity, market.amount_step)
    if (quantity <= 0 or (market.min_amount is not None and quantity < market.min_amount)
            or (market.min_notional is not None and quantity * price < market.min_notional)):
        return hold("below_venue_minimum_or_budget")
    action = ("buy" if identity.side == "long" else "short") if opening else (
        "sell" if identity.side == "long" else "cover")
    return TrancheDecision(action, quantity, plan.target_qty, "frozen_target_slice",
                           _decision_id(plan, len(ordered)),
                           quantity * plan.unit_risk if opening else Decimal(0), not opening)
