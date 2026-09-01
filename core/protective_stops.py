"""Protective stop mechanics: hybrid initial stop, Chandelier trail, risk recheck.

Implements SR2-1..SR2-4 of ``docs/current_strategy_remediation_roadmap.md``;
the invariants are written down in ``docs/protective_stop_contract.md``.

Everything here is a pure function over facts that are known **at signal
time** (SR2-1): the completed bar's ATR, Donchian level and highs. Nothing
reads a future bar, and nothing reads the entry fill when computing the
original sizing stop.

Three problems this closes:

* **STR-P1-02** - position size was fixed off the signal bar's close, so a
  breakout that gapped through the open put more than the configured risk at
  stake with no named action. :func:`evaluate_fill_risk` recomputes the risk
  from the *actual* fill and demands an explicit resize/rejection.
* The implicit ``close * 0.95`` fallback: an invalid Donchian stop silently
  became a 5% stop. :func:`plan_initial_stop` rejects instead, or clamps
  inside a pre-registered ATR band, and always says which rule it used.
* A trailing stop that could move *down* when ATR expanded.
  :func:`update_trailing_stop` is monotone by construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

STOP_METHOD_STRUCTURAL = "structural_donchian"
STOP_METHOD_ATR = "atr"
STOP_METHOD_HYBRID = "hybrid_max"
STOP_METHOD_REJECTED = "rejected"


@dataclass(frozen=True)
class ProtectiveStopPolicy:
    """Pre-registered stop parameters (SR2-2/SR2-3 research candidates)."""

    #: Use the ATR leg at all. Off keeps the pure Donchian stop (arm A of the
    #: SR2-2 A/B/C comparison), which is what the frozen baseline runs.
    use_atr_initial_stop: bool = False
    #: Maintain a Chandelier trailing stop (arm D).
    use_trailing_stop: bool = False
    atr_period: int = 14
    initial_atr_multiple: float = 2.0
    trailing_atr_multiple: float = 3.0
    #: Risk distance bounds as a fraction of the reference price. A signal
    #: whose stop is closer than the minimum is rejected outright rather than
    #: sized to an enormous position; one further than the maximum is clamped
    #: to the ATR leg (and the sizing shrinks accordingly).
    min_stop_distance_pct: float = 0.005
    max_stop_distance_pct: float = 0.35
    #: Enable the breakeven move only after this many R of open profit.
    #: ``None`` disables it (SR2-3 keeps it optional and off by default).
    breakeven_after_r: Optional[float] = None
    #: Estimated round-trip cost, in price units per unit, added to a
    #: breakeven stop so "breakeven" is not a guaranteed small loss.
    breakeven_cost_buffer: float = 0.0

    @classmethod
    def from_mapping(cls, mapping: Optional[Dict[str, Any]]) -> "ProtectiveStopPolicy":
        data = dict(mapping or {})
        return cls(**{
            key: value for key, value in data.items()
            if key in cls.__dataclass_fields__
        })


@dataclass(frozen=True)
class StopPlan:
    """The protective stop a signal asks for, and why."""

    stop_price: Optional[float]
    method: str
    structural_stop: Optional[float] = None
    atr_stop: Optional[float] = None
    reject_reason: Optional[str] = None

    @property
    def accepted(self) -> bool:
        return self.stop_price is not None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stop_price": self.stop_price,
            "method": self.method,
            "structural_stop": self.structural_stop,
            "atr_stop": self.atr_stop,
            "reject_reason": self.reject_reason,
        }


def plan_initial_stop(
    *,
    side: str,
    reference_price: float,
    structural_stop: Optional[float],
    atr: Optional[float],
    policy: ProtectiveStopPolicy,
) -> StopPlan:
    """``max(structural, reference - k*ATR)`` for longs; mirrored for shorts.

    Returns a rejection instead of inventing a stop when neither leg produces
    a usable level, so an unmeasurable signal never reaches sizing.
    """
    if reference_price <= 0:
        return StopPlan(None, STOP_METHOD_REJECTED,
                        reject_reason="non_positive_reference_price")
    long_side = side in {"buy", "long"}
    structural = _valid_stop(structural_stop, reference_price, long_side)
    atr_stop = None
    if policy.use_atr_initial_stop and atr is not None and atr > 0:
        raw = (
            reference_price - policy.initial_atr_multiple * atr if long_side
            else reference_price + policy.initial_atr_multiple * atr
        )
        atr_stop = _valid_stop(raw, reference_price, long_side)

    candidates = {
        STOP_METHOD_STRUCTURAL: structural,
        STOP_METHOD_ATR: atr_stop,
    }
    present = {name: value for name, value in candidates.items() if value is not None}
    if not present:
        return StopPlan(
            None, STOP_METHOD_REJECTED, structural_stop=structural,
            atr_stop=atr_stop,
            reject_reason="no_valid_stop_level",
        )
    # The tightest of the available levels: for a long that is the highest
    # stop price, for a short the lowest.
    chosen = max(present.values()) if long_side else min(present.values())
    method = (
        STOP_METHOD_HYBRID if len(present) > 1
        else next(iter(present))
    )

    distance = abs(reference_price - chosen)
    min_distance = policy.min_stop_distance_pct * reference_price
    max_distance = policy.max_stop_distance_pct * reference_price
    if distance < min_distance:
        return StopPlan(
            None, STOP_METHOD_REJECTED, structural_stop=structural,
            atr_stop=atr_stop,
            reject_reason=(
                f"stop_too_close: {distance:.10g} < {min_distance:.10g}"
            ),
        )
    if distance > max_distance:
        clamped = (
            reference_price - max_distance if long_side
            else reference_price + max_distance
        )
        return StopPlan(
            clamped, "clamped_max_distance", structural_stop=structural,
            atr_stop=atr_stop,
        )
    return StopPlan(chosen, method, structural_stop=structural, atr_stop=atr_stop)


def _valid_stop(
    value: Optional[float], reference_price: float, long_side: bool,
) -> Optional[float]:
    if value is None:
        return None
    try:
        stop = float(value)
    except (TypeError, ValueError):
        return None
    if stop != stop or stop <= 0:  # NaN or non-positive
        return None
    if long_side and stop >= reference_price:
        return None
    if not long_side and stop <= reference_price:
        return None
    return stop


def update_trailing_stop(
    *,
    side: str,
    current_stop: Optional[float],
    initial_stop: Optional[float],
    extreme_since_fill: Optional[float],
    atr: Optional[float],
    policy: ProtectiveStopPolicy,
    entry_price: Optional[float] = None,
) -> Optional[float]:
    """Chandelier update. Monotone by construction: a long stop never falls.

    ``new_stop = max(old_stop, initial_stop, highest_high - k*ATR)`` for longs
    (``min`` of the mirrored terms for shorts), so a widening ATR can only fail
    to raise the stop - it can never give back protection already earned.
    """
    long_side = side in {"buy", "long"}
    levels = [
        value for value in (current_stop, initial_stop)
        if value is not None and float(value) > 0
    ]
    if policy.use_trailing_stop and extreme_since_fill is not None and atr:
        candidate = (
            float(extreme_since_fill) - policy.trailing_atr_multiple * float(atr)
            if long_side
            else float(extreme_since_fill) + policy.trailing_atr_multiple * float(atr)
        )
        levels.append(candidate)
    if (
        policy.breakeven_after_r is not None
        and entry_price is not None
        and initial_stop is not None
        and extreme_since_fill is not None
    ):
        risk_per_unit = abs(float(entry_price) - float(initial_stop))
        if risk_per_unit > 0:
            open_r = (
                (float(extreme_since_fill) - float(entry_price)) / risk_per_unit
                if long_side
                else (float(entry_price) - float(extreme_since_fill)) / risk_per_unit
            )
            if open_r >= policy.breakeven_after_r:
                levels.append(
                    float(entry_price) + policy.breakeven_cost_buffer
                    if long_side
                    else float(entry_price) - policy.breakeven_cost_buffer
                )
    if not levels:
        return None
    return max(levels) if long_side else min(levels)


@dataclass(frozen=True)
class EntryRiskPolicy:
    """SR2-4: what to do when the real fill risks more than was reserved."""

    enabled: bool = True
    #: Fraction of the budget the actual risk may exceed before acting; covers
    #: rounding and the fee/slippage wedge between reference and fill price.
    tolerance: float = 0.10
    #: ``resize`` reduces the position to the affordable quantity;
    #: ``audit_only`` records the breach without trading.
    action: str = "resize"
    #: Never leave a dust position behind: below this fraction of the filled
    #: quantity the whole lot is closed instead of trimmed.
    min_remaining_fraction: float = 0.10

    @classmethod
    def from_mapping(cls, mapping: Optional[Dict[str, Any]]) -> "EntryRiskPolicy":
        data = dict(mapping or {})
        return cls(**{
            key: value for key, value in data.items()
            if key in cls.__dataclass_fields__
        })


@dataclass(frozen=True)
class FillRiskAssessment:
    """The post-fill answer to "how much am I actually risking?"."""

    symbol: str
    lot_id: str
    side: str
    fill_price: float
    protective_stop: float
    filled_qty: float
    actual_risk_per_unit: float
    actual_total_risk: float
    risk_budget: float
    risk_ratio: float
    breached: bool
    action: str
    resize_qty: float
    affordable_qty: float
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "lot_id": self.lot_id,
            "side": self.side,
            "fill_price": self.fill_price,
            "protective_stop": self.protective_stop,
            "filled_qty": self.filled_qty,
            "actual_risk_per_unit": self.actual_risk_per_unit,
            "actual_total_risk": self.actual_total_risk,
            "risk_budget": self.risk_budget,
            "risk_ratio": self.risk_ratio,
            "breached": self.breached,
            "action": self.action,
            "resize_qty": self.resize_qty,
            "affordable_qty": self.affordable_qty,
            "reason": self.reason,
        }


def evaluate_fill_risk(
    *,
    symbol: str,
    lot_id: str,
    side: str,
    fill_price: float,
    protective_stop: Optional[float],
    filled_qty: float,
    equity_at_fill: float,
    base_risk_per_trade: float,
    health_risk_multiplier: float = 1.0,
    policy: Optional[EntryRiskPolicy] = None,
) -> Optional[FillRiskAssessment]:
    """Recompute risk from the real fill and name the action to take.

    ``risk_budget = equity_at_fill * base_risk_per_trade * health_multiplier``.
    Returns ``None`` when the check does not apply (disabled, no stop, no
    quantity) - a missing stop is reported by the caller's own contract, not
    silently treated as "within budget".
    """
    policy = policy or EntryRiskPolicy()
    if not policy.enabled or filled_qty <= 0 or not protective_stop:
        return None
    stop = float(protective_stop)
    risk_per_unit = abs(float(fill_price) - stop)
    if risk_per_unit <= 0:
        return None
    actual_total_risk = risk_per_unit * float(filled_qty)
    risk_budget = (
        float(equity_at_fill) * float(base_risk_per_trade)
        * float(health_risk_multiplier)
    )
    if risk_budget <= 0:
        return None
    ratio = actual_total_risk / risk_budget
    affordable_qty = risk_budget / risk_per_unit
    breached = ratio > 1.0 + policy.tolerance
    action, resize_qty, reason = "none", 0.0, "within_budget"
    if breached:
        if policy.action == "audit_only":
            action, reason = "audit_only", "breach_recorded_without_resize"
        else:
            resize_qty = max(0.0, float(filled_qty) - affordable_qty)
            remaining = float(filled_qty) - resize_qty
            if remaining < policy.min_remaining_fraction * float(filled_qty):
                resize_qty = float(filled_qty)
                reason = "gap_risk_close_all"
            else:
                reason = "gap_risk_resize"
            action = "resize"
    return FillRiskAssessment(
        symbol=symbol,
        lot_id=lot_id,
        side=side,
        fill_price=float(fill_price),
        protective_stop=stop,
        filled_qty=float(filled_qty),
        actual_risk_per_unit=risk_per_unit,
        actual_total_risk=actual_total_risk,
        risk_budget=risk_budget,
        risk_ratio=ratio,
        breached=breached,
        action=action,
        resize_qty=resize_qty,
        affordable_qty=affordable_qty,
        reason=reason,
    )
