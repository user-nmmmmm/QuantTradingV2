"""Forced position reduction through the canonical order/fill path.

Split out of core/broker.py (A4) — see docs/architecture_review.md. See
core/broker_matching.py's module docstring for why this is a mixin rather
than a standalone collaborator object.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pandas as pd

from core.broker_types import BacktestOrderStatus


class LiquidationMixin:
    """Force-reduce positions by routing synthetic orders through the matcher.

    Expects ``self`` to carry ``pending_orders``, ``active_orders``,
    ``portfolio``, ``max_participation_rate``, ``execution_audit``, plus
    ``submit_order``/``process_orders``/``_set_status`` (from
    ``MatchingMixin``).
    """

    def force_liquidate(
        self,
        current_bar: Dict[str, pd.Series],
        *,
        timestamp: Any,
        reason: str = "MarginLiquidation",
        remaining_fraction: float = 0.0,
    ) -> List[Dict]:
        """Reduce marked positions immediately through the canonical fill path."""
        if not 0 <= remaining_fraction < 1:
            raise ValueError("remaining_fraction must be in [0, 1)")
        for order in list(self.pending_orders) + list(self.active_orders):
            if order.side in {"buy", "short"}:
                self._set_status(order, BacktestOrderStatus.CANCELED, timestamp)
        self.pending_orders = [o for o in self.pending_orders if o.side not in {"buy", "short"}]
        self.active_orders = [o for o in self.active_orders if o.side not in {"buy", "short"}]
        signal_time = pd.Timestamp(timestamp) - pd.Timedelta(microseconds=1)
        liquidation_bars: Dict[str, pd.Series] = {}
        for symbol, position in list(self.portfolio.positions.items()):
            bar = current_bar.get(symbol)
            if bar is None or position["qty"] == 0:
                continue
            reduce_qty = abs(position["qty"]) * (1.0 - remaining_fraction)
            if reduce_qty <= 0:
                continue
            mark = float(bar.get("mark_price", bar.get("close", bar.get("open"))))
            self.submit_order(
                symbol,
                "sell" if position["qty"] > 0 else "cover",
                reduce_qty,
                mark,
                timestamp=signal_time,
                strategy_id="AccountRisk",
                exit_reason=reason,
            )
            forced_bar = bar.copy()
            forced_bar.name = pd.Timestamp(timestamp)
            forced_bar["open"] = mark
            forced_bar["volume"] = max(
                float(forced_bar.get("volume", 0.0)),
                reduce_qty / self.max_participation_rate * 2.0,
            )
            liquidation_bars[symbol] = forced_bar
        if not liquidation_bars:
            return []
        trades = self.process_orders(liquidation_bars)
        for trade in trades:
            self.execution_audit.append({
                "timestamp": timestamp,
                "order_id": trade["order_id"],
                "symbol": trade["symbol"],
                "side": trade["side"],
                "outcome": "forced_liquidation",
                "reason": reason,
            })
        return trades
