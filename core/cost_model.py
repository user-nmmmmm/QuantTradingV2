"""Auditable execution-cost semantics shared by research and execution paths."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional


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
