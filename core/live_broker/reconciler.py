"""Idempotent order-fact reconciliation, startup recovery, and fill persistence.

Split out of core/live_broker.py (A4) — see docs/architecture_review.md. See
core/live_broker_submission.py's module docstring for why this is a mixin
rather than a standalone collaborator object.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

from core.domain import FillRecord, OrderErrorCode, OrderStatus, OrderSubmissionResult
from core.logger import get_logger
from core.orders import TERMINAL_STATUSES, classify_order_exception

# Same logger name as core.live_broker (logging.getLogger caches by name, so
# this is the identical object) -- tests use assertLogs("core.live_broker")
# and must keep catching records logged from this mixin too.
logger = get_logger("core.live_broker")

CONFIRMED_ABSENT_ERROR_MESSAGE = "order_not_found_by_client_id"


class OrderReconcilerMixin:
    """Reconcile the durable order ledger against the exchange's own fact.

    Expects ``self`` to carry ``order_store``, ``exchange``,
    ``exchange_boundary``, ``submitting_ttl``, ``_unknown_reconcile_attempts``,
    ``last_order_sync_at``, plus the shared plumbing on ``LiveBroker`` itself
    (``_retry_exchange_call``, ``_alert``, ``_result``, ``_now_iso``, ``_iso``,
    ``_clock``, ``_as_float``, ``_publish_fill_event``) and ``sync``/
    ``has_unresolved_unknown`` (from ``AccountSyncMixin``/``LiveBroker``).
    """

    def reconcile_order(self, client_order_id: str) -> OrderSubmissionResult:
        record = self.order_store.get(client_order_id)
        if record is None:
            raise KeyError(client_order_id)
        if OrderStatus(record["status"]) in TERMINAL_STATUSES:
            return self._result(client_order_id)
        if not record["submission_attempted"]:
            return self._result(client_order_id)

        previous_status = OrderStatus(record["status"])
        try:
            payload = self._retry_exchange_call(lambda: self._fetch_exchange_order(record))
        except Exception as exc:
            code = classify_order_exception(exc)
            current = OrderStatus(record["status"])
            if current is not OrderStatus.UNKNOWN:
                self.order_store.transition(
                    client_order_id, OrderStatus.UNKNOWN, self._now_iso(),
                    error_code=code.value, error_message=type(exc).__name__,
                )
            self._unknown_reconcile_attempts[client_order_id] = (
                self._unknown_reconcile_attempts.get(client_order_id, 0) + 1
            )
            self._alert("error", "reconcile_discrepancy", {
                "client_order_id": client_order_id, "error_code": code.value,
                "poll": self._unknown_reconcile_attempts[client_order_id],
            })
            return self._result(client_order_id)
        if not isinstance(payload, Mapping):
            payload = None
        if payload is None:
            self._unknown_reconcile_attempts[client_order_id] = (
                self._unknown_reconcile_attempts.get(client_order_id, 0) + 1
            )
            self._alert("error", "reconcile_discrepancy", {
                "client_order_id": client_order_id, "reason": "order_not_found",
                "poll": self._unknown_reconcile_attempts[client_order_id],
            })
            current = OrderStatus(record["status"])
            if current is not OrderStatus.UNKNOWN:
                self.order_store.transition(
                    client_order_id, OrderStatus.UNKNOWN, self._now_iso(),
                    error_code=OrderErrorCode.UNKNOWN.value,
                    error_message=CONFIRMED_ABSENT_ERROR_MESSAGE,
                )
            elif record.get("error_message") != CONFIRMED_ABSENT_ERROR_MESSAGE:
                # Already UNKNOWN from an earlier ambiguous failure (e.g. a
                # submission timeout). This poll independently confirmed the
                # exchange has no record of the order, which is strictly
                # more informative than the original transient-error label,
                # so record it. updated_at is deliberately left untouched:
                # the auto-expiry TTL below measures time since the order
                # first went UNKNOWN, not time since its last reconfirmation
                # poll, or a tight poll interval would keep resetting the
                # clock and the order would never expire.
                self.order_store.update(
                    client_order_id,
                    error_code=OrderErrorCode.UNKNOWN.value,
                    error_message=CONFIRMED_ABSENT_ERROR_MESSAGE,
                )
            return self._result(client_order_id)
        result = self._persist_exchange_payload(client_order_id, payload)
        if previous_status is OrderStatus.UNKNOWN and result.status is not OrderStatus.UNKNOWN:
            self._unknown_reconcile_attempts.pop(client_order_id, None)
            self._alert("info", "order_state_reconciled", {
                "client_order_id": client_order_id, "status": result.status.value,
            })
        if result.status in {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED}:
            self.sync()
        return result

    def recover_open_orders(self) -> Dict[str, OrderSubmissionResult]:
        results: Dict[str, OrderSubmissionResult] = {}
        for record in self.order_store.list_non_terminal():
            if (
                record['status'] == OrderStatus.SUBMITTING.value
                and not record['submission_attempted']
                and self._unattempted_submission_expired(record)
            ):
                self.order_store.resolve_as_unsubmitted(
                    record['client_order_id'],
                    confirmed_by='system:submitting_ttl',
                    reason=(
                        'persisted intent exceeded submitting TTL before '
                        'submission was marked attempted'
                    ),
                    now=self._now_iso(),
                )
                self._alert('critical', 'stale_submitting_expired', {
                    'client_order_id': record['client_order_id'],
                    'ttl_seconds': self.submitting_ttl.total_seconds(),
                })
                record = self.order_store.get(record['client_order_id']) or record
            elif (
                record['status'] == OrderStatus.UNKNOWN.value
                and record.get('error_message') == CONFIRMED_ABSENT_ERROR_MESSAGE
                and not record.get('exchange_order_id')
                and float(record.get('filled_qty') or 0.0) <= 0
                and self._unattempted_submission_expired(record)
            ):
                # Reconciliation independently confirmed the exchange has no
                # record of this order under its client id (not merely a
                # transient lookup failure). Other UNKNOWN causes are left
                # alone and require the manual confirm_order_not_submitted
                # path, since the exchange may genuinely have accepted them.
                self.order_store.resolve_as_unsubmitted(
                    record['client_order_id'],
                    confirmed_by='system:unknown_ttl',
                    reason=(
                        'reconciliation repeatedly confirmed the order absent '
                        'from the exchange and the submitting TTL elapsed'
                    ),
                    now=self._now_iso(),
                )
                self._unknown_reconcile_attempts.pop(record['client_order_id'], None)
                self._alert('critical', 'stale_unknown_confirmed_absent_expired', {
                    'client_order_id': record['client_order_id'],
                    'ttl_seconds': self.submitting_ttl.total_seconds(),
                })
                record = self.order_store.get(record['client_order_id']) or record
            client_id = record["client_order_id"]
            if record["status"] == OrderStatus.SUBMITTING.value and not record["submission_attempted"]:
                results[client_id] = self._result(client_id)
            else:
                results[client_id] = self.reconcile_order(client_id)
        if not self.has_unresolved_unknown():
            self.last_order_sync_at = self._clock()
        snapshot = getattr(self.order_store, "snapshot_if_due", None)
        if callable(snapshot):
            snapshot()
        return results

    def confirm_order_not_submitted(
        self,
        client_order_id: str,
        *,
        confirmed_by: str,
        reason: str,
    ) -> OrderSubmissionResult:
        '''Operator recovery path for an order independently verified absent.'''
        self.order_store.resolve_as_unsubmitted(
            client_order_id,
            confirmed_by=confirmed_by,
            reason=reason,
            now=self._now_iso(),
        )
        self._unknown_reconcile_attempts.pop(client_order_id, None)
        result = self._result(client_order_id)
        self._alert('critical', 'unknown_order_manually_resolved', {
            'client_order_id': client_order_id,
            'confirmed_by': confirmed_by,
            'reason': reason,
            'status': result.status.value,
        })
        return result

    def _unattempted_submission_expired(self, record: Dict[str, Any]) -> bool:
        try:
            updated_at = datetime.fromisoformat(str(record['updated_at']))
        except (KeyError, TypeError, ValueError):
            logger.error(
                'Invalid persisted order timestamp client_order_id=%s',
                record.get('client_order_id'),
            )
            return False
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return now - updated_at >= self.submitting_ttl

    def has_unresolved_unknown(self) -> bool:
        return any(
            record["status"] == OrderStatus.UNKNOWN.value
            for record in self.order_store.list_non_terminal()
        )

    def _persist_exchange_payload(
        self, client_order_id: str, payload: Dict[str, Any]
    ) -> OrderSubmissionResult:
        record = self.order_store.get(client_order_id)
        if record is None:
            raise KeyError(client_order_id)
        parsed = self.exchange_boundary.order_parser.parse(
            payload, requested_qty=record["requested_qty"]
        )
        status = parsed.status
        requested = float(parsed.requested_qty)
        filled = float(parsed.filled_qty)
        if status is OrderStatus.FILLED and payload.get("filled") is None:
            filled = requested
        remaining = float(parsed.remaining_qty)
        average = (
            None
            if parsed.average_fill_price is None
            else float(parsed.average_fill_price)
        )
        exchange_order_id = (
            parsed.exchange_order_id or record.get("exchange_order_id")
        )

        self._persist_fills(client_order_id, exchange_order_id, payload, filled, average)
        current_status = OrderStatus(record["status"])
        if current_status != status:
            self.order_store.transition(
                client_order_id, status, self._now_iso(),
                exchange_order_id=exchange_order_id,
                filled_qty=filled, remaining_qty=remaining,
                average_fill_price=average, error_code=OrderErrorCode.NONE.value,
                error_message=None, payload=payload,
            )
        else:
            self.order_store.update(
                client_order_id, exchange_order_id=exchange_order_id,
                filled_qty=filled, remaining_qty=remaining,
                average_fill_price=average, error_code=OrderErrorCode.NONE.value,
                error_message=None, payload=payload, updated_at=self._now_iso(),
            )
        return self._result(client_order_id)

    def _persist_fills(
        self,
        client_order_id: str,
        exchange_order_id: Optional[str],
        payload: Dict[str, Any],
        cumulative_filled: float,
        average: Optional[float],
    ) -> None:
        trades = payload.get("trades") or []
        if trades:
            for index, trade in enumerate(trades):
                fee = trade.get("fee") or {}
                fill_id = str(
                    trade.get("id")
                    or f"{exchange_order_id}:{trade.get('timestamp')}:{index}"
                )
                record = self.order_store.get(client_order_id) or {}
                fill_record = FillRecord(
                    fill_id=fill_id, client_order_id=client_order_id,
                    exchange_order_id=exchange_order_id,
                    qty=self._as_float(trade.get("amount")),
                    price=self._as_float(trade.get("price")),
                    fee=self._as_float(fee.get("cost")),
                    fee_currency=fee.get("currency"),
                    timestamp=self._iso(trade.get("datetime") or trade.get("timestamp")),
                    payload=trade,
                    symbol=record.get("symbol"),
                    side=record.get("side"),
                )
                if self.order_store.add_fill(fill_record):
                    self._publish_fill_event(fill_record, record)
                    intent = record.get("intent") or {}
                    self.trades.append({
                        "id": fill_id,
                        "fill_id": fill_id,
                        "symbol": record.get("symbol"),
                        "side": record.get("side"),
                        "qty": fill_record.qty,
                        "fill_price": fill_record.price,
                        "commission": fill_record.fee,
                        "timestamp": fill_record.timestamp,
                        "strategy_id": intent.get("strategy_id"),
                    })
            return
        existing_qty = sum(fill["qty"] for fill in self.order_store.fills_for(client_order_id))
        delta = max(cumulative_filled - existing_qty, 0.0)
        if delta > 0 and average:
            fill_id = f"{exchange_order_id}:cumulative:{cumulative_filled:.12f}"
            record = self.order_store.get(client_order_id) or {}
            fill_record = FillRecord(
                fill_id=fill_id, client_order_id=client_order_id,
                exchange_order_id=exchange_order_id, qty=delta, price=average,
                timestamp=self._now_iso(), payload={"synthetic_from_order": True},
                symbol=record.get("symbol"), side=record.get("side"),
            )
            if self.order_store.add_fill(fill_record):
                self._publish_fill_event(fill_record, record)
                intent = record.get("intent") or {}
                self.trades.append({
                    "id": fill_id,
                    "fill_id": fill_id,
                    "symbol": record.get("symbol"),
                    "side": record.get("side"),
                    "qty": delta,
                    "fill_price": average,
                    "commission": 0.0,
                    "timestamp": self._now_iso(),
                    "strategy_id": intent.get("strategy_id"),
                })

    def _fetch_exchange_order(self, record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if record.get("exchange_order_id"):
            return self.exchange.fetch_order(record["exchange_order_id"], record["symbol"])
        fetch_by_client = getattr(self.exchange, "fetch_order_by_client_order_id", None)
        if callable(fetch_by_client):
            return fetch_by_client(record["client_order_id"], record["symbol"])
        fetch_orders = getattr(self.exchange, "fetch_orders", None)
        if callable(fetch_orders):
            orders = fetch_orders(record["symbol"], params={"clientOrderId": record["client_order_id"]}) or []
            for payload in orders:
                candidate = payload.get("clientOrderId") or payload.get("client_order_id")
                if candidate == record["client_order_id"]:
                    return payload
        return None
