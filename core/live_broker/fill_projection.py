"""Replay fill ownership independently of exchange-synchronized cash balances."""
from __future__ import annotations

import hashlib
import math
from dataclasses import asdict
from collections import deque

from core.lots import Lot

from core.lots import CloseEvent, LotBook, LotIdAllocator
from core.entry_risk import resolve_approved_risk


class _StableIds(LotIdAllocator):
    def __init__(self):
        self.key = ""

    def next_lot_id(self):
        return "LOT-" + hashlib.sha256(self.key.encode()).hexdigest()[:24]

    def next_position_id(self):
        return "POS-" + hashlib.sha256(self.key.encode()).hexdigest()[:24]


def _legacy_replay_fill_projection(store, quote_currency):
    books, events, issues = {}, [], []
    ids = _StableIds()
    records = {row["client_order_id"]: row for row in store.list_with_fills()}
    fills = [(fill, row) for row in records.values() for fill in store.fills_for(row["client_order_id"])]
    for order_id, row in records.items():
        factual = sum(f["qty"] for f, r in fills if r["client_order_id"] == order_id
                      and not (f.get("payload") or {}).get("synthetic_from_order"))
        if abs(factual - float(row["filled_qty"])) > 1e-8:
            issues.append(f"trade_details_required:{order_id}")
    def ordering(item):
        fill = item[0]
        venue_id = str((fill.get("payload") or {}).get("id") or "")
        return (str(fill.get("timestamp") or ""),
                int(venue_id) if venue_id.isdigit() else int(fill.get("ledger_sequence", 0)))
    fills.sort(key=ordering)
    for fill, row in fills:
        _apply_fill(books, events, issues, ids, fill, row, quote_currency)
    return books, events, sorted(set(issues))


def _apply_fill(books, events, issues, ids, fill, row, quote_currency, *, strict=False):
    if (fill.get("payload") or {}).get("synthetic_from_order"):
        return
    symbol = row["symbol"]
    intent = row.get("intent") or {}
    if strict and (not intent.get("strategy_id") or not intent.get("initial_stop")) and row["side"] in {"buy", "short"}:
        issues.append(f"missing_entry_attribution_or_stop:{row['client_order_id']}")
        return
    if strict and fill.get("fee_evidence") != "recorded":
        issues.append(f"missing_fee_evidence:{fill['fill_id']}")
        return
    qty, price, fee = (float(fill[key]) for key in ("qty", "price", "fee"))
    if not all(math.isfinite(v) for v in (qty, price, fee)) or qty <= 0 or price <= 0:
        issues.append(f"invalid_fill:{fill['fill_id']}")
        return
    base = symbol.split("/")[0]
    fee_currency = fill.get("fee_currency")
    signed = qty if row["side"] in {"buy", "cover"} else -qty
    quote_fee = fee
    if fee and fee_currency == base:
        signed -= fee
        # Net quantity determines inventory; the quote value of the fee is
        # still an economic cost of the surviving lot / closing leg.
        quote_fee = fee * price
    elif fee and fee_currency != quote_currency:
        conversion = (fill.get("payload") or {}).get("fee_quote_price")
        if conversion is None or not math.isfinite(float(conversion)) or float(conversion) <= 0:
            issues.append(f"fee_conversion_required:{fill['fill_id']}")
            return
        quote_fee *= float(conversion)
    book = books.setdefault(symbol, LotBook(symbol, ids))
    if row["side"] in {"sell", "cover"} and abs(signed) > abs(book.net_qty) + 1e-9:
        issues.append(f"unowned_exit:{fill['fill_id']}")
        return
    if row["side"] in {"buy", "short"} and (not intent.get("strategy_id") or not intent.get("initial_stop")):
        issues.append(f"missing_entry_attribution_or_stop:{row['client_order_id']}")
    approved_share = None
    if row["side"] in {"buy", "short"}:
        reference = {**intent, "requested_qty": intent.get("requested_qty", row.get("requested_qty"))}
        if intent.get("approved_risk_amount") is not None or (
                reference.get("requested_qty") and (intent.get("reference_price") or intent.get("price"))):
            try:
                amount, _ = resolve_approved_risk(reference)
                requested = float(reference["requested_qty"])
                if requested <= 0 or not math.isfinite(requested):
                    raise ValueError("invalid requested quantity")
                approved_share = amount * qty / requested
            except (TypeError, ValueError):
                issues.append(f"invalid_entry_risk:{row['client_order_id']}")
                return
    if strict and row["side"] in {"buy", "short"} and approved_share is None:
        issues.append(f"missing_approved_risk:{row['client_order_id']}")
        return
    ids.key = row["client_order_id"]
    closes = book.apply_fill(signed, price, time=fill["timestamp"],
                             strategy_id=intent.get("strategy_id", ""), order_id=row["client_order_id"],
                             stop_price=intent.get("initial_stop"), fee=quote_fee,
                             approved_risk_amount=approved_share)
    for close in closes:
        gross = (price - close.entry_price) * close.qty_closed * (1 if close.side == "long" else -1)
        events.append(CloseEvent(
            close_event_id=f"{fill['fill_id']}:{close.lot_id}", position_id=close.position_id,
            lot_id=close.lot_id, symbol=symbol, opening_strategy_id=close.strategy_id,
            exit_reason=intent.get("exit_reason") or "external_risk_exit",
            qty=close.qty_closed, exit_price=price, theoretical_exit_price=price,
            realized_pnl=gross - close.entry_cost_share - quote_fee * close.qty_closed / abs(signed),
            timestamp=fill["timestamp"], is_position_fully_closed=abs(book.net_qty) < 1e-12,
            initial_risk=close.initial_risk_share, risk_action_id=intent.get("risk_action_id"),
        ))


def _ordering(fill):
    venue_id = str((fill.get("payload") or {}).get("id") or "")
    return (str(fill.get("timestamp") or ""),
            int(venue_id) if venue_id.isdigit() else int(fill.get("ledger_sequence", 0)))


class IncrementalFillProjection:
    """Derived open lots and close facts, atomically checkpointed with cursors.

    First access migrates the immutable raw ledger once. Later calls use
    indexed suffix queries. Late facts are visible invalid states rather than
    retroactively modifying previously delivered close events.
    """
    SCHEMA = "fill-projection/v1"

    def __init__(self, store, quote_currency):
        self.store = store
        self.quote_currency = quote_currency
        self.name = "ownership:" + quote_currency
        self.events = []
        self.event_cursor = 0
        self.processed_fill_count = 0
        self._load()

    def _load(self):
        self.version, state = self.store.projection_checkpoint(self.name)
        state = state or {"schema": self.SCHEMA, "fill_cursor": 0, "last_fill_id": None, "order_cursor": 0,
                          "books": {}, "issues": [], "order_issues": {}, "invalid_symbols": [], "last_ordering": None}
        if state.get("schema") != self.SCHEMA:
            raise ValueError("unsupported_fill_projection_checkpoint")
        self.state = state
        self.ids = _StableIds()
        self.books = {}
        for symbol, row in state["books"].items():
            book = LotBook(symbol, self.ids)
            book._lots = deque(Lot(**lot) for lot in row["lots"])
            book._current_position_id = row["position_id"]
            self.books[symbol] = book
        self._load_events()

    def _load_events(self):
        for sequence, payload in self.store.projection_close_events(self.name, self.event_cursor):
            self.events.append(CloseEvent(**payload))
            self.event_cursor = sequence

    def _state_with_books(self):
        return {**self.state, "books": {
            symbol: {"position_id": book._current_position_id,
                     "lots": [asdict(lot) for lot in book.open_lots]}
            for symbol, book in self.books.items() if book.open_lots
        }}

    def update(self):
        # A second process may have advanced this projection. Load its exact
        # committed open lots instead of re-applying the already handled fills.
        version, _ = self.store.projection_checkpoint(self.name)
        if version != self.version:
            self._load()
        if self.state["fill_cursor"] and self.store.projection_anchor(self.state["fill_cursor"]) != self.state.get("last_fill_id"):
            raise ValueError("fill_projection_cursor_identity_mismatch")
        fills, orders, factual_qty, order_cursor = self.store.projection_delta(
            self.state["fill_cursor"], self.state["order_cursor"])
        if not fills and order_cursor == self.state["order_cursor"]:
            return self.books, self.events, self.issues
        new_events = []
        try:
            order_issues = dict(self.state["order_issues"])
            for order_id, row in orders.items():
                if abs(factual_qty[order_id] - float(row["filled_qty"])) > 1e-8:
                    order_issues[order_id] = f"trade_details_required:{order_id}"
                else:
                    order_issues.pop(order_id, None)
            issues = list(self.state["issues"])
            invalid = set(self.state["invalid_symbols"])
            last = tuple(self.state["last_ordering"]) if self.state["last_ordering"] is not None else None
            for fill in sorted(fills, key=_ordering):
                row = orders.get(fill["client_order_id"])
                if row is None:
                    issues.append(f"missing_fill_order:{fill['fill_id']}")
                    continue
                symbol = row["symbol"]
                key = _ordering(fill)
                if last is not None and key < last:
                    issues.append(f"out_of_order_fill:{fill['fill_id']}")
                    invalid.add(symbol)
                if symbol in invalid:
                    continue
                before = len(issues)
                _apply_fill(self.books, new_events, issues, self.ids, fill, row, self.quote_currency, strict=True)
                self.processed_fill_count += 1
                if len(issues) > before:
                    invalid.add(symbol)
                last = key if last is None else max(last, key)
            latest_fill = max(fills, key=lambda fill: int(fill["ledger_sequence"])) if fills else None
            self.state.update({"fill_cursor": int(latest_fill["ledger_sequence"]) if latest_fill else self.state["fill_cursor"],
                               "last_fill_id": latest_fill["fill_id"] if latest_fill else self.state.get("last_fill_id"),
                               "order_cursor": order_cursor, "order_issues": order_issues,
                               "issues": sorted(set(issues)), "invalid_symbols": sorted(invalid),
                               "last_ordering": list(last) if last is not None else None})
            self.version = self.store.save_projection(self.name, self.version, self._state_with_books(),
                                                      [asdict(event) for event in new_events])
            self._load_events()
        except Exception:
            # In-memory mutations are discarded even if saving was interrupted.
            # Reading the transaction result also handles an ambiguous response
            # after a successful commit without manufacturing duplicate events.
            self._load()
            raise
        return self.books, self.events, self.issues

    @property
    def issues(self):
        return sorted(set(self.state["issues"]) | set(self.state["order_issues"].values()))


def replay_fill_projection(store, quote_currency):
    """Compatibility API; durable OrderStore uses incremental checkpoints."""
    if not hasattr(store, "projection_delta"):
        return _legacy_replay_fill_projection(store, quote_currency)
    cache = store._fill_projection_cache
    if quote_currency not in cache:
        cache[quote_currency] = IncrementalFillProjection(store, quote_currency)
    return cache[quote_currency].update()
