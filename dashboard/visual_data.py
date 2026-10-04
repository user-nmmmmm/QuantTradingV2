"""Bounded, read-only projections of cached candles and backtest reports."""

from __future__ import annotations

import csv
import io
import json
import math
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dashboard.report_analysis import ReportCache, analyze_equity, finite, utc_moment


_SYMBOL = re.compile(r"^[A-Z0-9]+/USDT$")
_RUN = re.compile(r"^[A-Za-z0-9._-]+$")
_MAX_REPORT_ROWS = 20_000
_MAX_TRADE_ROWS = 100_000
_MAX_REPORT_BYTES = 16 * 1024 * 1024
_MAX_METADATA_BYTES = 32 * 1024
_MAX_COLUMNS = 64
_MAX_CELL_LENGTH = 1024
_REPORT_CACHE = ReportCache()


def _safe_file(path: Path) -> bool:
    return not path.is_symlink() and path.is_file() and path.resolve().parent == path.parent.resolve()


def _signature(paths: list[Path]) -> tuple:
    signatures = []
    for path in paths:
        if not _safe_file(path):
            signatures.append(None)
            continue
        stat = path.stat()
        signatures.append((stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
    return tuple(signatures)


def _csv_reader(path: Path, *, allow_timestamp_index: bool = False) -> csv.DictReader:
    """Read at most the byte budget, including files growing during a request."""
    if path.stat().st_size > _MAX_REPORT_BYTES:
        raise ValueError("report exceeds dashboard byte limit")
    with path.open("rb") as handle:
        contents = handle.read(_MAX_REPORT_BYTES + 1)
    if len(contents) > _MAX_REPORT_BYTES:
        raise ValueError("report exceeds dashboard byte limit")
    try:
        reader = csv.DictReader(io.StringIO(contents.decode("utf-8-sig"), newline=""), strict=True)
        columns = reader.fieldnames or []
    except (UnicodeError, csv.Error) as error:
        raise ValueError("invalid report CSV encoding or schema") from error
    # Pandas writes the benchmark's unnamed DatetimeIndex as a blank first
    # header. Accept that canonical export only at the benchmark call site.
    if allow_timestamp_index and len(columns) > 1 and columns[0] == "":
        columns[0] = "timestamp"
        reader.fieldnames = columns
    if len(columns) > _MAX_COLUMNS or any(not name or len(name) > 128 for name in columns):
        raise ValueError("invalid report CSV columns")
    if len(set(columns)) != len(columns):
        raise ValueError("duplicate report CSV columns")
    return reader


def _csv_rows(reader: csv.DictReader, limit: int):
    try:
        for index, row in enumerate(reader):
            if index >= limit:
                raise ValueError("report exceeds dashboard row limit")
            yield row
    except csv.Error as error:
        raise ValueError("invalid report CSV data") from error


def _report_run(reports_dir: Path, run_id: str) -> Path:
    if not _RUN.fullmatch(run_id) or run_id in {".", ".."}:
        raise ValueError("invalid backtest id")
    run = reports_dir / run_id
    if (run.is_symlink() or not run.is_dir() or run.resolve().parent != reports_dir.resolve()
            or not _safe_file(run / "equity.csv") or not _report_ready(run)):
        raise FileNotFoundError("backtest report not found")
    return run


def _report_ready(run: Path) -> bool:
    """Web workers publish their report only after atomically marking success."""
    if not run.name.startswith("web_"):
        return True
    return _job_metadata(run).get("status") == "succeeded"


def _job_metadata(run: Path) -> dict[str, Any]:
    metadata = run / "dashboard_job.json"
    if not _safe_file(metadata):
        return {}
    try:
        with metadata.open("rb") as handle:
            contents = handle.read(_MAX_METADATA_BYTES + 1)
        if len(contents) > _MAX_METADATA_BYTES:
            return {}
        payload = json.loads(contents)
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def _report_parameters(run: Path) -> dict[str, Any]:
    """Project only approved, bounded experiment parameters from the marker."""
    metadata = _job_metadata(run)
    raw = metadata.get("parameters")
    if metadata.get("status") != "succeeded" or not isinstance(raw, dict):
        return {}
    result = {}
    if raw.get("source") in ("local", "synthetic"):
        result["source"] = raw["source"]
    symbols = raw.get("symbols")
    if (isinstance(symbols, list) and 1 <= len(symbols) <= 4
            and all(isinstance(symbol, str) and _SYMBOL.fullmatch(symbol) for symbol in symbols)):
        result["symbols"] = list(symbols)
    for name in ("start", "end"):
        value = raw.get(name)
        if isinstance(value, str) and len(value) == 10:
            try:
                result[name] = datetime.fromisoformat(value).date().isoformat()
            except ValueError:
                pass
    for name, lower, upper in (("capital", 100, 1_000_000_000), ("slippage_bps", 0, 100)):
        value = raw.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and lower <= value <= upper:
            result[name] = value
    seed = raw.get("seed")
    if isinstance(seed, int) and not isinstance(seed, bool) and 0 <= seed <= 2**32 - 1:
        result["seed"] = seed
    if "strategy" in raw:
        from dashboard.strategy_presets import validate_strategy
        try:
            result["strategy"] = validate_strategy(raw["strategy"])
        except (ValueError, TypeError):
            pass
    return result


def _configuration_identity(run: Path) -> dict[str, str | None]:
    metadata = _job_metadata(run)
    return {key: value if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) else None
            for key in ("config_sha256", "base_config_sha256") for value in [metadata.get(key)]}


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
        if (path.is_symlink() or not path.is_dir() or not _RUN.fullmatch(path.name)
                or path.resolve().parent != reports_dir.resolve() or not _report_ready(path)):
            continue
        equity = path / "equity.csv"
        if not _safe_file(equity):
            continue
        paths.append(path)
    return sorted(paths, key=lambda path: path.stat().st_mtime, reverse=True)


def list_backtests(reports_dir: Path, limit: int = 60) -> list[dict[str, Any]]:
    return [
        {
            "id": path.name,
            "modified_at": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
            "has_benchmark": _safe_file(path / "benchmark.csv"),
            "source": parameters.get("source"),
            "parameters": parameters,
        }
        for path in _run_paths(reports_dir)[:limit]
        for parameters in [_report_parameters(path)]
    ]


def _load_benchmark(path: Path, timestamps: set[str]) -> dict[str, float]:
    if not _safe_file(path):
        return {}
    points = {}
    reader = _csv_reader(path, allow_timestamp_index=True)
    columns = [name for name in (reader.fieldnames or []) if name != "timestamp"]
    if "timestamp" not in (reader.fieldnames or []) or not columns:
        return {}
    column = columns[0]
    # ISO dates and equivalent timezone representations refer to the same instant.
    original_timestamps = {utc_moment(timestamp): timestamp for timestamp in timestamps}
    for row in _csv_rows(reader, _MAX_REPORT_ROWS):
        try:
            timestamp = original_timestamps.get(utc_moment(row.get("timestamp", "")))
        except (TypeError, ValueError):
            continue
        value = _number(row.get(column))
        if None not in row and timestamp is not None and value is not None and value > 0:
            points[timestamp] = value
    return points


def load_backtest(reports_dir: Path, run_id: str) -> dict[str, Any]:
    run = _report_run(reports_dir, run_id)
    dependencies = [run / name for name in ("equity.csv", "benchmark.csv", "trades.csv", "dashboard_job.json")]
    signature = _signature(dependencies)
    cache_key = ("backtest", str(run.resolve()))
    cached = _REPORT_CACHE.get(cache_key, signature)
    if cached is not None:
        return cached
    points = []
    invalid_rows = 0
    peak = 0.0
    worst_drawdown = 0.0
    reader = _csv_reader(run / "equity.csv")
    if not {"timestamp", "equity"}.issubset(reader.fieldnames or []):
        raise ValueError("invalid equity file schema")
    for row in _csv_rows(reader, _MAX_REPORT_ROWS):
        timestamp = row.get("timestamp", "")
        equity = _number(row.get("equity"))
        try:
            moment = utc_moment(timestamp)
        except (TypeError, ValueError):
            invalid_rows += 1
            continue
        if (None in row or equity is None or equity <= 0
                or (points and moment <= points[-1]["_moment"])):
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
    trades = load_trades(reports_dir, run_id)
    analysis = analyze_equity(points)
    payload = {
        "id": run_id,
        "source": "equity.csv",
        "parameters": _report_parameters(run),
        "configuration": _configuration_identity(run),
        "period_start": points[0]["timestamp"],
        "period_end": points[-1]["timestamp"],
        "invalid_rows": invalid_rows,
        "metrics": {
            "start_equity": points[0]["equity"],
            "end_equity": points[-1]["equity"],
            "total_return": finite(points[-1]["equity"] / points[0]["equity"] - 1),
            "max_drawdown": worst_drawdown,
            "current_drawdown": points[-1]["drawdown"],
            "fills_count": trades["total"] if trades["available"] else None,
            **analysis.pop("metrics"),
        },
        "points": points,
        **analysis,
    }
    # Do not cache a mixture if a report was rewritten while it was being read.
    if signature == _signature(dependencies):
        _REPORT_CACHE.put(cache_key, signature, payload)
    return payload


def load_trades(reports_dir: Path, run_id: str, page: int = 1, page_size: int = 25) -> dict[str, Any]:
    """Paginate execution rows, preserving identifiers and CSV values as text."""
    if (isinstance(page, bool) or not isinstance(page, int) or not 1 <= page <= _MAX_TRADE_ROWS
            or isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 100):
        raise ValueError("invalid trade pagination")
    run = _report_run(reports_dir, run_id)
    path = run / "trades.csv"
    signature = _signature([path])
    cache_key = ("trades", str(run.resolve()), page, page_size)
    cached = _REPORT_CACHE.get(cache_key, signature)
    if cached is not None:
        return cached
    payload = {
        "id": run_id, "source": "trades.csv", "available": _safe_file(path),
        "page": page, "page_size": page_size, "total": 0, "pages": 0,
        "columns": [], "rows": [], "invalid_rows": 0, "truncated_cells": 0,
    }
    if payload["available"]:
        reader = _csv_reader(path)
        payload["columns"] = reader.fieldnames or []
        start = (page - 1) * page_size
        for row in _csv_rows(reader, _MAX_TRADE_ROWS):
            if None in row or any(value is None for value in row.values()):
                payload["invalid_rows"] += 1
                continue
            if start <= payload["total"] < start + page_size:
                projected = {}
                for name, value in row.items():
                    if len(value) > _MAX_CELL_LENGTH:
                        projected[name] = value[:_MAX_CELL_LENGTH] + "…"
                        payload["truncated_cells"] += 1
                    else:
                        projected[name] = value if value else None
                payload["rows"].append(projected)
            payload["total"] += 1
        payload["pages"] = math.ceil(payload["total"] / page_size)
    if signature == _signature([path]):
        _REPORT_CACHE.put(cache_key, signature, payload)
    return payload
