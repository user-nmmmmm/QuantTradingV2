from datetime import datetime, timedelta, timezone
import re

import pandas as pd


_TIMEFRAME_RE = re.compile(r"^(\d+)([mhdw])$")
_UNIT_SECONDS = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def timeframe_delta(timeframe: str) -> timedelta:
    match = _TIMEFRAME_RE.fullmatch((timeframe or "").strip().lower())
    if not match:
        raise ValueError(f"Unsupported fixed timeframe: {timeframe!r}")
    amount = int(match.group(1))
    if amount <= 0:
        raise ValueError("Timeframe amount must be positive")
    return timedelta(seconds=amount * _UNIT_SECONDS[match.group(2)])


def as_utc_timestamp(value) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def as_utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def observed_bars_after(dataframe: pd.DataFrame, i: int, timestamp) -> int:
    """Count observed bars after a timestamp, independent of window row shifts."""
    index = dataframe.index
    point = pd.Timestamp(timestamp)
    if isinstance(index, pd.DatetimeIndex):
        if not index.is_monotonic_increasing:
            raise ValueError("bar history must be sorted")
        if index.tz is None and point.tzinfo is not None:
            point = point.tz_convert("UTC").tz_localize(None)
        elif index.tz is not None:
            point = point.tz_localize("UTC") if point.tzinfo is None else point
            point = point.tz_convert(index.tz)
        return max(0, i + 1 - int(index.searchsorted(point, side="right")))
    # Preserve legacy non-datetime strategy fixtures.
    return max(0, i + 1 - int(index.searchsorted(timestamp, side="right")))


def closed_bars(
    dataframe: pd.DataFrame,
    timeframe: str,
    now: datetime,
    grace_seconds: float = 0.0,
) -> pd.DataFrame:
    """Return bars whose open timestamp plus timeframe is safely in the past."""
    if dataframe.empty:
        return dataframe
    delta = timeframe_delta(timeframe) + timedelta(seconds=max(grace_seconds, 0.0))
    cutoff = pd.Timestamp(as_utc_datetime(now)) - delta
    index = pd.DatetimeIndex(dataframe.index)
    if index.is_monotonic_increasing:
        comparable_cutoff = (
            cutoff.tz_localize(None) if index.tz is None else cutoff.tz_convert(index.tz)
        )
        stop = int(index.searchsorted(comparable_cutoff, side="right"))
        return dataframe.iloc[:stop]

    normalized = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
    return dataframe.loc[normalized <= cutoff]

