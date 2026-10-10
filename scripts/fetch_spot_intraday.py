"""Resumable anonymous Binance spot 1h history, with truthful import availability.

Historical event time is not historical knowledge. Every imported candle keeps
the real current response receipt and availability times, and is retrospective
only. Immutable raw pages and their hashes make gaps and resume behavior auditable.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import math
from pathlib import Path
import time

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]

from core.quote_observations import _PublicBookFetcher, quote_error_details, utc_now
from core.request_budget import request_scope

SYMBOLS = ("BTCUSDT", "ETHUSDT")
HOUR_MS = 3_600_000
SOURCE = "https://api.binance.com/api/v3/klines"


def sha256(content):
    return hashlib.sha256(content).hexdigest()


def save_json(path, value, *, exclusive=False):
    with path.open("x" if exclusive else "w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)


def timestamp_ms(value):
    stamp = pd.Timestamp(value)
    if pd.isna(stamp) or stamp.tzinfo is None or stamp != stamp.floor("h"):
        raise ValueError("hour-aligned timezone-aware UTC boundaries required")
    return int(stamp.tz_convert("UTC").timestamp()*1000)


def retry_after_seconds(value, now=None):
    """Parse Retry-After; a wait above the bounded budget stops for later resume."""
    if value is None:
        return None
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        stamp = parsedate_to_datetime(str(value))
        if stamp.tzinfo is None:
            raise ValueError("Retry-After date must be aware")
        seconds = (stamp-(now or utc_now())).total_seconds()
    if not math.isfinite(seconds):
        raise ValueError("invalid Retry-After")
    return max(0., seconds)


def validate_page(page, *, cursor, end_ms):
    if not isinstance(page, list) or len(page) > 1000:
        raise ValueError("invalid kline response shape")
    previous = cursor-HOUR_MS
    for row in page:
        if not isinstance(row, list) or len(row) < 11:
            raise ValueError("invalid kline row")
        opened, closed = int(row[0]), int(row[6])
        # Venue maintenance can close a historical candle before its usual
        # hour boundary. Preserve that real close time and flag it downstream.
        if opened < cursor or opened >= end_ms or opened % HOUR_MS or opened <= previous or not opened <= closed < opened+HOUR_MS:
            raise ValueError("invalid, duplicate or out-of-range candle time")
        prices = list(map(float, row[1:5]))
        volumes = [float(row[i]) for i in (5, 7, 9, 10)]
        if (not all(math.isfinite(p) and p > 0 for p in prices)
                or not all(math.isfinite(v) and v >= 0 for v in volumes)
                or prices[1] < max(prices[0], prices[3], prices[2])
                or prices[2] > min(prices[0], prices[3], prices[1])):
            raise ValueError("invalid OHLCV values")
        previous = opened
    return page


class BinanceSpotKlineClient:
    """Same configured anonymous Binance spot host as BBO; no market preload."""
    def __init__(self, timeout_seconds):
        self.public = _PublicBookFetcher("binance", "spot", "live", timeout_seconds)
        self.client = self.public.client
        self.status, self.retry_after = None, None
        def receipt(response, *args, **kwargs):
            self.status = response.status_code
            self.retry_after = response.headers.get("Retry-After")
            return response
        self.client.session.hooks["response"].append(receipt)

    def fetch(self, params, timeout_seconds):
        self.status, self.retry_after = None, None
        with request_scope(deadline=time.monotonic()+timeout_seconds, priority="research"):
            page = self.client.publicGetKlines(params)
        return page

    def close(self):
        self.public.close()


def download_intraday(output, *, start="2020-01-01T00:00:00Z", end_exclusive="2026-09-20T00:00:00Z",
                      symbols=SYMBOLS, timeout_seconds=5., resume=False, client=None,
                      max_retries=2, retry_wait_limit_seconds=10., max_pages=150):
    output = Path(output)
    start_ms, end_ms = timestamp_ms(start), timestamp_ms(end_exclusive)
    if start_ms >= end_ms or end_ms > int(utc_now().timestamp()*1000):
        raise ValueError("completed historical range required")
    if not symbols or len(set(symbols)) != len(symbols) or any(s not in SYMBOLS for s in symbols):
        raise ValueError("only the registered BTCUSDT/ETHUSDT spot symbols are allowed")
    if not 0 < timeout_seconds <= 10 or not 0 <= max_retries <= 3 or not 0 <= retry_wait_limit_seconds <= 30 or max_pages < 1:
        raise ValueError("bounded timeout, retries, retry wait and page count required")
    parameters = {"exchange_id": "binance", "environment": "live", "market_type": "spot", "interval": "1h",
        "symbols": list(symbols), "start_ms": start_ms, "end_exclusive_ms": end_ms, "limit": 1000,
        "source": SOURCE, "timeout_seconds": timeout_seconds, "max_retries": max_retries,
        "retry_wait_limit_seconds": retry_wait_limit_seconds, "max_pages": max_pages,
        "retrospective_only": True, "historical_pit_available_at": None,
        "availability_policy": "actual current response receipt and validation; never backfilled to candle close",
        "short_candle_policy": "retain original venue close time and flag; never fill missing hours",
        "public_read_only": True, "credentials_used": False, "orders_submitted": 0}
    source_hashes = {str(p.relative_to(ROOT)): sha256(p.read_bytes()) for p in
        (Path(__file__).resolve(), ROOT/"core/quote_observations.py")}
    registration = output/"registration.json"
    if resume:
        prior = json.loads(registration.read_text(encoding="utf-8"))
        if prior["parameters"] != parameters or prior["source_hashes"] != source_hashes:
            raise ValueError("resume requires unchanged download protocol and source identity")
    else:
        output.mkdir(parents=True, exist_ok=False)
        save_json(registration, {"schema": "spot-intraday-import/v1", "registered_at": utc_now().isoformat(),
            "parameters": parameters, "source_hashes": source_hashes,
            "official_docs": "https://developers.binance.com/en/docs/catalog/core-trading-spot-trading/api/rest-api/market"}, exclusive=True)
    owned = client is None
    client = client or BinanceSpotKlineClient(timeout_seconds)
    manifest = {"schema": "spot-intraday-import-result/v1", "parameters": parameters,
        "registration_sha256": sha256(registration.read_bytes()), "started_at": utc_now().isoformat(),
        "resumed": resume, "status": "started", "symbols": {}, "request_count": 0, "resumed_pages": 0}
    expected_rows = (end_ms-start_ms)//HOUR_MS
    session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    pages_processed = 0
    try:
        for symbol in symbols:
            folder = output/"raw"/symbol
            folder.mkdir(parents=True, exist_ok=True)
            cursor, candles, pages = start_ms, [], []
            while cursor < end_ms:
                if pages_processed >= max_pages:
                    raise RuntimeError("registered page budget exhausted")
                params = {"symbol": symbol, "interval": "1h", "startTime": cursor, "endTime": end_ms-1, "limit": 1000}
                raw_path, meta_path = folder/f"{cursor}.json", folder/f"{cursor}.metadata.json"
                if raw_path.exists() or meta_path.exists():
                    if not (raw_path.exists() and meta_path.exists()):
                        raise ValueError("incomplete immutable page pair requires explicit review")
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    content = raw_path.read_bytes()
                    if meta["sha256"] != sha256(content) or meta["params"] != params:
                        raise ValueError("immutable page identity mismatch")
                    page = json.loads(content)
                    manifest["resumed_pages"] += 1
                else:
                    for attempt in range(max_retries+1):
                        try:
                            sent = utc_now().isoformat()
                            manifest["request_count"] += 1
                            page = client.fetch(params, timeout_seconds)
                            observed = utc_now().isoformat()
                            validate_page(page, cursor=cursor, end_ms=end_ms)
                            if not page:
                                raise ValueError("empty historical page; range coverage remains incomplete")
                            content = json.dumps(page, separators=(",", ":"), allow_nan=False).encode("utf-8")
                            meta = {"source": SOURCE, "params": params, "sent_at": sent,
                                "observed_at": observed, "available_at": utc_now().isoformat(),
                                "historical_pit_available_at": None, "retrospective_only": True,
                                "serialization": "canonical JSON of unmodified decoded public response",
                                "sha256": sha256(content), "rows": len(page), "request_attempt": attempt+1}
                            # Raw pages are immutable; interrupted half-pairs are
                            # rejected on resume instead of silently repaired.
                            with raw_path.open("xb") as stream:
                                stream.write(content)
                            save_json(meta_path, meta, exclusive=True)
                            break
                        except Exception as exc:
                            status = getattr(client, "status", None)
                            if status != 429 or attempt >= max_retries:
                                raise
                            wait = retry_after_seconds(getattr(client, "retry_after", None))
                            wait = wait if wait is not None else float(attempt+1)
                            if wait > retry_wait_limit_seconds:
                                raise RuntimeError("rate-limit Retry-After exceeds bounded wait; resume later") from exc
                            time.sleep(wait)
                validate_page(page, cursor=cursor, end_ms=end_ms)
                page_id = f"{symbol}:{cursor}:{meta['sha256']}"
                for row in page:
                    candles.append({"timestamp": pd.Timestamp(int(row[0]), unit="ms", tz="UTC").isoformat(),
                        **dict(zip(("open", "high", "low", "close", "volume"), map(float, row[1:6]))),
                        "close_time": pd.Timestamp(int(row[6]), unit="ms", tz="UTC").isoformat(),
                        "bar_duration_ms": int(row[6])-int(row[0])+1,
                        "is_full_hour": int(row[6]) == int(row[0])+HOUR_MS-1,
                        "is_complete_bar": int(row[6]) == int(row[0])+HOUR_MS-1,
                        "quote_volume": float(row[7]), "trade_count": int(row[8]),
                        "taker_buy_base_volume": float(row[9]), "taker_buy_quote_volume": float(row[10]),
                        "observed_at": meta["observed_at"], "available_at": meta["available_at"],
                        "historical_pit_available_at": None, "source_page_id": page_id})
                pages.append({"path": str(raw_path.relative_to(output)), "metadata_path": str(meta_path.relative_to(output)),
                              "sha256": meta["sha256"], "rows": len(page)})
                cursor = int(page[-1][0])+HOUR_MS
                pages_processed += 1
                save_json(output/"checkpoint.json", {"session_id": session_id, "symbol": symbol, "next_start_ms": cursor,
                    "last_page_sha256": meta["sha256"], "completed_at": utc_now().isoformat()})
            frame = pd.DataFrame(candles)
            expected = pd.date_range(pd.Timestamp(start_ms, unit="ms", tz="UTC"),
                                     pd.Timestamp(end_ms-HOUR_MS, unit="ms", tz="UTC"), freq="h")
            actual = pd.DatetimeIndex(pd.to_datetime(frame["timestamp"], utc=True))
            missing = expected.difference(actual)
            csv_path = output/f"binance_{symbol}_spot_1h.csv"
            csv_content = frame.to_csv(index=False).encode("utf-8")
            if csv_path.exists():
                if csv_path.read_bytes() != csv_content:
                    raise ValueError("completed CSV identity changed")
            else:
                with csv_path.open("xb") as stream:
                    stream.write(csv_content)
            manifest["symbols"][symbol] = {"rows": len(frame), "expected_rows": expected_rows,
                "missing_hours": [s.isoformat() for s in missing], "duplicate_hours": int(actual.duplicated().sum()),
                "first_open": actual.min().isoformat(), "last_open": actual.max().isoformat(),
                "csv_path": str(csv_path.resolve()), "csv_sha256": sha256(csv_content), "pages": pages,
                "first_observed_at": frame.observed_at.min(), "last_available_at": frame.available_at.max()}
            manifest["symbols"][symbol]["shortened_hours"] = frame.loc[~frame["is_full_hour"], "timestamp"].tolist()
        manifest["status"] = "completed" if all(not row["missing_hours"] and not row["duplicate_hours"]
            and row["rows"] == expected_rows for row in manifest["symbols"].values()) else "incomplete_coverage"
    except Exception as exc:
        manifest.update(status="incomplete", error=quote_error_details(exc),
                        http_status=getattr(client, "status", None), resume_supported=True)
    finally:
        if owned:
            client.close()
        manifest["completed_at"] = utc_now().isoformat()
        save_json(output/f"session_{session_id}.json", manifest, exclusive=True)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start", default="2020-01-01T00:00:00Z")
    parser.add_argument("--end-exclusive", default="2026-09-20T00:00:00Z")
    parser.add_argument("--timeout-seconds", type=float, default=5.)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    result = download_intraday(args.output, start=args.start, end_exclusive=args.end_exclusive,
                               timeout_seconds=args.timeout_seconds, resume=args.resume)
    print(json.dumps({"status": result["status"], "request_count": result["request_count"],
        "resumed_pages": result["resumed_pages"], "rows": {s: r["rows"] for s, r in result["symbols"].items()},
        "error": result.get("error"), "output": str(args.output.resolve())}))


if __name__ == "__main__":
    main()
