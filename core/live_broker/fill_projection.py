"""Replay fill ownership independently of exchange-synchronized cash balances."""
from __future__ import annotations

import hashlib
import math

from core.lots import CloseEvent, LotBook, LotIdAllocator


class _StableIds(LotIdAllocator):
    def __init__(self):
        self.key = ""

    def next_lot_id(self):
        return "LOT-" + hashlib.sha256(self.key.encode()).hexdigest()[:24]

    def next_position_id(self):
        return "POS-" + hashlib.sha256(self.key.encode()).hexdigest()[:24]


def replay_fill_projection(store, quote_currency):
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
        symbol = row["symbol"]
        intent = row.get("intent") or {}
        qty, price, fee = (float(fill[key]) for key in ("qty", "price", "fee"))
        if not all(math.isfinite(v) for v in (qty, price, fee)) or qty <= 0 or price <= 0:
            issues.append(f"invalid_fill:{fill['fill_id']}")
            continue
        if (fill.get("payload") or {}).get("synthetic_from_order"):
            continue
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
                continue
            quote_fee *= float(conversion)
        book = books.setdefault(symbol, LotBook(symbol, ids))
        if row["side"] in {"sell", "cover"} and abs(signed) > abs(book.net_qty) + 1e-9:
            issues.append(f"unowned_exit:{fill['fill_id']}")
            continue
        if row["side"] in {"buy", "short"} and (not intent.get("strategy_id") or not intent.get("initial_stop")):
            issues.append(f"missing_entry_attribution_or_stop:{row['client_order_id']}")
        ids.key = row["client_order_id"]
        closes = book.apply_fill(signed, price, time=fill["timestamp"],
                                 strategy_id=intent.get("strategy_id", ""), order_id=row["client_order_id"],
                                 stop_price=intent.get("initial_stop"), fee=quote_fee)
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
    return books, events, sorted(set(issues))
