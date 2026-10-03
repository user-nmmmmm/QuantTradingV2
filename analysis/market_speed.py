"""Observed closed-bar price velocity, distinct from network latency."""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from core.market_data import normalize_market_frame
from core.timeframes import as_utc_timestamp, closed_bars, timeframe_delta


def summarize_market_speed(frame: pd.DataFrame, *, timeframe: str, observed_at: datetime) -> dict:
    normalized = normalize_market_frame(frame)
    required = {"close", "high", "low"}
    if not required.issubset(normalized.columns):
        raise ValueError("market speed requires close, high and low")
    eligible = closed_bars(normalized, timeframe, observed_at)
    if len(eligible) < 2:
        raise ValueError("at least two closed bars are required")
    close = pd.to_numeric(eligible["close"], errors="coerce")
    close = close.where(np.isfinite(close) & (close > 0))
    returns = np.log(close).diff() * 10000.
    minutes = eligible.index.to_series().diff().dt.total_seconds() / 60.
    velocities = (returns / minutes.where(minutes > 0)).dropna()
    if velocities.empty:
        raise ValueError("no valid adjacent positive-price pairs")
    absolute = velocities.abs()
    high, low = (pd.to_numeric(eligible[name], errors="coerce") for name in ("high", "low"))
    valid_range = np.isfinite(high) & np.isfinite(low) & (low > 0) & (high >= low)
    ranges = (np.log(high.where(valid_range) / low.where(valid_range)) * 10000.).dropna()
    interval = timeframe_delta(timeframe)
    interval_minutes = interval.total_seconds() / 60.
    last_close = as_utc_timestamp(eligible.index[-1]) + interval
    observed = as_utc_timestamp(observed_at)
    return {
        "definition": "10000 * log(close_t / close_previous) / actual_elapsed_minutes",
        "timeframe": timeframe,
        "observed_at": observed.isoformat(),
        "first_open": as_utc_timestamp(eligible.index[0]).isoformat(),
        "last_open": as_utc_timestamp(eligible.index[-1]).isoformat(),
        "last_close": last_close.isoformat(),
        "last_closed_bar_age_seconds": float((observed - last_close).total_seconds()),
        "closed_bars": len(eligible),
        "excluded_unclosed_bars": len(normalized) - len(eligible),
        "valid_price_pairs": len(velocities),
        "gap_count": int((minutes > interval_minutes * 1.5).sum()),
        "velocity_bps_per_minute": {
            "mean_signed": float(velocities.mean()),
            "latest_signed": float(velocities.iloc[-1]),
            "median_absolute": float(absolute.median()),
            "p95_absolute": float(absolute.quantile(.95)),
            "max_absolute": float(absolute.max()),
        },
        "bar_range_bps": {
            "median": float(ranges.median()) if len(ranges) else None,
            "p95": float(ranges.quantile(.95)) if len(ranges) else None,
        },
        "limits": "closed-bar descriptive statistics; gaps use actual elapsed time; "
                  "range is not a tick path; local clock skew affects age; not a price forecast",
    }
