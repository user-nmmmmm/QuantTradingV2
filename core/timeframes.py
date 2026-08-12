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
    now_utc = pd.Timestamp(as_utc_datetime(now))
    index = pd.DatetimeIndex(dataframe.index)
    index = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
    close_times = index + delta
    return dataframe.loc[close_times <= now_utc]

