"""Offline calibration from canonical events, request receipts and independent quotes.

No orders or network requests are made. Receipt time is never called matching
time. Missing real evidence cannot be upgraded by simulated test fixtures.
"""
from __future__ import annotations

from collections import Counter
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3

import numpy as np
import pandas as pd

from core.metrics.execution import calculate_execution_quality, _field
from analysis.paper_validation import block_bootstrap_indices
from core.quote_observations import validate_quote


def _utc(value):
    at = pd.Timestamp(value)
    if pd.isna(at) or at.tzinfo is None:
        raise ValueError("execution facts require aware timestamps")
    return at.tz_convert("UTC")


def deduplicate_request_records(records):
    """Replay identity, not a mutable list position, distinguishes receipts."""
    seen, result = {}, []
    for record in records:
        row = {k: v for k, v in record.items() if k != "sequence"}
        identity = row.get("observation_id")
        if identity:
            if identity in seen:
                if seen[identity] != row:
                    raise ValueError("conflicting_request_observation_id:"+str(identity))
                continue
            seen[identity] = row
        elif row in result:
            continue
        result.append(row)
    return result


def read_fill_provenance(order_store_path):
    """Read a strict scalar projection; never open/migrate the live ledger.

    Canonical FillEvent currently omits synthetic_from_order. Cumulative order
    snapshots and trades without a venue clock must not become matching-time
    calibration evidence merely because the envelope source says 'live'.
    """
    path = Path(order_store_path).resolve()
    result = []
    with closing(sqlite3.connect(path.as_uri()+"?mode=ro", uri=True, timeout=.05)) as connection:
        rows = connection.execute("SELECT o.account,o.exchange,o.symbol,f.fill_id,f.client_order_id,"
            "f.timestamp,f.qty,f.price,f.payload FROM fills f JOIN orders o USING(client_order_id)")
        for account, exchange, symbol, fill_id, client_id, timestamp, qty, price, document in rows:
            raw = json.loads(document)
            synthetic = bool(raw.get("synthetic_from_order")) or ":cumulative:" in fill_id
            venue_time = raw.get("timestamp") if raw.get("timestamp") is not None else raw.get("datetime")
            try:
                occurred = (_utc(pd.to_datetime(float(venue_time), unit="ms", utc=True))
                            if isinstance(venue_time, (int, float)) else _utc(venue_time))
                timestamp_valid = occurred == _utc(timestamp)
            except (ValueError, TypeError, OverflowError):
                timestamp_valid = False
            markers = {str(raw.get(key, "")).lower() for key in ("source", "environment", "execution_mode")}
            provenance_kind = ("simulation" if markers & {"simulation", "simulated", "paper", "backtest"}
                               else "sandbox" if "sandbox" in markers else None)
            available, availability_status = None, "unverified"
            local_times = [raw[k] for k in ("observed_at", "available_at") if raw.get(k) is not None]
            if local_times:
                try:
                    times = [_utc(v) for v in local_times]
                    if min(times) < _utc(timestamp):
                        raise ValueError("fill observed before occurrence")
                    available = max([_utc(timestamp), *times]).isoformat()
                    availability_status = "explicit_local_fields"
                except (ValueError, TypeError, OverflowError):
                    availability_status = "invalid"
            result.append({"account_id": account, "exchange_id": exchange, "symbol": symbol,
                "fill_id": fill_id, "client_order_id": client_id, "occurred_at": timestamp,
                "qty": qty, "price": price,
                "available_at": available, "availability_status": availability_status,
                "kind": provenance_kind or ("synthetic_cumulative_order" if synthetic else
                        "venue_trade" if timestamp_valid else "venue_trade_time_unverified")})
    return result


def join_quotes_to_execution(events, request_records, raw_quotes, *, market_context,
                             fill_provenance=(), as_of, max_quote_age_seconds=1.,
                             prequote_at="submit"):
    """Backward join independent BBO available before decision or submission.

    Explicit account market context supplies environment/product that legacy
    order telemetry lacks. Fill provenance must independently establish an
    actual venue trade and matching clock; missing evidence stays unmatched.
    Also require the quote to remain fresh at the actual fill, preserving the
    existing execution-quality metric's stricter maximum-age contract.
    """
    cutoff = _utc(as_of)
    if prequote_at not in {"decision", "submit"}:
        raise ValueError("prequote_at must be decision or submit")
    if not math.isfinite(max_quote_age_seconds) or max_quote_age_seconds < 0:
        raise ValueError("quote age must be finite and nonnegative")
    intents, fills, requests, proofs = {}, {}, {}, {}
    nonvenue_fills = set()
    for event in events:
        occurred = _utc(_field(event, "occurred_at"))
        observed = _utc(_field(event, "observed_at", occurred))
        if max(occurred, observed) > cutoff:
            continue
        payload = _field(event, "payload", {})
        account, cid = str(_field(event, "account_id", "")), str(_field(payload, "client_order_id", ""))
        if _field(event, "event_type") == "order_intent":
            intents[account, cid] = (occurred, payload)
        elif _field(event, "event_type") == "fill":
            key = (account, str(_field(payload, "fill_id", "")))
            if (_field(payload, "synthetic_from_order", False) or ":cumulative:" in key[1]
                    or str(_field(event, "source", "")).lower() in {"simulation", "backtest", "paper"}):
                nonvenue_fills.add(key)
            fact = (cid, occurred, payload)
            if key in fills and fills[key] != fact:
                raise ValueError("conflicting canonical fill identity")
            fills[key] = fact
    for request in deduplicate_request_records(request_records):
        if request.get("operation") == "submit" and _utc(request["received_at"]) <= cutoff:
            requests.setdefault((str(request.get("account_id", "")), str(request.get("client_order_id", ""))), []).append(request)
    for proof in fill_provenance or ():
        key = (str(proof.get("account_id", "")), str(proof.get("fill_id", "")))
        if key in proofs and proofs[key] != proof:
            raise ValueError("conflicting fill provenance")
        proofs[key] = proof
    valid_quotes, rejected_quotes = [], Counter()
    quote_ids = {}
    for raw in raw_quotes:
        # Appending demonstrably future facts never alters an older report.
        try:
            if max(_utc(raw["available_at"]), _utc(raw["observed_at"])) > cutoff:
                continue
        except (ValueError, TypeError, KeyError):
            rejected_quotes["invalid_quote_time"] += 1
            continue
        try:
            quote = validate_quote(raw)
        except (ValueError, TypeError, KeyError, OverflowError):
            rejected_quotes["invalid_quote_fields"] += 1
            continue
        prior = quote_ids.get(quote["quote_id"])
        if prior is not None:
            if prior != quote:
                raise ValueError("conflicting quote identity")
            continue
        quote_ids[quote["quote_id"]] = quote
        valid_quotes.append(quote)
    linked, matches = [], []
    for (account, fid), (cid, fill_at, fill) in sorted(fills.items()):
        row = {"account_id": account, "client_order_id": cid, "fill_id": fid,
               "fill_at": fill_at.isoformat(), "status": "unmatched", "reason": None}
        matches.append(row)
        def reject(reason):
            row["reason"] = reason
        proof = proofs.get((account, fid))
        if proof is None:
            reject("fill_provenance_missing")
            continue
        if proof.get("kind") != "venue_trade" or (account, fid) in nonvenue_fills:
            reject("synthetic_or_unverified_fill_time")
            continue
        try:
            identity_ok = (proof.get("client_order_id") == cid and _utc(proof.get("occurred_at")) == fill_at
                and proof.get("symbol") == _field(fill, "symbol")
                and math.isclose(float(proof["qty"]), float(_field(fill, "qty")), rel_tol=1e-10)
                and math.isclose(float(proof["price"]), float(_field(fill, "price")), rel_tol=1e-10))
        except (ValueError, TypeError, KeyError):
            identity_ok = False
        if not identity_ok:
            reject("fill_provenance_mismatch")
            continue
        intent_fact = intents.get((account, cid))
        receipts = requests.get((account, cid), [])
        if not intent_fact or len(receipts) != 1:
            reject("missing_intent_or_submit" if len(receipts) <= 1 else "ambiguous_submit_attempts")
            continue
        decision_at, intent = intent_fact
        request = receipts[0]
        sent_at = _utc(request["sent_at"])
        if sent_at < decision_at or sent_at > fill_at:
            reject("invalid_decision_submit_fill_order")
            continue
        context = (market_context or {}).get(account, {})
        if any(not context.get(k) for k in ("exchange_id", "environment", "market_type", "quote_currency")):
            reject("explicit_account_market_context_missing")
            continue
        symbol = _field(intent, "symbol")
        if (context["exchange_id"] != request.get("exchange_id")
                or context["exchange_id"] != proof.get("exchange_id")
                or any(request.get(k) is not None and request[k] != context[k]
                       for k in ("environment", "market_type"))
                or request.get("symbol") != symbol or _field(fill, "symbol") != symbol
                or (_field(intent, "exchange") and _field(intent, "exchange") != context["exchange_id"])
                or (_field(fill, "quote_currency") and _field(fill, "quote_currency") != context["quote_currency"])):
            reject("request_fill_market_identity_mismatch")
            continue
        anchor = sent_at if prequote_at == "submit" else decision_at
        row.update(prequote_at=prequote_at, prequote_cutoff=anchor.isoformat())
        group = [q for q in valid_quotes if q["symbol"] == symbol and all(
            q[k] == context[k] for k in ("exchange_id", "environment", "market_type", "quote_currency"))]
        if not group:
            reject("no_matching_market_quote")
            continue
        available = [q for q in group if max(_utc(q["observed_at"]), _utc(q["available_at"])) <= anchor]
        if not available:
            reject("no_quote_available_before_"+prequote_at)
            continue
        known = [q for q in available if q["occurred_at_status"] == "exchange_timestamp"]
        if not known:
            reject("exchange_quote_timestamp_unknown")
            continue
        eligible = [q for q in known if 0 <= (anchor-_utc(q["occurred_at"])).total_seconds() <= max_quote_age_seconds]
        if not eligible:
            reject("stale_at_"+prequote_at)
            continue
        quote = max(eligible, key=lambda q: (_utc(q["available_at"]), _utc(q["occurred_at"]), q["quote_id"]))
        fill_age = (fill_at-_utc(quote["occurred_at"])).total_seconds()
        row.update(quote_id=quote["quote_id"], quote_age_at_fill_seconds=fill_age,
                   quote_age_at_anchor_seconds=(anchor-_utc(quote["occurred_at"])).total_seconds())
        if not 0 <= fill_age <= max_quote_age_seconds:
            reject("prequote_stale_at_fill")
            continue
        linked.append({**quote, "account_id": account, "fill_id": fid,
                       "reference_id": quote["quote_id"]})
        row.update(status="matched", reason=None)
    counts = Counter(row["reason"] for row in matches if row["reason"])
    return {"schema": "execution-prequote-join/v1", "as_of": cutoff.isoformat(),
        "status": "complete" if matches and not counts else "insufficient",
        "prequote_at": prequote_at, "max_quote_age_seconds": max_quote_age_seconds,
        "fills": len(fills), "matched_fills": len(linked), "known_quotes": len(valid_quotes),
        "unmatched_reasons": dict(sorted(counts.items())), "rejected_quotes": dict(sorted(rejected_quotes.items())),
        "independent_quotes": linked, "matches": matches,
        "reason": "no_canonical_fills" if not fills else None,
        "policy": "same venue/environment/product; locally available before decision or submit; real venue fill clock required"}


def execution_sample_report(events, request_records=(), *, independent_quotes=None,
                            terminal_valuations=None, as_of, source="simulation",
                            predictions=None, max_quote_age_seconds=1.,
                            minimum_train_days=10, minimum_test_days=5,
                            raw_quotes=None, market_context=None, fill_provenance=None,
                            prequote_at="submit"):
    """Join observations and evaluate a constant cost baseline on later days.

    predictions maps 'account_id/client_order_id' to a pre-execution model
    forecast: {bps, available_at}. Training and testing split by whole UTC day,
    not order rows. All requests, rejected orders and unfilled orders remain in
    coverage counts. Gross price shortfall excludes fees and is not causal
    market impact. No result grants production admission.
    """
    if source not in {"simulation", "sandbox", "live"}:
        raise ValueError("explicit simulation/sandbox/live evidence source required")
    if minimum_train_days < 2 or minimum_test_days < 2:
        raise ValueError("at least two independent date cohorts per segment required")
    events = list(events or [])
    request_records = deduplicate_request_records(request_records)
    cutoff = _utc(as_of)
    quote_join = None
    unverified_fill_orders = set()
    if raw_quotes is not None:
        if independent_quotes is not None:
            raise ValueError("supply raw quote observations or prejoined independent quotes, not both")
        quote_join = join_quotes_to_execution(events, request_records, raw_quotes,
            market_context=market_context, fill_provenance=fill_provenance, as_of=cutoff,
            max_quote_age_seconds=max_quote_age_seconds, prequote_at=prequote_at)
        independent_quotes = quote_join["independent_quotes"]
        unverified_fill_orders = {(row["account_id"], row["client_order_id"])
            for row in quote_join["matches"] if row["reason"] in {
                "fill_provenance_missing", "synthetic_or_unverified_fill_time", "fill_provenance_mismatch"}}
    filtered = []
    errors = []
    for event in events:
        occurred = _utc(_field(event, "occurred_at"))
        observed = _utc(_field(event, "observed_at", occurred))
        if observed < occurred:
            raise ValueError("observation precedes event")
        if max(occurred, observed) <= cutoff:
            filtered.append(event)
    def known_facts(facts, *, kind):
        if facts is None:
            return None
        known = []
        for fact in facts:
            # Preserve malformed known facts for the underlying provenance
            # validator; only demonstrably future observations are excluded.
            try:
                available = _utc(_field(fact, "available_at"))
            except (ValueError, TypeError):
                known.append(fact)
                continue
            # The underlying quality validator checks available_at but does not
            # know the local observation clock. Do not silently ignore a bad
            # observation timestamp or call a post-fill quote pre-fill evidence.
            try:
                observed = _utc(_field(fact, "observed_at", available))
            except (ValueError, TypeError):
                if available <= cutoff:
                    errors.append("invalid_" + kind + "_observed_at")
                    known.append(fact)
                continue
            if max(available, observed) <= cutoff:
                known.append(fact)
                try:
                    occurred = _utc(_field(fact, "occurred_at"))
                except (ValueError, TypeError):
                    continue  # The underlying validator reports malformed facts.
                if observed < occurred:
                    errors.append(kind + "_observation_precedes_event")
                if kind == "independent_quote":
                    fill_key = (str(_field(fact, "account_id", "")), str(_field(fact, "fill_id", "")))
                    fill_at = fill_times.get(fill_key)
                    if fill_at is not None and observed > fill_at:
                        errors.append("independent_quote_observed_after_fill:" + fill_key[1])
        return known

    intents, fills, evidence_available = {}, {}, {}
    fill_times, first_fill_times = {}, {}
    for event in filtered:
        payload = _field(event, "payload", {})
        key = (str(_field(event, "account_id", "")), str(_field(payload, "client_order_id", "")))
        event_available = _utc(_field(event, "observed_at", _field(event, "occurred_at")))
        evidence_available[key] = max(evidence_available.get(key, event_available), event_available)
        if _field(event, "event_type") == "order_intent":
            intents[key] = (_utc(_field(event, "occurred_at")), payload)
        elif _field(event, "event_type") == "fill":
            fills.setdefault(key, {})[str(_field(payload, "fill_id"))] = payload
            occurred = _utc(_field(event, "occurred_at"))
            fill_times[(key[0], str(_field(payload, "fill_id")))] = occurred
            first_fill_times[key] = min(first_fill_times.get(key, occurred), occurred)
    quality = calculate_execution_quality(filtered,
        independent_quotes=known_facts(independent_quotes, kind="independent_quote"),
        terminal_valuations=known_facts(terminal_valuations, kind="terminal_valuation"), as_of=cutoff,
        max_quote_age_seconds=max_quote_age_seconds)
    requests = {}
    for request in request_records:
        if request.get("operation") != "submit":
            continue
        key = (str(request.get("account_id", "")), str(request.get("client_order_id", "")))
        if not all(key):
            errors.append("request_without_account_or_client_id")
            continue
        sent, received = _utc(request["sent_at"]), _utc(request["received_at"])
        if received < sent:
            errors.append("negative_request_latency")
            continue
        if received > cutoff:
            continue
        if key in requests and requests[key] != request:
            errors.append("multiple_submit_attempts_require_review:" + key[1])
        requests[key] = request
    rows = []
    for order in quality.get("orders", []):
        key = (order["account_id"], order["client_order_id"])
        intent_at, intent = intents.get(key, (None, {}))
        request = requests.get(key)
        row = {**order, "decision_at": intent_at.isoformat() if intent_at is not None else None,
            "execution_available_at": evidence_available[key].isoformat() if key in evidence_available else None,
            "symbol": _field(intent, "symbol"), "side": _field(intent, "action"),
            "reference_price": _field(intent, "reference_price"),
            "decision_to_submit_seconds": None, "request_receipt_seconds": None,
            "ack_observed": False, "model_error_bps": None}
        if request is not None and intent_at is not None:
            sent, received = _utc(request["sent_at"]), _utc(request["received_at"])
            if sent < intent_at:
                errors.append("submit_before_decision:" + key[1])
            elif key in first_fill_times and first_fill_times[key] < sent:
                errors.append("fill_before_submit:" + key[1])
            else:
                row.update(decision_to_submit_seconds=(sent - intent_at).total_seconds(),
                    request_receipt_seconds=(received - sent).total_seconds(),
                    ack_observed=request.get("outcome") == "ack",
                    submitted_at=sent.isoformat(), response_received_at=received.isoformat())
        if quote_join is not None:
            row["fill_clock_evidence"] = "unverified" if key in unverified_fill_orders else "venue_trade" if key in fills else "no_fill"
            if key in unverified_fill_orders:
                row["first_fill_seconds"] = row["completion_seconds"] = None
        reference = row["reference_price"]
        row["requested_notional"] = float(reference) * order["requested_qty"] if reference and order["requested_qty"] else None
        row["size_bucket"] = ("under_1k" if row["requested_notional"] < 1000 else
            "1k_to_10k" if row["requested_notional"] < 10000 else "10k_plus") if row["requested_notional"] else "unknown"
        row["liquidity"] = sorted({str(_field(f, "liquidity", "unknown")) for f in fills.get(key, {}).values()})
        prediction = (predictions or {}).get("/".join(key))
        if prediction is not None:
            value = float(prediction["bps"])
            if intent_at is None or _utc(prediction["available_at"]) > intent_at or not math.isfinite(value):
                errors.append("model_prediction_unavailable_at_decision:" + key[1])
            elif row["price_shortfall_bps"] is not None:
                row["model_error_bps"] = row["price_shortfall_bps"] - value
        rows.append(row)
    observed = [r for r in rows if r["decision_at"] and r["price_shortfall_bps"] is not None
                and (r["account_id"], r["client_order_id"]) not in unverified_fill_orders]
    cohort = pd.DataFrame(observed)
    calibration = {"status": "insufficient", "reason": "too_few_date_cohorts",
                   "minimum_train_days": minimum_train_days, "minimum_test_days": minimum_test_days}
    if not cohort.empty:
        cohort["day"] = pd.to_datetime(cohort["decision_at"], utc=True).dt.floor("D")
        days = sorted(cohort["day"].unique())
        n_train = max(minimum_train_days, int(len(days) * .7))
        if len(days) - n_train >= minimum_test_days:
            boundary = pd.Timestamp(days[n_train])
            mature = pd.to_datetime(cohort["execution_available_at"], utc=True) < boundary
            train = cohort[cohort["day"].isin(days[:n_train]) & mature]
            test = cohort[cohort["day"].isin(days[n_train:])].copy()
            mature_days = train["day"].nunique()
            if mature_days < minimum_train_days:
                calibration.update(reason="too_few_training_days_mature_before_test",
                    mature_train_days=int(mature_days), test_start=boundary.isoformat())
                train = None
        else:
            train = None
        if train is not None:
            # Equal day weighting prevents a burst of orders from dominating fit.
            fitted = float(train.groupby("day")["price_shortfall_bps"].mean().median())
            test["error"] = test["price_shortfall_bps"] - fitted
            daily_errors = test.groupby("day")["error"].mean().to_numpy(float)
            block = min(5, max(1, len(daily_errors) // 2))
            indices = block_bootstrap_indices(len(daily_errors), iterations=500,
                block_length=block, seed=42, method="circular")
            sampled = daily_errors[indices].mean(axis=1)
            calibration = {"status": "diagnostic", "fitted_train_cost_bps": fitted,
                "train_days": int(mature_days), "test_days": len(days) - n_train,
                "excluded_immature_training_orders": int((cohort["day"].isin(days[:n_train]) & ~mature).sum()),
                "test_start": pd.Timestamp(days[n_train]).isoformat(),
                "test_mean_error_bps": float(daily_errors.mean()),
                "test_mae_bps": float(test["error"].abs().mean()),
                "test_mean_error_block_ci95": np.quantile(sampled, [.025, .975]).tolist(),
                "block_length_days": block, "fit": "median_of_daily_mean_price_shortfall",
                "strata": [{"symbol": k[0], "side": k[1], "size_bucket": k[2],
                    "n": len(g), "mean_error_bps": float(g["error"].mean())}
                    for k, g in test.groupby(["symbol", "side", "size_bucket"], dropna=False)]}
    quote_ok = quality.get("independent_quote_shortfall_bps", {}).get("status") == "ok"
    coverage = {"orders": len(rows), "fills": sum(len(v) for v in fills.values()),
        "request_receipts": sum(r["ack_observed"] for r in rows),
        "submit_request_orders": len(requests),
        "submit_request_outcomes": dict(Counter(str(r.get("outcome", "unknown")) for r in requests.values())),
        "statuses": dict(Counter(r["final_status"] for r in rows)),
        "independent_quotes_complete": quote_ok,
        "unmatched_request_orders": len(set(requests) - set(intents))}
    output = {"schema": "execution-calibration/v1", "source": source, "as_of": cutoff.isoformat(),
        "status": "invalid" if errors or quality["status"] == "invalid_input" else
                  "diagnostic" if rows else "insufficient",
        "errors": sorted(set(errors)), "coverage": coverage, "orders": rows,
        "quality": quality, "calibration": calibration,
        "real_venue_calibration": bool(source == "live" and not errors and
            quality["status"] == "ok" and calibration["status"] == "diagnostic" and quote_ok
            and coverage["unmatched_request_orders"] == 0
            and len(rows) > 0 and all(r["ack_observed"] for r in rows)),
        "production_approved": False,
        "limitations": ["price shortfall excludes fees and is not causal impact",
            "request response receipt is not exchange matching time",
            "no automatic production cost/config update", "evidence source is caller provenance"]}
    if quote_join is not None:
        output["quote_join"] = quote_join
        output["coverage"]["matched_prequote_fills"] = quote_join["matched_fills"]
        output["coverage"]["unmatched_prequote_reasons"] = quote_join["unmatched_reasons"]
        output["real_venue_calibration"] = bool(output["real_venue_calibration"] and
            quote_join["status"] == "complete" and market_context and
            all(context.get("environment") == "live" for context in market_context.values()))
        if not quote_join["fills"] or not quote_join["matched_fills"]:
            output["status"] = "invalid" if output["status"] == "invalid" else "insufficient"
        output["limitations"].append("unknown or synthetic fill/quote occurrence clocks do not support true venue calibration")
        if unverified_fill_orders:
            for metric in ("first_fill_seconds", "completion_seconds"):
                output["quality"][metric] = {"status": "not_modeled", "value": None,
                    "reason": "synthetic or unverified venue matching clock", "unit": "seconds"}
    return output


def read_execution_ledger(path, *, as_of):
    """Read a scalar, point-in-time projection without adopting/migrating a ledger.

    A filename or a caller's 'live' assertion cannot identify a legacy database.
    Current mutable rows last updated after the cutoff are unavailable historically.
    """
    target, cutoff = Path(path).resolve(), _utc(as_of)
    with closing(sqlite3.connect(target.as_uri() + "?mode=ro", uri=True, timeout=.05)) as connection:
        connection.row_factory = sqlite3.Row
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        identity = None
        if "runtime_identity" in tables:
            records = connection.execute("SELECT identity FROM runtime_identity").fetchall()
            if len(records) != 1:
                raise ValueError("ambiguous runtime identity")
            raw = json.loads(records[0][0])
            identity = {k: raw.get(k) for k in ("exchange", "environment", "account", "market_type")}
            if identity["environment"] not in {"live", "sandbox"} or not all(identity.values()):
                raise ValueError("invalid runtime identity")
        orders, excluded = [], 0
        for record in connection.execute("SELECT client_order_id,exchange_order_id,exchange,account,symbol,"
                "side,requested_qty,price,status,submission_attempted,intent,payload,updated_at FROM orders"):
            row = dict(record)
            if _utc(row["updated_at"]) > cutoff:
                excluded += 1
                continue
            intent, payload = json.loads(row.pop("intent")), json.loads(row.pop("payload"))
            markers = {str(value.get(key, "")).lower() for value in (intent, payload)
                       if isinstance(value, dict) for key in ("source", "environment", "execution_mode")}
            evidence_class = identity["environment"] if identity else "unknown_environment"
            if markers & {"simulation", "simulated", "paper", "backtest"} or row["exchange"] in {"offline_fixture", "backtest", "paper"}:
                evidence_class = "simulation"
            elif identity and ((row["account"], row["exchange"]) != (identity["account"], identity["exchange"])
                    or ("sandbox" in markers and identity["environment"] != "sandbox")
                    or ("live" in markers and identity["environment"] != "live")):
                evidence_class = "identity_conflict"
            reference = intent.get("reference_price", intent.get("price", row["price"]))
            try:
                notional = float(row["requested_qty"]) * float(reference)
                if not math.isfinite(notional) or notional <= 0:
                    notional = None
            except (TypeError, ValueError):
                notional = None
            orders.append({**row, "evidence_class": evidence_class,
                "market_type": identity["market_type"] if identity else None,
                "requested_notional": notional, "size_bucket": _notional_bucket(notional),
                "order_date": _utc(row["updated_at"]).date().isoformat(),
                "date_basis": "last_order_update_not_decision"})
        allowed = {o["client_order_id"] for o in orders}
        fill_columns = {r[1] for r in connection.execute("PRAGMA table_info(fills)")}
        fee_rows = {}
        if {"fee", "fee_currency"}.issubset(fill_columns):
            for record in connection.execute("SELECT fill_id,fee,fee_currency,payload FROM fills"):
                fee_rows[record["fill_id"]] = dict(record)
        fee_statuses = dict(connection.execute("SELECT fill_id,fee_status FROM fill_evidence")) if "fill_evidence" in tables else {}
        fills = []
        for proof in read_fill_provenance(target):
            if proof["client_order_id"] in allowed and _utc(proof["occurred_at"]) <= cutoff:
                record = fee_rows.get(proof["fill_id"], {})
                raw = json.loads(record["payload"]) if record else {}
                fee = raw.get("fee") if isinstance(raw.get("fee"), dict) else {}
                fee_reason, quote_fee, fee_available = "missing_explicit_fee_evidence", None, None
                try:
                    cost, reported = float(record["fee"]), float(fee["cost"])
                    currency = record["fee_currency"]
                    if (fee_statuses.get(proof["fill_id"]) != "recorded" or not currency
                            or currency != fee.get("currency") or not math.isfinite(cost) or not math.isfinite(reported)
                            or not math.isclose(cost, reported, rel_tol=1e-10, abs_tol=1e-12)):
                        raise ValueError("fee evidence mismatch")
                    fee_reason = None
                    base, quote_currency = proof["symbol"].split("/")
                    if currency == quote_currency or cost == 0:
                        quote_fee = cost
                    elif currency == base:
                        quote_fee = cost * float(proof["price"])
                    else:
                        conversion = float(raw["fee_quote_price"])
                        conversion_at = _utc(raw["fee_quote_price_available_at"])
                        if not math.isfinite(conversion) or conversion <= 0 or not proof["available_at"] or conversion_at > _utc(proof["available_at"]):
                            raise ValueError("unavailable fee conversion")
                        quote_fee = cost * conversion
                    if proof["available_at"]:
                        fee_available = max(_utc(proof["available_at"]),
                            _utc(raw.get("fee_available_at", proof["available_at"]))).isoformat()
                    if not math.isfinite(quote_fee):
                        raise ValueError("invalid quote fee")
                except (KeyError, ValueError, TypeError, OverflowError):
                    fee_reason = fee_reason or "missing_point_in_time_fee_conversion"
                    quote_fee = None
                proof.update(fee_evidence_status=fee_statuses.get(proof["fill_id"], "legacy_unverified"),
                    fee_qualification_error=fee_reason, fee_quote_value=quote_fee, fee_available_at=fee_available)
                fills.append(proof)
    return {"path": str(target), "runtime_identity": identity, "orders": orders, "fills": fills,
            "excluded_future_updated_orders": excluded}


def _notional_bucket(value):
    return ("unknown" if value is None else "under_1k" if value < 1000 else
            "1k_to_10k" if value < 10000 else "10k_plus")


def execution_readiness_report(ledgers, request_records=(), quotes=(), *, as_of,
        symbols=("BTC/USDT", "ETH/USDT"), start_date=None, depth_records=(),
        minimum_train_days=10, minimum_test_days=5, minimum_stratum_fills=5,
        max_quote_age_seconds=1.):
    """Fail-closed evidence coverage, independent of cost fitting or deployment.

    Days with quotes or rejected orders never count as execution calibration days.
    Size refers to requested quote-currency notional, not turnover or market cap.
    Depth is displayed liquidity; it does not demonstrate realized order capacity.
    """
    cutoff = _utc(as_of)
    if min(minimum_train_days, minimum_test_days, minimum_stratum_fills) < 1:
        raise ValueError("positive evidence requirements required")
    if not math.isfinite(max_quote_age_seconds) or max_quote_age_seconds < 0:
        raise ValueError("invalid quote age")
    orders, fills, errors = {}, {}, []
    for ledger in ledgers:
        for row in ledger["orders"]:
            key = (row["exchange"], row["evidence_class"], row["account"], row["client_order_id"])
            previous = orders.get(key)
            if previous is None or _utc(row["updated_at"]) > _utc(previous["updated_at"]):
                orders[key] = row
            elif _utc(row["updated_at"]) == _utc(previous["updated_at"]) and row != previous:
                errors.append("conflicting_order_identity")
        local_orders = {r["client_order_id"]: r for r in ledger["orders"]}
        for row in ledger["fills"]:
            order = local_orders.get(row["client_order_id"])
            if order is None:
                errors.append("fill_without_order")
                continue
            key = (row["exchange_id"], order["evidence_class"], row["account_id"], row["fill_id"])
            value = {**row, "order": order}
            if key in fills and {k: v for k, v in fills[key].items() if k != "order"} != row:
                errors.append("conflicting_fill_identity")
            fills[key] = value
    requests = deduplicate_request_records(request_records)
    requests = [r for r in requests if _utc(r["received_at"]) <= cutoff]
    quote_map = {}
    for raw in quotes:
        row = validate_quote(raw)
        if max(_utc(row["observed_at"]), _utc(row["available_at"])) > cutoff:
            continue
        if row["quote_id"] in quote_map and quote_map[row["quote_id"]] != row:
            errors.append("conflicting_quote_identity")
        quote_map[row["quote_id"]] = row
    quotes = list(quote_map.values())
    qualified, price_qualified, exclusions = [], [], Counter()
    for row in fills.values():
        original = row["order"]
        order = orders[(original["exchange"], original["evidence_class"], original["account"], original["client_order_id"])]
        reason = None
        if order["evidence_class"] != "live":
            reason = order["evidence_class"]
        elif row["kind"] != "venue_trade":
            reason = row["kind"]
        elif not order["submission_attempted"] or not order["exchange_order_id"]:
            reason = "no_venue_submission_identity"
        elif not all(math.isfinite(float(row[k])) and float(row[k]) > 0 for k in ("qty", "price")):
            reason = "invalid_fill_amount_or_price"
        elif row.get("available_at") and _utc(row["available_at"]) > cutoff:
            reason = "fill_observed_after_cutoff"
        candidates = [r for r in requests if r.get("operation") == "submit"
            and r.get("client_order_id") == row["client_order_id"] and r.get("account_id") == row["account_id"]
            and r.get("exchange_id") == row["exchange_id"] and r.get("environment") == "live"
            and r.get("market_type") == order["market_type"]]
        if reason is None and (len(candidates) != 1 or candidates[0].get("outcome") != "ack"):
            reason = "missing_or_ambiguous_submit_ack"
        request = candidates[0] if len(candidates) == 1 else None
        matched = None
        if reason is None:
            sent, received, occurred = _utc(request["sent_at"]), _utc(request["received_at"]), _utc(row["occurred_at"])
            if received < sent or occurred < sent:
                reason = "inconsistent_execution_clock"
            else:
                matching = [q for q in quotes if q["environment"] == "live"
                    and q["exchange_id"] == row["exchange_id"] and q["market_type"] == order["market_type"]
                    and q["symbol"] == order["symbol"] and q["occurred_at"] is not None
                    and _utc(q["available_at"]) <= sent and _utc(q["occurred_at"]) <= sent
                    and (occurred - _utc(q["occurred_at"])).total_seconds() <= max_quote_age_seconds]
                if not matching:
                    reason = "missing_fresh_event_time_prequote"
                else:
                    matched = max(matching, key=lambda q: _utc(q["available_at"]))
        if reason:
            exclusions[reason] += 1
        else:
            candidate = {"fill_id": row["fill_id"], "client_order_id": row["client_order_id"],
                "symbol": order["symbol"], "side": order["side"], "size_bucket": order["size_bucket"],
                "day": _utc(request["sent_at"]).date().isoformat(),
                "available_at": max(_utc(order["updated_at"]), _utc(request["received_at"]),
                    _utc(row["occurred_at"]), _utc(row.get("available_at") or row["occurred_at"]),
                    _utc(row.get("fee_available_at") or row["occurred_at"])).isoformat(),
                "quote_id": matched["quote_id"]}
            price_qualified.append(candidate)
            fee_error = row.get("fee_qualification_error")
            if row.get("availability_status") != "explicit_local_fields":
                exclusions["fill_observation_time_unverified"] += 1
            elif row.get("fee_evidence_status") != "recorded" or fee_error or row.get("fee_quote_value") is None:
                exclusions[fee_error or "missing_explicit_fee_evidence"] += 1
            elif _utc(candidate["available_at"]) > cutoff:
                exclusions["fee_evidence_after_cutoff"] += 1
            else:
                qualified.append(candidate)
    # Partition whole UTC days; a day can never appear in both fit and holdout.
    days = sorted({r["day"] for r in qualified})
    split = max(minimum_train_days, int(len(days) * .7))
    test_start = days[split] if len(days) > split else None
    train = [r for r in qualified if r["day"] in days[:split] and test_start
             and _utc(r["available_at"]) < _utc(test_start + "T00:00:00Z")]
    test = [r for r in qualified if r["day"] in days[split:]]
    train_days, test_days = sorted({r["day"] for r in train}), sorted({r["day"] for r in test})
    strata = []
    for symbol in sorted(set(symbols) | {o["symbol"] for o in orders.values()}):
        for side in ("buy", "sell"):
            for bucket in ("under_1k", "1k_to_10k", "10k_plus"):
                select = lambda rs: sum(r["symbol"] == symbol and r["side"] == side and r["size_bucket"] == bucket for r in rs)
                ntrain, ntest = select(train), select(test)
                strata.append({"symbol": symbol, "side": side, "size_bucket": bucket,
                    "qualified_fills": select(qualified), "train_fills": ntrain, "test_fills": ntest,
                    "minimum_per_split": minimum_stratum_fills,
                    "train_missing": max(0, minimum_stratum_fills - ntrain),
                    "test_missing": max(0, minimum_stratum_fills - ntest),
                    "status": "coverage_met" if min(ntrain, ntest) >= minimum_stratum_fills else "gap"})
    observed_days = [o["order_date"] for o in orders.values()] + [_utc(q["observed_at"]).date().isoformat() for q in quotes]
    start = _utc(start_date + "T00:00:00Z") if start_date else _utc((min(observed_days) if observed_days else cutoff.date().isoformat()) + "T00:00:00Z")
    if start > cutoff or (cutoff - start).days > 3660:
        raise ValueError("audit calendar requires an ordered window of at most ten years")
    daily = []
    for day in pd.date_range(start, cutoff.floor("D"), freq="D"):
        date = day.date().isoformat()
        local = [o for o in orders.values() if o["order_date"] == date]
        daily.append({"date_utc": date, "orders_last_updated": len(local),
            "rejected_orders": sum(o["status"] == "rejected" for o in local),
            "submitted_orders": sum(bool(o["submission_attempted"]) for o in local),
            "observed_quotes": sum(_utc(q["observed_at"]).date().isoformat() == date for q in quotes),
            "qualified_live_fills": sum(r["day"] == date for r in qualified),
            "split": "train" if date in train_days else "test" if date in test_days else "unqualified",
            "status": "observed_execution" if date in days else "execution_gap"})
    observed_depth = [r for r in depth_records if r.get("kind") == "synchronized_book"
             and max(_utc(r["available_at"]), _utc(r["observed_at"])) <= cutoff]
    depth = [r for r in observed_depth if r.get("kind") == "synchronized_book"
             and r.get("environment") == "live" and r.get("sequence_verified") is True
             and r.get("clock_consistent") is True
             and _utc(r["occurred_at"]) <= _utc(r["observed_at"])
             and r.get("occurred_at") and max(_utc(r["available_at"]), _utc(r["observed_at"])) <= cutoff]
    blockers = []
    if not qualified:
        blockers.append("no_qualified_live_execution_fills")
    if len(train_days) < minimum_train_days or len(test_days) < minimum_test_days:
        blockers.append("insufficient_disjoint_mature_execution_days")
    if any(s["status"] == "gap" for s in strata):
        blockers.append("direction_size_holdout_coverage_gaps")
    if not depth:
        blockers.append("no_sequenced_event_time_depth")
    if any(r.get("clock_consistent") is not True for r in observed_depth):
        blockers.append("exchange_local_clock_conflict")
    # Even a rich passive book does not identify the strategy's actual impact.
    blockers.append("realized_capacity_requires_real_size_varying_fills_and_out_of_sample_validation")
    return {"schema": "execution-readiness/v1", "as_of": cutoff.isoformat(),
        "status": "invalid_inputs" if errors else "gaps" if blockers else "coverage_met", "errors": sorted(set(errors)),
        "ledger_sources": [{k: v for k, v in ledger.items() if k not in {"orders", "fills"}} for ledger in ledgers],
        "coverage": {"orders": len(orders), "orders_by_evidence_class": dict(Counter(o["evidence_class"] for o in orders.values())),
            "order_statuses": dict(Counter(o["status"] for o in orders.values())),
            "submission_attempted_orders": sum(bool(o["submission_attempted"]) for o in orders.values()),
            "ledger_fills": len(fills), "qualified_live_fills": len(qualified),
            "price_execution_qualified_fills": len(price_qualified), "cost_qualified_fills": len(qualified),
            "fill_exclusion_reasons": dict(exclusions), "request_observations": len(requests),
            "independent_quotes": len(quotes), "event_time_quotes": sum(q["occurred_at"] is not None for q in quotes),
            "raw_synchronized_depth_observations": len(observed_depth),
            "depth_clock_conflicts": sum(r.get("clock_consistent") is not True for r in observed_depth),
            "synchronized_depth_observations": len(depth)},
        "calendar": {"train_days": train_days, "test_days": test_days, "test_start": test_start,
            "minimum_train_days": minimum_train_days, "minimum_test_days": minimum_test_days,
            "missing_train_days": max(0, minimum_train_days - len(train_days)),
            "missing_test_days": max(0, minimum_test_days - len(test_days))},
        "daily_coverage": daily, "strata": strata, "qualified_fills": qualified, "blockers": blockers,
        "real_venue_calibration": False, "production_approved": False,
        "limitations": ["unidentified legacy databases remain unknown, irrespective of filename",
            "order date is last persisted update, not decision or exchange matching time",
            "capacity from passive displayed depth is a diagnostic, not proven fill capacity",
            "thresholds are explicit engineering coverage requirements, not statistical power guarantees",
            "no automatic orders or production configuration changes"]}
