"""Bounded daily-cache inspection and dependency-free browser market analytics.

These descriptive indicators do not change the engine's indicator conventions.
Every calculation uses chronological valid observations, without filling gaps.
"""

from __future__ import annotations

import csv
import io
import math
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from dashboard.report_analysis import ReportCache, finite, utc_moment
from dashboard.visual_data import _SYMBOL, _safe_file, _signature, list_markets


_MAX_BYTES = 16 * 1024 * 1024
_MAX_TOTAL_BYTES = 64 * 1024 * 1024
_MAX_ROWS = 20_000
_MAX_SYMBOLS = 32
_SAMPLE_SIZE = 20
_MARKET_CACHE = ReportCache(max_entries=8)
_FIELDS = {"timestamp", "open", "high", "low", "close", "volume"}
_INDICATORS = (
    "sma20", "sma50", "ema12", "ema26", "rsi14", "macd", "macd_signal",
    "macd_hist", "bb_mid", "bb_upper", "bb_lower", "atr14", "adx14",
    "stoch_k", "stoch_d", "obv", "roc12", "cci20", "williams_r14",
)

METHODOLOGY = {
    "observations": "First valid OHLCV row per UTC timestamp, sorted chronologically. Gaps are not filled; indicator periods count observations, not elapsed days. Naive timestamps are UTC. Indicators use all bounded history before the displayed limit.",
    "sma": "Arithmetic mean of the last 20 or 50 closes; null before a full window.",
    "ema": "EMA12/26: first full-window SMA seed; then alpha=2/(n+1), EMA[t]=alpha*close[t]+(1-alpha)*EMA[t-1].",
    "rsi14": "Wilder averages of 14 close-to-close gains/losses, seeded by their SMA. RSI=100*gain/(gain+loss); flat=50, only gains=100, only losses=0. First value needs 15 closes.",
    "macd": "MACD=EMA12-EMA26; signal=SMA-seeded EMA9 of available MACD; histogram=MACD-signal. First MACD at observation 26, signal/histogram at 34.",
    "bollinger": "20-close SMA +/- 2 population standard deviations (ddof=0); bb_mid is SMA20.",
    "atr14": "TR=max(high-low, abs(high-previous_close), abs(low-previous_close)); first TR=high-low. ATR14 seeds from the first 14 TRs, then Wilder alpha=1/14.",
    "adx14": "Strict directional movement: +DM=up if up>down and up>0, otherwise 0; -DM analogously. Wilder averages of 14 transitions; DI=100*smoothedDM/smoothedTR, DX=100*abs(+DI--DI)/(+DI+-DI). ADX is SMA-seeded Wilder14 of DX, first value at observation 28; zero movement gives 0.",
    "stochastic": "Fast %K=100*(close-lowest_low14)/(highest_high14-lowest_low14); %D=SMA3(%K). Zero range is null.",
    "obv": "OBV starts at 0; add current volume when close rises, subtract it when close falls, unchanged on equal close. Overflow is null thereafter.",
    "roc12": "100*(close[t]/close[t-12]-1), in percent; needs 13 observations.",
    "cci20": "Typical price=(high+low+close)/3; CCI=(typical-SMA20(typical))/(0.015*mean absolute deviation over 20 typical prices); zero deviation is null.",
    "williams_r14": "-100*(highest_high14-close)/(highest_high14-lowest_low14); zero range is null.",
    "undefined": "Warmup, zero denominators unless specified, and nonfinite results are JSON null.",
}

QUALITY_METHODOLOGY = {
    "daily": "Expected bars are one per UTC calendar day at 00:00. Dates and requested endpoints are inclusive. No missing rows are synthesized.",
    "coverage": "start/end bound valid daily observations. missing_days counts absent valid daily dates between those bounds (or in a requested selection). Missing file, empty file and malformed file are distinct.",
    "common_range": "Intersection of observed start/end bounds; complete indicates that each selected market has a valid daily observation on every date inside it.",
    "common_usable_range": "Longest contiguous run of valid dates shared by all selected markets; ties use the most recent run. Excludes dates affected by duplicates, out-of-order rows or invalid OHLCV.",
    "quality_counts": "Duplicate timestamps are normalized to UTC; out_of_order counts decreases from the preceding parseable timestamp. Categories may overlap. Malformed timestamps cannot be assigned to a date and block strict selection validation.",
    "limits": f"At most {_MAX_SYMBOLS} symbols, {_MAX_ROWS} rows and {_MAX_BYTES} bytes per CSV, {_MAX_TOTAL_BYTES} combined bytes per quality request; samples contain at most {_SAMPLE_SIZE} dates.",
}


def _mean(values: list[float]) -> float:
    # Normalize first: even sum(value/n) may overflow from rounding near DBL_MAX.
    scale = max(abs(value) for value in values)
    return scale * (math.fsum(value / scale for value in values) / len(values)) if scale else 0.0


def _smooth(values: list[float | None], period: int, alpha: float) -> list[float | None]:
    result: list[float | None] = []
    seed: list[float] = []
    previous = None
    for value in values:
        if value is None:
            seed = []
            previous = None
        elif previous is None:
            seed.append(value)
            if len(seed) == period:
                previous = finite(_mean(seed))
        else:
            previous = (previous if value == previous else
                        finite(alpha * value + (1 - alpha) * previous))
        result.append(previous)
    return result


def _rolling_mean(values: list[float | None], period: int) -> list[float | None]:
    return [finite(_mean(window)) if len(window) == period and None not in window else None
            for index in range(len(values))
            for window in [values[max(0, index - period + 1):index + 1]]]


def _indicators(candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    closes = [row["close"] for row in candles]
    ema12 = _smooth(closes, 12, 2 / 13)
    ema26 = _smooth(closes, 26, 2 / 27)
    macd = [finite(short - long) if short is not None and long is not None else None
            for short, long in zip(ema12, ema26)]
    signal = _smooth(macd, 9, 2 / 10)
    tr, gains, losses, plus, minus = [], [None], [None], [None], [None]
    typical, stochastic, williams = [], [], []
    obv = 0.0
    points = []
    for index, candle in enumerate(candles):
        high, low, close = candle["high"], candle["low"], candle["close"]
        typical.append(_mean([high, low, close]))
        if index:
            previous = candles[index - 1]
            change = close - previous["close"]
            gains.append(max(change, 0))
            losses.append(max(-change, 0))
            up, down = high - previous["high"], previous["low"] - low
            plus.append(up if up > down and up > 0 else 0.0)
            minus.append(down if down > up and down > 0 else 0.0)
            tr.append(max(high - low, abs(high - previous["close"]), abs(low - previous["close"])))
            if obv is not None:
                obv = finite(obv + (candle["volume"] if change > 0 else -candle["volume"] if change < 0 else 0))
        else:
            tr.append(high - low)
        window = candles[max(0, index - 13):index + 1]
        bottom, top = min(row["low"] for row in window), max(row["high"] for row in window)
        spread = top - bottom
        stochastic.append(100 * ((close - bottom) / spread) if len(window) == 14 and spread else None)
        williams.append(-100 * ((top - close) / spread) if len(window) == 14 and spread else None)
        points.append({**candle, "obv": obv})
    sma20, sma50 = _rolling_mean(closes, 20), _rolling_mean(closes, 50)
    avg_gain, avg_loss = _smooth(gains, 14, 1 / 14), _smooth(losses, 14, 1 / 14)
    atr = _smooth(tr, 14, 1 / 14)
    # DM and its denominator both use transitions, excluding the initial bar.
    plus_avg, minus_avg = _smooth(plus, 14, 1 / 14), _smooth(minus, 14, 1 / 14)
    dx = []
    for positive, negative in zip(plus_avg, minus_avg):
        if positive is None or negative is None:
            dx.append(None)
        elif not max(positive, negative):
            dx.append(0.0)
        else:
            scale = max(positive, negative)
            pos, neg = positive / scale, negative / scale
            # The identical smoothed TR denominator cancels in the DX ratio.
            dx.append(100 * abs(pos - neg) / (pos + neg))
    adx, stoch_d = _smooth(dx, 14, 1 / 14), _rolling_mean(stochastic, 3)
    for index, point in enumerate(points):
        gain, loss = avg_gain[index], avg_loss[index]
        rsi = None
        if gain is not None and loss is not None:
            scale = max(gain, loss)
            rsi = 50.0 if not scale else 100 * (gain / scale) / (gain / scale + loss / scale)
        deviation, cci = None, None
        if index >= 19:
            window = closes[index - 19:index + 1]
            scale = max(window)
            center = _mean([value / scale for value in window])
            deviation = scale * math.sqrt(_mean([(value / scale - center) ** 2 for value in window]))
            typical_window = typical[index - 19:index + 1]
            typical_mean = _mean(typical_window)
            mean_deviation = _mean([abs(value - typical_mean) for value in typical_window])
            denominator = .015 * mean_deviation
            if denominator:
                cci = finite((typical[index] - typical_mean) / denominator)
        point.update({
            "sma20": sma20[index], "sma50": sma50[index], "ema12": ema12[index],
            "ema26": ema26[index], "rsi14": rsi, "macd": macd[index],
            "macd_signal": signal[index],
            "macd_hist": finite(macd[index] - signal[index]) if signal[index] is not None else None,
            "bb_mid": sma20[index],
            "bb_upper": finite(sma20[index] + 2 * deviation) if deviation is not None else None,
            "bb_lower": finite(sma20[index] - 2 * deviation) if deviation is not None else None,
            "atr14": atr[index], "adx14": adx[index], "stoch_k": stochastic[index],
            "stoch_d": stoch_d[index], "roc12": finite(100 * (closes[index] / closes[index - 12] - 1)) if index >= 12 else None,
            "cci20": cci, "williams_r14": williams[index],
        })
    return points


def _symbol_path(data_dir: Path, symbol: str) -> Path:
    if not isinstance(symbol, str) or len(symbol) > 40 or not _SYMBOL.fullmatch(symbol):
        raise ValueError("invalid market symbol")
    return Path(data_dir) / (symbol.replace("/", "_") + ".csv")


def _read_market(data_dir: Path, symbol: str) -> dict[str, Any]:
    path = _symbol_path(data_dir, symbol)
    signature = _signature([path])
    key = ("market_raw", str(path.resolve()))
    cached = _MARKET_CACHE.get(key, signature)
    if cached is not None:
        return cached
    quality = {
        "symbol": symbol, "available": _safe_file(path), "error": None,
        "rows": 0, "valid_rows": 0, "daily_rows": 0, "invalid_rows": 0, "invalid_timestamps": 0,
        "invalid_ohlcv": 0, "duplicate_timestamps": 0, "out_of_order": 0,
        "non_daily_rows": 0, "start": None, "end": None, "missing_days": 0,
        "missing_dates_sample": [], "cache_updated_at": None,
    }
    payload = {"quality": quality, "candles": [], "valid_dates": [], "problem_dates": []}
    if not quality["available"]:
        quality["error"] = "market cache not found"
        return payload
    quality["cache_updated_at"] = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
    try:
        if path.stat().st_size > _MAX_BYTES:
            raise ValueError("market cache exceeds byte limit")
        with path.open("rb") as handle:
            contents = handle.read(_MAX_BYTES + 1)
        if len(contents) > _MAX_BYTES:
            raise ValueError("market cache exceeds byte limit")
        reader = csv.DictReader(io.StringIO(contents.decode("utf-8-sig"), newline=""), strict=True)
        fields = reader.fieldnames or []
        if (not _FIELDS.issubset(fields) or len(fields) > 64 or len(set(fields)) != len(fields)
                or any(not field or len(field) > 128 for field in fields)):
            raise ValueError("invalid market CSV schema")
        seen, valid_dates, problems, accepted = set(), set(), set(), {}
        previous = None
        for index, row in enumerate(reader):
            if index >= _MAX_ROWS:
                raise ValueError("market cache exceeds row limit")
            quality["rows"] += 1
            try:
                moment = utc_moment(row.get("timestamp", ""))
            except (TypeError, ValueError, OverflowError):
                quality["invalid_timestamps"] += 1
                quality["invalid_rows"] += 1
                continue
            day = moment.date().isoformat()
            duplicate = moment in seen
            out_of_order = previous is not None and moment < previous
            daily = moment.hour == moment.minute == moment.second == moment.microsecond == 0
            quality["duplicate_timestamps"] += int(duplicate)
            quality["out_of_order"] += int(out_of_order)
            quality["non_daily_rows"] += int(not daily)
            seen.add(moment)
            previous = moment
            try:
                values = {field: float(row[field]) for field in _FIELDS - {"timestamp"}}
                valid = (None not in row and all(math.isfinite(value) for value in values.values())
                         and min(values[field] for field in ("open", "high", "low", "close")) > 0
                         and values["volume"] >= 0
                         and values["low"] <= min(values["open"], values["close"])
                         and values["high"] >= max(values["open"], values["close"]))
            except (TypeError, ValueError):
                valid = False
            if duplicate or out_of_order or not daily or not valid:
                problems.add(day)
            if not valid:
                quality["invalid_ohlcv"] += 1
                quality["invalid_rows"] += 1
                continue
            if moment not in accepted:
                accepted[moment] = {"timestamp": moment.isoformat(), **values}
                if daily:
                    valid_dates.add(day)
        payload["candles"] = [accepted[moment] for moment in sorted(accepted)]
        payload["valid_dates"] = sorted(valid_dates)
        payload["problem_dates"] = sorted(problems)
        quality["valid_rows"] = len(accepted)
        quality["daily_rows"] = len(valid_dates)
        if valid_dates:
            quality["start"], quality["end"] = min(valid_dates), max(valid_dates)
            quality["missing_days"], quality["missing_dates_sample"] = _missing(
                payload["valid_dates"], quality["start"], quality["end"])
    except (ValueError, UnicodeError, csv.Error) as error:
        quality["error"] = str(error)
        payload.update({"candles": [], "valid_dates": [], "problem_dates": []})
    if signature == _signature([path]):
        _MARKET_CACHE.put(key, signature, payload)
    return payload


def _missing(days: list[str], start: str, end: str) -> tuple[int, list[str]]:
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    observed = [date.fromisoformat(day) for day in days if start <= day <= end]
    count = (last - first).days + 1 - len(observed)
    sample = []
    cursor = first.toordinal()
    for current in [day.toordinal() for day in observed] + [last.toordinal() + 1]:
        for ordinal in range(cursor, min(current, cursor + _SAMPLE_SIZE - len(sample))):
            sample.append(date.fromordinal(ordinal).isoformat())
        cursor = current + 1
        if len(sample) >= _SAMPLE_SIZE:
            break
    return count, sample


def _date_argument(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    try:
        if not isinstance(value, str) or len(value) != 10:
            raise ValueError()
        parsed = date.fromisoformat(value).isoformat()
        if parsed != value:
            raise ValueError()
        return parsed
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO calendar date (YYYY-MM-DD)") from error


def _longest_run(days: set[str]) -> dict[str, Any] | None:
    longest = None
    start = previous = None
    length = 0
    for day in sorted(days):
        current = date.fromisoformat(day)
        if previous is None or (current - previous).days != 1:
            start, length = day, 0
        length += 1
        if longest is None or length >= longest["days"]:
            longest = {"start": start, "end": day, "days": length}
        previous = current
    return longest


def load_market_analysis(data_dir: Path, symbol: str, limit: int = 240) -> dict[str, Any]:
    """Return at most 240 points; compute warmup using the complete bounded CSV."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 240:
        raise ValueError("market analysis limit must be between 1 and 240")
    path = _symbol_path(data_dir, symbol)
    signature = _signature([path])
    key = ("market_analysis", str(path.resolve()), limit)
    cached = _MARKET_CACHE.get(key, signature)
    if cached is not None:
        # Age depends on wall time rather than the file signature.
        cached["age_days"] = _age(cached["last_candle"])
        return cached
    raw = _read_market(data_dir, symbol)
    quality = raw["quality"]
    if not quality["available"]:
        raise FileNotFoundError("market cache not found")
    if quality["error"]:
        raise ValueError(quality["error"])
    if not raw["candles"]:
        raise ValueError("no valid market candles")
    points = _indicators(raw["candles"])[-limit:]
    payload = {
        "symbol": symbol, "timeframe": "1d", "source": "Binance spot local cache",
        "mode": "historical_cache", "cache_updated_at": quality["cache_updated_at"],
        "last_candle": points[-1]["timestamp"], "age_days": _age(points[-1]["timestamp"]),
        "invalid_rows": quality["invalid_rows"], "quality": quality,
        "candles": raw["candles"][-limit:], "points": points,
        "latest": {key: points[-1][key] for key in _INDICATORS},
        "methodology": dict(METHODOLOGY),
    }
    if signature == _signature([path]):
        _MARKET_CACHE.put(key, signature, payload)
    return payload


def _age(timestamp: str) -> int:
    return max(0, (datetime.now(timezone.utc).date() - utc_moment(timestamp).date()).days)


def load_data_quality(data_dir: Path, symbols: list[str] | None = None,
                      start: str | None = None, end: str | None = None) -> dict[str, Any]:
    """Inspect daily coverage; requested-window defects never get silently repaired."""
    chosen = list_markets(Path(data_dir)) if symbols is None else symbols
    if (not isinstance(chosen, (list, tuple)) or len(chosen) > _MAX_SYMBOLS
            or any(not isinstance(symbol, str) for symbol in chosen)
            or len(set(chosen)) != len(chosen)):
        raise ValueError(f"select at most {_MAX_SYMBOLS} unique market symbols")
    paths = [_symbol_path(data_dir, symbol) for symbol in chosen]
    if sum(path.stat().st_size for path in paths if _safe_file(path)) > _MAX_TOTAL_BYTES:
        raise ValueError("market selection exceeds combined byte limit")
    start, end = _date_argument(start, "start"), _date_argument(end, "end")
    if start and end and start > end:
        raise ValueError("start must not be after end")
    markets = [_read_market(data_dir, symbol) for symbol in chosen]
    reports = [dict(market["quality"]) for market in markets]
    errors = []
    if not chosen:
        errors.append("No market symbols selected")
    ready = bool(markets) and all(market["valid_dates"] and not market["quality"]["error"] for market in markets)
    common_range, usable = None, None
    if ready:
        left = max(market["valid_dates"][0] for market in markets)
        right = min(market["valid_dates"][-1] for market in markets)
        common_days = set(markets[0]["valid_dates"])
        clean_days = common_days - set(markets[0]["problem_dates"])
        for market in markets[1:]:
            common_days.intersection_update(market["valid_dates"])
            clean_days.intersection_update(set(market["valid_dates"]) - set(market["problem_dates"]))
        usable = (_longest_run(clean_days)
                  if not any(market["quality"]["invalid_timestamps"] for market in markets) else None)
        if left <= right:
            missing, _ = _missing(sorted(common_days), left, right)
            common_range = {"start": left, "end": right, "complete": missing == 0,
                            "missing_days": missing, "observations": len(common_days)}
    selected_start = start or (common_range["start"] if common_range else None)
    selected_end = end or (common_range["end"] if common_range else None)
    reversed_selection = bool(selected_start and selected_end and selected_start > selected_end)
    if reversed_selection:
        errors.append("Requested date bounds do not overlap available daily coverage")
    for market, report in zip(markets, reports):
        symbol = report["symbol"]
        selection = {"start": selected_start, "end": selected_end, "rows": 0,
                     "missing_days": None, "missing_dates_sample": [], "problem_days": 0,
                     "problem_dates_sample": [], "valid": False}
        report["selection"] = selection
        if report["error"]:
            errors.append(f"{symbol}: {report['error']}")
        elif not market["valid_dates"]:
            errors.append(f"{symbol}: no valid daily candles")
        elif selected_start and selected_end and not reversed_selection:
            missing, sample = _missing(market["valid_dates"], selected_start, selected_end)
            problems = [day for day in market["problem_dates"] if selected_start <= day <= selected_end]
            selection.update({"rows": sum(selected_start <= day <= selected_end for day in market["valid_dates"]),
                              "missing_days": missing, "missing_dates_sample": sample,
                              "problem_days": len(problems), "problem_dates_sample": problems[:_SAMPLE_SIZE]})
            if selected_start < report["start"] or selected_end > report["end"]:
                errors.append(f"{symbol}: requested dates exceed valid daily coverage")
            if missing:
                errors.append(f"{symbol}: {missing} missing daily candles in selection")
            if problems:
                errors.append(f"{symbol}: {len(problems)} dates with invalid, duplicate, non-daily or out-of-order rows in selection")
            if report["invalid_timestamps"]:
                errors.append(f"{symbol}: invalid timestamps cannot be assigned to the requested range")
            selection["valid"] = not missing and not problems and not report["invalid_timestamps"]
    if chosen and common_range is None:
        errors.append("Selected markets have no overlapping usable daily coverage")
    valid = bool(chosen) and not errors
    return {
        "timeframe": "1d", "symbols": reports, "common_range": common_range,
        "common_usable_range": usable,
        "selection": {"symbols": list(chosen), "start": selected_start, "end": selected_end,
                      "valid": valid, "errors": errors},
        "summary": {"markets": len(reports), "available": sum(item["available"] for item in reports),
                    "valid_rows": sum(item["valid_rows"] for item in reports),
                    "invalid_rows": sum(item["invalid_rows"] for item in reports),
                    "duplicate_timestamps": sum(item["duplicate_timestamps"] for item in reports),
                    "out_of_order": sum(item["out_of_order"] for item in reports),
                    "missing_days": sum(item["missing_days"] for item in reports),
                    "valid": valid},
        "methodology": dict(QUALITY_METHODOLOGY),
    }


def validate_data_selection(data_dir: Path, symbols: list[str], start: str,
                            end: str) -> dict[str, Any]:
    """Validate a real, contiguous daily selection before submitting a local job."""
    if not symbols or start is None or end is None:
        raise ValueError("symbols, start and end are required for local data selection")
    result = load_data_quality(data_dir, symbols, start, end)
    if not result["selection"]["valid"]:
        raise ValueError("; ".join(result["selection"]["errors"]))
    return result
