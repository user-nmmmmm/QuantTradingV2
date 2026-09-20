"""Execution quality from canonical order and fill facts (BM4).

An order counts once, even after replay. Price shortfall is measured on executed
quantity against the recorded decision price. Optional independent observations
support terminal opportunity cost and quote-relative diagnostics; absent market
facts remain unavailable. These retrospective diagnostics never affect orders.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
import math

import pandas as pd


def _field(value, name, default=None):
    return value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)


def _number(value, *, positive=False):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and (result > 0 if positive else result >= 0) else None


def calculate_execution_quality(
    events, *, terminal_valuations=None, independent_quotes=None, as_of=None,
    max_quote_age_seconds=1.0,
):
    """Return order outcomes, latency, price shortfall and optional fact metrics.

    ``terminal_valuations`` are independent market prices at a declared completed
    order evaluation horizon, keyed by (account_id, client_order_id). Fields:
    price, symbol, quote_currency, occurred_at, available_at, source_id,
    reference_id, independent=True. The price must be at/after the terminal
    outcome and available by the required ``as_of`` report cutoff. Cost is signed
    (terminal price - intent reference) * unfilled qty, reversed for sells.

    ``independent_quotes`` use the same provenance fields plus fill_id, bid and
    ask instead of price, keyed by (account_id, fill_id). Quotes must already be
    available at the fill, no older than max_quote_age_seconds (default 1 second).
    Fill-vs-mid and fill-vs-executable-side quote deviations exclude fees and
    describe observed prices; they are not causal market-impact estimates.
    Complete coverage and a common explicit quote currency are required before
    returning an aggregate. Replayed identical facts are counted once.
    """
    base = {"schema_version": "execution-quality/v2", "formula_version": "2.0",
            "latency_unit": "seconds", "shortfall_unit": "bps",
            "shortfall_policy": "executed_qty_price_only_vs_intent_reference; fees excluded",
            "opportunity_cost": {"status": "not_modeled", "value": None,
                                 "reason": "unfilled terminal valuation not recorded"},
            "independent_quote_shortfall_bps": {"status": "not_modeled", "value": None,
                                               "reason": "independent quotes not recorded"},
            "independent_executable_quote_shortfall_bps": {"status": "not_modeled", "value": None,
                                                          "reason": "independent quotes not recorded"}}
    if events is None:
        return {**base, "status": "not_modeled", "reason": "event stream not supplied", "orders": []}
    orders, fills, errors = {}, {}, []
    for event in events:
        kind = _field(event, "event_type")
        if kind not in {"order_intent", "order", "fill"}:
            continue
        payload = _field(event, "payload", {})
        order_id = _field(payload, "client_order_id")
        if not order_id:
            errors.append("order identity missing")
            continue
        key = (str(_field(event, "account_id", "")), str(order_id))
        row = orders.setdefault(key, {"account_id": key[0], "client_order_id": key[1],
                                     "fills": [], "statuses": set()})
        try:
            timestamp = pd.Timestamp(_field(event, "occurred_at"))
            if pd.isna(timestamp) or timestamp.tzinfo is None:
                raise ValueError("event timestamp must be aware UTC")
            timestamp = timestamp.tz_convert("UTC")
        except (TypeError, ValueError):
            errors.append(f"{key[1]}: invalid event timestamp")
            continue
        if kind == "order_intent":
            fields = {"requested_qty": _number(_field(payload, "requested_qty"), positive=True),
                      "reference_price": _number(_field(payload, "reference_price"), positive=True),
                      "side": str(_field(payload, "action", "")).lower(), "created_at": timestamp,
                      "symbol": _field(payload, "symbol"),
                      "quote_currency": _field(payload, "quote_currency")}
            if row.get("intent") is not None and row["intent"] != fields:
                errors.append(f"{key[1]}: conflicting order intent")
            row["intent"] = fields
        elif kind == "order":
            status = _field(payload, "status", "unknown")
            status = str(getattr(status, "value", status)).lower()
            row["statuses"].add(status)
            row["reported_filled_qty"] = max(row.get("reported_filled_qty", 0.0),
                                              _number(_field(payload, "filled_qty")) or 0.0)
            # Coarse venue clocks can put accepted/partial/filled at the same
            # instant. Replayed predecessors must not undo a terminal fact.
            priority = {"created": 0, "submitting": 1, "accepted": 2, "partially_filled": 3,
                        "cancel_pending": 4, "unknown": 5, "rejected": 6,
                        "expired": 7, "expired_unsubmitted": 7, "canceled": 8, "filled": 9}
            status_key = (timestamp, priority.get(status, -1))
            if "last_status_key" not in row or status_key >= row["last_status_key"]:
                row["last_status_key"] = status_key
                row["last_status_at"], row["last_status"] = timestamp, status
            row.setdefault("requested_qty", _number(_field(payload, "requested_qty"), positive=True))
        else:
            fill_id = _field(payload, "fill_id")
            qty, price = _number(_field(payload, "qty"), positive=True), _number(_field(payload, "price"), positive=True)
            if not fill_id or qty is None or price is None:
                errors.append(f"{key[1]}: invalid fill identity/quantity/price")
                continue
            fill_key = (key[0], str(fill_id))
            fact = (key, timestamp, qty, price, _field(payload, "symbol"), _field(payload, "quote_currency"))
            if fill_key in fills:
                if fills[fill_key] != fact:
                    errors.append(f"{key[1]}: conflicting duplicate fill")
                continue
            fills[fill_key] = fact
            row["fills"].append((timestamp, qty, price))

    records, first_latencies, complete_latencies = [], [], []
    shortfall_amount = reference_notional = 0.0
    shortfall_missing = latency_missing = 0
    for key, row in sorted(orders.items()):
        intent = row.get("intent") or {}
        requested = intent.get("requested_qty") or row.get("requested_qty")
        executions = sorted(row["fills"])
        qty = sum(fill[1] for fill in executions)
        if row.get("reported_filled_qty", 0.0) > qty + 1e-9:
            errors.append(f"{key[1]}: fill facts incomplete relative to order status")
        if requested is None or qty > requested + max(1e-9, requested * 1e-9):
            errors.append(f"{key[1]}: missing requested quantity or overfill")
        full = bool(requested is not None and qty >= requested * (1 - 1e-9))
        partial = qty > 0 and not full
        status = row.get("last_status", "unknown")
        if status == "filled" and not full:
            errors.append(f"{key[1]}: terminal filled status lacks complete fill facts")
        if status == "partially_filled" and qty <= 0:
            errors.append(f"{key[1]}: partial status lacks fill facts")
        record = {"account_id": key[0], "client_order_id": key[1], "requested_qty": requested,
                  "filled_qty": qty, "fully_filled": full, "partially_filled": partial,
                  "had_partial_fill": partial or "partially_filled" in row["statuses"],
                  "final_status": status, "first_fill_seconds": None, "completion_seconds": None,
                  "price_shortfall_bps": None}
        if executions:
            created = intent.get("created_at")
            if created is None:
                latency_missing += 1
            elif executions[0][0] < created:
                errors.append(f"{key[1]}: fill before intent")
                latency_missing += 1
            else:
                record["first_fill_seconds"] = (executions[0][0] - created).total_seconds()
                first_latencies.append(record["first_fill_seconds"])
                if full:
                    record["completion_seconds"] = (executions[-1][0] - created).total_seconds()
                    complete_latencies.append(record["completion_seconds"])
            reference, side = intent.get("reference_price"), intent.get("side")
            if reference is None or side not in {"buy", "cover", "sell", "short"}:
                shortfall_missing += 1
            else:
                direction = 1 if side in {"buy", "cover"} else -1
                amount = sum(direction * (price - reference) * quantity for _, quantity, price in executions)
                notional = qty * reference
                record["price_shortfall_bps"] = amount / notional * 10000
                shortfall_amount += amount
                reference_notional += notional
        records.append(record)
    counts = Counter(row["final_status"] for row in records)
    n = len(records)
    def metric(value, sample_size, missing=0, unit="ratio"):
        state = "invalid_input" if errors else "not_modeled" if missing else "ok" if sample_size else "insufficient_data"
        return {"value": value if state == "ok" else None, "status": state, "sample_size": sample_size,
                "unit": unit, "reason": "; ".join(sorted(set(errors))) if errors else
                "required intent reference/time missing" if missing else "no eligible observations" if not sample_size else None}
    additional = _independent_execution_diagnostics(
        orders, fills, errors, terminal_valuations=terminal_valuations,
        independent_quotes=independent_quotes, as_of=as_of,
        max_quote_age_seconds=max_quote_age_seconds)
    diagnostic_errors = [value["reason"] for value in additional.values()
                         if value.get("status") == "invalid_input"]
    return {**base, **additional,
            "status": "invalid_input" if errors or diagnostic_errors else "ok" if n else "insufficient_data",
            "reason": "; ".join(sorted(set(errors + diagnostic_errors))) or None, "sample_size": n, "orders": records,
            "terminal_status_counts": dict(counts),
            "full_fill_rate": metric(sum(r["fully_filled"] for r in records) / n if n else None, n),
            "partial_fill_rate": metric(sum(r["partially_filled"] for r in records) / n if n else None, n),
            "rejection_rate": metric(counts["rejected"] / n if n else None, n),
            "cancel_rate": metric(counts["canceled"] / n if n else None, n),
            "expiry_rate": metric((counts["expired"] + counts["expired_unsubmitted"]) / n if n else None, n),
            "first_fill_seconds": metric(sum(first_latencies) / len(first_latencies) if first_latencies else None,
                                         len(first_latencies), latency_missing, "seconds"),
            "completion_seconds": metric(sum(complete_latencies) / len(complete_latencies) if complete_latencies else None,
                                         len(complete_latencies), latency_missing, "seconds"),
            "implementation_shortfall_bps": metric(shortfall_amount / reference_notional * 10000 if reference_notional else None,
                                                    sum(bool(r["filled_qty"]) for r in records), shortfall_missing, "bps")}


def _utc_fact_time(value):
    timestamp = pd.Timestamp(value)
    if pd.isna(timestamp) or timestamp.tzinfo is None:
        raise ValueError("independent fact timestamps must be timezone aware")
    return timestamp.tz_convert("UTC")


def _independent_execution_diagnostics(
    orders, fills, execution_errors, *, terminal_valuations, independent_quotes,
    as_of, max_quote_age_seconds,
):
    order_instruments = {}
    for order_key, _, _, _, symbol, currency in fills.values():
        order_instruments.setdefault(order_key, set()).add((symbol, currency))

    def result(status, reason, rows=(), numerator=0.0, denominator=0.0, currency=None):
        value = (numerator / denominator * 10000 if denominator else 0.0) if status == "ok" else None
        if status == "ok" and any(not math.isfinite(number) for number in (numerator, denominator, value)):
            status, reason = "invalid_input", "execution aggregation overflow"
            rows = ()
        return {"status": status, "value": value if status == "ok" else None,
                "unit": "bps", "sample_size": len(rows),
                "reason": reason or ("no eligible observations" if status == "insufficient_data" else None),
                "amount": numerator if status == "ok" else None,
                "reference_notional": denominator if status == "ok" else None,
                "quote_currency": currency, "observations": list(rows)}

    def load_facts(values, identity, quote):
        records = {}
        for value in values:
            if not isinstance(value, Mapping):
                raise ValueError("independent facts must be mappings")
            required = ("account_id", identity, "symbol", "quote_currency", "source_id", "reference_id")
            if any(not isinstance(value.get(key), str) or not value[key].strip() for key in required):
                raise ValueError("independent fact identity, currency or source provenance missing")
            if value.get("independent") is not True:
                raise ValueError("market reference must be explicitly independently observed")
            fact = {key: value[key] for key in required}
            fact["occurred_at"] = _utc_fact_time(value.get("occurred_at"))
            fact["available_at"] = _utc_fact_time(value.get("available_at"))
            if fact["available_at"] < fact["occurred_at"]:
                raise ValueError("independent fact cannot be available before observation")
            for field in (("bid", "ask") if quote else ("price",)):
                fact[field] = _number(value.get(field), positive=True)
                if fact[field] is None:
                    raise ValueError("independent market prices must be finite and positive")
            if quote and fact["bid"] > fact["ask"]:
                raise ValueError("independent quote is crossed")
            key = (fact["account_id"], fact[identity])
            if key in records and records[key] != fact:
                raise ValueError("conflicting duplicate independent fact")
            records[key] = fact
        return records

    def check_instrument(fact, intent):
        if intent.get("symbol") is None:
            return False
        if fact["symbol"] != intent["symbol"]:
            raise ValueError("independent reference symbol does not match order intent")
        if intent.get("quote_currency") and fact["quote_currency"] != intent["quote_currency"]:
            raise ValueError("independent reference currency does not match order intent")
        return True

    def common_currency(facts):
        currencies = {fact["quote_currency"] for fact in facts.values()}
        if len(currencies) > 1:
            raise ValueError("different quote currencies require explicit currency conversion facts")
        return next(iter(currencies), None)

    def opportunity():
        if terminal_valuations is None:
            return result("not_modeled", "independent terminal valuations and report cutoff not supplied")
        if execution_errors:
            return result("invalid_input", "; ".join(sorted(set(execution_errors))))
        if as_of is None:
            return result("not_modeled", "explicit report as_of cutoff required for terminal valuation")
        try:
            cutoff = _utc_fact_time(as_of)
            facts = load_facts(terminal_valuations, "client_order_id", False)
            if any(key not in orders for key in facts):
                raise ValueError("terminal valuation references an unknown order")
            currency = common_currency(facts)
            rows, missing, amount, denominator = [], [], 0.0, 0.0
            for key, row in orders.items():
                intent = row.get("intent") or {}
                requested = intent.get("requested_qty") or row.get("requested_qty")
                qty = sum(fill[1] for fill in row["fills"])
                if row.get("last_status_at", cutoff) > cutoff or intent.get("created_at", cutoff) > cutoff:
                    raise ValueError("order facts occur after report cutoff")
                if any(fill[0] > cutoff for fill in row["fills"]):
                    raise ValueError("fill facts occur after report cutoff")
                remaining = max(0.0, requested - qty) if requested is not None else None
                if remaining is not None and remaining <= max(1e-9, requested * 1e-9):
                    if key in facts:
                        raise ValueError("terminal valuation supplied for an already fully filled order")
                    continue
                fact = facts.get(key)
                if fact is None or not intent.get("reference_price") or intent.get("side") not in {"buy", "cover", "sell", "short"}:
                    missing.append(key[1])
                    continue
                if not check_instrument(fact, intent):
                    missing.append(key[1])
                    continue
                for fill_symbol, fill_currency in order_instruments.get(key, ()):
                    if fill_symbol and fact["symbol"] != fill_symbol:
                        raise ValueError("independent terminal reference symbol does not match fill")
                    if fill_currency and fact["quote_currency"] != fill_currency:
                        raise ValueError("independent terminal reference currency does not match fill")
                if row.get("last_status") not in {"canceled", "rejected", "expired", "expired_unsubmitted"}:
                    raise ValueError("terminal opportunity cost requires an observed terminal order outcome")
                terminal = row["last_status_at"]
                if row["fills"] and max(fill[0] for fill in row["fills"]) > terminal:
                    raise ValueError("fill occurs after terminal outcome")
                if not terminal <= fact["occurred_at"] <= fact["available_at"] <= cutoff:
                    raise ValueError("terminal valuation falls outside terminal outcome/report cutoff")
                direction = 1 if intent["side"] in {"buy", "cover"} else -1
                cost = direction * (fact["price"] - intent["reference_price"]) * remaining
                amount += cost
                denominator += intent["reference_price"] * remaining
                rows.append({"account_id": key[0], "client_order_id": key[1], "unfilled_qty": remaining,
                             "amount": cost, "valuation_at": fact["occurred_at"].isoformat(),
                             "available_at": fact["available_at"].isoformat(),
                             "terminal_price": fact["price"], "intent_reference_price": intent["reference_price"],
                             "source_id": fact["source_id"], "reference_id": fact["reference_id"]})
            state = "not_modeled" if missing else "ok" if orders else "insufficient_data"
            reason = "terminal valuation, reference or instrument facts missing: " + ", ".join(missing) if missing else None
            return {**result(state, reason, rows, amount, denominator, currency),
                    "policy": "signed unfilled qty * (terminal independent price - intent reference); fees excluded",
                    "as_of": cutoff.isoformat()}
        except (TypeError, ValueError, OverflowError) as exc:
            return result("invalid_input", str(exc))

    def quote_costs():
        if independent_quotes is None:
            absent = result("not_modeled", "independent pre-fill quotes not supplied")
            return absent, dict(absent)
        if execution_errors:
            invalid = result("invalid_input", "; ".join(sorted(set(execution_errors))))
            return invalid, dict(invalid)
        try:
            max_age = _number(max_quote_age_seconds)
            if max_age is None:
                raise ValueError("max_quote_age_seconds must be finite and nonnegative")
            facts = load_facts(independent_quotes, "fill_id", True)
            if any(key not in fills for key in facts):
                raise ValueError("independent quote references an unknown fill")
            currency = common_currency(facts)
            rows, missing, mid_amount, side_amount, mid_notional, side_notional = [], [], 0.0, 0.0, 0.0, 0.0
            for key, (order_key, timestamp, qty, price, fill_symbol, fill_currency) in fills.items():
                fact = facts.get(key)
                intent = orders[order_key].get("intent") or {}
                if fact is None or intent.get("side") not in {"buy", "cover", "sell", "short"}:
                    missing.append(key[1])
                    continue
                if not check_instrument(fact, intent):
                    missing.append(key[1])
                    continue
                if fill_symbol and fact["symbol"] != fill_symbol:
                    raise ValueError("independent reference symbol does not match fill")
                if fill_currency and fact["quote_currency"] != fill_currency:
                    raise ValueError("independent reference currency does not match fill")
                if fact["available_at"] > timestamp or not 0 <= (timestamp - fact["occurred_at"]).total_seconds() <= max_age:
                    raise ValueError("independent quote is stale or was unavailable at fill time")
                buy = intent["side"] in {"buy", "cover"}
                direction = 1 if buy else -1
                midpoint = fact["bid"] / 2 + fact["ask"] / 2
                executable = fact["ask"] if buy else fact["bid"]
                mid_amount += direction * (price - midpoint) * qty
                side_amount += direction * (price - executable) * qty
                mid_notional += midpoint * qty
                side_notional += executable * qty
                rows.append({"account_id": key[0], "fill_id": key[1], "qty": qty,
                             "quote_at": fact["occurred_at"].isoformat(),
                             "available_at": fact["available_at"].isoformat(),
                             "bid": fact["bid"], "ask": fact["ask"], "fill_price": price,
                             "source_id": fact["source_id"], "reference_id": fact["reference_id"]})
            state = "not_modeled" if missing else "ok" if rows else "insufficient_data"
            reason = "independent quote or instrument facts missing: " + ", ".join(missing) if missing else None
            policy = {"policy": "observed fill-vs-independent quote deviation; excludes fees; not causal market impact",
                      "max_quote_age_seconds": max_age}
            return ({**result(state, reason, rows, mid_amount, mid_notional, currency), **policy},
                    {**result(state, reason, rows, side_amount, side_notional, currency), **policy})
        except (TypeError, ValueError, OverflowError) as exc:
            invalid = result("invalid_input", str(exc))
            return invalid, dict(invalid)

    midpoint, executable = quote_costs()
    return {"opportunity_cost": opportunity(), "independent_quote_shortfall_bps": midpoint,
            "independent_executable_quote_shortfall_bps": executable}
