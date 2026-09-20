"""Post-run, identity-preserving diagnostics for the fixed strategy review.

These tables never feed orders. A breakout at a later bar is a new setup, not a
delayed version of an earlier rejected setup. Missing gate facts and unobserved
counterfactual returns remain unknown. Work is linear in bars/observations plus
O(bars log bars) range-extrema preparation; no all-pairs candidate matching.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import Counter, defaultdict, deque
from dataclasses import asdict, is_dataclass
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from core.reproducibility import canonical_json
from core.state import MarketStateMachine


def _time(value):
    stamp = pd.to_datetime(value, errors="coerce", utc=True)
    return None if pd.isna(stamp) else stamp.tz_localize(None)


def _number(value):
    try:
        converted = float(value)
        return converted if math.isfinite(converted) else None
    except (TypeError, ValueError):
        return None


def _records(value):
    if isinstance(value, pd.DataFrame):
        return value.to_dict("records")
    if not isinstance(value, (list, tuple)):
        return []
    return [asdict(row) if is_dataclass(row) else dict(row) for row in value]


def _identity(symbol, window, timestamp):
    return f"TrendBreakout|{symbol}|buy|{window}|{timestamp.isoformat()}"


def _period(folder, result):
    explicit = result.get("review_period")
    if explicit is None and (folder / "summary.json").exists():
        explicit = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    if isinstance(explicit, dict):
        start, end = _time(explicit.get("start")), _time(explicit.get("end"))
        if start is not None and end is not None:
            return start, end, "explicit_run_period"
    curve = result.get("equity_curve")
    if isinstance(curve, (pd.Series, pd.DataFrame)) and not curve.empty:
        return _time(curve.index.min()), _time(curve.index.max()), "observed_equity_period"
    return None, None, "unknown"


def _frames_for_period(frames, start, end):
    output = {}
    if start is None or end is None:
        return output
    for symbol, source in frames.items():
        required = [column for column in ("open", "high", "low", "close", "volume")
                    if column in source]
        if not {"open", "high", "low", "close"}.issubset(required):
            continue
        frame = source[required].copy()
        frame.index = pd.to_datetime(frame.index, utc=True).tz_localize(None)
        if frame.index.has_duplicates or not frame.index.is_monotonic_increasing:
            raise ValueError(f"Diagnostic input must have unique ordered bars: {symbol}")
        active = frame.loc[(frame.index >= start) & (frame.index <= end)]
        if not active.empty:
            output[symbol] = pd.concat([frame.loc[frame.index < start].tail(100), active])
    return output


def _state_tables(frames, parameters, start, end, observations, trades):
    state_parameters = {key: value for key, value in parameters.get("state", {}).items()
                        if key in {"stability_period", "ma_fast", "ma_slow", "adx_period",
                                   "adx_threshold", "atr_period", "atr_pct_threshold"}}
    raw_machine = MarketStateMachine(**{**state_parameters, "stability_period": 1})
    stable_machine = MarketStateMachine(**state_parameters)
    window = int(parameters.get("research", {}).get("trend_breakout_parameters", {})
                 .get("entry_window", 20))
    observation_map = {(row["symbol"], _time(row.get("timestamp"))): row
                       for row in observations if row.get("symbol")}
    fills = defaultdict(list)
    for trade in trades:
        if trade.get("side") == "buy" and trade.get("order_id"):
            fills[str(trade["order_id"])].append(trade)
    transitions, setups = [], []
    for symbol, frame in frames.items():
        # Independent classification on raw OHLC, never reverse-engineered from
        # stable logs or pre-populated market_state/indicator columns.
        raw = raw_machine.calculate_states(frame.copy()).map(lambda item: item.name)
        stable = stable_machine.calculate_states(frame.copy()).map(lambda item: item.name)
        indices = frame.index
        starts = np.flatnonzero(raw.ne(raw.shift()).to_numpy())
        episode_at = np.empty(len(frame), dtype=int)
        episode_info = {}
        for left, right in zip(starts, [*starts[1:], len(frame)]):
            episode_at[left:right] = left
            target = raw.iat[left]
            matches = np.flatnonzero(stable.iloc[left:right].eq(target).to_numpy())
            confirmed = int(left + matches[0]) if len(matches) else None
            info = {
                "raw_state": target, "raw_change_at": indices[left],
                "stable_confirmed_at": indices[confirmed] if confirmed is not None else None,
                "confirmation_delay_bars": confirmed - left if confirmed is not None else None,
                "raw_episode_end": indices[right - 1],
                "confirmation_status": "confirmed" if confirmed is not None else
                    "right_censored" if right == len(frame) else "raw_state_reversed_before_confirmation",
            }
            episode_info[left] = info
            if start <= indices[left] <= end:
                transitions.append({"symbol": symbol, **info,
                                    "already_confirmed_at_episode_start": confirmed == left})
        for channel_window in sorted({20, window}):
            channel = frame.high.rolling(channel_window, min_periods=channel_window).max().shift(1)
            raw_breakout = frame.close.gt(channel) & (indices >= start) & (indices <= end)
            for location in np.flatnonzero(raw_breakout.to_numpy()):
                timestamp = indices[location]
                observation = observation_map.get((symbol, timestamp), {})
                configured = channel_window == window
                own_candidate = configured and observation.get("strategy") == "TrendBreakout"
                rank = _number(observation.get("rank")) if own_candidate else None
                order_id = observation.get("order_id") if own_candidate else None
                candidate = rank is not None or bool(order_id)
                exact_fills = fills.get(str(order_id), []) if order_id else []
                fill_times = [_time(row.get("fill_time", row.get("timestamp"))) for row in exact_fills]
                fill_times = [value for value in fill_times if value is not None]
                fill_at = min(fill_times) if fill_times else None
                reason = observation.get("reason")
                if not configured:
                    outcome = "comparison_window_only"
                elif fill_at is not None:
                    outcome = "same_signal_filled"
                elif order_id:
                    outcome = "same_signal_order_unfilled_or_censored"
                elif candidate:
                    outcome = "same_signal_candidate_unsubmitted"
                elif reason and reason != "not_evaluated":
                    outcome = "filtered_original_setup"
                else:
                    outcome = "unobserved_gate_facts"
                fills_later = int(indices.searchsorted(fill_at) - location) if fill_at is not None else None
                info = episode_info[int(episode_at[location])]
                setups.append({
                    "signal_id": _identity(symbol, channel_window, timestamp),
                    "symbol": symbol, "strategy": "TrendBreakout", "direction": "buy",
                    "channel_window": channel_window, "configured_window": configured,
                    "signal_time": timestamp, "channel_level": channel.iat[location],
                    "signal_close": frame.close.iat[location], "stable_state": stable.iat[location],
                    **info, "gate_reason": reason or "unknown",
                    "router_pass_observed": observation.get("strategy") == "TrendBreakout"
                        if observation else None,
                    "candidate_time": timestamp if candidate else None,
                    "order_time": timestamp if order_id else None, "order_id": order_id,
                    "first_fill_time": fill_at, "signal_to_fill_bars": fills_later,
                    "outcome": outcome, "counterfactual_pnl": None,
                    "signal_identity_rule": "exact_symbol_direction_window_bar_no_later_setup_reassignment",
                })
    return transitions, setups


class _Extremes:
    """Sparse range tables: constant-time extrema without repeated frame scans."""
    def __init__(self, frame):
        self.index = frame.index
        self.high = [frame.high.to_numpy(dtype=float)]
        self.low = [frame.low.to_numpy(dtype=float)]
        span = 1
        while span * 2 <= len(frame):
            self.high.append(np.maximum(self.high[-1][:-span], self.high[-1][span:]))
            self.low.append(np.minimum(self.low[-1][:-span], self.low[-1][span:]))
            span *= 2

    def favorable(self, start, end, side, price, exit_price):
        left, right = self.index.searchsorted(start), self.index.searchsorted(end)
        move = (exit_price - price) * (1 if side == "buy" else -1)
        if start not in self.index or end not in self.index:
            return None
        if right > left:
            level = (int(right - left)).bit_length() - 1
            span = 1 << level
            if side == "buy":
                extreme = max(self.high[level][left], self.high[level][right - span])
                move = max(move, extreme - price)
            else:
                extreme = min(self.low[level][left], self.low[level][right - span])
                move = max(move, price - extreme)
        return max(0.0, move) if math.isfinite(move) else None


def _execution_cost(trade, qty=None):
    quantity = _number(trade.get("qty"))
    commission = _number(trade.get("commission"))
    actual, reference = _number(trade.get("fill_price")), _number(trade.get("theoretical_price"))
    if quantity is None or quantity <= 0 or commission is None or actual is None or reference is None:
        return None
    fraction = 1 if qty is None else qty / quantity
    return (commission + abs(actual - reference) * quantity) * fraction


def _exit_tables(frames, trades, events, observations):
    extrema = {symbol: _Extremes(frame) for symbol, frame in frames.items()}
    event_map = {str(event["close_event_id"]): event for event in events if event.get("close_event_id")}
    observed_orders = {str(row["order_id"]): row for row in observations if row.get("order_id")}
    open_fills = defaultdict(deque)
    entry_orders = defaultdict(list)
    exits, closed_positions = [], []
    ordered = sorted(enumerate(trades), key=lambda pair: (
        _time(pair[1].get("fill_time", pair[1].get("timestamp"))) or pd.Timestamp.min, pair[0]))
    for sequence, trade in ordered:
        symbol, side = trade.get("symbol"), trade.get("side")
        timestamp = _time(trade.get("fill_time", trade.get("timestamp")))
        quantity, price = _number(trade.get("qty")), _number(trade.get("fill_price"))
        if timestamp is None or quantity is None or price is None or quantity <= 0:
            continue
        if side in {"buy", "short"}:
            opened = {"remaining": quantity, "price": price, "time": timestamp,
                      "side": side, "order_id": str(trade.get("order_id") or ""),
                      "strategy": trade.get("strategy_id"), "trade": trade}
            open_fills[symbol].append(opened)
            entry_orders[(symbol, trade.get("strategy_id"))].append((timestamp, sequence, trade))
            continue
        if side not in {"sell", "cover"}:
            continue
        event_ids = trade.get("close_event_ids") or []
        if not isinstance(event_ids, (list, tuple)):
            event_ids = []
        portions = [(event_map.get(str(event_id)), str(event_id)) for event_id in event_ids]
        if not portions:
            portions = [(None, f"unknown:{trade.get('order_id', sequence)}")]
        remaining_fill = quantity
        for event, event_id in portions:
            event_quantity = _number(event.get("qty")) if event else remaining_fill
            if event_quantity is None or event_quantity <= 0:
                continue
            remaining = min(event_quantity, remaining_fill)
            while remaining > 1e-12 and open_fills[symbol]:
                entry = open_fills[symbol][0]
                if (entry["side"] == "buy") != (side == "sell"):
                    break
                matched = min(remaining, entry["remaining"])
                pnl = _number(event.get("realized_pnl")) if event else None
                risk = _number(event.get("initial_risk")) if event else None
                gross = (price - entry["price"]) * matched * (1 if side == "sell" else -1)
                mfe = extrema[symbol].favorable(entry["time"], timestamp, entry["side"],
                                                entry["price"], price) if symbol in extrema else None
                peak = mfe * matched if mfe is not None else None
                observation = observed_orders.get(entry["order_id"], {})
                exits.append({
                    "close_event_id": event_id, "lot_id": event.get("lot_id") if event else None,
                    "position_id": event.get("position_id") if event else None,
                    "symbol": symbol, "strategy": entry["strategy"], "direction": entry["side"],
                    "entry_order_id": entry["order_id"], "entry_time": entry["time"],
                    "exit_time": timestamp, "exit_reason": trade.get("exit_reason"),
                    "entry_price": entry["price"], "exit_price": price, "quantity": matched,
                    "initial_stop": _number(observation.get("stop_loss")),
                    "initial_risk": risk * matched / event_quantity if risk is not None else None,
                    "realized_net_pnl": pnl * matched / event_quantity if pnl is not None else None,
                    "realized_gross_pnl": gross, "mfe_per_unit": mfe, "peak_gross_profit": peak,
                    "gross_profit_giveback": peak - gross if peak is not None else None,
                    "net_profit_giveback": peak - pnl * matched / event_quantity
                        if peak is not None and pnl is not None else None,
                    "observed_trend_capture": gross / peak if peak is not None and peak > 0 else None,
                    "score": _number(observation.get("score")),
                    "evidence_status": "observed_close" if event else "close_event_facts_missing",
                    "excursion_scope": "entry_open_to_pre_exit_completed_bars_plus_exit_price",
                })
                entry["remaining"] -= matched
                remaining -= matched
                remaining_fill -= matched
                if entry["remaining"] <= 1e-12:
                    open_fills[symbol].popleft()
            if remaining > 1e-12:
                exits.append({"close_event_id": event_id, "symbol": symbol,
                              "quantity": remaining, "exit_time": timestamp,
                              "evidence_status": "entry_facts_missing"})
                remaining_fill -= remaining
            if event and event.get("is_position_fully_closed") and event.get("exit_reason") in {
                "protective_stop", "hard_stop", "hard_stop_exit",
            }:
                closed_positions.append((event, trade, timestamp))
    reentries = []
    entry_times = {key: [item[0] for item in values] for key, values in entry_orders.items()}
    for event, exit_trade, closed_at in closed_positions:
        entry_key = (event.get("symbol"), event.get("opening_strategy_id"))
        subsequent = entry_orders.get(entry_key, [])
        times = entry_times.get(entry_key, [])
        location = bisect_right(times, closed_at)
        next_trade = subsequent[location][2] if location < len(subsequent) else None
        next_time = subsequent[location][0] if next_trade else None
        exit_cost = _execution_cost(exit_trade, _number(event.get("qty")))
        entry_cost = _execution_cost(next_trade) if next_trade else None
        reentries.append({
            "close_event_id": event["close_event_id"], "symbol": event["symbol"],
            "stop_exit_time": closed_at, "next_entry_time": next_time,
            "next_entry_order_id": next_trade.get("order_id") if next_trade else None,
            "days_to_next_entry": (next_time - closed_at).total_seconds() / 86400 if next_time else None,
            "stop_exit_execution_cost": exit_cost, "next_entry_execution_cost": entry_cost,
            "exit_and_reentry_execution_cost": exit_cost + entry_cost
                if exit_cost is not None and entry_cost is not None else None,
            "status": "observed_subsequent_entry" if next_trade else "right_censored_no_later_entry",
            "cost_scope": "commission_and_absolute_price_shortfall_excludes_separate_financing",
            "causal_claim": "none_next_entry_is_a_new_signal",
        })
    return exits, reentries


def _candidate_tables(observations, trades, exits):
    candidates = [dict(row) for row in observations if _number(row.get("rank")) is not None]
    batches = defaultdict(list)
    fills = Counter(str(row.get("order_id")) for row in trades if row.get("side") in {"buy", "short"})
    closed = defaultdict(list)
    for row in exits:
        if row.get("entry_order_id"):
            closed[str(row["entry_order_id"])].append(row)
    for row in candidates:
        batches[_time(row.get("timestamp"))].append(row)
    output = []
    for timestamp, batch in batches.items():
        scores = {_number(row.get("score")) for row in batch}
        for row in batch:
            desired = _number(row.get("sized_qty"))
            quantities = [_number(row.get(key)) for key in
                          ("clamped_qty", "budgeted_qty", "drawdown_clamped_qty")]
            actual = next((quantity for quantity in reversed(quantities) if quantity is not None), None)
            order = str(row.get("order_id") or "")
            matches = closed.get(order, [])
            pnl_values = [_number(match.get("realized_net_pnl")) for match in matches]
            known = bool(pnl_values) and all(value is not None for value in pnl_values)
            score = _number(row.get("score"))
            bucket = "unknown" if score is None else "negative" if score < 0 else "zero" if score == 0 \
                else "(0,1]" if score <= 1 else "(1,2]" if score <= 2 else ">(2)"
            output.append({
                "observation_id": row.get("observation_id"), "timestamp": timestamp,
                "symbol": row.get("symbol"), "strategy": row.get("strategy"),
                "rank": row.get("rank"), "score": score, "score_bucket": bucket,
                "batch_candidates": len(batch), "competing_batch": len(batch) > 1,
                "ordering": "tie_break_alphabetical" if len(scores) == 1 and len(batch) > 1 else "score",
                "reason": row.get("reason", "unknown"), "desired_qty": desired,
                "last_observed_budget_qty": actual,
                "budget_reduced_observed": actual < desired - 1e-12
                    if actual is not None and desired is not None else None,
                "binding_cap": row.get("binding_cap"), "order_id": order or None,
                "opening_fills": fills.get(order, 0), "closed_fragments": len(matches),
                "observed_closed_net_pnl": math.fsum(pnl_values) if known else None,
                "outcome_coverage": "observed_closed_portion" if known else
                    "open_or_missing_close_facts" if fills.get(order) else "unfilled_no_counterfactual_pnl",
            })
    buckets = []
    for label in sorted({row["score_bucket"] for row in output}):
        rows = [row for row in output if row["score_bucket"] == label]
        pnls = [row["observed_closed_net_pnl"] for row in rows if row["observed_closed_net_pnl"] is not None]
        buckets.append({"score_bucket": label, "candidate_count": len(rows),
                        "filled_order_count": sum(row["opening_fills"] > 0 for row in rows),
                        "with_closed_pnl_count": len(pnls),
                        "observed_closed_net_pnl": math.fsum(pnls) if pnls else None,
                        "counterfactual_rejected_pnl": None,
                        "interpretation": "selected_closed_portions_not_unbiased_score_calibration"})
    return output, buckets


def write_review_diagnostics(folder, frames, result, parameters) -> dict[str, Any]:
    """Write bounded, postprocessing-only evidence for one completed run.

    Period comes from ``result['review_period']`` or run_one's summary.json;
    equity bounds are a disclosed fallback. Input frames/result/config are never
    modified. A cached run lacking detailed facts produces explicit gaps.
    """
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    start, end, period_source = _period(folder, result)
    selected = _frames_for_period(frames, start, end)

    def scoped(rows, field):
        return [row for row in rows if start is not None and end is not None and
                (moment := _time(row.get(field, row.get("timestamp")))) is not None and start <= moment <= end]

    observations = scoped(_records(result.get("entry_observations")), "timestamp")
    trades = scoped(_records(result.get("trades")), "fill_time")
    trades = [row for row in trades if row.get("exit_reason") != "EndOfBacktest"]
    events = scoped(_records(result.get("close_event_records")), "timestamp")
    transitions, setups = _state_tables(selected, parameters, start, end, observations, trades)
    exits, reentries = _exit_tables(selected, trades, events, observations)
    candidates, buckets = _candidate_tables(observations, trades, exits)
    tables = {
        "review_state_transitions.csv": (transitions, ["symbol", "raw_change_at", "stable_confirmed_at"]),
        "review_setup_timing.csv": (setups, ["signal_id", "signal_time", "outcome"]),
        "review_exit_quality.csv": (exits, ["close_event_id", "evidence_status"]),
        "review_reentry_costs.csv": (reentries, ["close_event_id", "status"]),
        "review_candidate_allocation.csv": (candidates, ["observation_id", "score", "outcome_coverage"]),
        "review_score_buckets.csv": (buckets, ["score_bucket", "candidate_count"]),
    }
    for name, (rows, columns) in tables.items():
        pd.DataFrame(rows, columns=None if rows else columns).to_csv(folder / name, index=False)
    allocation = _records(result.get("allocation_audit"))
    summary = {
        "schema": "strategy_review_diagnostics/v1", "status": "complete" if selected else "insufficient_period_or_market_evidence",
        "start": start, "end": end, "period_source": period_source, "warmup_bars_per_symbol": 100,
        "symbol_count": len(selected), "entry_observation_count": len(observations),
        "raw_state_source": "independent_configured_MarketStateMachine_stability_period_1",
        "initial_stop_policy": parameters.get("stops", {}),
        "setup_outcomes": dict(Counter(row["outcome"] for row in setups)),
        "exit_evidence": dict(Counter(row["evidence_status"] for row in exits)),
        "unmatched_close_event_count": len({str(row.get("close_event_id")) for row in events
                                             if row.get("exit_reason") != "EndOfBacktest"}
                                            - {str(row.get("close_event_id")) for row in exits}),
        "candidate_count": len(candidates), "competing_batch_count": len({row["timestamp"] for row in candidates if row["competing_batch"]}),
        "allocation_audit_count_unlinked": len(allocation),
        "allocation_reason_counts_unlinked": dict(Counter(row.get("reason", "unknown") for row in allocation)),
        "allocation_linkage": "identity_bearing_entry_observations_only_untimestamped_allocator_rows_not_joined",
        "gate_fact_coverage": "observed_only" if observations else "unknown_no_entry_observations",
        "cost_scope": "realized_close_facts_net_execution_costs_separate_financing_not_allocated",
        "interpretation": "retrospective_descriptive_no_counterfactual_profit_or_causal_filter_claim",
        "router_zero_cooldown_semantics": "no_subsequent_cooldown_switch_bar_still_skipped",
        "artifacts": list(tables),
    }
    (folder / "review_diagnostics.json").write_text(canonical_json(summary) + "\n", encoding="utf-8")
    return json.loads(canonical_json(summary))
