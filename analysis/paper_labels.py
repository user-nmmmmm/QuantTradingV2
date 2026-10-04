"""Offline, bar-resolution triple-barrier research labels for P0 candidates.

No order is submitted. Intrabar ordering is unobservable: a bar crossing both
barriers is priced stop-first, flagged ambiguous, and excluded from training.
Availability is bounded by the complete exit bar, including open-gap exits.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Iterable, Mapping

import pandas as pd

from core.timeframes import as_utc_timestamp, timeframe_delta
from core.universe import normalize_symbol


def _time(value):
    point = as_utc_timestamp(value)
    if pd.isna(point):
        raise ValueError("a finite UTC timestamp is required")
    return point


def _nonnegative(value, name):
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and nonnegative")
    return value


@dataclass(frozen=True)
class BarrierConfig:
    timeframe: str = "1d"
    profit_take_bps: float = 200.0
    stop_loss_bps: float = 100.0
    max_holding_bars: int = 5

    def __post_init__(self):
        timeframe_delta(self.timeframe)
        for name in ("profit_take_bps", "stop_loss_bps"):
            value = _nonnegative(getattr(self, name), name)
            if not 0 < value < 10000:
                raise ValueError(f"{name} must lie strictly between 0 and 10000")
            object.__setattr__(self, name, value)
        if type(self.max_holding_bars) is not int or self.max_holding_bars < 1:
            raise ValueError("max_holding_bars must be a positive integer")


@dataclass(frozen=True)
class LabelCosts:
    """Explicit fixed scenario costs, in bps of initial reference notional.

    Each per-side cost is charged on entry and exit reference notionals. Carry is
    an explicit whole-trade debit; zero means a declared zero-carry scenario,
    not a claim that historical funding or borrowing was free. Financing with
    event-specific cash flows belongs in the project's financing ledger.
    """
    commission_bps_per_side: float = 0.0
    slippage_bps_per_side: float = 0.0
    spread_bps_per_side: float = 0.0
    impact_bps_per_side: float = 0.0
    carry_bps: float = 0.0

    def __post_init__(self):
        for name, value in asdict(self).items():
            object.__setattr__(self, name, _nonnegative(value, name))


def _candidate(value):
    item = value.to_dict() if hasattr(value, "to_dict") else dict(value)
    if not str(item.get("candidate_id", "")).strip():
        raise ValueError("candidate_id is required")
    if item.get("direction") not in {"long", "short"}:
        raise ValueError("candidate direction must be long or short")
    normalize_symbol(item.get("symbol", ""))
    context = item.get("context")
    if not isinstance(context, Mapping) or context.get("available_at") is None:
        raise ValueError("candidate context.available_at is required")
    return item


def _frame(frame):
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError("bars require a DatetimeIndex; timestamp units must be audited first")
    stamps = pd.DatetimeIndex([_time(value) for value in frame.index])
    if stamps.has_duplicates or not stamps.is_monotonic_increasing:
        raise ValueError("bars must have strictly ordered, unique timestamps")
    if not {"open", "high", "low", "close", "volume"} <= set(frame.columns):
        raise ValueError("bars require open, high, low, close, volume")
    result = frame.copy(deep=False)
    result.index = stamps
    return result


def triple_barrier_label(frame: pd.DataFrame, candidate, *, config: BarrierConfig,
                        costs: LabelCosts, as_of, split: str = "retrospective") -> dict:
    """Label one candidate from the first executable grid open after its signal.

    A late candidate starts at the first grid open on/after available_at. A
    missing expected bar censors rather than jumping across the gap. No future
    or incomplete bar's OHLC is read. label_end_time is the conservative exit
    bar-close bound suitable for overlap purging, not an invented tick time.
    available_at includes the signal and every bar inspected through the exit:
    a late entry/intermediate revision cannot make the label available earlier.
    """
    if split not in {"train", "validation", "retrospective"}:
        raise ValueError("final/holdout samples require the existing adjudication entrypoint")
    if not isinstance(config, BarrierConfig) or not isinstance(costs, LabelCosts):
        raise TypeError("config and costs must be explicit BarrierConfig and LabelCosts instances")
    item, bars, cutoff = _candidate(candidate), _frame(frame), _time(as_of)
    delta = pd.Timedelta(timeframe_delta(config.timeframe))
    signal_at = _time(item["timestamp"])
    context = item["context"]
    if context.get("timeframe", config.timeframe) != config.timeframe:
        raise ValueError("candidate and label timeframes disagree")
    available = _time(context["available_at"])
    if available < signal_at + delta:
        raise ValueError("candidate availability precedes signal-bar close")
    if signal_at.value % delta.value:
        raise ValueError("signal timestamp is not aligned to the declared bar grid")
    entry_at = max(signal_at + delta, available.ceil(delta))
    result = {
        "schema": "paper_triple_barrier/v1", "candidate_id": item["candidate_id"],
        "symbol": item["symbol"], "strategy": item.get("strategy"),
        "direction": item["direction"], "signal_time": signal_at.isoformat(),
        "signal_available_at": available.isoformat(), "entry_time": entry_at.isoformat(),
        "timeframe": config.timeframe, "horizon_bars": config.max_holding_bars,
        "resolution": "OHLCV_bar", "split": split,
        "scope": "independent_unlevered_bar_label_not_executable_trade",
        "cost_model": {"schema": "fixed_scenario_bps/v1", **asdict(costs)},
        "barriers": asdict(config), "status": "insufficient", "reason": None,
        "barrier": None, "ambiguous": False, "training_eligible": False,
        "entry_reference": None, "exit_reference": None, "exit_bar": None,
        "label_end_time": None, "available_at": None, "label": None,
        "gross_return_bps": None, "net_return_bps": None,
        "cost_components_bps": None, "execution_flags": [],
        "availability_policy": "explicit_bar_available_at_or_nominal_close_lower_bound",
    }

    def insufficient(reason):
        result["reason"] = reason
        return result

    if entry_at >= cutoff:
        return insufficient("not_matured")
    sign = 1 if item["direction"] == "long" else -1
    entry = None
    label_available = available
    for number in range(config.max_holding_bars):
        at = entry_at + number * delta
        if at + delta > cutoff:
            return insufficient("not_matured")
        if at not in bars.index:
            # A missing interior bar is distinct from a truncated file tail.
            reason = "missing_bar" if len(bars) and at <= bars.index[-1] else "tail_insufficient"
            return insufficient(reason)
        row = bars.loc[at]
        raw_available = row.get("available_at", at + delta)
        try:
            bar_available = _time(raw_available)
        except (TypeError, ValueError):
            return insufficient("invalid_available_at")
        if bar_available < at + delta:
            return insufficient("availability_before_bar_close")
        if bar_available > cutoff:
            return insufficient("not_matured")
        label_available = max(label_available, bar_available)
        try:
            open_, high, low, close, volume = [float(row[key]) for key in
                                             ("open", "high", "low", "close", "volume")]
        except (TypeError, ValueError):
            return insufficient("invalid_bar")
        if (not all(math.isfinite(value) for value in (open_, high, low, close, volume))
                or min(open_, high, low, close) <= 0 or volume < 0
                or low > min(open_, close) or high < max(open_, close) or low > high):
            return insufficient("invalid_bar")
        if entry is None:
            entry = open_
            result["entry_reference"] = entry
            take = entry * (1 + sign * config.profit_take_bps / 10000)
            stop = entry * (1 - sign * config.stop_loss_bps / 10000)
        take_hit = high >= take if sign > 0 else low <= take
        stop_hit = low <= stop if sign > 0 else high >= stop
        open_take = open_ >= take if sign > 0 else open_ <= take
        open_stop = open_ <= stop if sign > 0 else open_ >= stop
        exit_price, barrier, gap, ambiguous = None, None, False, False
        if number and (open_stop or open_take):
            exit_price, barrier, gap = open_, "stop_loss" if open_stop else "profit_take", True
        elif stop_hit and take_hit:
            exit_price, barrier, ambiguous = stop, "stop_loss", True
        elif stop_hit:
            exit_price, barrier = stop, "stop_loss"
        elif take_hit:
            exit_price, barrier = take, "profit_take"
        elif number == config.max_holding_bars - 1:
            exit_price, barrier = close, "time"
        if exit_price is None:
            continue
        ratio = exit_price / entry
        components = {name.removesuffix("_bps_per_side"): float(getattr(costs, name)) * (1 + ratio)
                      for name in ("commission_bps_per_side", "slippage_bps_per_side",
                                   "spread_bps_per_side", "impact_bps_per_side")}
        components["carry"] = float(costs.carry_bps)
        gross = sign * (ratio - 1) * 10000
        net = gross - math.fsum(components.values())
        flags = ["bar_path_ambiguous_stop_first"] if ambiguous else []
        if gap:
            flags.append("gap_exited_at_actual_open")
        result.update(status="matured", reason="ambiguous_stop_first" if ambiguous else None,
                      barrier=barrier, ambiguous=ambiguous, training_eligible=not ambiguous,
                      exit_reference=exit_price, exit_bar=at.isoformat(),
                      label_end_time=(at + delta).isoformat(), available_at=label_available.isoformat(),
                      label=1 if net > 0 else (-1 if net < 0 else 0),
                      gross_return_bps=gross, net_return_bps=net,
                      cost_components_bps=components, execution_flags=flags,
                      exit_time_resolution="conservative_bar_close_bound", bars_held=number + 1)
        return result
    raise AssertionError("time barrier must terminate a complete horizon")


def label_candidates(data_map: Mapping[str, pd.DataFrame], candidates: Iterable, *,
                     config: BarrierConfig, costs: LabelCosts, as_of,
                     split: str = "retrospective") -> dict:
    """Return one immutable-style outcome per unique P0 candidate, without sorting bars."""
    normalized = {}
    if split not in {"train", "validation", "retrospective"}:
        raise ValueError("final/holdout samples require the existing adjudication entrypoint")
    for symbol, frame in data_map.items():
        key = normalize_symbol(symbol)
        if key in normalized:
            raise ValueError("duplicate normalized data symbol")
        normalized[key] = frame
    results, seen = [], set()
    for raw in candidates:
        item = _candidate(raw)
        if item["candidate_id"] in seen:
            raise ValueError("duplicate candidate_id")
        seen.add(item["candidate_id"])
        key = normalize_symbol(item["symbol"])
        if key not in normalized:
            raise ValueError(f"missing declared symbol data: {key}")
        results.append(triple_barrier_label(normalized[key], item, config=config,
                                           costs=costs, as_of=as_of, split=split))
    return {"schema": "paper_triple_barrier_batch/v1", "as_of": _time(as_of).isoformat(),
            "outcomes": results, "summary": {
                "total": len(results), "matured": sum(row["status"] == "matured" for row in results),
                "ambiguous": sum(row["ambiguous"] for row in results),
                "training_eligible": sum(row["training_eligible"] for row in results),
                "insufficient": sum(row["status"] == "insufficient" for row in results)}}
