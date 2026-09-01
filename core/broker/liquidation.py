"""Forced position reduction through the canonical order/fill path.

Split out of core/broker.py (A4) — see docs/architecture_review.md. See
core/broker_matching.py's module docstring for why this is a mixin rather
than a standalone collaborator object.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd

from core.broker.matching import is_protective_stop
from core.broker.types import BacktestOrderStatus


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
        risk_action_id: Optional[str] = None,
    ) -> List[Dict]:
        """Reduce marked positions immediately through the canonical fill path."""
        if not 0 <= remaining_fraction < 1:
            raise ValueError("remaining_fraction must be in [0, 1)")
        # Opening orders are cancelled because the account is de-risking, and
        # venue-resident protective stops are cancelled because this action is
        # now the authoritative close: leaving a stop armed against a position
        # that is being liquidated is exactly the double-sell SR2-5 forbids.
        # Whatever survives a partial reduce is re-armed by the next sync.
        def _superseded(order) -> bool:
            return order.side in {"buy", "short"} or is_protective_stop(order)

        for order in list(self.pending_orders) + list(self.active_orders):
            if _superseded(order):
                self._set_status(order, BacktestOrderStatus.CANCELED, timestamp)
        self.pending_orders = [o for o in self.pending_orders if not _superseded(o)]
        self.active_orders = [o for o in self.active_orders if not _superseded(o)]
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
                # SR1-2: every close this one action produces shares an id, so
                # the opening strategies fold them into a single health cohort
                # instead of N independent "failures".
                risk_action_id=risk_action_id,
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
                "risk_action_id": risk_action_id,
            })
        return trades
