from __future__ import annotations

import os
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Optional

import ccxt

from core.foundation.alerting import AlertSink, build_default_alert_sink
from core.foundation.clock import ClockLike, coerce_clock
from core.foundation.domain import (
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
from core.live_broker.account_sync import AccountSync
from core.live_broker.reconciler import OrderReconciler
from core.live_broker.submission import SubmissionService
from core.foundation.logger import get_logger
from core.trading.order_store import OrderStore
from core.live_broker.retry import with_retry
from core.trading.portfolio import Portfolio
from core.risk.reservation import (
    RiskReservationProjection,
    ensure_opening_reservation,
)
from core.foundation.domain import SyncResult
from functools import cached_property

"""CCXT adapter backed by an authoritative idempotent order ledger.

Split by change reason (A4) — see docs/architecture_review.md:
- core/live_broker_submission.py    — intent construction, idempotent create, cancel
- core/live_broker_reconciler.py    — order-fact reconciliation, startup recovery
- core/live_broker_account_sync.py  — cash/position sync against the exchange

``LiveBroker`` holds the three collaborators below (``_submission``, ``_reconciler``, ``_account_sync``) and
forwards their methods with the same signatures; they reach the shared state through their ``host`` reference
and call back into ``LiveBroker``'s own methods, so behavior is identical to the former mixins (B4, ADR 0018).
``LiveBroker`` itself keeps ``__init__`` and the shared plumbing every collaborator calls back into (retry,
alert, clock/time formatting, result construction, event publishing).
"""

logger = get_logger(__name__)


class LiveBroker:
    """CCXT adapter backed by an authoritative idempotent order ledger."""

    @cached_property
    def _submission(self) -> SubmissionService:
        return SubmissionService(self)

    @cached_property
    def _reconciler(self) -> OrderReconciler:
        return OrderReconciler(self)

    @cached_property
    def _account_sync(self) -> AccountSync:
        return AccountSync(self)

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
        require_resident_protection: bool = False,
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
        self.require_resident_protection = require_resident_protection
        self.exchange_id = exchange_id
        self.environment = "sandbox" if sandbox else "live"
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
        self.trades: list[Any] = []  # Compatibility projection only; OrderStore is authoritative.
        from core.trading.order_latency import OrderLatencyRecorder, order_observation_sidecar_path
        self.order_latency = OrderLatencyRecorder(
            persistence_path=order_observation_sidecar_path(getattr(self.order_store, "path", None)))
        self.close_events: list[Any] = []
        self.projection_issues: list[Any] = []
        self.unowned_positions: dict[str, Any] = {}
        self._rebuild_fill_projection()
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
        from core.market.request_budget import install_exchange_budget
        install_exchange_budget(self.exchange, exchange_id, priority="critical")
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
            reference_price = intent.reference_price or intent.price or record.get("price") or 0
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

    def _rebuild_fill_projection(self):
        from core.live_broker.fill_projection import replay_fill_projection
        books, events, issues = replay_fill_projection(self.order_store, self.base_currency)
        self.portfolio.lot_books = books
        self.close_events = events
        self.projection_issues = issues

    def set_health_assessment(self, assessment) -> None:
        self.health_assessment = assessment

    def risk_price_facts(self, symbols, *, max_age_seconds=90.0):
        """Realtime execution-venue marks; a closed signal bar is not a risk mark."""
        now = self._clock()
        facts = {}
        for symbol in sorted(set(symbols)):
            ticker = self.exchange.fetch_ticker(symbol)
            price = float(ticker.get("last") or 0.0)
            timestamp = ticker.get("timestamp")
            if timestamp is None or not math.isfinite(price) or price <= 0:
                raise ValueError("ticker price/timestamp is unavailable")
            observed = datetime.fromtimestamp(float(timestamp) / 1000, tz=timezone.utc)
            age = (now - observed).total_seconds()
            if not 0 <= age <= max_age_seconds:
                raise ValueError("ticker mark is stale or from the future")
            facts[symbol] = {"price": price, "timestamp": observed, "source": self.exchange_id,
                             "expires_at": observed + timedelta(seconds=max_age_seconds)}
        self.last_risk_price_facts = facts
        return facts

    def _publish_fill_event(
        self, fill: FillRecord, record: Dict[str, Any]
    ) -> None:
        self._rebuild_fill_projection()
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

    def submit_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: Optional[float] = None,
        order_type: str = 'market',
        timestamp: Any = None,
        slippage: float = 0.0,
        strategy_id: str = 'Manual',
        exit_reason: str = 'signal',
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
        return self._submission.submit_order(
            symbol,
            side,
            qty,
            price,
            order_type,
            timestamp,
            slippage,
            strategy_id,
            exit_reason,
            time_in_force,
            position_side,
            reduce_only,
            sequence,
            stop_loss,
            zero_cost,
            reference_price,
            trigger_price,
            risk_action_id,
            approved_risk_amount,
            signal_id,
            causation_id,
        )

    def submit_intent(self, intent: OrderIntent) -> OrderSubmissionResult:
        return self._submission.submit_intent(intent)

    def record_local_rejection(
        self,
        intent: OrderIntent,
        message: str,
        code: OrderErrorCode = OrderErrorCode.SAFETY_POLICY,
    ) -> OrderSubmissionResult:
        return self._submission.record_local_rejection(intent, message, code)

    def cancel_order(self, client_order_id: str) -> OrderSubmissionResult:
        return self._submission.cancel_order(client_order_id)

    def cancel_symbol_orders(self, symbol: str) -> None:
        return self._submission.cancel_symbol_orders(symbol)

    def _build_intent(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: Optional[float],
        order_type: str,
        timestamp: Any,
        strategy_id: str,
        time_in_force: Optional[str],
        position_side: Optional[str],
        reduce_only: Optional[bool],
        sequence: int,
        reference_price: Optional[float] = None,
        trigger_price: Optional[float] = None,
        stop_loss: float = 0.0,
        exit_reason: Optional[str] = None,
        risk_action_id: Optional[str] = None,
        approved_risk_amount: Optional[float] = None,
        signal_id: Optional[str] = None,
        causation_id: Optional[str] = None,
    ) -> OrderIntent:
        return self._submission._build_intent(
            symbol,
            side,
            qty,
            price,
            order_type,
            timestamp,
            strategy_id,
            time_in_force,
            position_side,
            reduce_only,
            sequence,
            reference_price,
            trigger_price,
            stop_loss,
            exit_reason,
            risk_action_id,
            approved_risk_amount,
            signal_id,
            causation_id,
        )

    def _validate_intent(self, intent: OrderIntent) -> Optional[str]:
        return self._submission._validate_intent(intent)

    @staticmethod
    def _is_opening_record(record: Dict[str, Any]) -> bool:
        return SubmissionService._is_opening_record(record)

    def _has_other_active_open_order(self, symbol: str, client_order_id: Optional[str] = None) -> bool:
        return self._submission._has_other_active_open_order(symbol, client_order_id)

    def has_active_open_order(self, symbol: str) -> bool:
        return self._submission.has_active_open_order(symbol)

    def pending_open_notional(self, current_prices: Optional[Dict[str, float]] = None) -> Dict[str, float]:
        return self._submission.pending_open_notional(current_prices)

    def reconcile_order(self, client_order_id: str) -> OrderSubmissionResult:
        return self._reconciler.reconcile_order(client_order_id)

    def recover_open_orders(self) -> Dict[str, OrderSubmissionResult]:
        return self._reconciler.recover_open_orders()

    def confirm_order_not_submitted(
        self,
        client_order_id: str,
        *,
        confirmed_by: str,
        reason: str,
    ) -> OrderSubmissionResult:
        return self._reconciler.confirm_order_not_submitted(
            client_order_id,
            confirmed_by=confirmed_by,
            reason=reason,
        )

    def _unattempted_submission_expired(self, record: Dict[str, Any]) -> bool:
        return self._reconciler._unattempted_submission_expired(record)

    def has_unresolved_unknown(self) -> bool:
        return self._reconciler.has_unresolved_unknown()

    def _persist_exchange_payload(
        self,
        client_order_id: str,
        payload: Dict[str, Any],
    ) -> OrderSubmissionResult:
        return self._reconciler._persist_exchange_payload(client_order_id, payload)

    def _persist_fills(
        self,
        client_order_id: str,
        exchange_order_id: Optional[str],
        payload: Dict[str, Any],
        cumulative_filled: float,
        average: Optional[float],
    ) -> None:
        return self._reconciler._persist_fills(
            client_order_id,
            exchange_order_id,
            payload,
            cumulative_filled,
            average,
        )

    def _fetch_exchange_order(self, record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return self._reconciler._fetch_exchange_order(record)

    @property
    def account_source_configured(self) -> bool:
        return self._account_sync.account_source_configured

    def configure_account_source(
        self,
        source,
        *,
        environment: str,
        maximum_snapshot_age_seconds: float = 90,
    ) -> None:
        return self._account_sync.configure_account_source(
            source,
            environment=environment,
            maximum_snapshot_age_seconds=maximum_snapshot_age_seconds,
        )

    def _account_source_failure(self, reason, *, checked_at=None):
        return self._account_sync._account_source_failure(reason, checked_at=checked_at)

    def reconcile_full_account(self, *, checked_at=None):
        return self._account_sync.reconcile_full_account(checked_at=checked_at)

    def _compare_observed_account(self, actual, report, now):
        return self._account_sync._compare_observed_account(actual, report, now)

    def sync(self) -> SyncResult:
        return self._account_sync.sync()

    def _load_portfolio_fact(self):
        return self._account_sync._load_portfolio_fact()

    @staticmethod
    def _balance_number(value):
        return AccountSync._balance_number(value)

    def _sync_margin_account(self, balance):
        return self._account_sync._sync_margin_account(balance)

    def _sync_spot_positions(self, balance: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
        return self._account_sync._sync_spot_positions(balance)

    def _sync_derivatives_positions(self, balance: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
        return self._account_sync._sync_derivatives_positions(balance)

    def _extract_qty(self, payload: Dict[str, Any]) -> float:
        return self._account_sync._extract_qty(payload)

    @staticmethod
    def _position_direction(payload: Dict[str, Any], info: Dict[str, Any]) -> Optional[float]:
        return AccountSync._position_direction(payload, info)

    def _extract_avg_price(self, payload: Dict[str, Any]) -> float:
        return self._account_sync._extract_avg_price(payload)
