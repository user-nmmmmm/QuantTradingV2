"""Read-only, pinned exports for full-account reconciliation.

The adapter never queries a venue, writes a ledger, or infers opening capital.
An export has ``schema_version=1``, ``source_id``, ``evidence_kind`` (either
``synthetic_fixture`` or ``independent_export``), ``identity``, ``captured_at``,
``coverage`` (``from``, ``through``, and boolean ``complete``),
``opening_capital`` (``at``, ``amount``, ``currency``, ``source_id``, ``positions``),
``financing`` records, and a ``snapshot`` following account_reconciliation.py.
Opening positions must explicitly be empty; inventory migration is unsupported.
Cashflows carry ``at``, signed ``amount`` in the account currency, ``currency``
and ``source_id``. Fills carry native ``fee_amount``, ``fee_currency``,
``fee_base_amount``, ``fee_conversion_rate``, ``fee_conversion_source`` and
``fee_conversion_at``, alongside identity, price, quantity, side and time.
Every position includes a source and timestamp for its account-currency mark.
The snapshot's order/fill/cashflow lists cover the entire opening-to-capture
interval ``(opening_at, captured_at]``; the opening anchor precedes all events.
Exporters must declare complete account scope, including external
orders, assets, liabilities, fees and transfers, rather than only strategy data.

Configure the expected SHA-256, source id and identity outside the export.
A matching digest proves byte integrity, not truth or independent collection.
``provenance_verifier(digest, identity)`` is an optional *trusted external*
attestation boundary. Without its explicit True result, an independent export
can pass arithmetic checks but cannot authorize production risk. Synthetic
fixtures can never become production verified. No JSON flag grants trust.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol


class AccountSourceError(ValueError):
    """A source fact is unknown, incomplete, stale, or inconsistent."""


def _number(value, field, *, minimum=None):
    try:
        if value is None or isinstance(value, bool):
            raise ValueError()
        result = float(value)
        if not math.isfinite(result) or (minimum is not None and result < minimum):
            raise ValueError()
        return result
    except (TypeError, ValueError, OverflowError) as exc:
        raise AccountSourceError(f"{field}:invalid_number") from exc


def _time(value, field):
    try:
        if isinstance(value, bool):
            raise ValueError()
        if isinstance(value, (float, int)):
            value = datetime.fromtimestamp(float(value) / 1000, tz=timezone.utc)
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError) as exc:
        raise AccountSourceError(f"{field}:invalid_timestamp") from exc


def _records(value, field):
    if not isinstance(value, list):
        raise AccountSourceError(f"{field}:records_unavailable")
    indexed = {}
    for row in value:
        if not isinstance(row, dict) or not isinstance(row.get("record_id"), str) or not row["record_id"]:
            raise AccountSourceError(f"{field}:record_id_unavailable")
        if row["record_id"] in indexed:
            raise AccountSourceError(f"{field}:duplicate_record")
        indexed[row["record_id"]] = row
    return indexed


@dataclass(frozen=True)
class AccountSourceBundle:
    payload: Mapping[str, Any]
    sha256: str
    production_provenance_verified: bool = False


class ReadOnlyAccountSource(Protocol):
    def read(self, *, identity: Mapping[str, str], checked_at: datetime) -> AccountSourceBundle: ...


class PinnedAccountExport:
    """Read a pre-collected export; never treats a file's trust claim as proof."""

    def __init__(self, path, *, sha256: str, source_id: str,
                 evidence_kind: str = "independent_export",
                 provenance_verifier: Callable[[str, Mapping[str, str]], bool] | None = None):
        if len(sha256) != 64 or any(char not in "0123456789abcdef" for char in sha256):
            raise ValueError("sha256 must be a lowercase SHA-256 digest")
        if not source_id or evidence_kind not in {"synthetic_fixture", "independent_export"}:
            raise ValueError("explicit source_id and evidence_kind are required")
        self.path, self.sha256, self.source_id = Path(path), sha256, source_id
        self.evidence_kind, self.provenance_verifier = evidence_kind, provenance_verifier

    def read(self, *, identity, checked_at):
        raw = self.path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != self.sha256:
            raise AccountSourceError("source_hash_mismatch")
        def unique_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise AccountSourceError("source_duplicate_json_key")
                result[key] = value
            return result
        payload = json.loads(raw, object_pairs_hook=unique_pairs)
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise AccountSourceError("source_schema_unavailable")
        if payload.get("source_id") != self.source_id or payload.get("evidence_kind") != self.evidence_kind:
            raise AccountSourceError("source_identity_mismatch")
        if payload.get("identity") != dict(identity):
            raise AccountSourceError("account_identity_mismatch")
        verified = (self.evidence_kind == "independent_export"
                    and self.provenance_verifier is not None
                    and self.provenance_verifier(self.sha256, dict(identity)) is True)
        return AccountSourceBundle(payload, self.sha256, verified)


def normalize_account_source(bundle, *, identity, checked_at, maximum_age_seconds=90):
    """Validate coverage and fact provenance before using an independent export."""
    now = _time(checked_at, "checked_at")
    maximum_age = _number(maximum_age_seconds, "maximum_age_seconds", minimum=0)
    if maximum_age == 0:
        raise AccountSourceError("maximum_age_seconds:must_be_positive")
    data = deepcopy(dict(bundle.payload))
    if data.get("schema_version") != 1 or data.get("identity") != dict(identity):
        raise AccountSourceError("account_identity_mismatch")
    if not data.get("source_id") or data.get("evidence_kind") not in {"synthetic_fixture", "independent_export"}:
        raise AccountSourceError("source_provenance_unavailable")
    snapshot = data.get("snapshot")
    if not isinstance(snapshot, dict) or snapshot.get("identity") != dict(identity):
        raise AccountSourceError("snapshot_identity_mismatch")
    captured = _time(data.get("captured_at"), "captured_at")
    if captured != _time(snapshot.get("captured_at"), "snapshot.captured_at"):
        raise AccountSourceError("snapshot_capture_mismatch")
    if not 0 <= (now - captured).total_seconds() <= maximum_age:
        raise AccountSourceError("account_source_stale_or_future")
    opening = data.get("opening_capital")
    if not isinstance(opening, dict) or not opening.get("source_id"):
        raise AccountSourceError("opening_capital_source_unavailable")
    if opening.get("positions") != []:
        raise AccountSourceError("opening_inventory_migration_required")
    currency = identity["base_currency"]
    if opening.get("currency") != currency:
        raise AccountSourceError("opening_capital_currency_mismatch")
    opening["amount"] = _number(opening.get("amount"), "opening_capital.amount", minimum=0)
    start = _time(opening.get("at"), "opening_capital.at")
    coverage = data.get("coverage")
    if not isinstance(coverage, dict) or coverage.get("complete") is not True or coverage.get("scope") != "entire_account":
        raise AccountSourceError("complete_account_coverage_unavailable")
    if (_time(coverage.get("from"), "coverage.from") != start
            or _time(coverage.get("through"), "coverage.through") != captured or start > captured):
        raise AccountSourceError("source_coverage_gap")
    def event_time(value, name):
        at = _time(value, name)
        if not start < at <= captured:
            raise AccountSourceError(f"{name}:outside_coverage")
        return at
    for section in ("cashflows", "positions", "orders", "fills"):
        _records(snapshot.get(section), section)
    cash = snapshot.get("cash")
    if not isinstance(cash, dict):
        raise AccountSourceError("cash_facts_unavailable")
    for key in ("free", "locked", "total"):
        minimum = 0 if identity["market_type"] == "spot" or key == "locked" else None
        cash[key] = _number(cash.get(key), "cash." + key, minimum=minimum)
    for row in snapshot["orders"]:
        for key in ("requested_qty", "filled_qty", "remaining_qty"):
            row[key] = _number(row.get(key), "order." + key, minimum=0)
    for row in snapshot["cashflows"]:
        row["at"] = event_time(row.get("at"), "cashflow.at").isoformat()
        if row.get("currency") != currency or not row.get("source_id"):
            raise AccountSourceError("cashflow_currency_or_source_unavailable")
        row["amount"] = _number(row.get("amount"), "cashflow.amount")
        # Converted/non-cash deposits need an explicit inventory/FX migration.
        if row.get("kind") not in {"deposit", "withdrawal"} or (
                row["kind"] == "deposit" and row["amount"] <= 0) or (
                row["kind"] == "withdrawal" and row["amount"] >= 0):
            raise AccountSourceError("cashflow_direction_invalid")
    financing = _records(data.get("financing"), "financing")
    for row in financing.values():
        row["at"] = event_time(row.get("at"), "financing.at").isoformat()
        if row.get("currency") != currency or not row.get("source_id"):
            raise AccountSourceError("financing_currency_or_source_unavailable")
        row["amount"] = _number(row.get("amount"), "financing.amount", minimum=0)
    for row in snapshot["fills"]:
        fill_at = event_time(row.get("at"), "fill.at")
        conversion_at = event_time(row.get("fee_conversion_at"), "fill.fee_conversion_at")
        row["at"], row["fee_conversion_at"] = fill_at.isoformat(), conversion_at.isoformat()
        if abs((fill_at - conversion_at).total_seconds()) > maximum_age:
            raise AccountSourceError("fee_conversion_stale")
        if not row.get("fee_currency") or not row.get("fee_conversion_source"):
            raise AccountSourceError("fee_conversion_source_unavailable")
        amount = _number(row.get("fee_amount"), "fill.fee_amount", minimum=0)
        rate = _number(row.get("fee_conversion_rate"), "fill.fee_conversion_rate", minimum=0)
        converted = _number(row.get("fee_base_amount"), "fill.fee_base_amount", minimum=0)
        if rate <= 0 or abs(amount * rate - converted) > 1e-8:
            raise AccountSourceError("fee_conversion_mismatch")
        if row["fee_currency"] == currency and rate != 1:
            raise AccountSourceError("quote_currency_conversion_mismatch")
        row["fee_amount"], row["fee_conversion_rate"], row["fee_base_amount"] = amount, rate, converted
        for key in ("qty", "price"):
            row[key] = _number(row.get(key), "fill." + key, minimum=0)
            if row[key] == 0:
                raise AccountSourceError("fill_quantity_or_price_invalid")
    for row in snapshot["positions"]:
        row["qty"] = _number(row.get("qty"), "position.qty", minimum=0 if identity["market_type"] == "spot" else None)
        at = _time(row.get("price_at"), "position.price_at")
        age_limit = _number(row.get("max_price_age_seconds"), "position.max_price_age_seconds", minimum=0)
        if (not row.get("price_source") or row.get("price_currency") != currency
                or not 0 < age_limit <= maximum_age or not 0 <= (now - at).total_seconds() <= age_limit
                or at > captured):
            raise AccountSourceError("position_valuation_provenance_invalid")
        row["mark_price"] = _number(row.get("mark_price"), "position.mark_price", minimum=0)
        row["price_at"], row["max_price_age_seconds"] = at.isoformat(), age_limit
        if row["mark_price"] <= 0:
            raise AccountSourceError("position_mark_invalid")
    data["financing"] = list(financing.values())
    return data


def build_local_account_projection(broker, source, *, identity, checked_at):
    """Pure projection of authoritative OrderStore fills and existing lot books.

    There is no new persisted ledger. Only explicitly sourced opening capital,
    transfers, financing and marks enter from the export; local orders/fills,
    inventory, cash and PnL are reconstructed independently and compared with
    the export's full-account snapshot. Any unsupported inventory/fee movement
    remains a discrepancy instead of being silently booked.
    """
    snapshot = source["snapshot"]
    start = _time(source["opening_capital"]["at"], "opening.at")
    end = _time(source["captured_at"], "captured_at")
    quote = identity["base_currency"]
    initial = source["opening_capital"]["amount"]
    flows = sum(row["amount"] for row in snapshot["cashflows"])
    financing = sum(row["amount"] for row in source["financing"])
    cash, total_fees, orders, fills = initial + flows - financing, 0.0, [], []
    external_fills = _records(snapshot["fills"], "fills")
    authoritative_fills, _, _, _ = broker.order_store.projection_delta(0, 0)
    fills_by_order = {}
    for fill in authoritative_fills:
        fills_by_order.setdefault(fill["client_order_id"], []).append(fill)
    for row in broker.order_store.list_all():
        if row.get("exchange") != identity["exchange"] or row.get("account") != identity["account"]:
            raise AccountSourceError("local_order_account_identity_mismatch")
        if row.get("status") in {"unknown", "submitting", "cancel_pending"}:
            raise AccountSourceError("local_order_unresolved")
        raw_fills = fills_by_order.get(row["client_order_id"], [])
        # Locally rejected/unsubmitted intents have no venue facts to compare.
        if not row.get("exchange_order_id") and not raw_fills:
            if row.get("status") not in {"rejected", "expired_unsubmitted", "no_position"}:
                raise AccountSourceError("local_venue_order_identity_unavailable")
            continue
        orders.append({"record_id": row["client_order_id"], "exchange_order_id": row["exchange_order_id"],
                       "symbol": row["symbol"], "side": row["side"], "status": row["status"],
                       "requested_qty": row["requested_qty"], "filled_qty": row["filled_qty"],
                       "remaining_qty": row["remaining_qty"]})
        for fill in raw_fills:
            if (fill.get("payload") or {}).get("synthetic_from_order") or fill.get("fee_evidence") != "recorded":
                raise AccountSourceError("local_fill_or_fee_evidence_unavailable")
            at = _time(fill.get("timestamp"), "local_fill.at")
            if not start < at <= end:
                raise AccountSourceError("local_fill_outside_source_coverage")
            proof = external_fills.get(fill["fill_id"])
            if proof is None:
                raise AccountSourceError("independent_fill_missing")
            qty = _number(fill.get("qty"), "local_fill.qty", minimum=0)
            price = _number(fill.get("price"), "local_fill.price", minimum=0)
            fee = _number(fill.get("fee"), "local_fill.fee", minimum=0)
            native = fill.get("fee_currency")
            if native not in {quote, row["symbol"].split("/")[0]}:
                raise AccountSourceError("third_currency_fee_inventory_migration_required")
            if row["side"] not in {"buy", "sell", "short", "cover"} or qty <= 0 or price <= 0:
                raise AccountSourceError("local_fill_invalid")
            rate = _number(proof.get("fee_conversion_rate"), "fee_conversion_rate")
            base_fee = fee * rate
            signed = qty if row["side"] in {"buy", "cover"} else -qty
            cash -= signed * price + (fee if native == quote else 0)
            total_fees += base_fee
            fills.append({"record_id": fill["fill_id"], "order_id": row["client_order_id"],
                          "symbol": row["symbol"], "side": row["side"], "qty": qty, "price": price,
                          "at": at.isoformat(), "fee_amount": fee, "fee_currency": native,
                          "fee_base_amount": base_fee, "fee_conversion_rate": rate,
                          "fee_conversion_source": proof["fee_conversion_source"],
                          "fee_conversion_at": proof["fee_conversion_at"]})
    if broker.projection_issues:
        raise AccountSourceError("authoritative_fill_projection_unverified")
    marks = _records(snapshot["positions"], "positions")
    positions, value, cost_basis = [], 0.0, 0.0
    for symbol, book in broker.portfolio.lot_books.items():
        qty = book.net_qty
        if abs(qty) <= 1e-12:
            continue
        if symbol not in marks:
            raise AccountSourceError("account_position_mark_missing")
        mark = marks[symbol]
        positions.append({"record_id": symbol, "qty": qty, **{
            key: mark[key] for key in ("mark_price", "price_source", "price_at", "price_currency", "max_price_age_seconds")}})
        value += qty * float(mark["mark_price"])
        cost_basis += sum(lot.qty_open * lot.entry_price * (1 if lot.side == "long" else -1)
                          for lot in book.open_lots)
    # Locked cash is a venue reservation fact; total cash is independently
    # reconstructed. The normalized gate still checks free + locked = total.
    locked = _number(snapshot.get("cash", {}).get("locked"), "cash.locked", minimum=0)
    costs = total_fees + financing
    return {"captured_at": checked_at.isoformat(), "identity": dict(identity),
            "cash": {"free": cash - locked, "locked": locked, "total": cash},
            "positions": positions, "orders": orders, "fills": fills,
            "cashflows": deepcopy(snapshot["cashflows"]), "equity": cash + value,
            "capital_bridge": {"initial_capital": initial, "net_cashflows": flows,
                "realized_gross_pnl": cash + cost_basis - initial - flows + costs,
                "unrealized_gross_pnl": value - cost_basis, "costs": costs,
                "financing_costs": financing}}
