"""Per-trade win/loss, profit-factor, and R-multiple quality metrics.

Split out of core/metrics.py (A4) — see docs/architecture_review.md.
"""
from __future__ import annotations
from typing import Any, Dict, Iterable, Mapping
from statistics import NormalDist
import numpy as np
import pandas as pd


def calculate_profit_factor(pnls: Iterable[float], minimum_samples: int = 30,
                            confidence: float = 0.95) -> Dict[str, Any]:
    values = np.asarray(list(pnls), dtype=float)
    values = values[np.isfinite(values)]
    wins, losses, flat = values[values > 0], values[values < 0], values[values == 0]
    base = {"sample_size": int(len(values)), "win_count": int(len(wins)),
            "loss_count": int(len(losses)), "breakeven_count": int(len(flat)),
            "lower": None, "upper": None}
    if len(values) == 0:
        return {"value": None, "status": "insufficient", **base}
    gross_loss = float(abs(losses.sum()))
    if len(losses) == 0 or np.isclose(gross_loss, 0.0):
        return {"value": None, "status": "undefined", **base}
    lower, upper = _profit_factor_interval(values, confidence)
    return {"value": float(wins.sum() / gross_loss),
            "status": "ok" if len(values) >= minimum_samples else "insufficient",
            **base, "lower": lower, "upper": upper}


def calculate_trade_quality(
    trades: Iterable[Mapping[str, Any]],
    minimum_samples: int = 30,
    confidence: float = 0.95,
) -> Dict[str, Any]:
    """Per-closed-trade win/loss, holding-duration, and PF statistics (BM2).

    Each record must provide ``net_pnl``. ``entry_time``/``exit_time`` (any
    value ``pd.Timestamp`` can parse) enable holding-duration stats;
    ``strategy``/``symbol`` enable the grouped breakdowns. Missing optional
    fields degrade that specific section to "insufficient" rather than
    raising, so this tolerates the partial records different execution
    venues happen to have on hand.
    """
    records = list(trades)
    if not records:
        return {
            "sample_size": 0, "status": "insufficient", "win_count": 0,
            "loss_count": 0, "breakeven_count": 0, "win_rate": None,
            "avg_win": None, "avg_loss": None, "expectancy": None,
            "profit_factor": None, "profit_factor_status": "insufficient",
            "holding_duration_hours": _EMPTY_DURATION, "by_strategy": {}, "by_symbol": {},
        }

    pnls = np.array([float(t["net_pnl"]) for t in records], dtype=float)
    if not np.isfinite(pnls).all():
        raise ValueError("trade quality requires finite net_pnl for every trade")
    wins, losses = pnls[pnls > 0], pnls[pnls < 0]
    win_rate = float(len(wins) / len(pnls))
    loss_rate = float(len(losses) / len(pnls))
    avg_win = float(wins.mean()) if len(wins) else None
    avg_loss = float(losses.mean()) if len(losses) else None
    expectancy = win_rate * (avg_win or 0.0) + loss_rate * (avg_loss or 0.0)
    pf = calculate_profit_factor(pnls, minimum_samples=minimum_samples, confidence=confidence)

    return {
        "sample_size": len(records),
        "status": "ok" if len(records) >= minimum_samples else "insufficient",
        "win_count": int(len(wins)), "loss_count": int(len(losses)),
        "breakeven_count": int(len(pnls) - len(wins) - len(losses)),
        "win_rate": win_rate, "avg_win": avg_win, "avg_loss": avg_loss,
        "expectancy": float(expectancy),
        "distribution": _quality_distribution(records, pnls, confidence),
        "profit_factor": pf["value"], "profit_factor_status": pf["status"],
        "holding_duration_hours": _holding_duration_hours(records),
        "by_strategy": _trade_quality_breakdown(records, "strategy", minimum_samples, confidence),
        "by_symbol": _trade_quality_breakdown(records, "symbol", minimum_samples, confidence),
    }


def _quality_distribution(records, pnls, confidence):
    n = len(pnls)
    rate = float(np.mean(pnls > 0))
    z = NormalDist().inv_cdf((1 + confidence) / 2)
    center = (rate + z * z / (2 * n)) / (1 + z * z / n)
    margin = z * np.sqrt(rate * (1 - rate) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    streaks = {}
    for label, sign in (("wins", 1), ("losses", -1)):
        best_count = count = 0
        best_pnl = pnl = 0.0
        for value in pnls:
            if value * sign > 0:
                count, pnl = count + 1, pnl + float(value)
                if count > best_count:
                    best_count, best_pnl = count, pnl
            else:
                count, pnl = 0, 0.0
        streaks[label] = {"max_count": best_count, "net_pnl": best_pnl,
                          "tie_policy": "first longest sequence in supplied chronological order"}
    notional_returns = []
    for record, pnl in zip(records, pnls):
        entry, qty = record.get("entry_price"), record.get("qty")
        if entry is not None and qty is not None and float(entry) > 0 and float(qty) > 0:
            notional_returns.append(float(pnl) / (float(entry) * float(qty)))
    wins, losses = pnls[pnls > 0], pnls[pnls < 0]
    return {"sample_size": n, "currency_expectancy": float(pnls.mean()),
            "payoff_ratio": float(wins.mean() / -losses.mean()) if len(wins) and len(losses) else None,
            "max_win": float(wins.max()) if len(wins) else None,
            "max_loss": float(losses.min()) if len(losses) else None,
            "median": float(np.median(pnls)), "std": float(pnls.std(ddof=1)) if n > 1 else None,
            "quantiles": {str(q): float(np.quantile(pnls, q)) for q in (.05, .25, .75, .95)},
            "win_rate_interval": {"method": "Wilson", "confidence": confidence,
                                  "lower": max(0.0, float(center - margin)), "upper": min(1.0, float(center + margin))},
            "streaks": streaks,
            "holding_hours_winners": _holding_duration_hours([r for r in records if float(r["net_pnl"]) > 0]),
            "holding_hours_losers": _holding_duration_hours([r for r in records if float(r["net_pnl"]) < 0]),
            "expectancy_per_entry_notional": {"value": float(np.mean(notional_returns)) if len(notional_returns) == n else None,
                "status": "ok" if len(notional_returns) == n else "not_modeled", "sample_size": len(notional_returns),
                "reason": None if len(notional_returns) == n else "entry notional absent on some trades"}}


_EMPTY_DURATION: Dict[str, Any] = {
    "status": "insufficient", "sample_size": 0,
    "mean": None, "median": None, "min": None, "max": None,
}


def _holding_duration_hours(records: list[Mapping[str, Any]]) -> Dict[str, Any]:
    hours = []
    for trade in records:
        entry, exit_ = trade.get("entry_time"), trade.get("exit_time")
        if entry is None or exit_ is None:
            continue
        entry_ts, exit_ts = pd.Timestamp(entry), pd.Timestamp(exit_)
        if pd.isna(entry_ts) or pd.isna(exit_ts):
            continue
        hours.append((exit_ts - entry_ts).total_seconds() / 3600.0)
    if not hours:
        return dict(_EMPTY_DURATION)
    values = np.array(hours, dtype=float)
    return {"status": "ok", "sample_size": int(len(values)),
            "mean": float(values.mean()), "median": float(np.median(values)),
            "min": float(values.min()), "max": float(values.max())}


def _trade_quality_breakdown(
    records: list[Mapping[str, Any]], key: str, minimum_samples: int, confidence: float,
) -> Dict[str, Any]:
    groups: Dict[Any, list] = {}
    for trade in records:
        label = trade.get(key)
        if label is None:
            continue
        groups.setdefault(label, []).append(float(trade["net_pnl"]))
    breakdown = {}
    for label, pnls in groups.items():
        wins = [pnl for pnl in pnls if pnl > 0]
        pf = calculate_profit_factor(pnls, minimum_samples=minimum_samples, confidence=confidence)
        breakdown[str(label)] = {
            "sample_size": len(pnls),
            "win_rate": float(len(wins) / len(pnls)),
            "net_pnl": float(sum(pnls)),
            "profit_factor": pf["value"], "profit_factor_status": pf["status"],
        }
    return breakdown


def calculate_r_multiple_stats(trades: Iterable[Mapping[str, Any]]) -> Dict[str, Any]:
    """R-Multiple distribution, System Quality Number, and MAE/MFE summary (BM7).

    A trade's R-multiple is ``net_pnl / initial_risk``, where
    ``initial_risk`` is the caller-supplied $ amount risked at entry (e.g.
    qty * |entry_price - stop_price|). Trades missing ``initial_risk`` (or
    with a non-positive one) cannot have an R-multiple and are excluded
    from that statistic — reported via ``excluded_no_initial_risk`` rather
    than silently dropped or coerced to R=0. ``mae``/``mfe`` (maximum
    adverse/favorable excursion) are optional per-trade fields this only
    summarizes; it does not derive them from a price path, since that needs
    bar-by-bar prices during the holding period that closed-trade records
    don't carry here.
    """
    records = list(trades)
    r_multiples = []
    excluded = 0
    for trade in records:
        risk = trade.get("initial_risk")
        if risk is None or not np.isfinite(float(risk)) or float(risk) <= 0:
            excluded += 1
            continue
        r_multiples.append(float(trade["net_pnl"]) / float(risk))
    r_values = np.array(r_multiples, dtype=float)

    if len(r_values) < 2:
        r_stats: Dict[str, Any] = {
            "status": "not_modeled" if records and not len(r_values) else "insufficient",
            "reason": "approved initial risk is absent" if records and not len(r_values) else "at least two valid risk observations required",
            "sample_size": int(len(r_values)),
            "mean_r": float(r_values.mean()) if len(r_values) else None,
            "std_r": None, "sqn": None,
        }
    else:
        std_r = float(r_values.std(ddof=1))
        mean_r = float(r_values.mean())
        sqn = None if np.isclose(std_r, 0.0) else float(np.sqrt(len(r_values)) * mean_r / std_r)
        r_stats = {"status": "ok", "sample_size": int(len(r_values)),
                   "mean_r": mean_r, "std_r": std_r, "sqn": sqn}

    def _summary(values: list[float]) -> Dict[str, Any]:
        if not values:
            return {"status": "insufficient", "sample_size": 0, "mean": None, "median": None}
        arr = np.array(values, dtype=float)
        return {"status": "ok", "sample_size": int(len(arr)),
                "mean": float(arr.mean()), "median": float(np.median(arr))}

    return {
        "sample_size": len(records), "excluded_no_initial_risk": excluded,
        "r_multiple": r_stats,
        "mae": _summary([float(t["mae"]) for t in records if t.get("mae") is not None]),
        "mfe": _summary([float(t["mfe"]) for t in records if t.get("mfe") is not None]),
    }


def _profit_factor_interval(values: np.ndarray, confidence: float):
    if len(values) < 2 or not 0 < confidence < 1:
        return None, None
    samples = np.random.default_rng(42).choice(values, size=(2000, len(values)), replace=True)
    profits = np.where(samples > 0, samples, 0.0).sum(axis=1)
    losses = np.abs(np.where(samples < 0, samples, 0.0).sum(axis=1))
    finite = profits[losses > 0] / losses[losses > 0]
    if len(finite) == 0:
        return None, None
    alpha = (1 - confidence) / 2
    return float(np.quantile(finite, alpha)), float(np.quantile(finite, 1 - alpha))
