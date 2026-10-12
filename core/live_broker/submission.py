"""Order submission: intent construction, idempotent create, cancel.

Split out of core/live_broker.py (A4) — see docs/architecture_review.md.

It is a collaborator held by ``LiveBroker`` (B4, ADR 0018): it reads and writes the shared state through its ``host`` reference and calls back into the host's own methods, so overrides and ``patch.object`` on the host still intercept it. See core/live_broker/submission.py for the full explanation.
"""
from __future__ import annotations
from core.trading.order_latency import record_order_call

import math
from dataclasses import replace
from typing import Any, Dict, Optional

from core.foundation.domain import OrderErrorCode, OrderIntent, OrderStatus, OrderSubmissionResult
from core.exchange import ExchangeBoundaryError
from core.foundation.logger import get_logger
from core.trading.orders import TERMINAL_STATUSES, classify_order_exception, is_ambiguous_error
from core.risk.reservation import OpeningRiskRejected, ensure_opening_reservation
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.live_broker import LiveBroker

# Same logger name as core.live_broker (logging.getLogger caches by name, so
# this is the identical object) -- tests use assertLogs("core.live_broker")
# and must keep catching records logged from this module too.
logger = get_logger("core.live_broker")

DERIVATIVE_TYPES = {"future", "futures", "swap", "margin"}


class SubmissionService:
    """Intent construction, idempotent order creation, and cancellation.

    Needs from the host: ``portfolio``, ``market_type``, ``position_mode``,
    ``exchange_id``, ``account_id``, ``order_store``, ``exchange``,
    ``exchange_boundary``, ``event_pipeline``, ``health_assessment``, plus
    the shared plumbing on ``LiveBroker`` itself (``_retry_exchange_call``,
    ``_alert``, ``_result``, ``_now_iso``, ``_iso``, ``_bar_time``,
    ``_bar_timeframe``, ``_event_time``) and ``reconcile_order``/``sync``
    (from ``OrderReconciler``/``AccountSync``).
    """

    def __init__(self, host: "LiveBroker") -> None:
        self._host = host

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
        reference_price: Optional[float] = None,
        trigger_price: Optional[float] = None,
        risk_action_id: Optional[str] = None,
        approved_risk_amount: Optional[float] = None,
        signal_id: Optional[str] = None,
        causation_id: Optional[str] = None,
    ) -> OrderSubmissionResult:
        host = self._host
        del slippage, zero_cost
        canceled_protection = False
        if side in {"sell", "cover"} and order_type != "stop":
            for pending in host.order_store.list_non_terminal():
                if pending["symbol"] == symbol and (pending.get("intent") or {}).get("order_type") == "stop":
                    canceled_protection = True
                    host.cancel_order(pending["client_order_id"])
            # A stop may fill while cancellation is in flight. Recompute the
            # executable inventory from the venue before constructing the exit.
            if canceled_protection:
                sync_result = host.sync()
                # Full-account provenance/discrepancies block new entries,
                # while this explicit outcome still carries fresh inventory.
                # Network/invalid-balance failures never permit the exit to
                # guess quantity from an old snapshot.
                if not sync_result and getattr(sync_result, "error", None) != "account_reconciliation_failed":
                    qty = 0.0
        intent = host._build_intent(
            symbol, side, qty, price, order_type, timestamp, strategy_id,
            time_in_force, position_side, reduce_only, sequence,
            reference_price, trigger_price, stop_loss, exit_reason, risk_action_id,
            approved_risk_amount,
            signal_id, causation_id,
        )
        return host.submit_intent(intent)

    def submit_intent(self, intent: OrderIntent) -> OrderSubmissionResult:
        """Submit the exact canonical command used by every execution venue."""
        host = self._host
        if not isinstance(intent, OrderIntent):
            raise TypeError("intent must be OrderIntent")
        existing = host.order_store.get(intent.client_order_id)
        if existing is not None and not (
            existing["status"] == OrderStatus.SUBMITTING.value
            and not existing["submission_attempted"]
        ):
            # A replay reconciles the prior venue fact. Boundary changes must
            # never cause a duplicate request for an existing intent.
            return host.reconcile_order(intent.client_order_id)
        if (
            intent.action in {"buy", "short"}
            and not intent.reduce_only
            and host.health_assessment is not None
            and not bool(getattr(host.health_assessment, "allows_new_risk", False))
        ):
            codes = ",".join(getattr(host.health_assessment, "reason_codes", []))
            return host.record_local_rejection(
                intent, f"health fail-closed: {codes or 'UNHEALTHY'}",
                OrderErrorCode.SAFETY_POLICY,
            )
        # Account-type constraints are canonical broker configuration and are
        # therefore available even before (or without) venue metadata.
        validation_error = host._validate_intent(intent)
        if validation_error:
            return host.record_local_rejection(
                intent, validation_error, OrderErrorCode.TRADING_RULE
            )
        try:
            prepared = host._retry_exchange_call(
                lambda: host.exchange_boundary.prepare(
                    intent, reference_price=intent.reference_price or intent.price
                )
            )
            intent = prepared.intent
            if host.require_resident_protection and intent.action in {"buy", "short"}:
                market = prepared.market
                conditional = "stop_market" if market and market.is_derivative else "stop_loss"
                if "stop" not in host.exchange_boundary.capabilities.order_types or market is None or conditional not in market.order_types:
                    raise ExchangeBoundaryError("verified resident stop capability is required before new risk")
        except ExchangeBoundaryError as exc:
            return host.record_local_rejection(
                intent, str(exc), OrderErrorCode.TRADING_RULE
            )
        except Exception as exc:
            code = classify_order_exception(exc)
            host._alert('error', 'order_preparation_failed', {
                'client_order_id': intent.client_order_id,
                'error_code': code.value,
                'retry_attempts': host.retry_max_attempts,
            })
            return host.record_local_rejection(intent, type(exc).__name__, code)
        try:
            intent, _ = ensure_opening_reservation(
                host.event_pipeline, intent,
                reference_price=intent.reference_price or intent.price or 0,
                occurred_at=host._event_time(intent.created_at or intent.bar_time),
                source="live", guard=getattr(host, "opening_risk_guard", None),
            )
        except OpeningRiskRejected as exc:
            return host.record_local_rejection(intent, str(exc), OrderErrorCode.SAFETY_POLICY)
        now = host._now_iso()
        created = host.order_store.create_intent(intent, now)
        existing = host.order_store.get(intent.client_order_id)
        if not created and existing is not None:
            if existing["status"] == OrderStatus.SUBMITTING.value and not existing["submission_attempted"]:
                logger.info("Resuming pre-submit intent client_order_id=%s", intent.client_order_id)
            else:
                return host.reconcile_order(intent.client_order_id)
        if created:
            # Persisted intent is now a canonical SUBMITTING order fact.
            host._result(intent.client_order_id)
        if intent.action in {"buy", "short"} and host._has_other_active_open_order(
            intent.symbol, intent.client_order_id
        ):
            return host.record_local_rejection(
                intent,
                "active opening order blocks additional risk for this symbol",
                OrderErrorCode.SAFETY_POLICY,
            )

        host.order_store.mark_submission_attempted(intent.client_order_id, host._now_iso())
        try:
            # create_order is a non-idempotent write: a lost response after a
            # successful submission is indistinguishable from a lost request.
            # Retrying here could place a second live order, so an ambiguous
            # failure is classified UNKNOWN once and resolved out-of-band by
            # reconcile_order()'s idempotent fetch, never by resubmitting.
            payload = record_order_call(host, "submit", host.exchange.create_order,
                telemetry_context={"client_order_id": intent.client_order_id,
                    "symbol": intent.symbol, "decision_at": intent.created_at,
                    "side": intent.action, "reference_price": float(intent.reference_price)
                        if intent.reference_price is not None else None,
                    "requested_qty": float(intent.requested_qty)},
                                              **prepared.request.as_kwargs())
        except Exception as exc:
            code = classify_order_exception(exc)
            status = OrderStatus.UNKNOWN if is_ambiguous_error(code) else OrderStatus.REJECTED
            host.order_store.transition(
                intent.client_order_id,
                status,
                host._now_iso(),
                error_code=code.value,
                error_message=type(exc).__name__,
                payload={},
            )
            logger.error(
                "Order submission failed client_order_id=%s category=%s status=%s",
                intent.client_order_id, code.value, status.value,
            )
            if status is OrderStatus.UNKNOWN:
                host._alert("critical", "order_state_unknown", {
                    "client_order_id": intent.client_order_id,
                    "operation": "submit_order", "error_code": code.value,
                    "retry_attempts": host.retry_max_attempts,
                })
            return host._result(intent.client_order_id)

        result = host._persist_exchange_payload(intent.client_order_id, payload)
        logger.info(
            "Order response persisted client_order_id=%s exchange_order_id=%s status=%s",
            result.client_order_id, result.exchange_order_id, result.status.value,
        )
        if result.status in {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED}:
            host.sync()
        return result

    def record_local_rejection(
        self,
        intent: OrderIntent,
        message: str,
        code: OrderErrorCode = OrderErrorCode.SAFETY_POLICY,
    ) -> OrderSubmissionResult:
        host = self._host
        now = host._now_iso()
        host.order_store.create_intent(intent, now)
        current = host.order_store.get(intent.client_order_id)
        if current and current["status"] not in {status.value for status in TERMINAL_STATUSES}:
            host.order_store.transition(
                intent.client_order_id, OrderStatus.REJECTED, now,
                error_code=code.value, error_message=message,
            )
        return host._result(intent.client_order_id)

    def cancel_order(self, client_order_id: str) -> OrderSubmissionResult:
        host = self._host
        record = host.order_store.get(client_order_id)
        if record is None:
            raise KeyError(client_order_id)
        current = OrderStatus(record["status"])
        if current in TERMINAL_STATUSES:
            return host._result(client_order_id)
        if current is OrderStatus.UNKNOWN:
            resolved = host.reconcile_order(client_order_id)
            if resolved.status is OrderStatus.UNKNOWN or resolved.status in TERMINAL_STATUSES:
                return resolved
            record = host.order_store.get(client_order_id)
        host.order_store.transition(
            client_order_id, OrderStatus.CANCEL_PENDING, host._now_iso()
        )
        try:
            payload = record_order_call(host, "cancel", host.exchange.cancel_order,
                record["exchange_order_id"], record["symbol"],
                telemetry_context={"client_order_id": client_order_id, "symbol": record["symbol"]})
        except Exception:
            # Cancel rejection/race is resolved by querying the exchange fact.
            return host.reconcile_order(client_order_id)
        return host._persist_exchange_payload(client_order_id, payload)

    def cancel_symbol_orders(self, symbol: str) -> None:
        host = self._host
        for record in host.order_store.list_non_terminal():
            if record["symbol"] == symbol and record["side"] in {"buy", "short"} and record.get("exchange_order_id"):
                host.cancel_order(record["client_order_id"])

    def _build_intent(
        self, symbol: str, side: str, qty: float, price: Optional[float],
        order_type: str, timestamp: Any, strategy_id: str,
        time_in_force: Optional[str], position_side: Optional[str],
        reduce_only: Optional[bool], sequence: int,
        reference_price: Optional[float] = None,
        trigger_price: Optional[float] = None,
        stop_loss: float = 0.0,
        exit_reason: Optional[str] = None,
        risk_action_id: Optional[str] = None,
        approved_risk_amount: Optional[float] = None,
        signal_id: Optional[str] = None,
        causation_id: Optional[str] = None,
    ) -> OrderIntent:
        host = self._host
        held = host.portfolio.get_position(symbol)["qty"]
        derivative = host.market_type in DERIVATIVE_TYPES
        requested_reduce = reduce_only if reduce_only is not None else side in {"sell", "cover"}
        # reduceOnly is a derivatives-only exchange parameter. Spot sells still
        # clamp to owned inventory, but their canonical intent must not encode it.
        is_reduce = bool((derivative or host.market_type == "margin") and requested_reduce)
        if side == "sell":
            qty = min(qty, max(held, 0.0))
        elif (derivative or host.market_type == "margin") and side == "cover":
            qty = min(qty, max(-held, 0.0))
        if side in {"sell", "cover"}:
            # Spot margin offers no native reduce-only guarantee. Reserve inventory
            # against every unresolved exit, including unknown and pending cancel.
            pending = sum(float(row.get("remaining_qty") or 0) for row in host.order_store.list_non_terminal()
                          if row["symbol"] == symbol and row["side"] == side)
            qty = min(qty, max(abs(held) - pending, 0.0))
        raw_bar_time = host._bar_time if host._bar_time != "unknown" else host._iso(timestamp or host._clock())
        point = host._event_time(raw_bar_time)
        bar_time = point.isoformat().replace("+00:00", "Z")
        intent = OrderIntent(
            exchange=host.exchange_id, account=host.account_id, symbol=symbol,
            timeframe=host._bar_timeframe, bar_time=bar_time,
            strategy_id=strategy_id, action=side, sequence=sequence,
            requested_qty=qty, order_type=order_type.lower(), price=price,
            time_in_force=time_in_force or "GTC", reduce_only=bool(is_reduce),
            position_side=position_side, position_mode=host.position_mode,
            reference_price=reference_price if reference_price is not None else price,
            trigger_price=(trigger_price if trigger_price is not None else price) if order_type.lower() == "stop" else trigger_price,
            initial_stop=stop_loss if stop_loss else None,
            exit_reason=exit_reason if side in {"sell", "cover"} else None,
            risk_action_id=risk_action_id,
            approved_risk_amount=approved_risk_amount,
            signal_id=signal_id, causation_id=causation_id,
        )
        # Earlier live adapters used naive UTC or +00:00 bar strings in the
        # exchange-facing ID. Normalizing new commands must not make an old
        # durable command look new after an upgrade/restart. Keep that command
        # byte-for-byte, including its original approval and reservation IDs.
        known = {}
        for legacy_time in {raw_bar_time, bar_time, point.isoformat(), point.replace(tzinfo=None).isoformat()}:
            candidate = replace(intent, bar_time=legacy_time)
            row = host.order_store.get(candidate.client_order_id)
            if row is not None:
                known[candidate.client_order_id] = row
        if len(known) > 1:
            raise ValueError("ambiguous legacy time identities require order reconciliation")
        if known:
            row = next(iter(known.values()))
            return OrderIntent(**row["intent"])
        return intent

    def _validate_intent(self, intent: OrderIntent) -> Optional[str]:
        host = self._host
        if intent.action in {"buy", "short"} and getattr(host, "projection_issues", []):
            return "fill attribution recovery is required before new risk"
        if not math.isfinite(intent.requested_qty) or intent.requested_qty <= 0:
            return "quantity must be positive and reducible"
        if host.market_type not in DERIVATIVE_TYPES | {"margin"} and intent.action in {"short", "cover"}:
            return f"{intent.action} is not supported for account type {host.market_type}"
        if intent.action not in {"buy", "sell", "short", "cover"}:
            return "unsupported order side"
        if intent.action in {"sell", "cover"}:
            held = float(host.portfolio.get_position(intent.symbol)["qty"])
            available = max(held if intent.action == "sell" else -held, 0.0)
            reserved = sum(float(row.get("remaining_qty") or 0) for row in host.order_store.list_non_terminal()
                           if row["symbol"] == intent.symbol and row["side"] == intent.action
                           and row["client_order_id"] != intent.client_order_id)
            if intent.requested_qty > max(available - reserved, 0.0) + 1e-9:
                return "exit exceeds verified position after outstanding exits"
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
        host = self._host
        return any(
            record["symbol"] == symbol
            and record["client_order_id"] != client_order_id
            and host._is_opening_record(record)
            for record in host.order_store.list_non_terminal()
        )

    def has_active_open_order(self, symbol: str) -> bool:
        host = self._host
        return host._has_other_active_open_order(symbol)

    def pending_open_notional(
        self, current_prices: Optional[Dict[str, float]] = None
    ) -> Dict[str, float]:
        host = self._host
        return host.reservation_projection.pending_notional(current_prices)
