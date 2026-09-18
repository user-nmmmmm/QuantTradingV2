"""Auditable execution-cost semantics shared by research and execution paths."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional
import math


def market_slippage_components(*, price: float, quantity: float, volume: float,
                               bar: Any, spread_bps: float,
                               volatility_factor: float, use_impact: bool,
                               impact_coefficient: float, impact_exponent: float):
    """Deterministic spread/range/impact terms, shared with passive outcomes.

    ``bar`` is the execution bar when resolving outcomes, or the closed signal
    bar when making a labelled *estimate*. It must never be a future bar used
    as a signal feature. Random base slippage is handled by the caller.
    """
    raw_spread = bar.get("spread_bps") if bar is not None else None
    quoted_spread = (float(raw_spread) if raw_spread is not None
                     and math.isfinite(float(raw_spread)) else float(spread_bps))
    volatility = 0.0
    if bar is not None and price > 0:
        raw_vol = bar.get("volatility")
        volatility = (max(float(raw_vol), 0.0) if raw_vol is not None
                      and math.isfinite(float(raw_vol)) else
                      max(float(bar.get("high", price)) - float(bar.get("low", price)), 0.0) / price)
    participation = quantity / volume if use_impact and volume > 0 else 0.0
    return {
        "quoted_spread_bps": quoted_spread,
        "spread": max(quoted_spread, 0.0) / 20000.0,
        "volatility": volatility_factor * volatility,
        "impact": (impact_coefficient * max(participation, 0.0) ** impact_exponent
                   if use_impact and volume > 0 else 0.0),
        "participation": participation,
    }


@dataclass(frozen=True)
class CostBreakdown:
    commission: float
    slippage: float
    impact: float
    funding: Optional[float]
    borrow: Optional[float]
    funding_status: str
    borrow_status: str

    @property
    def modeled_total(self) -> float:
        return self.commission + self.slippage + self.impact + (self.funding or 0.0) + (self.borrow or 0.0)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "modeled_total": self.modeled_total}


@dataclass(frozen=True)
class CostModel:
    commission_rate: float
    slippage_rate: float = 0.0
    impact_rate: float = 0.0

    def calculate(
        self,
        *,
        quantity: float,
        price: float,
        funding_rate: Optional[float] = None,
        borrow_rate: Optional[float] = None,
        holding_fraction: float = 1.0,
    ) -> CostBreakdown:
        notional = abs(float(quantity) * float(price))
        if notional < 0 or holding_fraction < 0:
            raise ValueError("cost inputs cannot be negative")
        return CostBreakdown(
            commission=notional * self.commission_rate,
            slippage=notional * self.slippage_rate,
            impact=notional * self.impact_rate,
            funding=None if funding_rate is None else notional * funding_rate * holding_fraction,
            borrow=None if borrow_rate is None else notional * borrow_rate * holding_fraction,
            funding_status="not_modeled" if funding_rate is None else "modeled",
            borrow_status="not_modeled" if borrow_rate is None else "modeled",
        )
