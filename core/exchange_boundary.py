"""Canonical exchange boundary for live order validation and CCXT adaptation.

Only this module understands CCXT market metadata and payload conventions.  The
rest of the application consumes canonical intents, orders, and positions.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from enum import Enum
from threading import RLock
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Tuple

from core.domain import OrderIntent, OrderStatus


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


@dataclass(frozen=True)
class ValidationResult:
    intent: OrderIntent
    market: Optional[MarketSpecification]


class OrderValidator:
    """Validate canonical semantics and venue constraints without I/O."""

    def validate(
        self,
        intent: OrderIntent,
        capabilities: ExchangeCapabilities,
        market: Optional[MarketSpecification],
        *,
        reference_price: Optional[Any] = None,
    ) -> ValidationResult:
        if not isinstance(intent, OrderIntent):
            raise TypeError("intent must be OrderIntent")
        qty = _decimal(intent.requested_qty, "quantity")
        if qty is None or qty <= 0:
            raise OrderValidationError("quantity must be positive and reducible")
        action = str(intent.action).lower()
        if action not in {"buy", "sell", "short", "cover"}:
            raise OrderValidationError("unsupported order side")
        order_type = str(intent.order_type).lower()
        if order_type not in capabilities.order_types:
            raise OrderValidationError(f"unsupported order type: {order_type}")
        if order_type == "limit" and intent.price is None:
            raise OrderValidationError("limit order requires a price")
        if intent.price is not None and _decimal(intent.price, "price") <= 0:
            raise OrderValidationError("price must be positive")
        tif = str(intent.time_in_force).upper() if intent.time_in_force else None
        if tif and tif not in capabilities.time_in_force:
            raise OrderValidationError(f"unsupported time in force: {tif}")
        if intent.reduce_only and not capabilities.supports_reduce_only and market and market.is_derivative:
            raise OrderValidationError("exchange does not support reduce-only orders")
        if intent.position_mode not in {"one_way", "hedge"}:
            raise OrderValidationError("unsupported position mode")
        if intent.position_mode == "hedge" and not capabilities.supports_hedge_mode:
            raise OrderValidationError("exchange does not support hedge mode")
        if market is None:
            return ValidationResult(intent, None)
        if not market.active:
            raise OrderValidationError(f"market is inactive: {intent.symbol}")
        if action in {"short", "cover"} and not market.is_derivative:
            raise OrderValidationError(f"{action} is not supported for spot market {intent.symbol}")
        if market.order_types and order_type not in market.order_types:
            raise OrderValidationError(f"market does not support order type: {order_type}")
        if tif and market.time_in_force and tif not in market.time_in_force:
            raise OrderValidationError(f"market does not support time in force: {tif}")
        self._range(qty, market.min_amount, market.max_amount, "quantity")
        price = _decimal(intent.price, "price", optional=True)
        if price is not None:
            self._range(price, market.min_price, market.max_price, "price")
        notional_price = price or _decimal(reference_price, "reference_price", optional=True)
        if notional_price is not None:
            notional = qty * notional_price * market.contract_size
            self._range(notional, market.min_notional, market.max_notional, "notional")
        elif market.min_notional is not None:
            raise OrderValidationError("reference price is required to validate minimum notional")
        return ValidationResult(intent, market)

    @staticmethod
    def _range(value: Decimal, minimum: Optional[Decimal], maximum: Optional[Decimal], name: str) -> None:
        if minimum is not None and value < minimum:
            raise OrderValidationError(f"{name} {value} is below minimum {minimum}")
        if maximum is not None and value > maximum:
            raise OrderValidationError(f"{name} {value} exceeds maximum {maximum}")


class OrderNormalizer:
    """Quantize amount and price deterministically using market increments."""

    def normalize(self, intent: OrderIntent, market: Optional[MarketSpecification]) -> OrderIntent:
        if market is None:
            return intent
        qty = self._floor(_decimal(intent.requested_qty, "quantity"), market.amount_step)
        price = _decimal(intent.price, "price", optional=True)
        if price is not None:
            price = self._floor(price, market.price_step)
        if qty <= 0:
            raise OrderValidationError("quantity normalizes to zero")
        if price is not None and price <= 0:
            raise OrderValidationError("price normalizes to zero")
        return replace(
            intent,
            requested_qty=float(qty),
            price=None if price is None else float(price),
            order_type=str(intent.order_type).lower(),
            time_in_force=(str(intent.time_in_force).upper() if intent.time_in_force else None),
        )

    @staticmethod
    def _floor(value: Decimal, step: Optional[Decimal]) -> Decimal:
        if step is None:
            return value
        units = (value / step).to_integral_value(rounding=ROUND_DOWN)
        return units * step


@dataclass(frozen=True)
class CCXTOrderRequest:
    symbol: str
    type: str
    side: str
    amount: float
    price: Optional[float]
    params: Mapping[str, Any] = field(default_factory=dict)

    def as_kwargs(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol,
            "type": self.type,
            "side": self.side,
            "amount": self.amount,
            "price": self.price,
            "params": dict(self.params),
        }


class CCXTRequestMapper:
    """The only order-request mapping from domain language to CCXT language."""

    def map(self, intent: OrderIntent, capabilities: ExchangeCapabilities) -> CCXTOrderRequest:
        params: Dict[str, Any] = {}
        if capabilities.supports_client_order_id:
            params["clientOrderId"] = intent.client_order_id
        if intent.reduce_only:
            params["reduceOnly"] = True
        if intent.position_side:
            params["positionSide"] = intent.position_side
        if intent.time_in_force:
            params["timeInForce"] = str(intent.time_in_force).upper()
        side = {"short": "sell", "cover": "buy"}.get(intent.action, intent.action)
        return CCXTOrderRequest(
            symbol=intent.symbol,
            type=str(intent.order_type).lower(),
            side=side,
            amount=float(intent.requested_qty),
            price=None if intent.price is None else float(intent.price),
            params=params,
        )


@dataclass(frozen=True)
class CanonicalOrder:
    exchange_order_id: Optional[str]
    client_order_id: Optional[str]
    symbol: Optional[str]
    status: OrderStatus
    requested_qty: Decimal
    filled_qty: Decimal
    remaining_qty: Decimal
    average_fill_price: Optional[Decimal]


class OrderParser:
    """Parse heterogeneous CCXT order payloads into a canonical order fact."""

    def parse(self, payload: Mapping[str, Any], *, requested_qty: Any = 0) -> CanonicalOrder:
        if not isinstance(payload, Mapping):
            raise ValueError("order payload must be a mapping")
        amount_value = payload.get("amount")
        filled_value = payload.get("filled")
        requested = _decimal(
            requested_qty if amount_value in (None, "") else amount_value, "amount"
        ) or Decimal("0")
        filled = _decimal(0 if filled_value in (None, "") else filled_value, "filled") or Decimal("0")
        remaining_raw = payload.get("remaining")
        remaining = max(requested - filled, Decimal("0")) if remaining_raw is None else (_decimal(remaining_raw, "remaining") or Decimal("0"))
        raw_status = str(payload.get("status") or "").lower()
        if raw_status in {"closed", "filled"} or (requested > 0 and filled >= requested):
            status = OrderStatus.FILLED
            remaining = Decimal("0")
        elif raw_status == "expired":
            status = OrderStatus.EXPIRED
        elif filled > 0:
            status = OrderStatus.PARTIALLY_FILLED
        elif raw_status in {"open", "new", "accepted", "pending"}:
            status = OrderStatus.ACCEPTED
        elif raw_status in {"canceled", "cancelled"}:
            status = OrderStatus.CANCELED
        elif raw_status in {"rejected", "failed"}:
            status = OrderStatus.REJECTED
        elif remaining == 0 and requested > 0:
            status = OrderStatus.FILLED
        else:
            status = OrderStatus.UNKNOWN
        average = _decimal(payload.get("average") or payload.get("price"), "average", optional=True)
        return CanonicalOrder(
            exchange_order_id=None if payload.get("id") is None else str(payload.get("id")),
            client_order_id=_first_text(payload, "clientOrderId", "client_order_id"),
            symbol=_first_text(payload, "symbol"),
            status=status,
            requested_qty=requested,
            filled_qty=filled,
            remaining_qty=remaining,
            average_fill_price=average if average and average > 0 else None,
        )


@dataclass(frozen=True)
class CanonicalPosition:
    symbol: str
    qty: Decimal
    average_entry_price: Decimal = Decimal("0")
    position_side: Optional[str] = None


class PositionParser:
    """Parse signed and side+magnitude position formats without venue branches."""

    def parse(self, payload: Mapping[str, Any]) -> Optional[CanonicalPosition]:
        if not isinstance(payload, Mapping):
            raise ValueError("position payload must be a mapping")
        info = _mapping(payload.get("info"))
        symbol = _first_text(payload, "symbol") or _first_text(info, "symbol")
        if not symbol:
            raise ValueError("position symbol is unavailable")
        direction, position_side = self._direction(payload, info)
        qty: Optional[Decimal] = None
        for source in (payload, info):
            if source.get("positionAmt") not in (None, ""):
                qty = _decimal(source.get("positionAmt"), "positionAmt")
                if qty and direction is not None and (qty > 0) != (direction > 0):
                    raise ValueError("derivative position side conflicts with signed quantity")
                break
        if qty is None:
            for name in ("contracts", "qty", "size"):
                value = payload.get(name, info.get(name))
                if value in (None, ""):
                    continue
                magnitude = abs(_decimal(value, name) or Decimal("0"))
                if magnitude and direction is None:
                    raise ValueError("derivative position direction is unavailable")
                qty = magnitude * (direction or Decimal("1"))
                break
        qty = qty or Decimal("0")
        if qty == 0:
            return None
        average = Decimal("0")
        for name in ("entryPrice", "avgPrice", "average"):
            value = payload.get(name, info.get(name))
            candidate = _decimal(value, name, optional=True)
            if candidate is not None and candidate > 0:
                average = candidate
                break
        return CanonicalPosition(symbol, qty, average, position_side)

    @staticmethod
    def _direction(payload: Mapping[str, Any], info: Mapping[str, Any]) -> Tuple[Optional[Decimal], Optional[str]]:
        for name in ("side", "positionSide"):
            value = payload.get(name, info.get(name))
            normalized = str(value or "").strip().lower()
            if normalized in {"long", "buy"}:
                return Decimal("1"), normalized
            if normalized in {"short", "sell"}:
                return Decimal("-1"), normalized
        return None, None


def _first_text(payload: Mapping[str, Any], *names: str) -> Optional[str]:
    for name in names:
        value = payload.get(name)
        if value not in (None, ""):
            return str(value)
    return None


@dataclass(frozen=True)
class PreparedOrder:
    intent: OrderIntent
    market: Optional[MarketSpecification]
    metadata_version: Optional[str]
    request: CCXTOrderRequest


class ExchangeBoundary:
    """Orchestrates metadata, validation, normalization, and request mapping."""

    def __init__(
        self,
        capabilities: ExchangeCapabilities,
        metadata: Optional[MarketMetadataLoader] = None,
        *,
        require_metadata: bool = True,
        validator: Optional[OrderValidator] = None,
        normalizer: Optional[OrderNormalizer] = None,
        request_mapper: Optional[CCXTRequestMapper] = None,
    ) -> None:
        self.capabilities = capabilities
        self.metadata = metadata
        self.require_metadata = require_metadata
        self.validator = validator or OrderValidator()
        self.normalizer = normalizer or OrderNormalizer()
        self.request_mapper = request_mapper or CCXTRequestMapper()
        self.order_parser = OrderParser()
        self.position_parser = PositionParser()

    def prepare(self, intent: OrderIntent, *, reference_price: Any = None) -> PreparedOrder:
        # Generic validation always happens before metadata I/O.
        self.validator.validate(intent, self.capabilities, None, reference_price=reference_price)
        snapshot: Optional[MetadataSnapshot] = None
        market: Optional[MarketSpecification] = None
        if self.metadata is not None:
            try:
                snapshot = self.metadata.load()
                market = snapshot.market(intent.symbol)
            except MetadataUnavailableError:
                if self.require_metadata:
                    raise
        elif self.require_metadata:
            raise MetadataUnavailableError("market metadata loader is not configured")
        normalized = self.normalizer.normalize(intent, market)
        # Validate after rounding because normalization can cross a venue limit.
        self.validator.validate(normalized, self.capabilities, market, reference_price=reference_price)
        return PreparedOrder(
            normalized,
            market,
            None if snapshot is None else snapshot.version,
            self.request_mapper.map(normalized, self.capabilities),
        )

