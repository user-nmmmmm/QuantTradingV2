from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

import ccxt

from core.alerting import AlertSink, build_default_alert_sink
from core.clock import ClockLike, coerce_clock
from core.domain import (
    FillRecord,
    OrderErrorCode,
    OrderIntent,
    OrderStatus,
    OrderSubmissionResult,
)
from core.events import FillEvent, OrderEvent, TradingEventPipeline
from core.exchange import (
    ExchangeBoundary,
    ExchangeCapabilities,
    MarketMetadataLoader,
    MetadataChangeHaltPolicy,
)
from core.live_broker_account_sync import AccountSyncMixin
from core.live_broker_reconciler import OrderReconcilerMixin
from core.live_broker_submission import SubmissionServiceMixin
from core.logger import get_logger
from core.order_store import OrderStore
from core.retry import with_retry
from core.portfolio import Portfolio
from core.risk_reservation import (
    RiskReservationProjection,
    ensure_opening_reservation,
)

"""CCXT adapter backed by an authoritative idempotent order ledger.

Split by change reason (A4) — see docs/architecture_review.md:
- core/live_broker_submission.py    — intent construction, idempotent create, cancel
- core/live_broker_reconciler.py    — order-fact reconciliation, startup recovery
- core/live_broker_account_sync.py  — cash/position sync against the exchange

``LiveBroker`` composes the three mixins below via inheritance rather than
holding separate collaborator objects, so every method still reads/writes
the exact same ``self`` attributes as before the split — behavior-identical,
mechanical. A true composition redesign is a bigger change on this
money-path code (the only module here that talks to a real exchange) and is
deliberately left for a dedicated pass, not bundled into this file-size
cleanup. ``LiveBroker`` itself keeps ``__init__`` and the shared plumbing
every mixin calls back into (retry, alert, clock/time formatting, result
construction, event publishing).
"""

logger = get_logger(__name__)


class LiveBroker(SubmissionServiceMixin, OrderReconcilerMixin, AccountSyncMixin):
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

    def set_health_assessment(self, assessment) -> None:
        self.health_assessment = assessment

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
