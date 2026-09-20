"""Durable portfolio risk actions bound to the position lifecycle they reduced.

An action is checkpointed before exchange side effects. Targets are frozen only
after opening orders have been canceled and their final fills reconciled. A
completed proportional reduction is permanent; a liquidation directive instead
keeps observing inventory while the directive remains active.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Any, Callable, TYPE_CHECKING

from core.domain import OrderStatus
from core.risk.actions import RiskActionPlan
from live_trading.recovery import balance_sync_succeeded


TERMINAL_RISK_STATUSES = {
    OrderStatus.CANCELED, OrderStatus.FILLED, OrderStatus.EXPIRED, OrderStatus.REJECTED,
}


@dataclass(frozen=True)
class RiskActionProgress:
    pending: bool = False
    performed: bool = False

    @property
    def blocks_entries(self):
        return self.pending or self.performed


class PortfolioRiskActionsMixin:
    """Execution helpers used by the live tick orchestrator."""

    if TYPE_CHECKING:
        broker: Any
        state_store: Any
        _snapshot: Any
        _now: Callable[[], datetime]
        _alert: Callable[..., Any]
        _submit_risk_exit: Callable[..., bool]

    def _risk_action_state_store(self):
        ensure = getattr(self, "_ensure_state_store", None)
        return ensure() if callable(ensure) else self.state_store

    def _risk_action_position(self, symbol):
        portfolio = self.broker.portfolio
        held = float(portfolio.get_position(symbol)["qty"])
        lots = portfolio.open_lots(symbol)
        signed_lots = sum(lot.qty_open * (1 if lot.side == "long" else -1) for lot in lots)
        if not isfinite(held) or abs(held - signed_lots) > 1e-8:
            raise ValueError(f"position_fill_ledger_mismatch:{symbol}")
        return {
            "position_ids": sorted({lot.position_id for lot in lots}),
            "side": "long" if held > 0 else "short" if held < 0 else "flat",
            "original_qty": abs(held),
        }

    def _risk_action_unverifiable(self, checkpoint, reason):
        checkpoint["recovery_reason"] = reason
        self._operational_state = "DEGRADED"
        self._alert("error", "portfolio_risk_action_unverifiable", {
            "action_id": checkpoint["action_id"], "reason": reason,
        })

    def _cancel_risk_orders(self, orders):
        """Cancel with authoritative order facts; UNKNOWN never releases stock."""
        for record in orders:
            result = self.broker.cancel_order(record["client_order_id"])
            if result.status not in TERMINAL_RISK_STATUSES:
                self._operational_state = "DEGRADED"
                return False
        return balance_sync_succeeded(self.broker, self.broker.sync())

    def _legacy_portfolio_risk_action(self, plan, targets, checkpoint):
        """Only attributed fills prove which historical positions a target owns."""
        orders = self.broker.order_store
        for row in orders.list_non_terminal():
            if (row.get("intent") or {}).get("risk_action_id") == plan.action_id:
                self.broker.reconcile_order(row["client_order_id"])
        if not balance_sync_succeeded(self.broker, self.broker.sync()):
            raise ValueError("legacy_account_sync_failed")
        positions = {}
        for symbol, target in targets.items():
            target = float(target)
            if not isfinite(target) or target < 0:
                raise ValueError(f"legacy_target_invalid:{symbol}")
            events = [event for event in self.broker.close_events
                      if event.risk_action_id == plan.action_id and event.symbol == symbol]
            if not events:
                raise ValueError(f"legacy_position_identity_missing:{symbol}")
            records = [row for row in orders.list_all()
                       if row["symbol"] == symbol
                       and (row.get("intent") or {}).get("risk_action_id") == plan.action_id]
            sides = {row["side"] for row in records}
            if len(sides) != 1 or not sides.issubset({"sell", "cover"}):
                raise ValueError(f"legacy_exit_direction_unverifiable:{symbol}")
            ids = sorted({event.position_id for event in events})
            current = self._risk_action_position(symbol)
            original_remaining = sum(lot.qty_open for lot in self.broker.portfolio.open_lots(symbol)
                                     if lot.position_id in ids)
            closed = sum(event.qty for event in events)
            original = target / plan.remaining_fraction if plan.remaining_fraction else closed + original_remaining
            if closed + original_remaining > original + 1e-8:
                raise ValueError(f"legacy_target_quantity_unverifiable:{symbol}")
            if set(current["position_ids"]) & set(ids) and not set(current["position_ids"]).issubset(ids):
                raise ValueError(f"legacy_mixed_position_identity:{symbol}")
            positions[symbol] = {
                "position_ids": ids, "side": "long" if sides == {"sell"} else "short",
                "original_qty": original, "target_qty": target,
            }
        checkpoint.update(status="active", positions=positions,
                          migration={"source": f"risk_targets:{plan.action_id}",
                                     "evidence": "attributed_close_events_and_order_ledger"})
        return checkpoint

    def _reconcile_portfolio_risk_action(self, plan):
        state = self._risk_action_state_store()
        active_index = state.get("portfolio_risk_action_active")
        active_id = (active_index.get("action_id") if isinstance(active_index, dict) else active_index)
        active = state.get(f"portfolio_risk_action:{active_id}") if active_id else None
        if active is None and isinstance(active_index, dict):
            # The index carries the preparing checkpoint, so interruption
            # between the two durable writes cannot orphan a started action.
            active = active_index
            state.set(f"portfolio_risk_action:{active_id}", active)
        # Finish an existing reduction even if the breaker changes back to hold.
        # A new liquidation is stronger and supersedes any proportional action.
        supersedes = None
        if active and active.get("status") != "completed":
            if plan is not None and plan.remaining_fraction == 0 and plan.action_id != active_id:
                supersedes = active_id
            else:
                plan = RiskActionPlan(active["action_id"], active["reason"], active["remaining_fraction"])
        if plan is None:
            return RiskActionProgress()
        key = f"portfolio_risk_action:{plan.action_id}"
        checkpoint = state.get(key)
        performed = False
        if checkpoint is None:
            checkpoint = {
                "version": 1, "action_id": plan.action_id, "reason": plan.reason,
                "remaining_fraction": plan.remaining_fraction, "status": "preparing",
                "created_at": self._now().isoformat(), "completed_at": None,
                "positions": {}, "supersedes": supersedes,
            }
            # Persist before canceling anything, including a crash during migration.
            state.set("portfolio_risk_action_active", checkpoint)
            state.set(key, checkpoint)
        if checkpoint["status"] == "completed" and plan.remaining_fraction > 0:
            return RiskActionProgress()
        if checkpoint["status"] == "legacy_unverifiable":
            self._risk_action_unverifiable(checkpoint, checkpoint["recovery_reason"])
            return RiskActionProgress(pending=True)

        legacy = state.get(f"risk_targets:{plan.action_id}")
        if checkpoint["status"] == "preparing" and legacy is not None:
            backup_key = f"portfolio_risk_action_legacy_backup:{plan.action_id}"
            if state.get(backup_key) is None:
                state.set(backup_key, {"targets": legacy, "captured_at": self._now().isoformat()})
            try:
                self._legacy_portfolio_risk_action(plan, legacy, checkpoint)
            except ValueError as exc:
                checkpoint["status"] = "legacy_unverifiable"
                self._risk_action_unverifiable(checkpoint, str(exc))
                state.set(key, checkpoint)
                return RiskActionProgress(pending=True)
            state.set(key, checkpoint)

        # A continuous liquidation repeats preparation, so a late opening fill
        # or a newly discovered symbol cannot escape a still-active halt.
        if checkpoint["status"] == "preparing" or plan.remaining_fraction == 0:
            orders = [row for row in self.broker.order_store.list_non_terminal()
                      if row["side"] in {"buy", "short"}
                      or (checkpoint.get("supersedes") and
                          (row.get("intent") or {}).get("risk_action_id") == checkpoint["supersedes"])]
            performed = bool(orders)
            if not self._cancel_risk_orders(orders):
                return RiskActionProgress(pending=True, performed=performed)
            try:
                positions = {}
                for symbol, pos in self.broker.portfolio.positions.items():
                    if abs(float(pos["qty"])) <= 1e-12:
                        continue
                    original = self._risk_action_position(symbol)
                    original["target_qty"] = original["original_qty"] * plan.remaining_fraction
                    positions[symbol] = original
            except ValueError as exc:
                self._risk_action_unverifiable(checkpoint, str(exc))
                state.set(key, checkpoint)
                return RiskActionProgress(pending=True, performed=performed)
            # Keep zero targets even while flat if an owned order is unresolved.
            if plan.remaining_fraction == 0:
                for symbol, original in checkpoint["positions"].items():
                    positions.setdefault(symbol, original)
            checkpoint.update(status="active", positions=positions, completed_at=None)
            state.set(key, checkpoint)
            state.set("portfolio_risk_action_active", checkpoint)
            if checkpoint.get("supersedes"):
                old_key = f"portfolio_risk_action:{checkpoint['supersedes']}"
                old = state.get(old_key)
                if old:
                    old.update(status="completed", completed_at=self._now().isoformat(),
                               completion_reason="superseded", superseded_by=plan.action_id)
                    state.set(old_key, old)

        done = True
        for symbol, original in checkpoint["positions"].items():
            before = len(self.broker.order_store.list_all())
            before_qty = float(self.broker.portfolio.get_position(symbol)["qty"])
            had_pending = any((row.get("intent") or {}).get("risk_action_id") == plan.action_id
                              for row in self.broker.order_store.list_non_terminal())
            accepted = self._submit_risk_exit(
                symbol, plan.reason, original["target_qty"], plan.action_id,
                self._snapshot.prices.get(symbol),
                original_position_ids=original["position_ids"] if plan.remaining_fraction > 0 else None,
                original_side=original["side"] if plan.remaining_fraction > 0 else None,
            )
            after_qty = float(self.broker.portfolio.get_position(symbol)["qty"])
            performed = (performed or had_pending or len(self.broker.order_store.list_all()) != before
                         or abs(after_qty - before_qty) > 1e-12)
            done = done and accepted
        if done:
            checkpoint.update(status="completed", completed_at=self._now().isoformat())
            state.set(key, checkpoint)
            current_index = state.get("portfolio_risk_action_active")
            indexed_id = current_index.get("action_id") if isinstance(current_index, dict) else current_index
            if indexed_id == plan.action_id:
                state.set("portfolio_risk_action_active", None)
        return RiskActionProgress(pending=not done, performed=performed)
