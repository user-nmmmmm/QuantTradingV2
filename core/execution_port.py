"""Structural execution interfaces shared by backtest and live routing."""

from __future__ import annotations

from typing import Any, Optional, Protocol


class ExecutionResult(Protocol):
    @property
    def accepted(self) -> bool:
        """Whether the execution venue accepted the order for processing."""


class ExecutionPort(Protocol):
    """Smallest order interface consumed by strategies and the router."""

    def submit_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: Optional[float] = None,
        order_type: str = "market",
        timestamp: Any = None,
        slippage: float = 0.0,
        strategy_id: str = "Manual",
        exit_reason: str = "signal",
    ) -> ExecutionResult:
        ...

    def cancel_symbol_orders(self, symbol: str) -> Optional[int]:
        ...
