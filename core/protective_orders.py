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
import json
from math import isfinite
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
LIVE_ORDER_STATUSES = frozenset({"open", "new", "accepted", "partial", "partially_filled"})
#: Venue order states that mean the order is gone.
DEAD_ORDER_STATUSES = frozenset({"canceled", "cancelled", "rejected", "expired", "filled"})
#: Venue states that are not a fact yet: fail closed on them.
INDETERMINATE_ORDER_STATUSES = frozenset({"unknown", "submitting", "submitted", "pending_cancel", "cancel_pending", "created"})


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
    position_ids: Optional[tuple[str, ...]] = None

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
    position_ids: Optional[tuple[str, ...]] = None

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
            "position_ids": list(self.position_ids) if self.position_ids is not None else None,
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


def protective_position_reference(position_ids: Iterable[str]) -> str:
    """Persist the causal position epoch in the existing immutable order intent."""
    ids = tuple(sorted(set(position_ids)))
    if not ids or any(not isinstance(item, str) or not item for item in ids):
        raise ValueError("protective position IDs are required")
    return "protective-position-v1:" + json.dumps(ids, separators=(",", ":"))


def parse_protective_position_reference(value: Any) -> Optional[tuple[str, ...]]:
    if value is None or not str(value).startswith("protective-position-v1:"):
        return None
    try:
        values = json.loads(str(value).split(":", 1)[1])
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid protective position reference") from exc
    if not isinstance(values, list) or not values or any(not isinstance(v, str) or not v for v in values):
        raise ValueError("invalid protective position reference")
    return tuple(sorted(set(values)))


def authoritative_position_ids(portfolio: Any, symbol: str) -> tuple[str, ...]:
    """Read the existing lot projection; mismatched/missing facts are an error."""
    held = float(portfolio.get_position(symbol)["qty"])
    lots = list(portfolio.open_lots(symbol))
    total = sum(float(lot.qty_open) * (1 if lot.side == "long" else -1) for lot in lots)
    if not isfinite(held) or not isfinite(total) or abs(held - total) > _QTY_EPS:
        raise ValueError(f"protective_position_ledger_mismatch:{symbol}")
    if any(lot.side not in {"long", "short"} or not isfinite(float(lot.qty_open))
           or float(lot.qty_open) <= 0 or not lot.position_id for lot in lots):
        raise ValueError(f"protective_position_identity_missing:{symbol}")
    if held and (not lots or any((lot.side == "long") != (held > 0) for lot in lots)):
        raise ValueError(f"protective_position_identity_missing:{symbol}")
    if not held and lots:
        raise ValueError(f"protective_flat_position_has_lots:{symbol}")
    return tuple(sorted({str(lot.position_id) for lot in lots}))


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
        self._requested_stop: Dict[str, float] = {}
        self._position_ids: Dict[str, frozenset[str]] = {}
        self._position_sides: Dict[str, bool] = {}
        self.audit: List[Dict[str, Any]] = []

    def forget(self, symbol: str) -> None:
        self._accepted_stop.pop(symbol, None)
        self._requested_stop.pop(symbol, None)
        self._position_ids.pop(symbol, None)
        self._position_sides.pop(symbol, None)

    @property
    def tracked_symbols(self) -> frozenset[str]:
        """Symbols whose ratchet must be retired once flat is confirmed.

        Account snapshots often omit flat symbols; iterating only current
        positions/orders would keep old stop levels alive indefinitely.
        """
        return frozenset(self._accepted_stop) | frozenset(self._requested_stop) | frozenset(self._position_ids)

    def restore_confirmed_stop(self, order: ProtectiveOrder, *, position_ids: Iterable[str],
                               position_qty: float) -> None:
        """Restore a same-epoch ratchet from the existing durable order ledger.

        In particular, an acknowledged cancellation is not permission to loosen
        the last confirmed stop after a crash before its replacement was sent.
        Rejected, unknown and legacy unattributed records cannot establish it.
        """
        ids = frozenset(position_ids)
        qty = float(position_qty)
        confirmed = LIVE_ORDER_STATUSES | {"canceled", "cancelled", "filled", "expired"}
        if (not ids or not order.position_ids or not ids.intersection(order.position_ids)
                or str(order.status).lower() not in confirmed or not qty
                or not isfinite(qty) or order.side != protective_side(qty)
                or not isfinite(order.stop_price) or order.stop_price <= 0):
            return
        prior = self._position_ids.get(order.symbol)
        if prior is not None and (not prior.intersection(ids)
                                  or self._position_sides.get(order.symbol) != (qty > 0)):
            self.forget(order.symbol)
        self._position_ids[order.symbol] = ids
        self._position_sides[order.symbol] = qty > 0
        previous = self._accepted_stop.get(order.symbol)
        self._accepted_stop[order.symbol] = (order.stop_price if previous is None else
                                            max(previous, order.stop_price) if qty > 0 else
                                            min(previous, order.stop_price))

    def evaluate(
        self,
        *,
        symbol: str,
        position_qty: float,
        desired_stop: Optional[float],
        open_protective_orders: Iterable[ProtectiveOrder] = (),
        entry_pending: bool = False,
        record: bool = True,
        position_ids: Optional[Iterable[str]] = None,
        total_position_qty: Optional[float] = None,
    ) -> ProtectivePlan:
        """Reconcile venue protection with the position and desired level."""
        orders = [
            order for order in open_protective_orders
            if order.symbol == symbol
        ]
        live = [order for order in orders if order.is_live]
        indeterminate = [order for order in orders if order.is_indeterminate]
        qty = float(position_qty)
        total_qty = qty if total_position_qty is None else float(total_position_qty)
        if not isfinite(qty) or not isfinite(total_qty):
            raise ValueError("protective quantities must be finite")
        if abs(qty) > abs(total_qty) + _QTY_EPS or qty * total_qty < 0:
            raise ValueError("protective quantity must be within the actual position")
        ids = None if position_ids is None else tuple(sorted(set(position_ids)))
        if ids is not None and abs(total_qty) > _QTY_EPS:
            if not ids or any(not isinstance(value, str) or not value for value in ids):
                raise ValueError("nonflat protection requires authoritative position IDs")
            previous = self._position_ids.get(symbol)
            if (previous is None or not previous.intersection(ids)
                    or self._position_sides.get(symbol) != (total_qty > 0)):
                self.forget(symbol)
            self._position_ids[symbol] = frozenset(ids)
            self._position_sides[symbol] = total_qty > 0

        if abs(qty) <= _QTY_EPS:
            if abs(total_qty) <= _QTY_EPS:
                plan = self._flat_plan(symbol, live, indeterminate, entry_pending)
            else:
                # Inventory reserved by a still-pending exit is not a flat
                # lifecycle. Cancel conflicting stops without forgetting it.
                plan = ProtectivePlan(symbol, ProtectiveState.REPLACING, [
                    ProtectiveIntent(ProtectiveAction.CANCEL, symbol, "exit_inventory_reserved",
                                     cancel_order_id=order.order_id, state=ProtectiveState.REPLACING)
                    for order in live + indeterminate], note="exit inventory reserved; epoch retained")
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
                    position_ids=ids,
                )],
                note="indeterminate protective order",
            )
            return self._record(plan, record)

        long_side = qty > 0
        side = protective_side(qty)
        if ids is not None:
            stale = [order for order in live if order.position_ids is not None and
                     (not set(order.position_ids).intersection(ids) or order.side != side)]
            legacy = [order for order in live if order.position_ids is None]
            # Unattributed legacy orders cannot prove an old stop belongs to a
            # new epoch. Migration may tighten, but never silently loosen it.
            if legacy and (desired_stop is None or not isfinite(float(desired_stop))
                           or float(desired_stop) <= 0 or any(
                               _is_tighter(order.stop_price, float(desired_stop), long_side)
                               for order in legacy)):
                return self._record(ProtectivePlan(symbol, ProtectiveState.FAILED, [
                    ProtectiveIntent(ProtectiveAction.FLATTEN, symbol,
                                     "legacy_protective_position_unverified", side=side, qty=abs(qty),
                                     state=ProtectiveState.FAILED, position_ids=ids)],
                    note="legacy protection cannot be loosened without attributed identity"), record)
            replacing = stale + legacy
            if replacing:
                current = [order for order in live if order not in replacing]
                target = self._ratchet(symbol, desired_stop, current, long_side)
                if len(replacing) == 1 and not current and target is not None:
                    self._requested_stop[symbol] = target
                    reason = "position_epoch_changed" if stale else "legacy_position_identity_migration"
                    return self._record(ProtectivePlan(symbol, ProtectiveState.REPLACING, [
                        ProtectiveIntent(ProtectiveAction.REPLACE, symbol, reason, side=side,
                                         qty=abs(qty), stop_price=target,
                                         cancel_order_id=replacing[0].order_id,
                                         state=ProtectiveState.REPLACING, position_ids=ids)],
                        effective_stop=target, note=reason), record)
                return self._record(ProtectivePlan(symbol, ProtectiveState.REPLACING, [
                    ProtectiveIntent(ProtectiveAction.CANCEL, symbol, "stale_position_protection",
                                     cancel_order_id=order.order_id, state=ProtectiveState.REPLACING)
                    for order in replacing], note="cancel stale protection before rearming"), record)
        target = self._ratchet(symbol, desired_stop, live, long_side)

        if target is None:
            # No usable protective level: an open position with no stop is the
            # exact exposure SR2 exists to remove.
            plan = ProtectivePlan(
                symbol, ProtectiveState.FAILED,
                [ProtectiveIntent(
                    ProtectiveAction.FLATTEN, symbol, "no_protective_level",
                    side=side, qty=abs(qty), state=ProtectiveState.FAILED,
                    position_ids=ids,
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
            self._requested_stop[symbol] = target
            intents.append(ProtectiveIntent(
                ProtectiveAction.PLACE, symbol, "missing_protection",
                side=side, qty=abs(qty), stop_price=target,
                state=ProtectiveState.UNPROTECTED,
                position_ids=ids,
            ))
            plan = ProtectivePlan(
                symbol, ProtectiveState.UNPROTECTED, intents,
                effective_stop=None, protected_qty=0.0,
                note="protection awaiting venue confirmation",
            )
            return self._record(plan, record)

        current = live[0]
        qty_mismatch = abs(current.qty - abs(qty)) > _QTY_EPS or current.side != side
        level_moved = _is_tighter(target, current.stop_price, long_side)
        not_reduce_only = not current.reduce_only
        if qty_mismatch or level_moved or not_reduce_only:
            self._requested_stop[symbol] = target
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
                position_ids=ids,
            ))
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
                self._requested_stop.get(symbol),
                *(order.stop_price for order in live),
            )
            if value is not None and isfinite(float(value)) and float(value) > 0
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
        position_ids: Optional[Mapping[str, Iterable[str]]] = None,
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
                position_ids=None if position_ids is None else position_ids.get(symbol, ()),
            ))
        return plans
