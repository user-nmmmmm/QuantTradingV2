"""Offline event-sourced ledger for audit and reconciliation research.

This module is NOT wired into the live or backtest order path. ``Portfolio``
and ``LotBook`` (see ``core/portfolio.py``, ``core/lots.py``) remain the
single authoritative source of account facts for trading. ``AuthoritativeLedger``
and ``PortfolioProjection`` here exist for offline event-sourced replay and
reconciliation research; do not import this module from ``core``,
``router``, ``strategies``, ``backtest``, or ``live_trading``.

See docs/architecture_review.md (A3) for the reasoning behind this decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple
from uuid import UUID

from core.domain import PortfolioSnapshot
from core.event_store import SQLiteEventStore
from core.events import EventEnvelope, FillEvent, TradingEventPipeline


ZERO = Decimal("0")
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def as_decimal(value: Any, name: str = "value") -> Decimal:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be numeric")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{name} must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be a finite decimal")
    return result


def _currency(value: str, name: str = "currency") -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip().upper()


def quote_currency_for(symbol: str, explicit: Optional[str] = None) -> str:
    if explicit:
        return _currency(explicit, "quote_currency")
    instrument = symbol.split(":", 1)[0]
    for separator in ("/", "-"):
        if separator in instrument:
            return _currency(instrument.rsplit(separator, 1)[1], "quote_currency")
    raise ValueError(
        f"Cannot infer quote currency for {symbol!r}; provide quote_currency"
    )


@dataclass(frozen=True)
class CashEvent:
    currency: str
    amount: Decimal
    reason: str = "adjustment"
    reference: Optional[str] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "currency", _currency(self.currency))
        object.__setattr__(self, "amount", as_decimal(self.amount, "amount"))
        if not self.reason:
            raise ValueError("reason is required")


@dataclass(frozen=True)
class MarkPriceEvent:
    symbol: str
    price: Decimal
    quote_currency: str
    price_time: datetime

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("symbol is required")
        price = as_decimal(self.price, "price")
        if price <= ZERO:
            raise ValueError("price must be positive")
        if self.price_time.tzinfo is None or self.price_time.utcoffset() is None:
            raise ValueError("price_time must be timezone-aware")
        object.__setattr__(self, "price", price)
        object.__setattr__(self, "quote_currency", _currency(self.quote_currency))
        object.__setattr__(
            self, "price_time", self.price_time.astimezone(timezone.utc)
        )


@dataclass(frozen=True)
class PositionState:
    symbol: str
    qty: Decimal = ZERO
    average_cost: Decimal = ZERO
    realized_pnl: Decimal = ZERO
    quote_currency: str = "USD"

    @property
    def avg_price(self) -> Decimal:
        return self.average_cost

    def unrealized_pnl(self, mark_price: Any) -> Decimal:
        mark = as_decimal(mark_price, "mark_price")
        return self.qty * (mark - self.average_cost)


@dataclass(frozen=True)
class PositionReduction:
    before: PositionState
    after: PositionState
    realized_delta: Decimal
    closed_qty: Decimal
    opened_qty: Decimal


class AverageCostPositionReducer:
    """Average-cost reducer with correct close-and-reverse semantics."""

    @staticmethod
    def reduce(
        position: PositionState,
        qty_delta: Any,
        price: Any,
        *,
        quote_currency: Optional[str] = None,
    ) -> PositionReduction:
        delta = as_decimal(qty_delta, "qty_delta")
        fill_price = as_decimal(price, "price")
        if delta == ZERO:
            raise ValueError("qty_delta cannot be zero")
        if fill_price <= ZERO:
            raise ValueError("price must be positive")
        old_qty = position.qty
        new_qty = old_qty + delta
        currency = _currency(quote_currency or position.quote_currency)

        if old_qty == ZERO:
            after = PositionState(
                position.symbol, new_qty, fill_price, position.realized_pnl, currency
            )
            return PositionReduction(
                position, after, ZERO, ZERO, abs(delta)
            )

        same_direction = (old_qty > ZERO and delta > ZERO) or (
            old_qty < ZERO and delta < ZERO
        )
        if same_direction:
            average = (
                abs(old_qty) * position.average_cost + abs(delta) * fill_price
            ) / abs(new_qty)
            after = replace(
                position,
                qty=new_qty,
                average_cost=average,
                quote_currency=currency,
            )
            return PositionReduction(position, after, ZERO, ZERO, abs(delta))

        closed = min(abs(old_qty), abs(delta))
        direction = Decimal("1") if old_qty > ZERO else Decimal("-1")
        realized = closed * (fill_price - position.average_cost) * direction
        remaining_open = max(ZERO, abs(delta) - abs(old_qty))
        if new_qty == ZERO:
            average = ZERO
        elif remaining_open > ZERO:
            # The over-closing portion starts the opposite position at this fill.
            average = fill_price
        else:
            # A partial close never changes the surviving lot's cost basis.
            average = position.average_cost
        after = replace(
            position,
            qty=new_qty,
            average_cost=average,
            realized_pnl=position.realized_pnl + realized,
            quote_currency=currency,
        )
        return PositionReduction(
            position, after, realized, closed, remaining_open
        )


@dataclass(frozen=True)
class FillPosting:
    event_id: UUID
    fill_id: str
    sequence: int
    symbol: str
    side: str
    qty: Decimal
    signed_qty: Decimal
    price: Decimal
    quote_currency: str
    realized_pnl: Decimal


class FillLedger:
    def __init__(self) -> None:
        self._postings: list[FillPosting] = []
        self._by_event: Dict[UUID, FillPosting] = {}

    @property
    def postings(self) -> Tuple[FillPosting, ...]:
        return tuple(self._postings)

    def apply(
        self, event: EventEnvelope, position: PositionState, sequence: int
    ) -> Tuple[FillPosting, PositionState]:
        if not isinstance(event.payload, FillEvent):
            raise TypeError("FillLedger requires a FillEvent envelope")
        existing = self._by_event.get(event.event_id)
        if existing is not None:
            return existing, position
        fill = event.payload
        side = fill.side.strip().lower()
        if side in {"buy", "b"}:
            signed_qty = fill.qty
        elif side in {"sell", "s"}:
            signed_qty = -fill.qty
        else:
            raise ValueError(f"Unsupported fill side: {fill.side!r}")
        explicit_quote = getattr(fill, "quote_currency", None)
        quote = quote_currency_for(fill.symbol, explicit_quote)
        reduction = AverageCostPositionReducer.reduce(
            position, signed_qty, fill.price, quote_currency=quote
        )
        posting = FillPosting(
            event.event_id,
            fill.fill_id,
            sequence,
            fill.symbol,
            side,
            fill.qty,
            signed_qty,
            fill.price,
            quote,
            reduction.realized_delta,
        )
        self._postings.append(posting)
        self._by_event[event.event_id] = posting
        return posting, reduction.after


@dataclass(frozen=True)
class FeePosting:
    event_id: UUID
    fill_id: str
    sequence: int
    amount: Decimal
    currency: str


class FeeLedger:
    def __init__(self) -> None:
        self._entries: list[FeePosting] = []
        self._by_event: Dict[UUID, FeePosting] = {}

    @property
    def entries(self) -> Tuple[FeePosting, ...]:
        return tuple(self._entries)

    @property
    def totals(self) -> Dict[str, Decimal]:
        result: Dict[str, Decimal] = {}
        for entry in self._entries:
            result[entry.currency] = result.get(entry.currency, ZERO) + entry.amount
        return result

    def apply(
        self, event: EventEnvelope, quote_currency: str, sequence: int
    ) -> Optional[FeePosting]:
        fill = event.payload
        if not isinstance(fill, FillEvent):
            raise TypeError("FeeLedger requires a FillEvent envelope")
        if event.event_id in self._by_event:
            return self._by_event[event.event_id]
        if fill.fee == ZERO:
            return None
        currency = _currency(fill.fee_currency or quote_currency)
        entry = FeePosting(
            event.event_id, fill.fill_id, sequence, fill.fee, currency
        )
        self._entries.append(entry)
        self._by_event[event.event_id] = entry
        return entry


class CashLedger:
    def __init__(self) -> None:
        self._balances: Dict[str, Decimal] = {}
        self._seen: set[UUID] = set()

    @property
    def balances(self) -> Dict[str, Decimal]:
        return dict(self._balances)

    def post(self, event_id: UUID, currency: str, amount: Any) -> bool:
        if event_id in self._seen:
            return False
        code = _currency(currency)
        self._balances[code] = self._balances.get(code, ZERO) + as_decimal(
            amount, "cash amount"
        )
        self._seen.add(event_id)
        return True

    def apply_cash_event(self, event: EventEnvelope) -> bool:
        if not isinstance(event.payload, CashEvent):
            raise TypeError("CashLedger requires a CashEvent envelope")
        return self.post(
            event.event_id, event.payload.currency, event.payload.amount
        )

    def apply_fill(
        self,
        event: EventEnvelope,
        posting: FillPosting,
        fee: Optional[FeePosting],
    ) -> bool:
        if event.event_id in self._seen:
            return False
        # Positive signed quantity consumes quote cash; a sale produces it.
        quote_flow = -(posting.signed_qty * posting.price)
        self._balances[posting.quote_currency] = (
            self._balances.get(posting.quote_currency, ZERO) + quote_flow
        )
        if fee is not None:
            self._balances[fee.currency] = (
                self._balances.get(fee.currency, ZERO) - fee.amount
            )
        self._seen.add(event.event_id)
        return True


@dataclass(frozen=True)
class ReconciliationDiscrepancy:
    category: str
    key: str
    expected: Decimal
    actual: Decimal
    difference: Decimal
    tolerance: Decimal
    severity: str
    requires_manual_review: bool


@dataclass(frozen=True)
class ReconciliationReport:
    discrepancies: Tuple[ReconciliationDiscrepancy, ...]
    checked_at: datetime

    @property
    def ok(self) -> bool:
        return not self.discrepancies

    @property
    def has_critical(self) -> bool:
        return any(item.severity == "critical" for item in self.discrepancies)


class PortfolioProjection:
    """Projection whose complete state is derived only from ledger events."""

    def __init__(self, base_currency: str = "USD") -> None:
        self.base_currency = _currency(base_currency, "base_currency")
        self.positions: Dict[str, PositionState] = {}
        self.cash = CashLedger()
        self.fills = FillLedger()
        self.fees = FeeLedger()
        self.marks: Dict[str, MarkPriceEvent] = {}
        self.realized_by_currency: Dict[str, Decimal] = {}
        self.last_sequence = 0
        self.last_event_time = EPOCH
        self._seen: set[UUID] = set()

    def apply(self, event: EventEnvelope, sequence: Optional[int] = None) -> bool:
        if not isinstance(event, EventEnvelope):
            raise TypeError("projection accepts only EventEnvelope values")
        if event.event_id in self._seen:
            return False
        resolved_sequence = sequence if sequence is not None else self.last_sequence + 1
        if resolved_sequence <= self.last_sequence:
            raise ValueError("events must be applied in strictly increasing sequence")
        payload = event.payload
        recognized = True
        if isinstance(payload, CashEvent):
            self.cash.apply_cash_event(event)
        elif isinstance(payload, FillEvent):
            current = self.positions.get(
                payload.symbol,
                PositionState(
                    payload.symbol,
                    quote_currency=quote_currency_for(
                        payload.symbol, getattr(payload, "quote_currency", None)
                    ),
                ),
            )
            posting, updated = self.fills.apply(event, current, resolved_sequence)
            self.realized_by_currency[posting.quote_currency] = (
                self.realized_by_currency.get(posting.quote_currency, ZERO)
                + posting.realized_pnl
            )
            fee = self.fees.apply(event, posting.quote_currency, resolved_sequence)
            self.cash.apply_fill(event, posting, fee)
            if updated.qty == ZERO:
                self.positions.pop(payload.symbol, None)
            else:
                self.positions[payload.symbol] = updated
        elif isinstance(payload, MarkPriceEvent):
            self.marks[payload.symbol] = payload
        else:
            recognized = False
        self._seen.add(event.event_id)
        self.last_sequence = resolved_sequence
        self.last_event_time = max(self.last_event_time, event.occurred_at)
        return recognized

    def reset(self) -> None:
        base_currency = self.base_currency
        self.__init__(base_currency)

    def rebuild(
        self,
        store: SQLiteEventStore,
        *,
        account_id: Optional[str] = None,
        through_sequence: Optional[int] = None,
    ) -> "PortfolioProjection":
        self.reset()
        for record in store.read_records(
            account_id=account_id, through_sequence=through_sequence
        ):
            self.apply(record.event, record.sequence)
        return self

    def snapshot(
        self,
        *,
        fx_rates: Optional[Mapping[str, Any]] = None,
        synced_at: Optional[datetime] = None,
        require_marks: bool = True,
    ) -> PortfolioSnapshot:
        rates = {key.upper(): as_decimal(value, f"fx rate {key}") for key, value in (fx_rates or {}).items()}
        for key, value in rates.items():
            if value <= ZERO:
                raise ValueError(f"fx rate {key} must be positive")

        def convert(amount: Decimal, currency: str) -> Decimal:
            code = _currency(currency)
            if code == self.base_currency:
                return amount
            direct = rates.get(code) or rates.get(f"{code}/{self.base_currency}")
            inverse = rates.get(f"{self.base_currency}/{code}")
            if direct is not None:
                return amount * direct
            if inverse is not None:
                return amount / inverse
            raise ValueError(
                f"Missing FX rate from {code} to {self.base_currency}"
            )

        missing = sorted(
            symbol for symbol in self.positions if symbol not in self.marks
        )
        if require_marks and missing:
            raise ValueError(
                f"Missing mark prices for positions: {', '.join(missing)}"
            )

        equity = sum(
            (convert(amount, currency) for currency, amount in self.cash.balances.items()),
            ZERO,
        )
        gross = ZERO
        net = ZERO
        unrealized = ZERO
        realized = sum(
            (convert(amount, currency) for currency, amount in self.realized_by_currency.items()),
            ZERO,
        )
        prices: Dict[str, float] = {}
        price_times: Dict[str, datetime] = {}
        positions: Dict[str, Dict[str, Any]] = {}
        for symbol, position in self.positions.items():
            mark_event = self.marks.get(symbol)
            mark = mark_event.price if mark_event else position.average_cost
            quote = position.quote_currency
            market_value = convert(position.qty * mark, quote)
            equity += market_value
            gross += abs(market_value)
            net += market_value
            unrealized += convert(position.unrealized_pnl(mark), quote)
            prices[symbol] = float(mark)
            if mark_event:
                price_times[symbol] = mark_event.price_time
            positions[symbol] = {
                "qty": position.qty,
                "avg_price": position.average_cost,
                "average_cost": position.average_cost,
                "mark_price": mark,
                "market_value": market_value,
                "realized_pnl": position.realized_pnl,
                "unrealized_pnl": position.unrealized_pnl(mark),
                "quote_currency": quote,
            }
        effective_time = synced_at or self.last_event_time
        if effective_time.tzinfo is None or effective_time.utcoffset() is None:
            raise ValueError("synced_at must be timezone-aware")
        return PortfolioSnapshot(
            cash=float(convert(
                self.cash.balances.get(self.base_currency, ZERO),
                self.base_currency,
            )),
            equity=float(equity),
            gross_exposure=float(gross),
            net_exposure=float(net),
            prices=prices,
            price_times=price_times,
            synced_at=effective_time.astimezone(timezone.utc),
            positions=positions,
            cash_balances=self.cash.balances,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            fees=self.fees.totals,
            base_currency=self.base_currency,
            last_sequence=self.last_sequence,
        )

    def reconcile(
        self,
        *,
        external_cash: Mapping[str, Any],
        external_positions: Mapping[str, Any],
        cash_tolerance: Any = "0.00000001",
        position_tolerance: Any = "0.00000001",
        checked_at: Optional[datetime] = None,
    ) -> ReconciliationReport:
        cash_limit = as_decimal(cash_tolerance, "cash_tolerance")
        position_limit = as_decimal(position_tolerance, "position_tolerance")
        if cash_limit < ZERO or position_limit < ZERO:
            raise ValueError("reconciliation tolerances cannot be negative")
        discrepancies: list[ReconciliationDiscrepancy] = []

        local_cash = self.cash.balances
        normalized_external_cash = {
            _currency(key): as_decimal(value, f"cash {key}")
            for key, value in external_cash.items()
        }
        for currency in sorted(set(local_cash) | set(normalized_external_cash)):
            self._compare(
                discrepancies,
                "cash",
                currency,
                local_cash.get(currency, ZERO),
                normalized_external_cash.get(currency, ZERO),
                cash_limit,
            )

        normalized_external_positions: Dict[str, Decimal] = {}
        for symbol, value in external_positions.items():
            raw_qty = value.get("qty", ZERO) if isinstance(value, Mapping) else value
            normalized_external_positions[symbol] = as_decimal(
                raw_qty, f"position {symbol}"
            )
        local_positions = {
            symbol: position.qty for symbol, position in self.positions.items()
        }
        for symbol in sorted(
            set(local_positions) | set(normalized_external_positions)
        ):
            self._compare(
                discrepancies,
                "position",
                symbol,
                local_positions.get(symbol, ZERO),
                normalized_external_positions.get(symbol, ZERO),
                position_limit,
            )

        time = checked_at or self.last_event_time
        if time.tzinfo is None or time.utcoffset() is None:
            raise ValueError("checked_at must be timezone-aware")
        return ReconciliationReport(
            tuple(discrepancies), time.astimezone(timezone.utc)
        )

    @staticmethod
    def _compare(
        target: list[ReconciliationDiscrepancy],
        category: str,
        key: str,
        expected: Decimal,
        actual: Decimal,
        tolerance: Decimal,
    ) -> None:
        difference = actual - expected
        if abs(difference) <= tolerance:
            return
        critical_threshold = max(tolerance * Decimal("10"), Decimal("0.00000001"))
        severity = "critical" if abs(difference) > critical_threshold else "warning"
        target.append(
            ReconciliationDiscrepancy(
                category,
                key,
                expected,
                actual,
                difference,
                tolerance,
                severity,
                severity == "critical",
            )
        )


class AuthoritativeLedger:
    """Facade coupling persistence, publication, projection, and rebuild."""

    def __init__(
        self,
        store: SQLiteEventStore,
        *,
        account_id: str,
        base_currency: str = "USD",
        run_id: Optional[str] = None,
        clock=None,
    ) -> None:
        self.store = store
        self.account_id = account_id
        self.pipeline = TradingEventPipeline(
            run_id=run_id, clock=clock, store=store
        )
        self.projection = PortfolioProjection(base_currency)
        self.projection.rebuild(store, account_id=account_id)

    def record_cash(
        self,
        currency: str,
        amount: Any,
        *,
        reason: str = "adjustment",
        reference: Optional[str] = None,
        occurred_at: Optional[datetime] = None,
        idempotency_key: Optional[str] = None,
        source: str = "ledger",
    ) -> EventEnvelope:
        payload = CashEvent(currency, as_decimal(amount), reason, reference)
        event = self.pipeline.publish(
            payload,
            event_type="cash",
            occurred_at=occurred_at,
            account_id=self.account_id,
            idempotency_key=idempotency_key,
            source=source,
        )
        self._project_stored(event)
        return event

    def record_mark(
        self,
        symbol: str,
        price: Any,
        quote_currency: str,
        price_time: datetime,
        *,
        idempotency_key: Optional[str] = None,
        source: str = "market",
    ) -> EventEnvelope:
        payload = MarkPriceEvent(
            symbol, as_decimal(price), quote_currency, price_time
        )
        event = self.pipeline.publish(
            payload,
            event_type="mark_price",
            occurred_at=price_time,
            account_id=self.account_id,
            symbol=symbol,
            idempotency_key=idempotency_key,
            source=source,
        )
        self._project_stored(event)
        return event

    def record_fill(self, event: EventEnvelope) -> bool:
        if not isinstance(event.payload, FillEvent):
            raise TypeError("record_fill requires a FillEvent envelope")
        if event.account_id != self.account_id:
            raise ValueError("fill account does not match ledger account")
        self.pipeline.consume(event)
        return self._project_stored(event)

    def rebuild(self, through_sequence: Optional[int] = None) -> PortfolioProjection:
        return self.projection.rebuild(
            self.store,
            account_id=self.account_id,
            through_sequence=through_sequence,
        )

    def snapshot(self, **kwargs: Any) -> PortfolioSnapshot:
        return self.projection.snapshot(**kwargs)

    def _project_stored(self, event: EventEnvelope) -> bool:
        stored = self.store.get(event.event_id)
        if stored is None:
            raise RuntimeError("event was not durably stored")
        return self.projection.apply(stored.event, stored.sequence)


__all__ = [
    "CashEvent",
    "MarkPriceEvent",
    "PositionState",
    "PositionReduction",
    "AverageCostPositionReducer",
    "FillPosting",
    "FillLedger",
    "FeePosting",
    "FeeLedger",
    "CashLedger",
    "ReconciliationDiscrepancy",
    "ReconciliationReport",
    "PortfolioProjection",
    "AuthoritativeLedger",
    "as_decimal",
    "quote_currency_for",
]
