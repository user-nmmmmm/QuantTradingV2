from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping, Optional

import ccxt

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
    ) -> OrderSubmissionResult:
        del slippage, exit_reason
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
            prepared = self.exchange_boundary.prepare(
                intent, reference_price=intent.price
            )
            intent = prepared.intent
        except ExchangeBoundaryError as exc:
            return self.record_local_rejection(
                intent, str(exc), OrderErrorCode.TRADING_RULE
            )
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

        try:
            payload = self._fetch_exchange_order(record)
        except Exception as exc:
            code = classify_order_exception(exc)
            current = OrderStatus(record["status"])
            if current is not OrderStatus.UNKNOWN:
                self.order_store.transition(
                    client_order_id, OrderStatus.UNKNOWN, self._now_iso(),
                    error_code=code.value, error_message=type(exc).__name__,
                )
            return self._result(client_order_id)
        if not isinstance(payload, Mapping):
            payload = None
        if payload is None:
            current = OrderStatus(record["status"])
            if current is not OrderStatus.UNKNOWN:
                self.order_store.transition(
                    client_order_id, OrderStatus.UNKNOWN, self._now_iso(),
                    error_code=OrderErrorCode.UNKNOWN.value,
                    error_message="order_not_found_by_client_id",
                )
            return self._result(client_order_id)
        result = self._persist_exchange_payload(client_order_id, payload)
        if result.status in {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED}:
            self.sync()
        return result

    def recover_open_orders(self) -> Dict[str, OrderSubmissionResult]:
        results: Dict[str, OrderSubmissionResult] = {}
        for record in self.order_store.list_non_terminal():
            client_id = record["client_order_id"]
            if record["status"] == OrderStatus.SUBMITTING.value and not record["submission_attempted"]:
                results[client_id] = self._result(client_id)
            else:
                results[client_id] = self.reconcile_order(client_id)
        if not self.has_unresolved_unknown():
            self.last_order_sync_at = self._clock()
        return results

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
                self.trades.append(
                    {"id": fill_id, "symbol": record["symbol"],
                     "qty": delta, "price": average, "timestamp": self._now_iso()}
                )

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
        is_reduce = reduce_only if reduce_only is not None else side in {"sell", "cover"}
        if self.market_type in DERIVATIVE_TYPES and is_reduce:
            closable = max(held, 0.0) if side == "sell" else max(-held, 0.0)
            qty = min(qty, closable)
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

    def sync(self) -> SyncResult:
        try:
            balance = self.exchange.fetch_balance()
            free = balance.get("free", {}) if isinstance(balance, dict) else {}
            total = balance.get("total", {}) if isinstance(balance, dict) else {}
            next_cash = self._as_float(
                free.get(self.base_currency), self._as_float(total.get(self.base_currency))
            )
            if self.market_type in DERIVATIVE_TYPES:
                positions = self._sync_derivatives_positions(balance)
            else:
                positions = self._sync_spot_positions(balance)
            self.portfolio.cash = next_cash
            self.portfolio.positions = positions
            self.last_account_sync_at = self._clock()
            return SyncResult(True, self.last_account_sync_at)
        except Exception as exc:
            logger.error("Failed to sync portfolio category=%s", type(exc).__name__)
            return SyncResult(False, self._clock(), type(exc).__name__)

    def _sync_spot_positions(self, balance: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
        positions: Dict[str, Dict[str, float]] = {}
        for currency, amount in (balance.get("total", {}) or {}).items():
            qty = self._as_float(amount)
            if currency != self.base_currency and qty != 0:
                positions[f"{currency}/{self.base_currency}"] = {"qty": qty, "avg_price": 0.0}
        return positions

    def _sync_derivatives_positions(self, balance: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
        raw_positions = []
        fetch_positions = getattr(self.exchange, "fetch_positions", None)
        if callable(fetch_positions):
            raw_positions = fetch_positions() or []
        if not raw_positions:
            raw_positions = balance.get("positions", []) or balance.get("info", {}).get("positions", [])
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
