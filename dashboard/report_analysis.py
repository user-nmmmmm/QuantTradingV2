"""Small, dependency-free report analytics and a bounded immutable result cache."""

from __future__ import annotations

import json
import math
import statistics
from collections import OrderedDict
from datetime import datetime, timezone
from threading import RLock
from typing import Any


class ReportCache:
    """Store serialized results so callers cannot mutate cached state.

    Keys include every input file's identity, size and nanosecond timestamps.
    Both entry count and total serialized bytes are bounded; old versions of a
    logical entry are removed as soon as a new version is stored.
    """

    def __init__(self, max_entries: int = 8, max_bytes: int = 16 * 1024 * 1024):
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._items: OrderedDict[tuple, tuple[tuple, bytes]] = OrderedDict()
        self._bytes = 0
        self._lock = RLock()

    def get(self, key: tuple, signature: tuple) -> Any | None:
        with self._lock:
            item = self._items.get(key)
            if item is None or item[0] != signature:
                return None
            self._items.move_to_end(key)
            encoded = item[1]
        return json.loads(encoded)

    def put(self, key: tuple, signature: tuple, value: Any) -> None:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._bytes -= len(old[1])
            if len(encoded) > self.max_bytes:
                return
            while self._items and (
                len(self._items) >= self.max_entries or self._bytes + len(encoded) > self.max_bytes
            ):
                _, (_, evicted) = self._items.popitem(last=False)
                self._bytes -= len(evicted)
            self._items[key] = (signature, encoded)
            self._bytes += len(encoded)


def utc_moment(timestamp: str) -> datetime:
    moment = datetime.fromisoformat(timestamp)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def finite(value: float) -> float | None:
    return value if math.isfinite(value) else None


def analyze_equity(points: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute observation-period statistics without assuming daily sampling."""
    moments = [utc_moment(point["timestamp"]) for point in points]
    returns = [finite(right["equity"] / left["equity"] - 1)
               for left, right in zip(points, points[1:])]
    # An unrepresentable return invalidates aggregate period statistics instead
    # of quietly changing their sample or reporting a misleading success ratio.
    valid_returns = returns if all(value is not None for value in returns) else []
    days = (moments[-1] - moments[0]).total_seconds() / 86400
    annualized = None
    if days > 0:
        try:
            annualized = finite(math.expm1(
                (math.log(points[-1]["equity"]) - math.log(points[0]["equity"])) * 365.25 / days
            ))
        except OverflowError:
            pass
    aligned = points[0]["benchmark"] is not None and points[-1]["benchmark"] is not None
    benchmark_return = (finite(points[-1]["benchmark"] / points[0]["benchmark"] - 1)
                        if aligned else None)
    total_return = finite(points[-1]["equity"] / points[0]["equity"] - 1)
    monthly = []
    for index, (point, moment) in enumerate(zip(points, moments)):
        month = moment.strftime("%Y-%m")
        if not monthly or monthly[-1]["month"] != month:
            baseline = points[index - 1] if index else point
            monthly.append({
                "month": month, "return": None, "period_start": baseline["timestamp"],
                "period_end": point["timestamp"], "periods": 0, "_equity": baseline["equity"],
            })
        current = monthly[-1]
        current["period_end"] = point["timestamp"]
        current["periods"] += int(index > 0)
        if current["periods"]:
            current["return"] = finite(point["equity"] / current["_equity"] - 1)
    for current in monthly:
        del current["_equity"]
    return {
        "metrics": {
            "observations": len(points), "periods": len(returns), "duration_days": days,
            "annualized_return": annualized,
            "period_volatility": (finite(statistics.stdev(valid_returns))
                                  if len(valid_returns) > 1 else None),
            "positive_period_ratio": (sum(value > 0 for value in valid_returns) / len(valid_returns)
                                      if valid_returns else None),
            "best_period_return": max(valid_returns, default=None),
            "worst_period_return": min(valid_returns, default=None),
            "benchmark_total_return": benchmark_return,
            "excess_return": (finite(total_return - benchmark_return)
                              if total_return is not None and benchmark_return is not None else None),
        },
        "monthly_returns": monthly,
        "benchmark_aligned": aligned,
        "methodology": {
            "period_return": "Adjacent valid equity observations: equity[t] / equity[t-1] - 1; sampling may be irregular. Aggregate period statistics are undefined if any return is nonfinite.",
            "positive_period_ratio": "Positive observation-period returns / all observation-period returns; not a closed-trade win rate.",
            "period_volatility": "Sample standard deviation of observation-period returns; not annualized.",
            "annualized_return": "(end_equity / start_equity) ** (365.25 / elapsed_days) - 1; undefined for zero elapsed time.",
            "monthly_return": "Last equity observed in UTC calendar month / previous observed month-end equity - 1. First month starts at first observation; missing months are omitted. Intervals spanning missing months belong to their ending month.",
            "benchmark_return": "Last matched benchmark / first matched benchmark - 1; available only when both report endpoints match. Excess is strategy return minus benchmark return (percentage points).",
            "fills_count": "Valid execution rows in trades.csv; fills are not closed trades.",
        },
    }
