from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from decimal import Decimal
from typing import Any, Dict, Optional, Tuple


class OrderStatus(str, Enum):
    EXPIRED_UNSUBMITTED = 'expired_unsubmitted'
    CREATED = "created"
    SUBMITTING = "submitting"
    SUBMITTED = "submitting"
    ACCEPTED = "accepted"
    OPEN = "accepted"
    PARTIALLY_FILLED = "partial"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELED = "canceled"
    REJECTED = "rejected"
    NO_POSITION = "no_position"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


class OrderErrorCode(str, Enum):
    NONE = "none"
    NETWORK = "network"
    TIMEOUT = "timeout"
    RATE_LIMIT = "rate_limit"
    AUTHENTICATION = "authentication"
    TRADING_RULE = "trading_rule"
    INSUFFICIENT_FUNDS = "insufficient_funds"
    EXCHANGE_UNAVAILABLE = "exchange_unavailable"
    SAFETY_POLICY = "safety_policy"
    UNKNOWN = "unknown"


def _finite_decimal(value: Any, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be numeric")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (ArithmeticError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field_name} must be a finite decimal")
    return result


@dataclass(frozen=True)
class RiskDecision:
    """Immutable result of one pre-trade risk evaluation."""

    decision_id: str
    account: str
    symbol: str
    action: str
    requested_qty: Decimal
    approved_qty: Decimal
    reference_price: Decimal
    approved: bool
    reason: str
    intent_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.decision_id or not self.symbol or not self.action:
            raise ValueError("decision_id, symbol and action are required")
        requested = _finite_decimal(self.requested_qty, "requested_qty")
        approved = _finite_decimal(self.approved_qty, "approved_qty")
        price = _finite_decimal(self.reference_price, "reference_price")
        if requested <= 0 or approved < 0 or price <= 0:
            raise ValueError("quantities and reference_price are outside their valid range")
        if self.approved and approved <= 0:
            raise ValueError("an approved decision must approve a positive quantity")
        if not self.approved and approved != 0:
            raise ValueError("a rejected decision cannot approve quantity")
        if approved > requested:
            raise ValueError("approved_qty cannot exceed requested_qty")
        object.__setattr__(self, "requested_qty", requested)
        object.__setattr__(self, "approved_qty", approved)
        object.__setattr__(self, "reference_price", price)


@dataclass(frozen=True)
class RiskReservation:
    """Risk capacity reserved for an approved opening order intent."""

    reservation_id: str
    risk_decision_id: str
    intent_id: str
    account: str
    symbol: str
    action: str
    reserved_qty: Decimal
    reference_price: Decimal

    def __post_init__(self) -> None:
        if not all((self.reservation_id, self.risk_decision_id, self.intent_id, self.symbol)):
            raise ValueError("reservation identity and symbol are required")
        qty = _finite_decimal(self.reserved_qty, "reserved_qty")
        price = _finite_decimal(self.reference_price, "reference_price")
        if qty <= 0 or price <= 0:
            raise ValueError("reserved_qty and reference_price must be positive")
        object.__setattr__(self, "reserved_qty", qty)
        object.__setattr__(self, "reference_price", price)

    @property
    def reserved_notional(self) -> Decimal:
        return self.reserved_qty * self.reference_price


@dataclass(frozen=True)
class OrderIntent:
    exchange: str
    account: str
    symbol: str
    timeframe: str
    bar_time: str
    strategy_id: str
    action: str
    sequence: int
    requested_qty: float
    order_type: str = "market"
    price: Optional[float] = None
    time_in_force: Optional[str] = None
    reduce_only: bool = False
    position_side: Optional[str] = None
    position_mode: str = "one_way"
    signal_id: Optional[str] = None
    risk_decision_id: Optional[str] = None
    reservation_id: Optional[str] = None
    causation_id: Optional[str] = None
    created_at: Optional[str] = None

    @property
    def identity(self) -> Dict[str, Any]:
        """Stable business identity used by all deterministic order IDs."""

        return {
            "exchange": self.exchange,
            "account": self.account,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "bar_time": self.bar_time,
            "strategy_id": self.strategy_id,
            "action": self.action,
            "sequence": self.sequence,
        }

    @property
    def client_order_id(self) -> str:
        # Keep the established exchange-facing format and digest byte-for-byte
        # compatible. Optional causal metadata must not change idempotency.
        digest = hashlib.sha256(
            json.dumps(
                self.identity, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()[:24]
        return f"qt_{digest}"

    @property
    def intent_id(self) -> str:
        """Canonical intent ID; intentionally identical to the legacy ID."""

        return self.client_order_id

    @property
    def correlation_id(self) -> str:
        """Carry the originating signal correlation, or start a new chain."""

        return self.signal_id or self.intent_id


@dataclass(frozen=True)
class OrderSubmissionResult:
    client_order_id: str
    status: OrderStatus
    requested_qty: float
    filled_qty: float = 0.0
    remaining_qty: float = 0.0
    average_fill_price: Optional[float] = None
    exchange_order_id: Optional[str] = None
    error_code: OrderErrorCode = OrderErrorCode.NONE
    message: Optional[str] = None
    safely_persisted: bool = True
    payload: Dict[str, Any] = field(default_factory=dict)

    @property
    def safe_to_complete_bar(self) -> bool:
        return self.safely_persisted and self.status is not OrderStatus.UNKNOWN

    @property
    def accepted(self) -> bool:
        return self.status in {
            OrderStatus.ACCEPTED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
        }

    def __bool__(self) -> bool:
        return self.accepted


@dataclass(frozen=True)
class FillRecord:
    fill_id: str
    client_order_id: str
    exchange_order_id: Optional[str]
    qty: float
    price: float
    fee: float = 0.0
    fee_currency: Optional[str] = None
    timestamp: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    symbol: Optional[str] = None
    side: Optional[str] = None
    correlation_id: Optional[str] = None
    causation_id: Optional[str] = None


@dataclass(frozen=True)
class SyncResult:
    ok: bool
    synced_at: datetime
    error: Optional[str] = None

    def __bool__(self) -> bool:
        return self.ok


@dataclass(frozen=True)
class PortfolioSnapshot:
    cash: float
    equity: float
    gross_exposure: float
    net_exposure: float
    prices: Dict[str, float]
    price_times: Dict[str, datetime]
    synced_at: datetime


    positions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    cash_balances: Dict[str, Decimal] = field(default_factory=dict)
    realized_pnl: Decimal = Decimal("0")
    unrealized_pnl: Decimal = Decimal("0")
    fees: Dict[str, Decimal] = field(default_factory=dict)
    base_currency: str = "USD"
    last_sequence: int = 0


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
