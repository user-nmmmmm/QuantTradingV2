from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Mapping, Optional

import ccxt

from core.alerting import AlertSink, build_default_alert_sink
from core.clock import ClockLike, coerce_clock
from core.domain import (
    FillRecord,
    OrderErrorCode,
    OrderIntent,
    OrderStatus,
    OrderSubmissionResult,
    SyncResult,
)
from core.events import FillEvent, OrderEvent, TradingEventPipeline
from core.exchange_boundary import (
    ExchangeBoundary,
    ExchangeBoundaryError,
    ExchangeCapabilities,
    MarketMetadataLoader,
    MetadataChangeHaltPolicy,
)
from core.logger import get_logger
from core.order_store import OrderStore
from core.retry import with_retry
from core.orders import (
    TERMINAL_STATUSES,
    classify_order_exception,
    is_ambiguous_error,
)
from core.portfolio import Portfolio
from core.risk_reservation import (
    RiskReservationProjection,
    ensure_opening_reservation,
)


logger = get_logger(__name__)
DERIVATIVE_TYPES = {"future", "futures", "swap", "margin"}
CONFIRMED_ABSENT_ERROR_MESSAGE = "order_not_found_by_client_id"


class LiveBroker:
    """CCXT adapter backed by an authoritative idempotent order ledger."""

    def __init__(
        self,
        portfolio: Portfolio,
        exchange_id: str = "binance",
        api_key: str = None,
        secret: str = None,
        sandbox: bool = False,
        market_type: str = "spot",
        base_currency: str = "USDT",
        password: Optional[str] = None,
        exchange_options: Optional[Dict[str, Any]] = None,
        order_store: Optional[OrderStore] = None,
        account_id: Optional[str] = None,
        position_mode: str = "one_way",
        clock: Optional[ClockLike] = None,
        event_pipeline: Optional[TradingEventPipeline] = None,
        exchange_boundary: Optional[ExchangeBoundary] = None,
        require_market_metadata: bool = False,
        metadata_ttl: timedelta = timedelta(hours=1),
        metadata_change_policy: Optional[MetadataChangeHaltPolicy] = None,
        alert_sink: Optional[AlertSink] = None,
        retry_max_attempts: int = 3,
        retry_base_delay: float = 0.5,
        retry_max_delay: float = 8.0,
        retry_sleep_fn: Optional[Callable[[float], None]] = None,
        submitting_ttl: timedelta = timedelta(minutes=5),
    ) -> None:
        self.portfolio = portfolio
        self.exchange_id = exchange_id
        self.market_type = market_type
        self.base_currency = base_currency
        self.account_id = account_id or market_type
        self.position_mode = position_mode
        self.order_store = order_store or OrderStore(":memory:")
        self._clock = coerce_clock(clock).now
        self.event_pipeline = event_pipeline or TradingEventPipeline(clock=self._clock)
        self.reservation_projection = RiskReservationProjection(self.event_pipeline)
        self._restore_reservations()
        self._last_event_by_order: Dict[str, str] = {}
        self._bar_timeframe = "unknown"
        self._bar_time = "unknown"
        self.trades = []  # Compatibility projection only; OrderStore is authoritative.
        self.health_assessment = None
        self.last_account_sync_at: Optional[datetime] = None
        self.last_order_sync_at: Optional[datetime] = None

        self.alert_sink = alert_sink or build_default_alert_sink(logger)
        self.retry_max_attempts = retry_max_attempts
        self.retry_base_delay = retry_base_delay
        self.retry_max_delay = retry_max_delay
        self.retry_sleep_fn = retry_sleep_fn
        if submitting_ttl.total_seconds() <= 0:
            raise ValueError('submitting_ttl must be positive')
        self.submitting_ttl = submitting_ttl
        self._unknown_reconcile_attempts: Dict[str, int] = {}
        exchange_class = getattr(ccxt, exchange_id)
        options = dict(exchange_options or {})
        options.setdefault("defaultType", market_type)
        config = {
            "apiKey": api_key or os.getenv("EXCHANGE_API_KEY"),
            "secret": secret or os.getenv("EXCHANGE_SECRET"),
            "password": password or os.getenv("EXCHANGE_PASSWORD"),
            "enableRateLimit": True,
            "options": options,
        }
        self.exchange = exchange_class(config)
        if hasattr(self.exchange, "session") and hasattr(self.exchange.session, "trust_env"):
            self.exchange.session.trust_env = True
        if sandbox:
            self.exchange.set_sandbox_mode(True)
        self.exchange_boundary = exchange_boundary or ExchangeBoundary(
            ExchangeCapabilities.from_ccxt(self.exchange, exchange_id),
            MarketMetadataLoader(
                self.exchange,
                exchange_id,
                ttl=metadata_ttl,
                clock=self._clock,
                change_policy=metadata_change_policy,
            ),
            # Minimal test doubles may intentionally omit metadata. Live
            # deployments can enable strict fail-closed metadata explicitly.
            require_metadata=require_market_metadata,
        )
        logger.info(
            "Initialized %s in %s mode with market_type=%s",
            exchange_id, "SANDBOX" if sandbox else "LIVE", market_type,
        )


    def _restore_reservations(self) -> None:
        """Rehydrate non-terminal reservations from the durable intent ledger."""
        for record in self.order_store.list_non_terminal():
            if not self._is_opening_record(record):
                continue
            intent_data = record.get("intent") or {}
            try:
                intent = OrderIntent(**intent_data)
            except (TypeError, ValueError):
                logger.error(
                    "Cannot restore reservation client_order_id=%s",
                    record.get("client_order_id"),
                )
                continue
            reference_price = intent.price or record.get("price") or 0
            ensure_opening_reservation(
                self.event_pipeline,
                intent,
                reference_price=reference_price,
                occurred_at=self._event_time(intent.created_at or intent.bar_time),
                source="live",
                reason="restored_from_durable_intent",
            )

    def set_bar_context(self, timeframe: str, bar_time: Any) -> None:
        self._bar_timeframe = str(timeframe)
        self._bar_time = self._iso(bar_time)

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

    def set_health_assessment(self, assessment) -> None:
        self.health_assessment = assessment

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

    def _publish_fill_event(
        self, fill: FillRecord, record: Dict[str, Any]
    ) -> None:
        intent_data = record.get("intent") or {}
        intent = OrderIntent(**intent_data)
        envelope = self.event_pipeline.publish(
            FillEvent(
                fill_id=fill.fill_id,
                client_order_id=fill.client_order_id,
                exchange_order_id=fill.exchange_order_id,
                symbol=fill.symbol or record["symbol"],
                side=fill.side or record["side"],
                qty=fill.qty,
                price=fill.price,
                fee=fill.fee,
                fee_currency=fill.fee_currency,
            ),
            occurred_at=self._event_time(fill.timestamp),
            correlation_id=intent.correlation_id,
            causation_id=(
                self._last_event_by_order.get(fill.client_order_id)
                or intent.causation_id
            ),
            idempotency_key=fill.fill_id,
            account_id=intent.account,
            symbol=intent.symbol,
            timeframe=intent.timeframe,
            source="live",
        )
        self._last_event_by_order[fill.client_order_id] = str(envelope.event_id)

    def _publish_order_event(
        self, result: OrderSubmissionResult, record: Dict[str, Any]
    ) -> None:
        idempotency_key = (
            f"{result.client_order_id}:{result.status.value}:"
            f"{result.filled_qty:.12f}:{result.remaining_qty:.12f}"
        )
        if any(
            event.source == "live" and event.idempotency_key == idempotency_key
            for event in self.event_pipeline.events
        ):
            return
        intent = OrderIntent(**(record.get("intent") or {}))
        intent_event = next(
            (
                event for event in self.event_pipeline.events
                if event.source == "live"
                and event.event_type == "order_intent"
                and event.idempotency_key == intent.client_order_id
            ),
            None,
        )
        if intent_event is None:
            intent_event = self.event_pipeline.publish_intent(
                intent, occurred_at=self._event_time(intent.created_at or intent.bar_time), source="live"
            )
        envelope = self.event_pipeline.publish(
            OrderEvent(
                client_order_id=result.client_order_id,
                exchange_order_id=result.exchange_order_id,
                status=result.status,
                requested_qty=result.requested_qty,
                filled_qty=result.filled_qty,
                remaining_qty=result.remaining_qty,
                average_fill_price=result.average_fill_price,
                error_code=result.error_code.value,
                message=result.message,
            ),
            occurred_at=self._event_time(record.get("updated_at")),
            correlation_id=intent.correlation_id,
            causation_id=(
                self._last_event_by_order.get(result.client_order_id)
                or intent_event
            ),
            idempotency_key=idempotency_key,
            account_id=intent.account,
            symbol=intent.symbol,
            timeframe=intent.timeframe,
            source="live",
        )
        self._last_event_by_order[result.client_order_id] = str(envelope.event_id)

    def _result(self, client_order_id: str) -> OrderSubmissionResult:
        record = self.order_store.get(client_order_id)
        if record is None:
            raise KeyError(client_order_id)
        result = OrderSubmissionResult(
            client_order_id=client_order_id,
            exchange_order_id=record.get("exchange_order_id"),
            status=OrderStatus(record["status"]),
            requested_qty=record["requested_qty"],
            filled_qty=record.get("filled_qty", 0.0),
            remaining_qty=record.get("remaining_qty", 0.0),
            average_fill_price=record.get("average_fill_price"),
            error_code=OrderErrorCode(record.get("error_code") or "none"),
            message=record.get("error_message"),
            safely_persisted=True,
            payload=record.get("payload", {}),
        )
        self._publish_order_event(result, record)
        return result

    def _retry_exchange_call(self, fn: Callable[[], Any]) -> Any:
        options = {
            "max_attempts": self.retry_max_attempts,
            "base_delay": self.retry_base_delay,
            "max_delay": self.retry_max_delay,
            "retryable": lambda code: code in {
                OrderErrorCode.NETWORK,
                OrderErrorCode.TIMEOUT,
                OrderErrorCode.RATE_LIMIT,
                OrderErrorCode.EXCHANGE_UNAVAILABLE,
            },
        }
        if self.retry_sleep_fn is not None:
            options["sleep_fn"] = self.retry_sleep_fn
        return with_retry(fn, **options)

    def _alert(self, level: str, event: str, context: Dict[str, Any]) -> None:
        try:
            self.alert_sink.notify(level, event, context)
        except Exception as exc:
            logger.error(
                "alert_delivery_failed event=%s category=%s",
                event, type(exc).__name__,
            )

    def _load_portfolio_fact(self):
        balance = self.exchange.fetch_balance()
        free = balance.get("free", {}) if isinstance(balance, dict) else {}
        total = balance.get("total", {}) if isinstance(balance, dict) else {}
        next_cash = self._as_float(
            total.get(self.base_currency), self._as_float(free.get(self.base_currency))
        )
        positions = (
            self._sync_derivatives_positions(balance)
            if self.market_type in DERIVATIVE_TYPES
            else self._sync_spot_positions(balance)
        )
        return next_cash, positions

    def sync(self) -> SyncResult:
        try:
            next_cash, positions = self._retry_exchange_call(self._load_portfolio_fact)
            self.portfolio.cash = next_cash
            self.portfolio.positions = positions
            self.last_account_sync_at = self._clock()
            return SyncResult(True, self.last_account_sync_at)
        except Exception as exc:
            logger.exception("Failed to sync portfolio category=%s", type(exc).__name__)
            self._alert("error", "account_sync_failed", {
                "error": type(exc).__name__,
                "retry_attempts": self.retry_max_attempts,
            })
            return SyncResult(False, self._clock(), type(exc).__name__)

    def _sync_spot_positions(self, balance: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
        positions: Dict[str, Dict[str, float]] = {}
        for currency, amount in (balance.get("total", {}) or {}).items():
            qty = self._as_float(amount)
            if currency != self.base_currency and qty != 0:
                positions[f"{currency}/{self.base_currency}"] = {"qty": qty, "avg_price": 0.0}
        return positions

    def _sync_derivatives_positions(self, balance: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
        fetch_positions = getattr(self.exchange, "fetch_positions", None)
        info = balance.get("info", {}) if isinstance(balance, dict) else {}
        if callable(fetch_positions):
            # An explicit, even empty, response from the dedicated endpoint is
            # an authoritative fact: genuinely flat.
            raw_positions = fetch_positions() or []
        elif isinstance(balance, dict) and "positions" in balance:
            raw_positions = balance.get("positions") or []
        elif isinstance(info, dict) and "positions" in info:
            raw_positions = info.get("positions") or []
        else:
            # No source at all exposes derivative positions; treating this as
            # "flat" would let trading continue on an unverified fake empty
            # snapshot. Fail closed instead.
            raise ValueError(
                "exchange does not expose derivative positions via fetch_positions "
                "or balance payload; cannot verify account fact"
            )
        positions: Dict[str, Dict[str, float]] = {}
        for raw in raw_positions:
            parsed = self.exchange_boundary.position_parser.parse(raw)
            if parsed is None:
                continue
            symbol = parsed.symbol
            if symbol in positions:
                raise ValueError(f"multiple derivative position legs are unsupported for {symbol}")
            positions[symbol] = {
                "qty": float(parsed.qty),
                "avg_price": float(parsed.average_entry_price),
            }
        return positions

    def _extract_qty(self, payload: Dict[str, Any]) -> float:
        info = payload.get("info", {}) if isinstance(payload, dict) else {}
        direction = self._position_direction(payload, info)

        for value in (payload.get("positionAmt"), info.get("positionAmt")):
            qty = self._as_float(value)
            if qty == 0:
                continue
            if direction is not None and (qty > 0) != (direction > 0):
                raise ValueError("derivative position side conflicts with signed quantity")
            return qty

        for value in (
            payload.get("contracts"),
            payload.get("qty"),
            payload.get("size"),
            info.get("contracts"),
            info.get("qty"),
            info.get("size"),
        ):
            magnitude = abs(self._as_float(value))
            if magnitude == 0:
                continue
            if direction is None:
                raise ValueError("derivative position direction is unavailable")
            return magnitude * direction
        return 0.0

    @staticmethod
    def _position_direction(
        payload: Dict[str, Any], info: Dict[str, Any]
    ) -> Optional[float]:
        for value in (
            payload.get("side"),
            payload.get("positionSide"),
            info.get("side"),
            info.get("positionSide"),
        ):
            normalized = str(value or "").strip().lower()
            if normalized in {"long", "buy"}:
                return 1.0
            if normalized in {"short", "sell"}:
                return -1.0
        return None

    def _extract_avg_price(self, payload: Dict[str, Any]) -> float:
        info = payload.get("info", {}) if isinstance(payload, dict) else {}
        for value in (payload.get("entryPrice"), payload.get("avgPrice"), payload.get("average"), info.get("entryPrice"), info.get("avgPrice")):
            price = self._as_float(value)
            if price > 0:
                return price
        return 0.0

    def close(self) -> None:
        self.order_store.close()

    @staticmethod
    def _ccxt_side(side: str) -> str:
        return "sell" if side == "short" else "buy" if side == "cover" else side

    @staticmethod
    def _event_time(value: Any) -> datetime:
        if value in (None, "", "unknown"):
            return datetime.now(timezone.utc)
        if isinstance(value, (int, float)) or (
            isinstance(value, str) and value.strip().replace(".", "", 1).isdigit()
        ):
            # CCXT numeric timestamps are milliseconds since Unix epoch.
            return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
        if isinstance(value, datetime):
            parsed = value
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _now_iso(self) -> str:
        return self._iso(self._clock())

    @staticmethod
    def _iso(value: Any) -> str:
        if value is None:
            return ""
        if hasattr(value, "isoformat"):
            return value.isoformat()
        return str(value)

    @staticmethod
    def _as_float(value: Any, default: float = 0.0) -> float:
        try:
            return default if value in (None, "") else float(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _as_optional_float(value: Any) -> Optional[float]:
        parsed = LiveBroker._as_float(value, 0.0)
        return parsed if parsed > 0 else None
