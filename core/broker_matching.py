"""Order queue management and bar-by-bar price/quantity matching.

Split out of core/broker.py (A4) — see docs/architecture_review.md.

This is a mixin, not a standalone collaborator object: ``Broker`` combines
``MatchingMixin``, ``FillServiceMixin``, ``FinancingMixin``, and
``LiquidationMixin`` via inheritance so every method still reads/writes the
same ``self`` attributes it always has. That keeps the split mechanical and
behavior-identical — a full composition redesign is a bigger change on
money-path-adjacent code and is deliberately left for a dedicated pass, not
bundled into a file-size cleanup.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional

import pandas as pd

from core.broker_types import BacktestOrderStatus, Order, OrderType, TimeInForce
from core.domain import OrderIntent
from core.events import OrderEvent
from core.logger import get_logger
from core.risk_reservation import ensure_opening_reservation

logger = get_logger(__name__)

#: ``exit_reason`` carried by every venue-resident protective stop, in the
#: backtest exactly as in live (see ``core/protective_orders.py``). Matching,
#: forced liquidation and end-of-backtest all key off this so that a resident
#: stop is never filled twice or filled by a path that is not the stop.
PROTECTIVE_EXIT_REASON = "protective_stop"


def is_protective_stop(order: Order) -> bool:
    """True for a venue-resident protective stop order (SR2-5 / STR-P1-01)."""
    return (
        order.order_type is OrderType.STOP
        and str(order.exit_reason) == PROTECTIVE_EXIT_REASON
    )


class MatchingMixin:
    """Order submission, per-bar matching, and order-book bookkeeping.

    Expects ``self`` to carry ``event_pipeline``, ``exchange_id``,
    ``account_id``, ``timeframe``, ``_intent_sequence``, ``pending_orders``,
    ``active_orders``, ``last_prices``, ``max_participation_rate``,
    ``execution_audit``, ``reservation_projection``, and
    ``_execute_trade`` (from ``FillServiceMixin``).
    """

    def submit_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: Optional[float] = None,
        order_type: str = "market",  # keeping string for compatibility, convert to Enum
        timestamp: Any = None,
        slippage: float = 0.0,
        strategy_id: str = "Manual",
        exit_reason: str = "signal",
        time_in_force: str = "GTC",
        expire_time: Any = None,
        sequence: Optional[int] = None,
        _intent: Optional[OrderIntent] = None,
        stop_loss: float = 0.0,
        zero_cost: bool = False,
        risk_action_id: Optional[str] = None,
    ) -> Order:
        """
        提交订单（进入撮合队列）。

        参数：
        - order_type：'market' / 'limit' / 'stop'（为兼容旧接口，这里仍接受字符串并映射到 Enum）
        - price：限价/止损单必填；市价单可为 None
        - timestamp：信号产生时间（通常为 bar i 的收盘时间），用于日志与反向检查

        行为：
        - 仅做基本参数校验与结构化封装，加入 pending_orders
        - 实际撮合发生在 process_orders（由引擎在每根 bar 调用）
        """
        # Map string to Enum
        otype_map = {
            "market": OrderType.MARKET,
            "limit": OrderType.LIMIT,
            "stop": OrderType.STOP,
        }
        otype = otype_map.get(order_type.lower(), OrderType.MARKET)
        resolved_sequence = self._intent_sequence if sequence is None else sequence
        if sequence is None and _intent is None:
            self._intent_sequence += 1
        intent = _intent or OrderIntent(
            exchange=self.exchange_id, account=self.account_id, symbol=symbol,
            timeframe=self.timeframe, bar_time=self._iso_time(timestamp),
            strategy_id=strategy_id, action=side, sequence=resolved_sequence,
            requested_qty=qty, order_type=order_type.lower(), price=price,
            time_in_force=time_in_force,
        )
        intent, intent_envelope = ensure_opening_reservation(
            self.event_pipeline,
            intent,
            reference_price=price or 0,
            occurred_at=self._event_time(timestamp),
            source="backtest",
        )

        order = Order(
            symbol=symbol,
            side=side,
            qty=qty,
            order_type=otype,
            price=price,
            timestamp=timestamp,
            slippage=slippage,
            strategy_id=strategy_id,
            submitted_date=(
                pd.Timestamp(timestamp).date() if timestamp is not None else None
            ),
            exit_reason=exit_reason,
            status=BacktestOrderStatus.CREATED,
            remaining_qty=max(qty, 0.0),
            time_in_force=TimeInForce(time_in_force.upper()),
            expire_time=expire_time,
            id=intent.client_order_id,
            intent=intent,
            last_event_id=str(intent_envelope.event_id),
            stop_loss=stop_loss,
            zero_cost=zero_cost,
            risk_action_id=risk_action_id,
        )
        if qty <= 0:
            logger.warning(
                "Order rejected: quantity must be positive for %s %s %.8f",
                symbol,
                side,
                qty,
            )
            self._set_status(order, BacktestOrderStatus.REJECTED, timestamp)
            return order

        if otype in [OrderType.LIMIT, OrderType.STOP] and price is None:
            logger.warning(
                "Order rejected: price is required for %s order on %s",
                order_type,
                symbol,
            )
            self._set_status(order, BacktestOrderStatus.REJECTED, timestamp)
            return order

        self.pending_orders.append(order)
        self._publish_order_event(order, timestamp)
        return order

    def submit_intent(self, intent: OrderIntent) -> Order:
        """Submit the exact canonical command without rebuilding its identity."""
        if not isinstance(intent, OrderIntent):
            raise TypeError("intent must be OrderIntent")
        return self.submit_order(
            symbol=intent.symbol,
            side=intent.action,
            qty=intent.requested_qty,
            price=intent.price,
            order_type=intent.order_type,
            timestamp=pd.Timestamp(intent.bar_time),
            strategy_id=intent.strategy_id,
            time_in_force=intent.time_in_force or "GTC",
            sequence=intent.sequence,
            _intent=intent,
        )

    @staticmethod
    def _event_time(value: Any) -> datetime:
        if value is None or value == "unknown":
            return datetime.now(timezone.utc)
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        else:
            timestamp = timestamp.tz_convert("UTC")
        return timestamp.to_pydatetime()

    @classmethod
    def _iso_time(cls, value: Any) -> str:
        return cls._event_time(value).isoformat().replace("+00:00", "Z")

    def _publish_order_event(self, order: Order, occurred_at: Any) -> None:
        if order.intent is None:
            return
        payload = OrderEvent(
            client_order_id=order.id,
            status=order.status,
            requested_qty=order.qty,
            filled_qty=order.filled_qty,
            remaining_qty=order.remaining_qty,
            average_fill_price=order.avg_fill_price or None,
        )
        envelope = self.event_pipeline.publish(
            payload,
            occurred_at=self._event_time(occurred_at),
            correlation_id=order.intent.correlation_id,
            causation_id=order.last_event_id,
            idempotency_key=(
                f"{order.id}:{order.status.value}:"
                f"{order.filled_qty:.12f}:{order.remaining_qty:.12f}"
            ),
            account_id=order.intent.account,
            symbol=order.symbol,
            timeframe=order.intent.timeframe,
            source="backtest",
        )
        order.last_event_id = str(envelope.event_id)

    def _set_status(
        self, order: Order, status: Any, occurred_at: Any = None
    ) -> None:
        order.status = status
        self._publish_order_event(order, occurred_at)

    def process_orders(
        self,
        current_bar: Dict[str, pd.Series],
        *,
        order_filter: Optional[Callable[[Order], bool]] = None,
    ) -> List[Dict]:
        """Match eligible orders against real bars with a shared volume budget.

        ``order_filter`` restricts this pass to the orders it accepts; every
        other working order is carried forward untouched, keeping its place in
        the book and its share of the next pass's volume budget. The backtest
        uses it to match venue-resident protective stops at their own point in
        the bar without giving the rest of the book a second bite at the same
        bar's liquidity (STR-P1-01).
        """
        executed_trades: List[Dict] = []
        next_active_orders: List[Order] = []
        for symbol, bar in current_bar.items():
            mark = bar.get("mark_price", bar.get("close", bar.get("open")))
            if mark is not None and pd.notna(mark):
                self.last_prices[symbol] = float(mark)
        for order in self.pending_orders:
            self._set_status(order, BacktestOrderStatus.SUBMITTED, order.timestamp)
            self.active_orders.append(order)
        self.pending_orders = []

        volume_budget = {
            symbol: max(float(bar.get("volume", 0.0)), 0.0) * self.max_participation_rate
            for symbol, bar in current_bar.items()
        }

        for order in self.active_orders:
            if order_filter is not None and not order_filter(order):
                next_active_orders.append(order)
                continue
            bar_data = current_bar.get(order.symbol)
            if bar_data is None:
                next_active_orders.append(order)
                continue
            current_time = bar_data.name
            if order.timestamp is not None and current_time <= order.timestamp:
                next_active_orders.append(order)
                continue
            if order.expire_time is not None and current_time > order.expire_time:
                self._set_status(order, BacktestOrderStatus.EXPIRED, current_time)
                continue
            if (
                order.time_in_force == TimeInForce.DAY
                and order.submitted_date is not None
                and current_time.date() > order.submitted_date
            ):
                self._set_status(order, BacktestOrderStatus.EXPIRED, current_time)
                continue

            open_price = float(bar_data["open"])
            high_price = float(bar_data["high"])
            low_price = float(bar_data["low"])
            limit_price = float(order.price) if order.price is not None else None
            exec_price = None
            is_maker = False
            if order.order_type == OrderType.MARKET:
                exec_price = open_price
            elif order.order_type == OrderType.LIMIT:
                if limit_price is None:
                    self._set_status(order, BacktestOrderStatus.REJECTED, current_time)
                    continue
                if order.side in {"buy", "cover"} and low_price <= limit_price:
                    exec_price = open_price if open_price <= limit_price else limit_price
                    is_maker = open_price > limit_price
                elif order.side in {"sell", "short"} and high_price >= limit_price:
                    exec_price = open_price if open_price >= limit_price else limit_price
                    is_maker = open_price < limit_price
            elif order.order_type == OrderType.STOP:
                if limit_price is None:
                    self._set_status(order, BacktestOrderStatus.REJECTED, current_time)
                    continue
                if order.side in {"buy", "cover"} and high_price >= limit_price:
                    exec_price = max(open_price, limit_price)
                elif order.side in {"sell", "short"} and low_price <= limit_price:
                    exec_price = min(open_price, limit_price)

            if exec_price is None:
                if order.time_in_force in {TimeInForce.IOC, TimeInForce.FOK}:
                    self._set_status(order, BacktestOrderStatus.CANCELED, current_time)
                else:
                    next_active_orders.append(order)
                continue

            available = volume_budget.get(order.symbol, 0.0)
            requested = order.remaining_qty
            if order.time_in_force == TimeInForce.FOK and available < requested:
                self._audit_order(order, current_time, "rejected", "fok_volume")
                self._set_status(order, BacktestOrderStatus.CANCELED, current_time)
                continue
            fill_qty = min(requested, available)
            if fill_qty <= 0:
                if order.time_in_force in {TimeInForce.IOC, TimeInForce.FOK}:
                    self._set_status(order, BacktestOrderStatus.CANCELED, current_time)
                else:
                    next_active_orders.append(order)
                continue

            if fill_qty < requested:
                self._audit_order(
                    order,
                    current_time,
                    "partial_fill",
                    "participation_limit",
                    requested_qty=requested,
                    fill_qty=fill_qty,
                )

            trade = self._execute_trade(
                order, exec_price, current_time, fill_qty,
                float(bar_data.get("volume", 0.0)), is_maker=is_maker,
                bar_context=bar_data,
            )
            if trade is None:
                if order.status is BacktestOrderStatus.SUBMITTING:
                    self._set_status(order, BacktestOrderStatus.REJECTED, current_time)
                continue
            executed_trades.append(trade)
            volume_budget[order.symbol] = max(available - trade["qty"], 0.0)
            previous_filled = order.filled_qty
            order.filled_qty += trade["qty"]
            order.remaining_qty = max(order.qty - order.filled_qty, 0.0)
            order.avg_fill_price = (
                (order.avg_fill_price * previous_filled + trade["fill_price"] * trade["qty"])
                / order.filled_qty
            )
            if order.remaining_qty <= 1e-12:
                self._set_status(order, BacktestOrderStatus.FILLED, current_time)
            elif order.time_in_force == TimeInForce.IOC:
                self._set_status(order, BacktestOrderStatus.CANCELED, current_time)
            else:
                self._set_status(order, BacktestOrderStatus.PARTIALLY_FILLED, current_time)
                next_active_orders.append(order)

        self.active_orders = next_active_orders
        return executed_trades

    def _audit_order(
        self,
        order: Order,
        timestamp: Any,
        outcome: str,
        reason: str,
        **details: Any,
    ) -> None:
        self.execution_audit.append({
            "timestamp": timestamp,
            "order_id": order.id,
            "symbol": order.symbol,
            "side": order.side,
            "outcome": outcome,
            "reason": reason,
            **details,
        })

    def has_active_open_order(self, symbol: str) -> bool:
        return any(
            order.symbol == symbol
            and order.side in {"buy", "short"}
            and order.remaining_qty > 0
            for order in self.pending_orders + self.active_orders
        )

    def pending_open_notional(
        self, current_prices: Optional[Dict[str, float]] = None
    ) -> Dict[str, float]:
        return self.reservation_projection.pending_notional(current_prices)

    def cancel_protective_stops(self, symbols: Optional[Iterable[str]] = None) -> int:
        """Cancel venue-resident protective stops, optionally for some symbols.

        Used wherever another path becomes the authoritative close (forced
        liquidation, end-of-backtest), so a resident stop can never fill on top
        of a close that has already happened.
        """
        wanted = None if symbols is None else set(symbols)

        def _targeted(order: Order) -> bool:
            return is_protective_stop(order) and (
                wanted is None or order.symbol in wanted
            )

        cancelled = [
            order for order in self.pending_orders + self.active_orders
            if _targeted(order)
        ]
        self.pending_orders = [o for o in self.pending_orders if not _targeted(o)]
        self.active_orders = [o for o in self.active_orders if not _targeted(o)]
        for order in cancelled:
            self._set_status(order, BacktestOrderStatus.CANCELED)
        return len(cancelled)

    def cancel_symbol_orders(self, symbol: str) -> int:
        """
        Cancel all pending and active orders for a given symbol.
        Returns the number of orders cancelled.
        Called on state switch to prevent stale limit orders from filling
        in the wrong market regime.
        """
        before = len(self.pending_orders) + len(self.active_orders)

        cancelled = [o for o in self.pending_orders if o.symbol == symbol]
        self.pending_orders = [o for o in self.pending_orders if o.symbol != symbol]
        cancelled.extend(o for o in self.active_orders if o.symbol == symbol)
        self.active_orders = [o for o in self.active_orders if o.symbol != symbol]

        for o in cancelled:
            self._set_status(o, BacktestOrderStatus.CANCELED)

        n = before - len(self.pending_orders) - len(self.active_orders)
        if n > 0:
            logger.info(
                "Cancelled %s stale order(s) for %s during state switch",
                n,
                symbol,
            )
        return n
