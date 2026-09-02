"""Trustworthiness diagnostics for backtest results.

`core/metrics.py` answers "how did the strategy perform". This module answers
a different and prior question: **should the performance number be believed at
all**, and **is the system actually doing what its code says**.

Every metric here exists because a real defect hid behind its absence:

- `calculate_pnl_concentration` — a +74% return whose top 10 of 329 trades
  contributed 128% of the profit is a handful of lucky trades, not an edge.
  Headline return/Sharpe/PF cannot distinguish the two.
- `calculate_exit_attribution` — TrendBreakout entered 91 times and its own
  Donchian exit rule fired zero times; every position was force-closed by the
  router on regime change. Strategy/symbol attribution shows none of this, so
  the exit parameters looked tuned while being entirely inert.
- `calculate_lifecycle_coverage` — the strategy-health "alpha death" gate and
  the mean-reversion cooldown both feed off closed-trade callbacks that fired
  once in 97 fills, silently disabling both safeguards.
- `calculate_calendar_returns` — positional segments (`calculate_segment_returns`)
  cannot show that 4 of 10 calendar years were negative and one year carried
  55% of all profit.
- `calculate_streaks` — consecutive-loss runs drive both risk-of-ruin and the
  cooldown logic, and were never surfaced (BM2-06).

Conventions match core/metrics.py: pure functions, no input mutation, `None`
plus a status string instead of NaN/Infinity, and an explicit `sample_size`.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, Iterable, List, Mapping, Optional

from core.allocation import holding_period_audit, joint_entry_exit_attribution

import numpy as np
import pandas as pd

DIAGNOSTICS_FORMULA_VERSION = "1.0"

# A strategy that opens positions but almost never closes them on its own exit
# rule is not implementing the exit policy its code describes.
INERT_EXIT_RATIO_THRESHOLD = 0.10


def _net_pnls(trades: Iterable[Mapping[str, Any]]) -> np.ndarray:
    values = [
        float(trade["net_pnl"])
        for trade in trades
        if trade.get("net_pnl") is not None
    ]
    return np.asarray(values, dtype=float)


def calculate_pnl_concentration(
    trades: Iterable[Mapping[str, Any]],
    top_n: Iterable[int] = (1, 3, 5, 10),
) -> Dict[str, Any]:
    """How much of the net profit rests on a few trades.

    For each N: the share of total net PnL contributed by the N most profitable
    trades, and what the total would be without them. A share above 100% means
    the system is net-losing once those trades are removed.

    Also reports the Herfindahl-Hirschman index over positive PnL (1.0 = a
    single trade produced everything, ~1/n = evenly spread), which unlike the
    top-N shares does not depend on an arbitrary cutoff.
    """
    values = _net_pnls(trades)
    n = int(values.size)
    if n == 0:
        return {"status": "insufficient", "sample_size": 0, "total_net_pnl": None,
                "top_n": {}, "profit_hhi": None}

    total = float(values.sum())
    ordered = np.sort(values)[::-1]

    shares: Dict[str, Any] = {}
    for count in top_n:
        count = int(count)
        if count <= 0 or count > n:
            continue
        contribution = float(ordered[:count].sum())
        shares[str(count)] = {
            "contribution": contribution,
            # Share is undefined against a zero/negative total: dividing would
            # invert the sign and read as a meaningless percentage.
            "share_of_total": (contribution / total) if total > 0 else None,
            "total_excluding": total - contribution,
            "sample_size": count,
        }

    wins = ordered[ordered > 0]
    gross_profit = float(wins.sum())
    profit_hhi = (
        float(((wins / gross_profit) ** 2).sum()) if gross_profit > 0 else None
    )

    return {
        "status": "ok",
        "sample_size": n,
        "total_net_pnl": total,
        "gross_profit": gross_profit,
        "win_count": int(wins.size),
        "top_n": shares,
        "profit_hhi": profit_hhi,
        "formula_version": DIAGNOSTICS_FORMULA_VERSION,
    }


def calculate_exit_attribution(
    trades: Iterable[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Who actually closed each position, and why.

    Splits closed trades by `exit_reason` and by whether the closing actor
    (`exit_strategy`) was the strategy that opened the position or an external
    one (the router's regime-switch liquidation). `own_exit_ratio` below
    `INERT_EXIT_RATIO_THRESHOLD` is flagged in `inert_exit_logic`: those
    strategies' exit rules — and every parameter feeding them — are effectively
    dead, whatever the code says.
    """
    records = [dict(trade) for trade in trades]
    if not records:
        return {"status": "insufficient", "sample_size": 0, "by_reason": {},
                "by_strategy": {}, "inert_exit_logic": [], "own_exit_ratio": None}

    reason_counts: Counter = Counter()
    per_strategy: Dict[str, Dict[str, Any]] = {}
    own_total = 0

    for record in records:
        reason = record.get("exit_reason") or "unknown"
        reason_counts[str(reason)] += 1

        opener = str(record.get("strategy") or "Unknown")
        closer = record.get("exit_strategy")
        # Missing closer information must not be scored as a self-exit; that
        # would mask exactly the defect this function exists to surface.
        is_own = closer is not None and str(closer) == opener
        own_total += int(is_own)

        entry = per_strategy.setdefault(
            opener,
            {"closed_trades": 0, "own_exits": 0, "external_exits": 0,
             "net_pnl": 0.0, "reasons": Counter()},
        )
        entry["closed_trades"] += 1
        entry["own_exits" if is_own else "external_exits"] += 1
        entry["net_pnl"] += float(record.get("net_pnl") or 0.0)
        entry["reasons"][str(reason)] += 1

    inert: List[str] = []
    by_strategy: Dict[str, Any] = {}
    for name, entry in per_strategy.items():
        closed = entry["closed_trades"]
        ratio = entry["own_exits"] / closed if closed else None
        if ratio is not None and ratio < INERT_EXIT_RATIO_THRESHOLD:
            inert.append(name)
        by_strategy[name] = {
            "closed_trades": closed,
            "own_exits": entry["own_exits"],
            "external_exits": entry["external_exits"],
            "own_exit_ratio": ratio,
            "net_pnl": entry["net_pnl"],
            "reasons": dict(entry["reasons"]),
        }

    total = len(records)
    return {
        "status": "ok",
        "sample_size": total,
        "by_reason": dict(reason_counts),
        "by_strategy": by_strategy,
        "own_exit_ratio": own_total / total,
        "inert_exit_logic": sorted(inert),
        "inert_threshold": INERT_EXIT_RATIO_THRESHOLD,
        "formula_version": DIAGNOSTICS_FORMULA_VERSION,
    }


def calculate_lifecycle_coverage(
    closed_trades: Iterable[Mapping[str, Any]],
    observed_close_events: Mapping[str, int],
) -> Dict[str, Any]:
    """Do strategies observe the round trips that their own state depends on?

    Strategy-level safeguards (health/alpha-death gates, consecutive-loss
    cooldowns) update from a closed-trade callback. If the callback misses
    closures — e.g. because the closing fill is tagged to the router rather
    than the strategy — those safeguards silently stop working while still
    appearing implemented.

    `observed_close_events` maps strategy name -> callback invocation count.
    Coverage well below 1.0 means the safeguards are running blind.
    """
    expected: Counter = Counter()
    for trade in closed_trades:
        expected[str(trade.get("strategy") or "Unknown")] += 1

    if not expected:
        return {"status": "insufficient", "sample_size": 0, "by_strategy": {},
                "blind_strategies": []}

    by_strategy: Dict[str, Any] = {}
    blind: List[str] = []
    for name, expected_count in expected.items():
        observed = int(observed_close_events.get(name, 0))
        coverage = observed / expected_count if expected_count else None
        if coverage is not None and coverage < 1.0:
            blind.append(name)
        by_strategy[name] = {
            "expected_closures": expected_count,
            "observed_closures": observed,
            "coverage": coverage,
        }

    total_expected = sum(expected.values())
    total_observed = sum(
        int(observed_close_events.get(name, 0)) for name in expected
    )
    return {
        "status": "ok",
        "sample_size": total_expected,
        "overall_coverage": total_observed / total_expected,
        "by_strategy": by_strategy,
        "blind_strategies": sorted(blind),
        "formula_version": DIAGNOSTICS_FORMULA_VERSION,
    }


def calculate_calendar_returns(
    equity: pd.Series,
    trades: Optional[Iterable[Mapping[str, Any]]] = None,
    freq: str = "YE",
) -> Dict[str, Any]:
    """Calendar-period returns (default: yearly), optionally with trade counts.

    Unlike `calculate_segment_returns`, which slices by index position, this
    aligns to real calendar boundaries — the only way to see "4 of 10 years
    were negative" or "one year carried most of the profit".
    """
    clean = equity.dropna()
    if clean.empty or not isinstance(clean.index, pd.DatetimeIndex):
        return {"status": "insufficient", "sample_size": 0, "periods": []}

    clean = clean.sort_index()
    period_end = clean.resample(freq).last().dropna()
    if period_end.empty:
        return {"status": "insufficient", "sample_size": 0, "periods": []}

    # Seed with the opening equity so the first period measures from the actual
    # start of the backtest rather than being dropped by pct_change.
    opening = pd.Series([float(clean.iloc[0])], index=[clean.index[0]])
    basis = pd.concat([opening, period_end])
    basis = basis[~basis.index.duplicated(keep="last")].sort_index()
    returns = basis.pct_change(fill_method=None).dropna()

    trade_counts: Counter = Counter()
    pnl_by_period: Dict[Any, float] = {}
    if trades is not None:
        for trade in trades:
            exit_time = trade.get("exit_time")
            if exit_time is None:
                continue
            key = _to_period(exit_time, freq)
            trade_counts[key] += 1
            pnl_by_period[key] = pnl_by_period.get(key, 0.0) + float(
                trade.get("net_pnl") or 0.0
            )

    periods: List[Dict[str, Any]] = []
    for stamp, value in returns.items():
        key = _to_period(stamp, freq)
        periods.append({
            "period": str(key),
            "return": float(value),
            "end_equity": float(basis.loc[stamp]),
            "trades": int(trade_counts.get(key, 0)) if trades is not None else None,
            "net_pnl": pnl_by_period.get(key) if trades is not None else None,
        })

    values = [entry["return"] for entry in periods]
    negative = [entry for entry in periods if entry["return"] < 0]
    return {
        "status": "ok",
        "sample_size": len(periods),
        "periods": periods,
        "negative_periods": len(negative),
        "negative_ratio": len(negative) / len(periods) if periods else None,
        "best": max(periods, key=lambda e: e["return"]) if periods else None,
        "worst": min(periods, key=lambda e: e["return"]) if periods else None,
        "mean_return": float(np.mean(values)) if values else None,
        "formula_version": DIAGNOSTICS_FORMULA_VERSION,
    }


def _period_alias(freq: str) -> str:
    """Map a resample alias to the matching Period alias."""
    head = freq.upper().rstrip("E").rstrip("S") or "Y"
    return {"Y": "Y", "A": "Y", "Q": "Q", "M": "M", "W": "W", "D": "D"}.get(
        head[0], "Y"
    )


def _to_period(stamp: Any, freq: str) -> Any:
    """Bucket a timestamp by its own wall-clock date.

    ``Timestamp.to_period`` drops the timezone (with a warning) rather than
    converting, so make that explicit: a trade closed at 00:30 +08:00 belongs
    to the calendar period the trader saw it in, not the UTC one it maps to.
    Equity timestamps and trade timestamps come from the same run, so both
    sides bucket consistently.
    """
    value = pd.Timestamp(stamp)
    if value.tzinfo is not None:
        value = value.tz_localize(None)
    return value.to_period(_period_alias(freq))


def calculate_streaks(trades: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """Consecutive win/loss runs over chronologically ordered closed trades (BM2-06).

    Drives risk-of-ruin intuition and is the quantity consecutive-loss cooldown
    rules key off, so a run length far beyond the configured threshold means
    that rule never engaged.
    """
    records = [
        trade for trade in trades if trade.get("net_pnl") is not None
    ]
    if not records:
        return {"status": "insufficient", "sample_size": 0,
                "max_win_streak": None, "max_loss_streak": None}

    def _sort_key(trade: Mapping[str, Any]):
        exit_time = trade.get("exit_time")
        return (exit_time is None, pd.Timestamp(exit_time) if exit_time is not None else 0)

    ordered = sorted(records, key=_sort_key)

    max_win = max_loss = 0
    current_win = current_loss = 0
    best_win_pnl = worst_loss_pnl = 0.0
    win_run_pnl = loss_run_pnl = 0.0

    for trade in ordered:
        pnl = float(trade["net_pnl"])
        if pnl > 0:
            current_win += 1
            win_run_pnl += pnl
            current_loss = 0
            loss_run_pnl = 0.0
            if current_win > max_win:
                max_win, best_win_pnl = current_win, win_run_pnl
        elif pnl < 0:
            current_loss += 1
            loss_run_pnl += pnl
            current_win = 0
            win_run_pnl = 0.0
            if current_loss > max_loss:
                max_loss, worst_loss_pnl = current_loss, loss_run_pnl
        else:
            # Breakeven breaks both runs without counting toward either.
            current_win = current_loss = 0
            win_run_pnl = loss_run_pnl = 0.0

    return {
        "status": "ok",
        "sample_size": len(ordered),
        "max_win_streak": max_win,
        "max_win_streak_pnl": best_win_pnl,
        "max_loss_streak": max_loss,
        "max_loss_streak_pnl": worst_loss_pnl,
        "formula_version": DIAGNOSTICS_FORMULA_VERSION,
    }


def _to_timestamp(value: Any) -> Optional[pd.Timestamp]:
    """Best-effort tz-naive UTC timestamp, so gaps can be compared safely."""
    if value is None:
        return None
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if stamp is pd.NaT or pd.isna(stamp):
        return None
    return stamp.tz_convert(None) if stamp.tzinfo is not None else stamp


def _in_window(value: Any, start: Any, end: Any) -> bool:
    stamp = _to_timestamp(value)
    if stamp is None or start is None or end is None:
        return False
    return pd.Timestamp(start) <= stamp <= pd.Timestamp(end)


def strategy_activity_consistency(
    closed_trades: Iterable[Mapping[str, Any]],
    equity: pd.Series,
    strategy_health: Optional[Mapping[str, Mapping[str, Any]]] = None,
    *,
    silent_gap_days: int = 365,
) -> Dict[str, Any]:
    """SR1-4: refuse to call a silently disabled strategy "completed".

    The 2021-2026 failure this guards against: the health gate flipped off,
    ``should_enter`` returned ``None`` for the rest of the run, and the report
    still said ``status=completed``/``inactive_bars=0``. If the run contains a
    trading gap longer than ``silent_gap_days`` while every strategy still
    claims to be ``active``, that is a defect in the health/reporting pipeline,
    not a market with no setups - it is surfaced as a high-priority finding.
    """
    records = [dict(trade) for trade in closed_trades]
    exits = sorted(
        stamp for stamp in (
            _to_timestamp(trade.get("exit_time") or trade.get("fill_time"))
            for trade in records
        ) if stamp is not None
    )
    if equity is None or len(equity) == 0:
        return {"status": "insufficient", "sample_size": len(records)}
    index = [
        stamp for stamp in (_to_timestamp(value) for value in equity.index)
        if stamp is not None
    ]
    if not index:
        return {"status": "insufficient", "sample_size": len(records)}
    start, end = index[0], index[-1]
    boundaries = [start, *exits, end]
    longest = pd.Timedelta(0)
    gap_start = gap_end = None
    for earlier, later in zip(boundaries[:-1], boundaries[1:]):
        span = pd.Timestamp(later) - pd.Timestamp(earlier)
        if span > longest:
            longest, gap_start, gap_end = span, earlier, later
    health = {name: dict(value) for name, value in (strategy_health or {}).items()}
    statuses = {name: str(entry.get("status", "unknown")) for name, entry in health.items()}
    all_active = bool(statuses) and all(value == "active" for value in statuses.values())
    # A long quiet stretch is only a defect if the alpha actually produced
    # setups during it: a market that offered nothing is not a broken gate.
    setups_in_gap = [
        name for name, entry in health.items()
        if _in_window(entry.get("last_raw_setup_at"), gap_start, gap_end)
    ]
    suppressed = sum(
        int(entry.get("suppressed_raw_setups") or 0) for entry in health.values()
    )
    silent = longest.days >= silent_gap_days and (
        (all_active and bool(setups_in_gap)) or suppressed > 0
    )
    findings = []
    if silent:
        findings.append(
            f"no closed trade for {longest.days} days "
            f"({pd.Timestamp(gap_start).date()} -> {pd.Timestamp(gap_end).date()}) "
            f"while strategy health reports {statuses or 'no health state'} and "
            f"{suppressed} raw setups were suppressed "
            f"(strategies with setups inside the gap: {setups_in_gap or 'none'}): "
            "either the health lifecycle or the reporting of it is wrong"
        )
    return {
        "status": "ok",
        "sample_size": len(records),
        "longest_no_trade_days": int(longest.days),
        "longest_no_trade_start": (
            pd.Timestamp(gap_start).isoformat() if gap_start is not None else None
        ),
        "longest_no_trade_end": (
            pd.Timestamp(gap_end).isoformat() if gap_end is not None else None
        ),
        "silent_gap_days_threshold": int(silent_gap_days),
        "suppressed_raw_setups": int(suppressed),
        "strategies_with_setups_in_gap": setups_in_gap,
        "strategy_health_status": statuses,
        "silent_inactivity_detected": bool(silent),
        "findings": findings,
    }


def build_diagnostics(
    closed_trades: Iterable[Mapping[str, Any]],
    equity: pd.Series,
    observed_close_events: Optional[Mapping[str, int]] = None,
    strategy_health: Optional[Mapping[str, Mapping[str, Any]]] = None,
    closed_legs: Optional[Iterable[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    """Run the full diagnostic suite over one backtest's closed trades/equity.

    ``closed_trades`` are round trips (one position, flat to flat), which is
    the unit every diagnostic here reasons about. ``closed_legs`` are the
    finer FIFO open/close matches, and exist as a separate argument for
    ``lifecycle_coverage`` alone: its counterpart counter,
    ``Strategy.observed_close_events``, increments once per *lot close*, so
    comparing it against round trips would report coverage above 1.0 on any
    run whose orders were split by the participation cap. It defaults to
    ``closed_trades`` for callers that only have the one granularity.
    """
    records = [dict(trade) for trade in closed_trades]
    leg_records = (
        records if closed_legs is None else [dict(leg) for leg in closed_legs]
    )
    suite: Dict[str, Any] = {
        "pnl_concentration": calculate_pnl_concentration(records),
        "exit_attribution": calculate_exit_attribution(records),
        "joint_entry_exit_attribution": joint_entry_exit_attribution(records),
        "holding_period_audit": holding_period_audit(records, max_holding_days=365),
        "calendar_returns": calculate_calendar_returns(equity, records),
        "streaks": calculate_streaks(records),
        "strategy_activity_consistency": strategy_activity_consistency(
            records, equity, strategy_health
        ),
    }
    if observed_close_events is not None:
        suite["lifecycle_coverage"] = calculate_lifecycle_coverage(
            leg_records, observed_close_events
        )
    return suite
