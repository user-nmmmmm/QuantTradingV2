"""Refine ambiguous research labels and reconcile labels with actual cash flows.

Fine bars must reproduce their coarse parent and be complete. This module never
invents a tick path, orders, liquidity or financing facts.
"""
from __future__ import annotations

from dataclasses import asdict
import math

import numpy as np
import pandas as pd

from analysis.paper_labels import BarrierConfig, LabelCosts, _frame, _time, label_candidates
from core.timeframes import timeframe_delta
from core.universe import normalize_symbol


def refine_barrier_labels(frames, candidates, *, config: BarrierConfig, costs: LabelCosts,
                         as_of, fine_frames=None, fine_timeframe="1h"):
    """Resolve only proven coarse-bar conflicts; preserve every unresolved label.

    A complete fine interval must reproduce parent OHLC (volume can differ by
    feed conventions). Both barriers inside one fine bar remain ambiguous.
    Availability still includes the parent evidence used to verify the path.
    """
    result = label_candidates(frames, candidates, config=config, costs=costs, as_of=as_of)
    coarse_delta = pd.Timedelta(timeframe_delta(config.timeframe))
    fine_delta = pd.Timedelta(timeframe_delta(fine_timeframe))
    if fine_delta >= coarse_delta or coarse_delta.value % fine_delta.value:
        raise ValueError("fine timeframe must exactly divide the coarse timeframe")
    coarse = {normalize_symbol(k): _frame(v) for k, v in frames.items()}
    fine = {normalize_symbol(k): _frame(v) for k, v in (fine_frames or {}).items()}
    cutoff = _time(as_of)
    before = result["summary"]["ambiguous"]
    resolved = 0
    for row in result["outcomes"]:
        if not row["ambiguous"]:
            continue
        row["coarse_path_ambiguous"] = True
        symbol = normalize_symbol(row["symbol"])
        row["refinement_status"] = "fine_data_missing"
        if symbol not in fine:
            continue
        start = _time(row["exit_bar"])
        expected = pd.date_range(start, start + coarse_delta, freq=fine_delta, inclusive="left")
        data = fine[symbol]
        if not expected.isin(data.index).all():
            row["refinement_status"] = "fine_interval_incomplete"
            continue
        interval = data.loc[expected]
        if "is_complete_bar" in interval and not interval["is_complete_bar"].map(
                lambda value: value is True or isinstance(value, (bool, np.bool_)) and bool(value)
                or isinstance(value, str) and value.strip().lower() == "true").all():
            row["refinement_status"] = "fine_interval_incomplete"
            continue
        values = interval[["open", "high", "low", "close", "volume"]].to_numpy(float)
        if (not np.isfinite(values).all() or (values[:, :4] <= 0).any()
                or (values[:, 4] < 0).any()
                or (values[:, 2] > np.minimum(values[:, 0], values[:, 3])).any()
                or (values[:, 1] < np.maximum(values[:, 0], values[:, 3])).any()):
            row["refinement_status"] = "invalid_fine_ohlcv"
            continue
        available = pd.Series([_time(v) for v in interval.get(
            "available_at", pd.Series(expected + fine_delta, index=expected))], index=expected)
        if ((available.to_numpy() < (expected + fine_delta).to_numpy()).any()
                or (available > cutoff).any()):
            row["refinement_status"] = "fine_data_not_available"
            continue
        aggregate = [values[0, 0], values[:, 1].max(), values[:, 2].min(), values[-1, 3]]
        parent = coarse[symbol].loc[start, ["open", "high", "low", "close"]].to_numpy(float)
        if not np.allclose(aggregate, parent, rtol=1e-8, atol=1e-10):
            row["refinement_status"] = "fine_parent_mismatch"
            continue
        entry = row["entry_reference"]
        sign = 1 if row["direction"] == "long" else -1
        take = entry * (1 + sign * config.profit_take_bps / 10000)
        stop = entry * (1 - sign * config.stop_loss_bps / 10000)
        row["refinement_status"] = "fine_path_still_ambiguous"
        for at, bar in interval.iterrows():
            opening, high, low = (float(bar[k]) for k in ("open", "high", "low"))
            open_stop = opening <= stop if sign > 0 else opening >= stop
            open_take = opening >= take if sign > 0 else opening <= take
            stop_hit = low <= stop if sign > 0 else high >= stop
            take_hit = high >= take if sign > 0 else low <= take
            gap = (open_stop or open_take) and at > _time(row["entry_time"])
            if gap:
                price, barrier = opening, "stop_loss" if open_stop else "profit_take"
            elif stop_hit and take_hit:
                break
            elif stop_hit or take_hit:
                price, barrier = (stop, "stop_loss") if stop_hit else (take, "profit_take")
            else:
                continue
            ratio = price / entry
            components = {k.removesuffix("_bps_per_side"): v * (1 + ratio)
                          for k, v in asdict(costs).items() if k.endswith("_bps_per_side")}
            components["carry"] = costs.carry_bps
            gross = sign * (ratio - 1) * 10000
            net = gross - math.fsum(components.values())
            row.update(ambiguous=False, training_eligible=True, reason=None,
                barrier=barrier, exit_reference=price, fine_exit_bar=at.isoformat(),
                label_end_time=(at + fine_delta).isoformat(),
                available_at=max(_time(row["available_at"]), available.max()).isoformat(),
                resolution=f"{fine_timeframe}_within_{config.timeframe}",
                exit_time_resolution="fine_bar_close_bound", refinement_status="resolved",
                label=1 if net > 0 else -1 if net < 0 else 0,
                gross_return_bps=gross, net_return_bps=net, cost_components_bps=components,
                execution_flags=["fine_path_verified"] + (["gap_exited_at_actual_open"] if gap else []))
            resolved += 1
            break
    result["summary"].update(ambiguous=sum(r["ambiguous"] for r in result["outcomes"]),
        training_eligible=sum(r["training_eligible"] for r in result["outcomes"]),
        coarse_ambiguous=before, resolved_with_fine_data=resolved)
    result["fine_data_contract"] = {
        "timeframe": fine_timeframe, "supplied_symbols": sorted(fine),
        "policy": "complete parent-consistent interval; unresolved conflicts retained",
        "execution_calibrated": False}
    return result


def reconcile_label_cashflows(label, fills, financing=(), *, financing_complete=False):
    """Compare a label with matched entry/exit fills in its reference currency.

    Facts require candidate_id, fill_id, leg=entry|exit, qty, price, fee_quote,
    quote_currency and occurred_at. Financing requires event_id, candidate_id,
    cost_quote and quote_currency. Credit financing is a negative cost.
    No equal-quantity closed round trip means no net-return claim.
    """
    identity = label["candidate_id"]
    seen, unique, currencies = {}, [], set()
    for item in fills:
        if item.get("candidate_id") != identity:
            continue
        key = item.get("fill_id")
        if not key or item.get("leg") not in {"entry", "exit"}:
            raise ValueError("fill identity and entry/exit leg required")
        if key in seen:
            if seen[key] != item:
                raise ValueError("conflicting duplicate fill")
            continue
        _time(item["occurred_at"])
        if not item.get("quote_currency"):
            raise ValueError("explicit quote currency required")
        for k in ("qty", "price", "fee_quote"):
            value = float(item[k])
            if not math.isfinite(value) or value < 0 or (k != "fee_quote" and value == 0):
                raise ValueError("invalid fill amount")
        currencies.add(item["quote_currency"])
        seen[key] = dict(item)
        unique.append(item)
    entries = [f for f in unique if f["leg"] == "entry"]
    exits = [f for f in unique if f["leg"] == "exit"]
    base = {"candidate_id": identity, "scope": "matched_fill_cashflow_diagnostic",
            "status": "insufficient", "financing_complete": bool(financing_complete)}
    if not entries or not exits:
        return {**base, "reason": "matched_entry_exit_facts_missing", "actual_net_bps": None}
    if min(_time(f["occurred_at"]) for f in exits) < max(_time(f["occurred_at"]) for f in entries):
        raise ValueError("overlapping entry/exit legs require lot-level attribution")
    entry_qty, exit_qty = (sum(float(f["qty"]) for f in group) for group in (entries, exits))
    if not math.isclose(entry_qty, exit_qty, rel_tol=1e-9, abs_tol=1e-12):
        return {**base, "reason": "open_or_mismatched_quantity", "actual_net_bps": None}
    charges, seen_financing = 0., {}
    for event in financing:
        if event.get("candidate_id") != identity:
            continue
        key = event.get("event_id")
        if not key:
            raise ValueError("financing event identity required")
        if key in seen_financing:
            if seen_financing[key] != event:
                raise ValueError("conflicting financing event")
            continue
        amount = float(event["cost_quote"])
        if not math.isfinite(amount) or not event.get("quote_currency"):
            raise ValueError("invalid financing fact")
        seen_financing[key] = dict(event)
        currencies.add(event["quote_currency"])
        charges += amount
    if len(currencies) != 1:
        raise ValueError("currency conversion facts required")
    entry_notional, exit_notional = (sum(float(f["qty"]) * float(f["price"]) for f in group)
                                     for group in (entries, exits))
    fees = sum(float(f["fee_quote"]) for f in unique)
    sign = 1 if label["direction"] == "long" else -1
    gross = sign * (exit_notional - entry_notional)
    net = gross - fees - charges
    net_bps = net / entry_notional * 10000
    return {**base, "status": "ok" if financing_complete else "incomplete_costs",
        "quote_currency": next(iter(currencies)), "entry_notional": entry_notional,
        "gross_pnl": gross, "fees": fees, "financing_cost": charges, "net_pnl": net,
        "actual_net_bps": net_bps if financing_complete else None,
        "observed_net_bps_before_missing_costs": net_bps,
        "label_net_bps": label.get("net_return_bps"),
        "label_error_bps": (net_bps - label["net_return_bps"])
            if financing_complete and label.get("net_return_bps") is not None else None,
        "accounting_residual": net - (gross - fees - charges)}
