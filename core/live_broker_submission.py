"""Order submission: intent construction, idempotent create, cancel.

Split out of core/live_broker.py (A4) — see docs/architecture_review.md.

This is a mixin, not a standalone collaborator object: ``LiveBroker``
combines ``SubmissionServiceMixin``, ``OrderReconcilerMixin``, and
``AccountSyncMixin`` via inheritance so every method still reads/writes the
same ``self`` attributes it always has. That keeps the split mechanical and
behavior-identical — a full composition redesign is a bigger change on this
money-path code and is deliberately left for a dedicated pass, not bundled
into a file-size cleanup.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from core.domain import OrderErrorCode, OrderIntent, OrderStatus, OrderSubmissionResult
from core.exchange_boundary import ExchangeBoundaryError
from core.logger import get_logger
from core.orders import TERMINAL_STATUSES, classify_order_exception, is_ambiguous_error
from core.risk_reservation import ensure_opening_reservation

# Same logger name as core.live_broker (logging.getLogger caches by name, so
# this is the identical object) -- tests use assertLogs("core.live_broker")
# and must keep catching records logged from this mixin too.
logger = get_logger("core.live_broker")

DERIVATIVE_TYPES = {"future", "futures", "swap", "margin"}


class SubmissionServiceMixin:
    """Intent construction, idempotent order creation, and cancellation.

    Expects ``self`` to carry ``portfolio``, ``market_type``, ``position_mode``,
    ``exchange_id``, ``account_id``, ``order_store``, ``exchange``,
    ``exchange_boundary``, ``event_pipeline``, ``health_assessment``, plus
    the shared plumbing on ``LiveBroker`` itself (``_retry_exchange_call``,
    ``_alert``, ``_result``, ``_now_iso``, ``_iso``, ``_bar_time``,
    ``_bar_timeframe``, ``_event_time``) and ``reconcile_order``/``sync``
    (from ``OrderReconcilerMixin``/``AccountSyncMixin``).
    """

    def submit_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: float = None,
        order_type: str = "market",
        timestamp: Any = None,
        slippage: float = 0.0,
        strategy_id: str = "Manual",
        exit_reason: str = "signal",
        time_in_force: Optional[str] = None,
        position_side: Optional[str] = None,
        reduce_only: Optional[bool] = None,
        sequence: int = 0,
        stop_loss: float = 0.0,
        zero_cost: bool = False,
    ) -> OrderSubmissionResult:
        del slippage, exit_reason, stop_loss, zero_cost
        intent = self._build_intent(
            symbol, side, qty, price, order_type, timestamp, strategy_id,
            time_in_force, position_side, reduce_only, sequence,
        )
        return self.submit_intent(intent)

    def submit_intent(self, intent: OrderIntent) -> OrderSubmissionResult:
        """Submit the exact canonical command used by every execution venue."""
        if not isinstance(intent, OrderIntent):
            raise TypeError("intent must be OrderIntent")
        existing = self.order_store.get(intent.client_order_id)
        if existing is not None and not (
            existing["status"] == OrderStatus.SUBMITTING.value
            and not existing["submission_attempted"]
        ):
            # A replay reconciles the prior venue fact. Boundary changes must
            # never cause a duplicate request for an existing intent.
            return self.reconcile_order(intent.client_order_id)
        if (
            intent.action in {"buy", "short"}
            and not intent.reduce_only
            and self.health_assessment is not None
            and not bool(getattr(self.health_assessment, "allows_new_risk", False))
        ):
            codes = ",".join(getattr(self.health_assessment, "reason_codes", []))
            return self.record_local_rejection(
                intent, f"health fail-closed: {codes or 'UNHEALTHY'}",
                OrderErrorCode.SAFETY_POLICY,
            )
        # Account-type constraints are canonical broker configuration and are
        # therefore available even before (or without) venue metadata.
        validation_error = self._validate_intent(intent)
        if validation_error:
            return self.record_local_rejection(
                intent, validation_error, OrderErrorCode.TRADING_RULE
            )
        try:
            prepared = self._retry_exchange_call(
                lambda: self.exchange_boundary.prepare(
                    intent, reference_price=intent.price
                )
            )
            intent = prepared.intent
        except ExchangeBoundaryError as exc:
            return self.record_local_rejection(
                intent, str(exc), OrderErrorCode.TRADING_RULE
            )
        except Exception as exc:
            code = classify_order_exception(exc)
            self._alert('error', 'order_preparation_failed', {
                'client_order_id': intent.client_order_id,
                'error_code': code.value,
                'retry_attempts': self.retry_max_attempts,
            })
            return self.record_local_rejection(intent, type(exc).__name__, code)
        intent, _ = ensure_opening_reservation(
            self.event_pipeline,
            intent,
            reference_price=intent.price or 0,
            occurred_at=self._event_time(intent.created_at or intent.bar_time),
            source="live",
        )
        now = self._now_iso()
        created = self.order_store.create_intent(intent, now)
        existing = self.order_store.get(intent.client_order_id)
        if not created and existing is not None:
            if existing["status"] == OrderStatus.SUBMITTING.value and not existing["submission_attempted"]:
                logger.info("Resuming pre-submit intent client_order_id=%s", intent.client_order_id)
            else:
                return self.reconcile_order(intent.client_order_id)
        if created:
            # Persisted intent is now a canonical SUBMITTING order fact.
            self._result(intent.client_order_id)
        if intent.action in {"buy", "short"} and self._has_other_active_open_order(
            intent.symbol, intent.client_order_id
        ):
            return self.record_local_rejection(
                intent,
                "active opening order blocks additional risk for this symbol",
                OrderErrorCode.SAFETY_POLICY,
            )

        self.order_store.mark_submission_attempted(intent.client_order_id, self._now_iso())
        try:
            # create_order is a non-idempotent write: a lost response after a
            # successful submission is indistinguishable from a lost request.
            # Retrying here could place a second live order, so an ambiguous
            # failure is classified UNKNOWN once and resolved out-of-band by
            # reconcile_order()'s idempotent fetch, never by resubmitting.
            payload = self.exchange.create_order(**prepared.request.as_kwargs())
        except Exception as exc:
            code = classify_order_exception(exc)
            status = OrderStatus.UNKNOWN if is_ambiguous_error(code) else OrderStatus.REJECTED
            self.order_store.transition(
                intent.client_order_id,
                status,
                self._now_iso(),
                error_code=code.value,
                error_message=type(exc).__name__,
                payload={},
            )
            logger.error(
                "Order submission failed client_order_id=%s category=%s status=%s",
                intent.client_order_id, code.value, status.value,
            )
            if status is OrderStatus.UNKNOWN:
                self._alert("critical", "order_state_unknown", {
                    "client_order_id": intent.client_order_id,
                    "operation": "submit_order", "error_code": code.value,
                    "retry_attempts": self.retry_max_attempts,
                })
            return self._result(intent.client_order_id)

        result = self._persist_exchange_payload(intent.client_order_id, payload)
        logger.info(
            "Order response persisted client_order_id=%s exchange_order_id=%s status=%s",
            result.client_order_id, result.exchange_order_id, result.status.value,
        )
        if result.status in {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED}:
            self.sync()
        return result

    def record_local_rejection(
        self,
        intent: OrderIntent,
        message: str,
        code: OrderErrorCode = OrderErrorCode.SAFETY_POLICY,
    ) -> OrderSubmissionResult:
        now = self._now_iso()
        self.order_store.create_intent(intent, now)
        current = self.order_store.get(intent.client_order_id)
        if current and current["status"] not in {status.value for status in TERMINAL_STATUSES}:
            self.order_store.transition(
                intent.client_order_id, OrderStatus.REJECTED, now,
                error_code=code.value, error_message=message,
            )
        return self._result(intent.client_order_id)

    def cancel_order(self, client_order_id: str) -> OrderSubmissionResult:
        record = self.order_store.get(client_order_id)
        if record is None:
            raise KeyError(client_order_id)
        current = OrderStatus(record["status"])
        if current in TERMINAL_STATUSES:
            return self._result(client_order_id)
        self.order_store.transition(
            client_order_id, OrderStatus.CANCEL_PENDING, self._now_iso()
        )
        try:
            payload = self.exchange.cancel_order(record["exchange_order_id"], record["symbol"])
        except Exception:
            # Cancel rejection/race is resolved by querying the exchange fact.
            return self.reconcile_order(client_order_id)
        return self._persist_exchange_payload(client_order_id, payload)

    def cancel_symbol_orders(self, symbol: str) -> None:
        for record in self.order_store.list_non_terminal():
            if record["symbol"] == symbol and record.get("exchange_order_id"):
                self.cancel_order(record["client_order_id"])

    def _build_intent(
        self, symbol: str, side: str, qty: float, price: Optional[float],
        order_type: str, timestamp: Any, strategy_id: str,
        time_in_force: Optional[str], position_side: Optional[str],
        reduce_only: Optional[bool], sequence: int,
    ) -> OrderIntent:
        held = self.portfolio.get_position(symbol)["qty"]
        derivative = self.market_type in DERIVATIVE_TYPES
        requested_reduce = reduce_only if reduce_only is not None else side in {"sell", "cover"}
        # reduceOnly is a derivatives-only exchange parameter. Spot sells still
        # clamp to owned inventory, but their canonical intent must not encode it.
        is_reduce = bool(derivative and requested_reduce)
        if side == "sell":
            qty = min(qty, max(held, 0.0))
        elif derivative and side == "cover":
            qty = min(qty, max(-held, 0.0))
        bar_time = self._bar_time if self._bar_time != "unknown" else self._iso(timestamp or self._clock())
        return OrderIntent(
            exchange=self.exchange_id, account=self.account_id, symbol=symbol,
            timeframe=self._bar_timeframe, bar_time=bar_time,
            strategy_id=strategy_id, action=side, sequence=sequence,
            requested_qty=qty, order_type=order_type.lower(), price=price,
            time_in_force=time_in_force, reduce_only=bool(is_reduce),
            position_side=position_side, position_mode=self.position_mode,
        )

    def _validate_intent(self, intent: OrderIntent) -> Optional[str]:
        if intent.requested_qty <= 0:
            return "quantity must be positive and reducible"
        if self.market_type not in DERIVATIVE_TYPES and intent.action in {"short", "cover"}:
            return f"{intent.action} is not supported for account type {self.market_type}"
        if intent.action not in {"buy", "sell", "short", "cover"}:
            return "unsupported order side"
        return None

    @staticmethod
    def _is_opening_record(record: Dict[str, Any]) -> bool:
        intent = record.get("intent") or {}
        return (
            record.get("side") in {"buy", "short"}
            and not bool(intent.get("reduce_only", False))
            and float(record.get("remaining_qty") or 0.0) > 0
        )

    def _has_other_active_open_order(
        self, symbol: str, client_order_id: Optional[str] = None
    ) -> bool:
        return any(
            record["symbol"] == symbol
            and record["client_order_id"] != client_order_id
            and self._is_opening_record(record)
            for record in self.order_store.list_non_terminal()
        )

    def has_active_open_order(self, symbol: str) -> bool:
        return self._has_other_active_open_order(symbol)

    def pending_open_notional(
        self, current_prices: Optional[Dict[str, float]] = None
    ) -> Dict[str, float]:
        return self.reservation_projection.pending_notional(current_prices)
