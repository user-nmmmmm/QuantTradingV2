"""Freeze official, venue-explicit public candles for the registered review.

Only GET requests to the named public endpoints are used. No credentials,
alternate exchanges, synthetic bars, or implicit quote/market substitutions.
Raw responses and errors are immutable; every output records the protocol hash.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import time
from urllib.parse import urlencode

import pandas as pd
import requests


ROOT = Path(__file__).resolve().parents[1]
VENUES = ("binance", "okx")
SYMBOLS = ("BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "ADA/USDT", "LTC/USDT")
TIMEFRAMES = {"1d": 86_400_000, "4h": 14_400_000}
ENDPOINTS = {
    "binance": "https://api.binance.com/api/v3/klines",
    "okx": "https://www.okx.com/api/v5/market/history-candles",
}
DOCUMENTATION = {
    "binance": "https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints",
    "okx": "https://www.okx.com/docs-v5/en/#rest-api-market-data-get-candlesticks-history",
}


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            + "\n").encode("utf-8")


def immutable_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != payload:
            raise ValueError(f"Refusing to replace immutable evidence: {path}")
        return
    with path.open("xb") as stream:
        stream.write(payload)


def utc_ms(value) -> int:
    timestamp = pd.Timestamp(value)
    timestamp = timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")
    return int(timestamp.value // 1_000_000)


def normalize_timestamp_ms(value) -> int:
    """Binance archive dates may use microseconds; REST usually uses ms."""
    number = int(value)
    if number >= 100_000_000_000_000:
        number //= 1000
    if not utc_ms("2000-01-01") <= number < utc_ms("2100-01-01"):
        raise ValueError("Unsupported candle timestamp unit or range")
    return number


def year_windows(start_ms: int, end_ms: int):
    cursor = start_ms
    while cursor < end_ms:
        year = pd.Timestamp(cursor, unit="ms", tz="UTC").year
        following = utc_ms(f"{year + 1}-01-01")
        stop = min(end_ms, following)
        yield cursor, stop
        cursor = stop


class PublicSourceError(RuntimeError):
    pass


class PublicClient:
    """Save request facts before returning a body to the candle parser."""
    def __init__(self, root: Path, timeout=20.0):
        self.root, self.timeout = root, timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "QuantTradingV1-registered-review/1"})

    def get(self, venue: str, parameters: dict):
        url = ENDPOINTS[venue] + "?" + urlencode(sorted(parameters.items()))
        request_id = digest(url.encode("utf-8"))
        for attempt in range(1, 4):
            base = self.root / "raw" / venue / f"{request_id}.{attempt}"
            metadata_path, body_path = base.with_suffix(f".{attempt}.json"), base.with_suffix(f".{attempt}.body")
            if metadata_path.exists():
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                if metadata["url"] != url:
                    raise ValueError("Raw request identity mismatch")
                raw = body_path.read_bytes() if body_path.exists() else b""
                if digest(raw) != metadata["body_sha256"]:
                    raise ValueError("Raw response hash mismatch")
            else:
                metadata = {
                    "url": url, "venue": venue, "method": "GET", "attempt": attempt,
                    "accessed_at": datetime.now(timezone.utc).isoformat(),
                    "http_status": None, "error": None,
                }
                raw = b""
                try:
                    response = self.session.get(url, timeout=self.timeout, allow_redirects=False)
                    raw = response.content
                    metadata["http_status"] = response.status_code
                    if response.status_code != 200:
                        metadata["error"] = f"HTTP {response.status_code}"
                except requests.RequestException as exc:
                    metadata["error"] = f"{type(exc).__name__}: {exc}"
                metadata["body_sha256"] = digest(raw)
                immutable_write(body_path, raw)
                immutable_write(metadata_path, json_bytes(metadata))
                time.sleep(0.15)
            record = {
                **metadata, "metadata_path": str(metadata_path.relative_to(self.root)),
                "body_path": str(body_path.relative_to(self.root)),
            }
            if metadata["error"] is None:
                try:
                    return json.loads(raw), record
                except (ValueError, UnicodeDecodeError) as exc:
                    raise PublicSourceError(f"Non-JSON official response: {record}") from exc
            if metadata["http_status"] in {400, 401, 403, 404, 451}:
                break
            if attempt < 3:
                time.sleep(attempt)
        raise PublicSourceError(f"Official source unavailable: {record}")


def parse_candle(venue: str, row, timeframe: str, start_ms: int, end_ms: int, asof_ms: int):
    minimum_fields = 7 if venue == "binance" else 9
    if len(row) < minimum_fields:
        raise ValueError("Incomplete candle row")
    timestamp = normalize_timestamp_ms(row[0])
    duration = TIMEFRAMES[timeframe]
    if timestamp % duration:
        raise ValueError("Candle is not aligned to the registered UTC boundary")
    if not start_ms <= timestamp < end_ms:
        return None
    if timestamp + duration > asof_ms:
        return None
    if venue == "okx" and str(row[8]) != "1":
        return None
    if venue == "binance" and normalize_timestamp_ms(row[6]) != timestamp + duration - 1:
        raise ValueError("Binance close time does not describe a complete interval")
    values = [float(value) for value in row[1:6]]
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Non-finite candle price or volume")
    opening, high, low, close, volume = values
    if min(opening, high, low, close) <= 0 or volume < 0:
        raise ValueError("Invalid candle price or volume")
    if low > min(opening, close) or high < max(opening, close):
        raise ValueError("Inconsistent OHLC envelope")
    return timestamp, opening, high, low, close, volume


def gap_intervals(timestamps, start_ms: int, end_ms: int, duration: int):
    missing = sorted(set(range(start_ms, end_ms, duration)) - set(timestamps))
    groups = []
    for timestamp in missing:
        if groups and timestamp == groups[-1][1]:
            groups[-1][1] += duration
            groups[-1][2] += 1
        else:
            groups.append([timestamp, timestamp + duration, 1])
    return [{"start": pd.Timestamp(a, unit="ms", tz="UTC").isoformat(),
             "end_exclusive": pd.Timestamp(b, unit="ms", tz="UTC").isoformat(),
             "missing_bars": n} for a, b, n in groups]


def fetch_stream(client, venue, symbol, timeframe, start_ms, end_ms, asof_ms):
    candles, request_records, failures = {}, [], []
    for lower, upper in year_windows(start_ms, end_ms):
        cursor = lower if venue == "binance" else upper
        page_count = 0
        try:
            while (cursor < upper if venue == "binance" else cursor > lower):
                if venue == "binance":
                    parameters = {"symbol": symbol.replace("/", ""), "interval": timeframe,
                                  "startTime": cursor, "endTime": upper - 1, "limit": 1000,
                                  "timeZone": "0"}
                else:
                    parameters = {"instId": symbol.replace("/", "-"),
                                  "bar": "1Dutc" if timeframe == "1d" else "4H",
                                  "after": cursor, "before": lower - 1, "limit": 300}
                payload, record = client.get(venue, parameters)
                request_records.append(record)
                if venue == "binance":
                    rows = payload
                else:
                    if payload.get("code") != "0":
                        raise PublicSourceError(f"OKX API error: {payload}")
                    rows = payload.get("data")
                if not isinstance(rows, list):
                    raise PublicSourceError(f"Unexpected {venue} candle payload")
                if not rows:
                    break
                page_count += 1
                if page_count > 100:
                    raise PublicSourceError("Pagination exceeded registered annual bound")
                raw_times = [normalize_timestamp_ms(row[0]) for row in rows]
                for row in rows:
                    try:
                        candle = parse_candle(venue, row, timeframe, lower, upper, asof_ms)
                    except (ValueError, TypeError, IndexError) as exc:
                        failures.append({
                            "start_ms": lower, "end_exclusive_ms": upper,
                            "rejected_candle_timestamp": row[0] if row else None,
                            "request_body_sha256": record.get("body_sha256"),
                            "reason": f"{type(exc).__name__}: {exc}",
                        })
                        continue
                    if candle is not None:
                        old = candles.get(candle[0])
                        if old is not None and old != candle:
                            raise PublicSourceError("Conflicting duplicate candle")
                        candles[candle[0]] = candle
                following = max(raw_times) + TIMEFRAMES[timeframe] if venue == "binance" else min(raw_times)
                if (following <= cursor if venue == "binance" else following >= cursor):
                    raise PublicSourceError("Official API did not advance the pagination cursor")
                cursor = following
        except (PublicSourceError, ValueError, TypeError, KeyError, IndexError) as exc:
            failures.append({"start_ms": lower, "end_exclusive_ms": upper,
                             "reason": f"{type(exc).__name__}: {exc}"})
    frame = pd.DataFrame(sorted(candles.values()), columns=["timestamp", "open", "high", "low", "close", "volume"])
    if not frame.empty:
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], unit="ms", utc=True)
    return frame, request_records, failures


def collect_one(root, raw_root, protocol, identity, venue, symbol, timeframe):
    name = f"{venue}_{symbol.replace('/', '')}_{timeframe}"
    manifest_path = root / f"{name}.manifest.json"
    if manifest_path.exists():
        saved = json.loads(manifest_path.read_text(encoding="utf-8"))
        if saved["identity"] != identity:
            raise ValueError("Collector or protocol changed; use a new evidence directory")
        if digest((root / saved["csv_path"]).read_bytes()) != saved["csv_sha256"]:
            raise ValueError("Frozen candle CSV checksum mismatch")
        return saved
    spec = protocol["public_data"]
    start, end = utc_ms(spec["warmup_start"]), utc_ms(spec["recent_end_exclusive"])
    asof = min(end, utc_ms(protocol["registered_at"]))
    frame, requests_log, failures = fetch_stream(
        PublicClient(raw_root), venue, symbol, timeframe, start, end, asof,
    )
    content = frame.to_csv(index=False, lineterminator="\n").encode("utf-8")
    path = root / f"{name}.csv"
    immutable_write(path, content)
    actual = [utc_ms(value) for value in frame["timestamp"]]
    gaps = gap_intervals(actual, start, end, TIMEFRAMES[timeframe])
    historical_start, historical_end = utc_ms(spec["start"]), utc_ms(spec["end"]) + TIMEFRAMES["1d"]
    segments = {
        "warmup": (start, historical_start),
        "historical": (historical_start, historical_end),
        "recent_drift": (historical_end, end),
    }
    result = {
        "schema": "strategy_review_public_stream/v1", "identity": identity,
        "venue": venue, "market_type": "spot", "symbol": symbol, "timeframe": timeframe,
        "endpoint": ENDPOINTS[venue], "documentation": DOCUMENTATION[venue],
        "raw_evidence_directory": str(raw_root.resolve()),
        "status": "complete" if not gaps and not failures else "evidence_incomplete",
        "archive_checksum": "not_applicable_public_rest", "substitution": None,
        "timestamp_unit": "milliseconds_normalized", "timezone": "UTC",
        "csv_path": path.name, "csv_sha256": digest(content), "rows": len(frame),
        "missing_bars": sum(item["missing_bars"] for item in gaps), "gaps": gaps,
        "segments": {key: {"expected": (b-a)//TIMEFRAMES[timeframe],
                           "observed": sum(a <= stamp < b for stamp in actual)}
                     for key, (a, b) in segments.items()},
        "requests": requests_log, "failures": failures,
    }
    immutable_write(manifest_path, json_bytes(result))
    print(json.dumps({"stream": name, "status": result["status"], "rows": len(frame),
                      "missing_bars": result["missing_bars"], "failures": len(failures)}), flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, default=ROOT / "reports/strategy_review_20260919")
    parser.add_argument("--output-subdir", default="public_data_validated")
    args = parser.parse_args()
    raw_protocol = (args.batch / "review_protocol.json").read_bytes()
    protocol = json.loads(raw_protocol)
    spec = protocol["public_data"]
    if tuple(spec["venues"]) != VENUES or tuple(spec["symbols"]) != SYMBOLS or set(spec["timeframes"]) != set(TIMEFRAMES):
        raise ValueError("Public matrix differs from the fixed registered review")
    identity = {"protocol_sha256": digest(raw_protocol), "collector_sha256": digest(Path(__file__).read_bytes())}
    root = args.batch / args.output_subdir
    if not root.resolve().is_relative_to(args.batch.resolve()):
        raise ValueError("Output must remain inside the selected review batch")
    raw_root = args.batch / "public_data"
    tasks = [(venue, symbol, timeframe) for venue in VENUES for symbol in SYMBOLS for timeframe in TIMEFRAMES]
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(collect_one, root, raw_root, protocol, identity, *task) for task in tasks]
        results = [future.result() for future in futures]
    manifest = {
        "schema": "strategy_review_public_manifest/v1", "identity": identity,
        "matrix_streams": len(tasks), "complete_streams": sum(r["status"] == "complete" for r in results),
        "rows": sum(r["rows"] for r in results), "missing_bars": sum(r["missing_bars"] for r in results),
        "retrospective_only": True, "missing_values_filled": False,
        "venue_substitution_allowed": False, "streams": results,
    }
    immutable_write(root / "manifest.json", json_bytes(manifest))
    print(json.dumps({k: v for k, v in manifest.items() if k != "streams"}), flush=True)


if __name__ == "__main__":
    main()
