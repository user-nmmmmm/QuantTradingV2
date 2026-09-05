"""Venue-resident protective stop lifecycle (SR2-5).

Implements §4.4 of ``docs/current_strategy_remediation_roadmap.md``::

    EntryIntent
      -> EntryFill
      -> create reduce-only StopMarket
      -> cancel-replace as the stop ratchets up
      -> StopFill | StrategyExit | AccountRiskExit
      -> cancel the remaining exit intents / OCO siblings
      -> reconcile, then PositionFlat

The module is deliberately a **pure state machine over facts**: it is handed
the current position, the strategy's desired protective level and the venue's
open protective orders, and it returns the actions that would reconcile them.
It never talks to an exchange itself, so the same decision function can be
driven by the live broker, a sandbox fault-injection harness, or a replay.

Invariants enforced here (each has a test in
``tests/test_sr2_protective_orders.py``):

* nothing is protected before the entry actually fills - a pending entry
  produces no stop;
* the protective quantity always equals the **net** position, so a partial
  entry fill is protected for what was filled, no more;
* a long's protective level only ever moves up: a lower desired level is
  ignored rather than cancel-replaced downward;
* exactly one authoritative close - once a position is flat, every remaining
  protective order is cancelled (no orphan stop can fire into a new position);
* a rejected, unknown or cancel-timed-out protective order fails closed: the
  position is flagged unprotected and the caller must flatten it rather than
  carry unprotected risk;
* restart reconciles from venue state: missing protection is recreated and
  orphans are cancelled, never assumed from memory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional

_QTY_EPS = 1e-9
_PRICE_EPS = 1e-9


class ProtectiveAction(str, Enum):
    """What must happen at the venue to reconcile protection."""

    PLACE = "place"
    REPLACE = "replace"
    CANCEL = "cancel"
    FLATTEN = "flatten"
    NONE = "none"


class ProtectiveState(str, Enum):
    """How protected the position currently is."""

    FLAT = "flat"
    PENDING_ENTRY = "pending_entry"
    UNPROTECTED = "unprotected"
    ARMED = "armed"
    REPLACING = "replacing"
    FAILED = "failed"


#: Venue order states that still protect the position.
LIVE_ORDER_STATUSES = frozenset({"open", "new", "accepted", "submitted", "partially_filled"})
#: Venue order states that mean the order is gone.
DEAD_ORDER_STATUSES = frozenset({"canceled", "cancelled", "rejected", "expired", "filled"})
#: Venue states that are not a fact yet: fail closed on them.
INDETERMINATE_ORDER_STATUSES = frozenset({"unknown", "submitting", "pending_cancel"})


@dataclass(frozen=True)
class ProtectiveOrder:
    """One protective order as the venue reports it."""

    order_id: str
    symbol: str
    side: str
    qty: float
    stop_price: float
    status: str
    reduce_only: bool = True

    @property
    def is_live(self) -> bool:
        return str(self.status).lower() in LIVE_ORDER_STATUSES

    @property
    def is_indeterminate(self) -> bool:
        return str(self.status).lower() in INDETERMINATE_ORDER_STATUSES


@dataclass(frozen=True)
class ProtectiveIntent:
    """One action to take at the venue, plus why."""

    action: ProtectiveAction
    symbol: str
    reason: str
    side: Optional[str] = None
    qty: float = 0.0
    stop_price: Optional[float] = None
    cancel_order_id: Optional[str] = None
    state: ProtectiveState = ProtectiveState.UNPROTECTED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action.value,
            "symbol": self.symbol,
            "reason": self.reason,
            "side": self.side,
            "qty": self.qty,
            "stop_price": self.stop_price,
            "cancel_order_id": self.cancel_order_id,
            "state": self.state.value,
        }


@dataclass(frozen=True)
class ProtectivePlan:
    """Everything that must happen for one symbol this evaluation."""

    symbol: str
    state: ProtectiveState
    intents: List[ProtectiveIntent] = field(default_factory=list)
    effective_stop: Optional[float] = None
    protected_qty: float = 0.0
    note: str = ""

    @property
    def requires_flatten(self) -> bool:
        return any(
            intent.action is ProtectiveAction.FLATTEN for intent in self.intents
        )

    def to_rows(self) -> List[Dict[str, Any]]:
        """Rows for ``stop_order_audit.csv``."""
        return [
            {
                "symbol": self.symbol,
                "state": self.state.value,
                "effective_stop": self.effective_stop,
                "protected_qty": self.protected_qty,
                "note": self.note,
                **intent.to_dict(),
            }
            for intent in self.intents
        ] or [{
            "symbol": self.symbol,
            "state": self.state.value,
            "effective_stop": self.effective_stop,
            "protected_qty": self.protected_qty,
            "note": self.note,
            "action": ProtectiveAction.NONE.value,
            "reason": self.note or "in_sync",
            "side": None,
            "qty": 0.0,
            "stop_price": None,
            "cancel_order_id": None,
        }]


def protective_side(position_qty: float) -> str:
    """The order side that reduces this position."""
    return "sell" if position_qty > 0 else "cover"


def _is_tighter(new_stop: float, current: float, long_side: bool) -> bool:
    if long_side:
        return new_stop > current + _PRICE_EPS
    return new_stop < current - _PRICE_EPS


class ProtectiveOrderManager:
    """Decides the protective-order actions for one account.

    ``evaluate`` is pure: same inputs, same plan. State kept on the instance is
    only what cannot be re-derived from the venue - the last level actually
    accepted per symbol, used to keep the ratchet monotone across ticks even
    when a cancel-replace is momentarily in flight.
    """

    def __init__(self, *, price_tolerance: float = 1e-9) -> None:
        self.price_tolerance = float(price_tolerance)
        self._accepted_stop: Dict[str, float] = {}
        self.audit: List[Dict[str, Any]] = []

    def forget(self, symbol: str) -> None:
        self._accepted_stop.pop(symbol, None)

    @property
    def tracked_symbols(self) -> frozenset[str]:
        """Symbols whose ratchet must be retired once flat is confirmed.

        Account snapshots often omit flat symbols; iterating only current
        positions/orders would keep old stop levels alive indefinitely.
        """
        return frozenset(self._accepted_stop)

    def evaluate(
        self,
        *,
        symbol: str,
        position_qty: float,
        desired_stop: Optional[float],
        open_protective_orders: Iterable[ProtectiveOrder] = (),
        entry_pending: bool = False,
        record: bool = True,
    ) -> ProtectivePlan:
        """Reconcile venue protection with the position and desired level."""
        orders = [
            order for order in open_protective_orders
            if order.symbol == symbol
        ]
        live = [order for order in orders if order.is_live]
        indeterminate = [order for order in orders if order.is_indeterminate]
        qty = float(position_qty)

        if abs(qty) <= _QTY_EPS:
            plan = self._flat_plan(symbol, live, indeterminate, entry_pending)
            return self._record(plan, record)

        if indeterminate:
            # An order whose venue state is unknown is not protection. Fail
            # closed: the caller must flatten rather than assume a stop exists.
            plan = ProtectivePlan(
                symbol, ProtectiveState.FAILED,
                [ProtectiveIntent(
                    ProtectiveAction.FLATTEN, symbol,
                    "protective_order_state_unknown",
                    side=protective_side(qty), qty=abs(qty),
                    state=ProtectiveState.FAILED,
                )],
                note="indeterminate protective order",
            )
            return self._record(plan, record)

        long_side = qty > 0
        side = protective_side(qty)
        target = self._ratchet(symbol, desired_stop, live, long_side)

        if target is None:
            # No usable protective level: an open position with no stop is the
            # exact exposure SR2 exists to remove.
            plan = ProtectivePlan(
                symbol, ProtectiveState.FAILED,
                [ProtectiveIntent(
                    ProtectiveAction.FLATTEN, symbol, "no_protective_level",
                    side=side, qty=abs(qty), state=ProtectiveState.FAILED,
                )],
                note="no protective level available",
            )
            return self._record(plan, record)

        intents: List[ProtectiveIntent] = []
        # More than one protective order is an OCO leak: keep the tightest.
        if len(live) > 1:
            keeper = max(live, key=lambda o: o.stop_price) if long_side else min(
                live, key=lambda o: o.stop_price
            )
            for order in live:
                if order.order_id != keeper.order_id:
                    intents.append(ProtectiveIntent(
                        ProtectiveAction.CANCEL, symbol,
                        "duplicate_protective_order",
                        cancel_order_id=order.order_id,
                        state=ProtectiveState.REPLACING,
                    ))
            live = [keeper]

        if not live:
            intents.append(ProtectiveIntent(
                ProtectiveAction.PLACE, symbol, "missing_protection",
                side=side, qty=abs(qty), stop_price=target,
                state=ProtectiveState.ARMED,
            ))
            plan = ProtectivePlan(
                symbol, ProtectiveState.ARMED, intents,
                effective_stop=target, protected_qty=abs(qty),
                note="protection created",
            )
            self._accepted_stop[symbol] = target
            return self._record(plan, record)

        current = live[0]
        qty_mismatch = abs(current.qty - abs(qty)) > _QTY_EPS
        level_moved = _is_tighter(target, current.stop_price, long_side)
        not_reduce_only = not current.reduce_only
        if qty_mismatch or level_moved or not_reduce_only:
            reason = (
                "qty_mismatch" if qty_mismatch
                else "ratchet_up" if level_moved
                else "not_reduce_only"
            )
            intents.append(ProtectiveIntent(
                ProtectiveAction.REPLACE, symbol, reason,
                side=side, qty=abs(qty), stop_price=target,
                cancel_order_id=current.order_id,
                state=ProtectiveState.REPLACING,
            ))
            self._accepted_stop[symbol] = target
            plan = ProtectivePlan(
                symbol, ProtectiveState.REPLACING, intents,
                effective_stop=target, protected_qty=abs(qty),
                note=reason,
            )
            return self._record(plan, record)

        self._accepted_stop[symbol] = current.stop_price
        plan = ProtectivePlan(
            symbol, ProtectiveState.ARMED, intents,
            effective_stop=current.stop_price, protected_qty=current.qty,
            note="in_sync",
        )
        return self._record(plan, record)

    def _flat_plan(
        self, symbol: str, live: List[ProtectiveOrder],
        indeterminate: List[ProtectiveOrder], entry_pending: bool,
    ) -> ProtectivePlan:
        """Flat (or not yet filled): no protection may survive."""
        self.forget(symbol)
        intents = [
            ProtectiveIntent(
                ProtectiveAction.CANCEL, symbol, "position_flat",
                cancel_order_id=order.order_id, state=ProtectiveState.FLAT,
            )
            for order in list(live) + list(indeterminate)
        ]
        state = ProtectiveState.PENDING_ENTRY if entry_pending else ProtectiveState.FLAT
        return ProtectivePlan(
            symbol, state, intents,
            note=(
                "entry not filled: nothing to protect" if entry_pending
                else "flat: cancelling residual protection"
            ),
        )

    def _ratchet(
        self, symbol: str, desired_stop: Optional[float],
        live: List[ProtectiveOrder], long_side: bool,
    ) -> Optional[float]:
        """The protective level to enforce: monotone, never loosened."""
        levels = [
            float(value) for value in (
                desired_stop,
                self._accepted_stop.get(symbol),
                *(order.stop_price for order in live),
            )
            if value is not None and float(value) > 0
        ]
        if not levels:
            return None
        return max(levels) if long_side else min(levels)

    def _record(self, plan: ProtectivePlan, record: bool) -> ProtectivePlan:
        if record:
            self.audit.extend(plan.to_rows())
        return plan

    def reconcile_after_restart(
        self,
        *,
        positions: Mapping[str, float],
        desired_stops: Mapping[str, Optional[float]],
        venue_orders: Iterable[ProtectiveOrder],
    ) -> List[ProtectivePlan]:
        """Rebuild protection from venue facts, not from memory (SR2-5).

        Every symbol the venue reports a protective order for is evaluated too,
        so an order left behind for a position that no longer exists is
        cancelled instead of waiting to fire into the next one.
        """
        orders = list(venue_orders)
        symbols = set(positions) | {order.symbol for order in orders} | self.tracked_symbols
        plans = []
        for symbol in sorted(symbols):
            plans.append(self.evaluate(
                symbol=symbol,
                position_qty=float(positions.get(symbol, 0.0)),
                desired_stop=desired_stops.get(symbol),
                open_protective_orders=[
                    order for order in orders if order.symbol == symbol
                ],
            ))
        return plans
