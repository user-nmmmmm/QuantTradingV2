"""Bounded, read-only projections of cached candles and backtest reports."""

from __future__ import annotations

import csv
import math
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_SYMBOL = re.compile(r"^[A-Z0-9]+/USDT$")
_RUN = re.compile(r"^[A-Za-z0-9._-]+$")
_MAX_REPORT_ROWS = 20_000


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def list_markets(data_dir: Path) -> list[str]:
    if not data_dir.is_dir():
        return []
    markets = []
    for path in data_dir.glob("*_USDT.csv"):
        if path.is_symlink() or not path.is_file():
            continue
        symbol = path.stem.replace("_", "/")
        if _SYMBOL.fullmatch(symbol):
            markets.append(symbol)
    return sorted(markets, key=lambda symbol: (symbol != "BTC/USDT", symbol != "ETH/USDT", symbol))


def load_candles(data_dir: Path, symbol: str, limit: int = 120) -> dict[str, Any]:
    if not _SYMBOL.fullmatch(symbol) or not 30 <= limit <= 240:
        raise ValueError("invalid symbol or candle limit")
    if symbol not in list_markets(data_dir):
        raise FileNotFoundError("market cache not found")
    path = data_dir / f"{symbol.replace('/', '_')}.csv"
    candles: deque[dict[str, Any]] = deque(maxlen=limit)
    invalid_rows = 0
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not {"timestamp", "open", "high", "low", "close", "volume"}.issubset(reader.fieldnames or []):
            raise ValueError("invalid candle file schema")
        for row in reader:
            opened = _number(row.get("open"))
            high = _number(row.get("high"))
            low = _number(row.get("low"))
            close = _number(row.get("close"))
            volume = _number(row.get("volume"))
            timestamp = row.get("timestamp", "")
            try:
                datetime.fromisoformat(timestamp)
            except (TypeError, ValueError):
                invalid_rows += 1
                continue
            if (
                any(value is None for value in (opened, high, low, close, volume))
                or min(opened, high, low, close) <= 0
                or volume < 0
                or high < low
                or high < max(opened, close)
                or low > min(opened, close)
            ):
                invalid_rows += 1
                continue
            candles.append({
                "timestamp": timestamp, "open": opened, "high": high,
                "low": low, "close": close, "volume": volume,
            })
    if not candles:
        raise ValueError("no valid candles")
    latest_date = datetime.fromisoformat(candles[-1]["timestamp"]).date()
    return {
        "symbol": symbol,
        "timeframe": "1d",
        "source": "Binance spot local cache",
        "mode": "historical_cache",
        "cache_updated_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
        "last_candle": candles[-1]["timestamp"],
        "age_days": max(0, (datetime.now(timezone.utc).date() - latest_date).days),
        "invalid_rows": invalid_rows,
        "candles": list(candles),
    }


def _run_paths(reports_dir: Path) -> list[Path]:
    if not reports_dir.is_dir():
        return []
    paths = []
    for path in reports_dir.iterdir():
        if path.is_symlink() or not path.is_dir() or not _RUN.fullmatch(path.name):
            continue
        equity = path / "equity.csv"
        if equity.is_symlink() or not equity.is_file():
            continue
        paths.append(path)
    return sorted(paths, key=lambda path: path.stat().st_mtime, reverse=True)


def list_backtests(reports_dir: Path, limit: int = 60) -> list[dict[str, Any]]:
    return [
        {
            "id": path.name,
            "modified_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
            "has_benchmark": (path / "benchmark.csv").is_file(),
        }
        for path in _run_paths(reports_dir)[:limit]
    ]


def _load_benchmark(path: Path, timestamps: set[str]) -> dict[str, float]:
    if path.is_symlink() or not path.is_file():
        return {}
    points = {}
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = [name for name in (reader.fieldnames or []) if name != "timestamp"]
        if "timestamp" not in (reader.fieldnames or []) or not columns:
            return {}
        column = columns[0]
        for row in reader:
            timestamp = row.get("timestamp", "")
            value = _number(row.get(column))
            if timestamp in timestamps and value is not None and value > 0:
                points[timestamp] = value
    return points


def load_backtest(reports_dir: Path, run_id: str) -> dict[str, Any]:
    if not _RUN.fullmatch(run_id) or run_id in {".", ".."}:
        raise ValueError("invalid backtest id")
    run = next((path for path in _run_paths(reports_dir) if path.name == run_id), None)
    if run is None:
        raise FileNotFoundError("backtest report not found")
    points = []
    invalid_rows = 0
    peak = 0.0
    worst_drawdown = 0.0
    with (run / "equity.csv").open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not {"timestamp", "equity"}.issubset(reader.fieldnames or []):
            raise ValueError("invalid equity file schema")
        for row in reader:
            if len(points) >= _MAX_REPORT_ROWS:
                raise ValueError("backtest report exceeds dashboard row limit")
            timestamp = row.get("timestamp", "")
            equity = _number(row.get("equity"))
            try:
                moment = datetime.fromisoformat(timestamp)
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
                else:
                    moment = moment.astimezone(timezone.utc)
            except (TypeError, ValueError):
                invalid_rows += 1
                continue
            if equity is None or equity <= 0 or (points and moment <= points[-1]["_moment"]):
                invalid_rows += 1
                continue
            peak = max(peak, equity)
            drawdown = equity / peak - 1
            worst_drawdown = min(worst_drawdown, drawdown)
            points.append({"timestamp": timestamp, "equity": equity, "drawdown": drawdown, "_moment": moment})
    if not points:
        raise ValueError("no valid equity points")
    for point in points:
        del point["_moment"]
    benchmark = _load_benchmark(run / "benchmark.csv", {point["timestamp"] for point in points})
    for point in points:
        point["benchmark"] = benchmark.get(point["timestamp"])
    fills_count = None
    trades_path = run / "trades.csv"
    if trades_path.is_file() and not trades_path.is_symlink():
        with trades_path.open("r", encoding="utf-8-sig", newline="") as handle:
            fills_count = max(0, sum(1 for _ in csv.reader(handle)) - 1)
    return {
        "id": run_id,
        "source": "equity.csv",
        "period_start": points[0]["timestamp"],
        "period_end": points[-1]["timestamp"],
        "invalid_rows": invalid_rows,
        "metrics": {
            "start_equity": points[0]["equity"],
            "end_equity": points[-1]["equity"],
            "total_return": points[-1]["equity"] / points[0]["equity"] - 1,
            "max_drawdown": worst_drawdown,
            "current_drawdown": points[-1]["drawdown"],
            "fills_count": fills_count,
        },
        "points": points,
    }
