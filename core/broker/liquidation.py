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
        scope_symbols: Optional[set[str]] = None,
    ) -> List[Dict]:
        """Reduce marked positions immediately through the canonical fill path."""
        if not 0 <= remaining_fraction < 1:
            raise ValueError("remaining_fraction must be in [0, 1)")
        targets = None
        if risk_action_id is not None:
            actions = getattr(self, "risk_action_targets", None)
            if actions is None:
                self.risk_action_targets = actions = {}
            if risk_action_id not in actions:
                actions[risk_action_id] = {
                    symbol: {"position_id": self.portfolio.get_lot_book(symbol)._current_position_id,
                             "target_qty": abs(float(position["qty"])) * remaining_fraction,
                             "status": "pending"}
                    for symbol, position in self.portfolio.positions.items()
                    if position["qty"] and (scope_symbols is None or symbol in scope_symbols)
                }
            targets = actions[risk_action_id]
            for symbol, target in targets.items():
                qty = abs(float(self.portfolio.get_position(symbol)["qty"]))
                current_id = self.portfolio.get_lot_book(symbol)._current_position_id
                if qty <= target["target_qty"] + 1e-12:
                    target["status"] = "complete"
                elif current_id != target["position_id"]:
                    target["status"] = "position_replaced"
            scope_symbols = {symbol for symbol, target in targets.items() if target["status"] == "pending"}
        # Opening orders are cancelled because the account is de-risking, and
        # venue-resident protective stops are cancelled because this action is
        # now the authoritative close: leaving a stop armed against a position
        # that is being liquidated is exactly the double-sell SR2-5 forbids.
        # Whatever survives a partial reduce is re-armed by the next sync.
        def _superseded(order) -> bool:
            if (risk_action_id is not None and order.risk_action_id == risk_action_id
                    and order.side in {"sell", "cover"}):
                return False
            return (scope_symbols is None or order.symbol in scope_symbols) and (
                order.side in {"buy", "short", "sell", "cover"} or is_protective_stop(order))

        for order in list(self.pending_orders) + list(self.active_orders):
            if _superseded(order):
                self._set_status(order, BacktestOrderStatus.CANCELED, timestamp)
        self.pending_orders = [o for o in self.pending_orders if not _superseded(o)]
        self.active_orders = [o for o in self.active_orders if not _superseded(o)]
        signal_time = pd.Timestamp(timestamp) - pd.Timedelta(microseconds=1)
        liquidation_bars: Dict[str, pd.Series] = {}
        for symbol, position in list(self.portfolio.positions.items()):
            if scope_symbols is not None and symbol not in scope_symbols:
                continue
            target = targets.get(symbol) if targets is not None else None
            if targets is not None and target is None:
                continue
            if target is not None:
                current_id = self.portfolio.get_lot_book(symbol)._current_position_id
                if current_id != target["position_id"]:
                    target["status"] = "position_replaced"
                    continue
                outstanding = sum(float(order.remaining_qty) for order in self.pending_orders + self.active_orders
                                  if order.symbol == symbol and order.risk_action_id == risk_action_id
                                  and order.side in {"sell", "cover"})
                needed = max(abs(position["qty"]) - target["target_qty"], 0.0)
                target["status"] = "complete" if needed <= 1e-12 else "pending"
                reduce_qty = max(needed - outstanding, 0.0)
            else:
                reduce_qty = abs(position["qty"]) * (1.0 - remaining_fraction)
            bar = current_bar.get(symbol)
            if bar is None or position["qty"] == 0:
                continue
            if target is not None and reduce_qty <= 1e-12:
                continue
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
            # Preserve actual liquidity. An emergency does not create volume.
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
