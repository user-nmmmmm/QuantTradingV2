"""Venue-resident protective stops inside the backtest (STR-P1-01).

Before this module the backtest discovered a stop breach *after* the bar had
closed and then exited with a market order at the **next** bar's open, while
the live path carried a real reduce-only stop-market order at the venue.  The
two therefore priced and timed the same stop differently, which is the largest
remaining execution-risk gap in
``docs/current_strategy_remediation_roadmap.md`` (§17.2).

What this simulator does is keep the *same* resident stop intent the live path
keeps - it drives :class:`core.protective_orders.ProtectiveOrderManager`, the
exact object ``live_trading/tick_orchestrator.py`` drives - and lets the
historical matcher fill it inside the bar.

Pre-registered conservative intrabar path
-----------------------------------------
An OHLC bar does not say in which order the extremes were touched, so the
simulator commits to one path and never chooses per bar:

``open -> adverse extreme -> favourable extreme -> close``

For a long that is ``open -> low -> high -> close``.  The consequences are all
the pessimistic ones:

* a stop is tested against the **low** (short: the **high**) of every bar it is
  armed for, so no breach is skipped;
* the fill is ``min(open, stop)`` for a long (``max(open, stop)`` for a short),
  so a bar that gaps straight through the level fills at the gapped open, not
  at the unreachable stop price;
* the adverse extreme is walked before the favourable one, so when a bar could
  have hit both the stop and a profitable exit, the stop wins.

Ordering inside one bar
-----------------------
The engine calls :meth:`ResidentStopSimulator.step` after the matcher has
filled the orders queued by the previous bar and before this bar's strategy
logic runs:

1. queued market/limit orders fill at the open (entries, strategy exits);
2. **step**: reconcile protection against the position that now exists, then
   match the resident stops against this bar;
3. strategy position management and entry collection run on the close.

Because reconciliation happens after (1), a position opened at this bar's open
is protected *within its own entry bar*, and a position already closed at this
bar's open has its residual stop cancelled instead of firing into the next
one.  Because the desired level is read before (3), the level in force is
always the one derived from the previous completed bar - no lookahead.

The missing-level case is deliberately not a flatten here.  Live, a position
with no protective level is an operational failure and is flattened; in a
backtest it usually means a strategy arm that does not define stops at all, so
flattening would silently rewrite the research question.  Such bars are
recorded as ``unprotected_position`` audit rows and counted, so the condition
is visible rather than either hidden or acted on.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional

import pandas as pd

from core.broker.matching import PROTECTIVE_EXIT_REASON, is_protective_stop
from core.broker.types import BacktestOrderStatus
from core.logger import get_logger
from core.protective_orders import (
    ProtectiveAction,
    ProtectiveOrder,
    ProtectiveOrderManager,
    ProtectiveState,
)
from core.runtime import MarketDataSlice

logger = get_logger(__name__)

#: The one intrabar path the backtest is allowed to assume, recorded in every
#: audit row so a report can never be read without it.
CONSERVATIVE_BAR_PATH = "open->adverse_extreme->favourable_extreme->close"

#: Broker order states that still count as working protection.
_WORKING_STATUSES = {
    BacktestOrderStatus.CREATED,
    BacktestOrderStatus.SUBMITTED,
    BacktestOrderStatus.PARTIALLY_FILLED,
}


class ResidentStopSimulator:
    """Keeps one reduce-only stop order per open position and matches it in-bar.

    Parameters mirror the live switch: ``enabled`` is
    ``protective_orders.backtest_resident`` and turns the whole mechanism off,
    leaving the legacy close-based ``hard_stop_exit`` path as the only stop.
    """

    def __init__(
        self,
        broker: Any,
        strategies: Mapping[str, Any],
        *,
        enabled: bool = True,
        manager: Optional[ProtectiveOrderManager] = None,
    ) -> None:
        self.broker = broker
        self.strategies = strategies
        self.enabled = bool(enabled)
        self.manager = manager or ProtectiveOrderManager()
        self.audit: List[Dict[str, Any]] = []
        #: order id -> the level that order enforces, so a fill can be audited
        #: against the stop that fired rather than against its fill price.
        self._levels: Dict[str, float] = {}
        self.triggered_stops = 0
        self.unprotected_position_bars = 0

    # ------------------------------------------------------------------ API

    def step(self, event: MarketDataSlice, *, bar_index: int = -1) -> List[Dict]:
        """Reconcile protection, then match it against this bar. Returns fills."""
        if not self.enabled:
            return []
        bars = dict(event.bars)
        self._sync(bars, timestamp=event.timestamp, bar_index=bar_index)
        trades = self.broker.process_orders(bars, order_filter=is_protective_stop)
        for trade in trades:
            self.triggered_stops += 1
            level = self._levels.get(str(trade.get("order_id")))
            self.audit.append({
                "timestamp": event.timestamp,
                "bar_index": bar_index,
                "symbol": trade["symbol"],
                "state": ProtectiveState.ARMED.value,
                "action": "fill",
                "reason": "stop_triggered",
                "side": trade["side"],
                "qty": trade["qty"],
                "stop_price": level,
                # The path assumption in one number: a bar that gapped through
                # the level fills at the open, never at the unreachable stop.
                "trigger_price": trade.get("theoretical_price"),
                "fill_price": trade["fill_price"],
                "effective_stop": level,
                "protected_qty": trade["qty"],
                "cancel_order_id": None,
                "note": CONSERVATIVE_BAR_PATH,
            })
        return trades

    def cancel_all(self, symbols: Optional[Iterable[str]] = None) -> int:
        """Drop resident protection because another path is closing the position."""
        cancelled = self.broker.cancel_protective_stops(symbols)
        if cancelled:
            for symbol in (symbols or []):
                self.manager.forget(symbol)
            if symbols is None:
                self.manager = ProtectiveOrderManager()
        return cancelled

    # -------------------------------------------------------------- internals

    def _sync(self, bars: Dict[str, pd.Series], *, timestamp: Any, bar_index: int) -> None:
        portfolio = self.broker.portfolio
        venue_orders = self._resident_orders()
        symbols = set(portfolio.positions) | {order.symbol for order in venue_orders}
        for symbol in sorted(symbols):
            qty = float(portfolio.get_position(symbol).get("qty", 0.0))
            has_resident = any(order.symbol == symbol for order in venue_orders)
            if qty == 0.0 and not has_resident:
                # Flat with nothing armed: there is no protection question to
                # answer, and recording one row per flat symbol per bar would
                # bury the real transitions in stop_order_audit.csv.
                self.manager.forget(symbol)
                continue
            desired = self._desired_stop(symbol)
            if qty != 0.0 and desired is None and not has_resident:
                # No level to enforce and nothing already armed: record it and
                # leave the position to the legacy path rather than flattening
                # a research arm that never asked for a stop.
                self.unprotected_position_bars += 1
                self._record(symbol, timestamp, bar_index, {
                    "state": ProtectiveState.UNPROTECTED.value,
                    "action": ProtectiveAction.NONE.value,
                    "reason": "no_protective_level",
                    "side": None, "qty": abs(qty), "stop_price": None,
                    "cancel_order_id": None, "effective_stop": None,
                    "protected_qty": 0.0, "note": "unprotected_position",
                })
                continue
            plan = self.manager.evaluate(
                symbol=symbol,
                position_qty=qty,
                desired_stop=desired,
                open_protective_orders=venue_orders,
                entry_pending=bool(self.broker.has_active_open_order(symbol)),
                record=False,
            )
            for row in plan.to_rows():
                self._record(symbol, timestamp, bar_index, row)
            for intent in plan.intents:
                self._apply(intent, bars=bars, timestamp=timestamp)

    def _apply(self, intent, *, bars: Dict[str, pd.Series], timestamp: Any) -> None:
        symbol = intent.symbol
        if intent.action in (ProtectiveAction.CANCEL, ProtectiveAction.REPLACE):
            self._cancel_order(intent.cancel_order_id, timestamp)
        if intent.action in (ProtectiveAction.PLACE, ProtectiveAction.REPLACE):
            if intent.stop_price is None or intent.qty <= 0:
                return
            placed = self.broker.submit_order(
                symbol,
                intent.side,
                intent.qty,
                price=float(intent.stop_price),
                order_type="stop",
                # One microsecond before the bar, so the matcher treats it as
                # already resting when the bar opens: this is what makes the
                # entry bar itself protected.
                timestamp=pd.Timestamp(timestamp) - pd.Timedelta(microseconds=1),
                strategy_id=self._owning_strategy(symbol),
                exit_reason=PROTECTIVE_EXIT_REASON,
            )
            self._levels[str(placed.id)] = float(intent.stop_price)
        if intent.action is ProtectiveAction.FLATTEN:
            # Reachable only for an indeterminate order, which the historical
            # matcher never produces; keep the fail-closed branch anyway so
            # backtest and live cannot silently diverge if that changes.
            logger.warning(
                "Protective flatten in backtest: %s (%s)", symbol, intent.reason
            )
            bar = bars.get(symbol)
            price = float(bar["close"]) if bar is not None else None
            self.broker.submit_order(
                symbol, intent.side, intent.qty, price=price,
                order_type="market", timestamp=timestamp,
                strategy_id=self._owning_strategy(symbol),
                exit_reason="unprotected_flatten",
            )

    def _cancel_order(self, order_id: Optional[str], timestamp: Any) -> None:
        if not order_id:
            return
        for bucket_name in ("pending_orders", "active_orders"):
            bucket = getattr(self.broker, bucket_name)
            for order in list(bucket):
                if order.id != order_id:
                    continue
                bucket.remove(order)
                self.broker._set_status(
                    order, BacktestOrderStatus.CANCELED, timestamp
                )

    def _resident_orders(self) -> List[ProtectiveOrder]:
        """The broker's own order book, read the way live reads the venue."""
        orders = []
        for order in list(self.broker.pending_orders) + list(self.broker.active_orders):
            if not is_protective_stop(order) or order.status not in _WORKING_STATUSES:
                continue
            orders.append(ProtectiveOrder(
                order_id=str(order.id),
                symbol=str(order.symbol),
                side=str(order.side),
                qty=float(order.remaining_qty or order.qty),
                stop_price=float(order.price or 0.0),
                status=(
                    "partially_filled"
                    if order.status is BacktestOrderStatus.PARTIALLY_FILLED
                    else "open"
                ),
                reduce_only=True,
            ))
        return orders

    def _owning_strategy(self, symbol: str) -> str:
        lot_book = self.broker.portfolio.lot_books.get(symbol)
        if lot_book is not None:
            for lot in lot_book.open_lots:
                return str(lot.strategy_id)
        return "ProtectiveStop"

    def _desired_stop(self, symbol: str) -> Optional[float]:
        """The level the owning strategy wants enforced, same rule as live."""
        for strategy in self.strategies.values():
            context = getattr(strategy, "context", {}).get(symbol) or {}
            stop = context.get("effective_stop", context.get("stop_loss"))
            if stop:
                return float(stop)
        return None

    def _record(
        self, symbol: str, timestamp: Any, bar_index: int, row: Mapping[str, Any],
    ) -> None:
        if row.get("action") == ProtectiveAction.NONE.value and row.get(
            "reason"
        ) in {"in_sync", ""}:
            # An unchanged stop every bar would bury the real transitions.
            return
        self.audit.append({
            "timestamp": timestamp,
            "bar_index": bar_index,
            "trigger_price": None,
            "fill_price": None,
            **{key: value for key, value in row.items() if key != "symbol"},
            "symbol": symbol,
        })
