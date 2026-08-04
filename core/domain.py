from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional, Tuple


class OrderStatus(str, Enum):
    CREATED = "created"
    SUBMITTING = "submitting"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partial"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELED = "canceled"
    REJECTED = "rejected"
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

    @property
    def client_order_id(self) -> str:
        identity = {
            "exchange": self.exchange,
            "account": self.account,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "bar_time": self.bar_time,
            "strategy_id": self.strategy_id,
            "action": self.action,
            "sequence": self.sequence,
        }
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:24]
        return f"qt_{digest}"


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


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
