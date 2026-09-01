"""Exchange capabilities, market specifications, and versioned metadata loading.

Split out of core/exchange_boundary.py (A4) — see docs/architecture_review.md.
Also houses the shared decimal/mapping helpers and the boundary error
hierarchy, since every other split module needs them and this is the most
foundational one (no dependency on core.domain).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from threading import RLock
from typing import Any, Callable, Dict, Iterable, Mapping, Optional


DERIVATIVE_MARKET_TYPES = frozenset({"future", "futures", "swap", "margin"})


class ExchangeBoundaryError(ValueError):
    """Base class for deterministic, pre-submission exchange-boundary errors."""


class OrderValidationError(ExchangeBoundaryError):
    """A canonical order cannot be accepted by the selected market."""


class MetadataUnavailableError(ExchangeBoundaryError):
    """Trading metadata is required but could not be loaded."""


class MetadataChangedError(ExchangeBoundaryError):
    """Trading is halted because market metadata changed unexpectedly."""


def _decimal(value: Any, name: str, *, optional: bool = False) -> Optional[Decimal]:
    if value in (None, "") and optional:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


def _positive_optional(value: Any, name: str) -> Optional[Decimal]:
    result = _decimal(value, name, optional=True)
    if result is None or result == 0:
        return None
    if result < 0:
        raise ValueError(f"{name} cannot be negative")
    return result


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


@dataclass(frozen=True)
class ExchangeCapabilities:
    """Venue features expressed without leaking CCXT's ``has`` dictionary."""

    exchange_id: str
    order_types: frozenset[str] = frozenset({"market", "limit"})
    time_in_force: frozenset[str] = frozenset({"GTC", "IOC", "FOK", "PO"})
    supports_client_order_id: bool = True
    supports_reduce_only: bool = False
    supports_hedge_mode: bool = False
    supports_fetch_positions: bool = False
    supports_market_orders: bool = True
    supports_limit_orders: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "order_types", frozenset(str(v).lower() for v in self.order_types))
        object.__setattr__(self, "time_in_force", frozenset(str(v).upper() for v in self.time_in_force))

    @classmethod
    def from_ccxt(cls, exchange: Any, exchange_id: Optional[str] = None) -> "ExchangeCapabilities":
        has = _mapping(getattr(exchange, "has", {}))
        venue_id = exchange_id or str(getattr(exchange, "id", "unknown"))
        market = has.get("createMarketOrder") is not False
        limit = has.get("createLimitOrder") is not False
        types = set()
        if market:
            types.add("market")
        if limit:
            types.add("limit")
        options = _mapping(getattr(exchange, "options", {}))
        return cls(
            exchange_id=venue_id,
            order_types=frozenset(types),
            supports_client_order_id=options.get("clientOrderId") is not False,
            supports_reduce_only=bool(
                has.get("reduceOnly")
                or options.get("reduceOnly")
                or options.get("defaultType") in DERIVATIVE_MARKET_TYPES
            ),
            supports_hedge_mode=bool(has.get("setPositionMode") or options.get("hedgeMode")),
            supports_fetch_positions=bool(has.get("fetchPositions")),
            supports_market_orders=market,
            supports_limit_orders=limit,
        )


@dataclass(frozen=True)
class MarketSpecification:
    """Canonical set of constraints needed to make an order venue-valid."""

    symbol: str
    market_type: str = "spot"
    active: bool = True
    amount_step: Optional[Decimal] = None
    price_step: Optional[Decimal] = None
    min_amount: Optional[Decimal] = None
    max_amount: Optional[Decimal] = None
    min_price: Optional[Decimal] = None
    max_price: Optional[Decimal] = None
    min_notional: Optional[Decimal] = None
    max_notional: Optional[Decimal] = None
    contract_size: Decimal = Decimal("1")
    base: Optional[str] = None
    quote: Optional[str] = None
    settle: Optional[str] = None
    linear: Optional[bool] = None
    inverse: Optional[bool] = None
    order_types: frozenset[str] = frozenset()
    time_in_force: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("market symbol is required")
        for name in (
            "amount_step", "price_step", "min_amount", "max_amount", "min_price",
            "max_price", "min_notional", "max_notional",
        ):
            object.__setattr__(self, name, _positive_optional(getattr(self, name), name))
        contract_size = _decimal(self.contract_size, "contract_size")
        if contract_size is None or contract_size <= 0:
            raise ValueError("contract_size must be positive")
        object.__setattr__(self, "contract_size", contract_size)
        object.__setattr__(self, "market_type", str(self.market_type).lower())
        object.__setattr__(self, "order_types", frozenset(str(v).lower() for v in self.order_types))
        object.__setattr__(self, "time_in_force", frozenset(str(v).upper() for v in self.time_in_force))

    @property
    def is_derivative(self) -> bool:
        return self.market_type in DERIVATIVE_MARKET_TYPES

    @classmethod
    def from_ccxt(cls, market: Mapping[str, Any], precision_mode: Any = None) -> "MarketSpecification":
        limits = _mapping(market.get("limits"))
        amount_limits = _mapping(limits.get("amount"))
        price_limits = _mapping(limits.get("price"))
        cost_limits = _mapping(limits.get("cost"))
        precision = _mapping(market.get("precision"))
        info = _mapping(market.get("info"))
        market_type = str(
            market.get("type")
            or ("swap" if market.get("swap") else "future" if market.get("future") else "spot")
        ).lower()
        return cls(
            symbol=str(market.get("symbol") or market.get("id") or ""),
            market_type=market_type,
            active=market.get("active") is not False,
            amount_step=_precision_step(precision.get("amount"), precision_mode),
            price_step=_precision_step(precision.get("price"), precision_mode),
            min_amount=amount_limits.get("min"),
            max_amount=amount_limits.get("max"),
            min_price=price_limits.get("min"),
            max_price=price_limits.get("max"),
            min_notional=cost_limits.get("min"),
            max_notional=cost_limits.get("max"),
            contract_size=market.get("contractSize") or 1,
            base=market.get("base"),
            quote=market.get("quote"),
            settle=market.get("settle"),
            linear=market.get("linear"),
            inverse=market.get("inverse"),
            order_types=frozenset(_iter_strings(market.get("orderTypes") or info.get("orderTypes"))),
            time_in_force=frozenset(_iter_strings(market.get("timeInForce") or info.get("timeInForce"))),
        )

    def version_payload(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "market_type": self.market_type,
            "active": self.active,
            "amount_step": _text(self.amount_step),
            "price_step": _text(self.price_step),
            "min_amount": _text(self.min_amount),
            "max_amount": _text(self.max_amount),
            "min_price": _text(self.min_price),
            "max_price": _text(self.max_price),
            "min_notional": _text(self.min_notional),
            "max_notional": _text(self.max_notional),
            "contract_size": _text(self.contract_size),
            "base": self.base,
            "quote": self.quote,
            "settle": self.settle,
            "linear": self.linear,
            "inverse": self.inverse,
            "order_types": sorted(self.order_types),
            "time_in_force": sorted(self.time_in_force),
        }


def _iter_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple, set, frozenset)):
        return (str(item) for item in value)
    return ()


def _precision_step(value: Any, precision_mode: Any) -> Optional[Decimal]:
    raw = _positive_optional(value, "precision")
    if raw is None:
        return None
    # CCXT DECIMAL_PLACES is 2.  TICK_SIZE (the current default) supplies the
    # increment directly.  Accept the textual form as well for test adapters.
    if precision_mode in (2, "DECIMAL_PLACES", "decimal_places"):
        return Decimal("1").scaleb(-int(raw))
    return raw


def _text(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else format(value, "f")


@dataclass(frozen=True)
class MetadataSnapshot:
    exchange_id: str
    version: str
    loaded_at: datetime
    markets: Mapping[str, MarketSpecification]

    def market(self, symbol: str) -> MarketSpecification:
        try:
            return self.markets[symbol]
        except KeyError as exc:
            raise OrderValidationError(f"unknown market: {symbol}") from exc


class MetadataChangeAction(str, Enum):
    HALT = "halt"
    ACCEPT = "accept"


@dataclass(frozen=True)
class MetadataChangeHaltPolicy:
    """Fail closed when executable market constraints change mid-run."""

    action: MetadataChangeAction = MetadataChangeAction.HALT

    def should_halt(self, previous: MetadataSnapshot, current: MetadataSnapshot) -> bool:
        return self.action is MetadataChangeAction.HALT and previous.version != current.version


class MarketMetadataLoader:
    """Thread-safe, versioned TTL cache around CCXT ``load_markets``."""

    def __init__(
        self,
        exchange: Any,
        exchange_id: Optional[str] = None,
        *,
        ttl: timedelta = timedelta(hours=1),
        clock: Optional[Callable[[], datetime]] = None,
        change_policy: Optional[MetadataChangeHaltPolicy] = None,
    ) -> None:
        if ttl < timedelta(0):
            raise ValueError("metadata ttl cannot be negative")
        self.exchange = exchange
        self.exchange_id = exchange_id or str(getattr(exchange, "id", "unknown"))
        self.ttl = ttl
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.change_policy = change_policy or MetadataChangeHaltPolicy()
        self._snapshot: Optional[MetadataSnapshot] = None
        self._halt_reason: Optional[str] = None
        self._lock = RLock()

    @property
    def snapshot(self) -> Optional[MetadataSnapshot]:
        return self._snapshot

    @property
    def halted(self) -> bool:
        return self._halt_reason is not None

    @property
    def halt_reason(self) -> Optional[str]:
        return self._halt_reason

    def load(self, *, force: bool = False) -> MetadataSnapshot:
        with self._lock:
            now = self.clock()
            if self._halt_reason:
                raise MetadataChangedError(self._halt_reason)
            if (
                not force
                and self._snapshot is not None
                and now - self._snapshot.loaded_at < self.ttl
            ):
                return self._snapshot
            loader = getattr(self.exchange, "load_markets", None)
            if not callable(loader):
                raise MetadataUnavailableError("exchange does not expose load_markets")
            refresh_cached_markets = force or self._snapshot is not None
            raw_markets = loader(reload=refresh_cached_markets)
            if not isinstance(raw_markets, Mapping) or not raw_markets:
                raise MetadataUnavailableError("exchange returned no market metadata")
            precision_mode = getattr(self.exchange, "precisionMode", None)
            markets: Dict[str, MarketSpecification] = {}
            for key, raw in raw_markets.items():
                if not isinstance(raw, Mapping):
                    continue
                enriched = dict(raw)
                enriched.setdefault("symbol", key)
                try:
                    spec = MarketSpecification.from_ccxt(enriched, precision_mode)
                except ValueError as exc:
                    raise MetadataUnavailableError(
                        f"invalid market metadata for {key}: {exc}"
                    ) from exc
                markets[spec.symbol] = spec
            if not markets:
                raise MetadataUnavailableError("exchange returned no usable market metadata")
            version = self.version_for(markets)
            candidate = MetadataSnapshot(self.exchange_id, version, now, dict(markets))
            if self._snapshot and self.change_policy.should_halt(self._snapshot, candidate):
                self._halt_reason = (
                    "market metadata changed; trading halted until explicitly acknowledged "
                    f"({self._snapshot.version[:12]} -> {candidate.version[:12]})"
                )
                raise MetadataChangedError(self._halt_reason)
            self._snapshot = candidate
            return candidate

    def acknowledge_change(self) -> MetadataSnapshot:
        """Explicitly adopt current metadata after an operator review."""
        with self._lock:
            self._halt_reason = None
            self._snapshot = None
        return self.load(force=True)

    @staticmethod
    def version_for(markets: Mapping[str, MarketSpecification]) -> str:
        payload = [markets[symbol].version_payload() for symbol in sorted(markets)]
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
