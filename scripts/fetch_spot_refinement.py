"""Fetch only ambiguous candidates' UTC days as retrospective spot 1m evidence.

This standalone successor does not alter the accepted hourly downloader or the
public observation client. It cannot submit orders or infer historical knowledge.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sys
import time

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.fetch_spot_intraday import BinanceSpotKlineClient, SOURCE, retry_after_seconds, save_json, sha256
from core.quote_observations import quote_error_details, utc_now

MINUTE_MS = 60_000
SYMBOL_IDS = {"BTC/USDT": "BTCUSDT", "ETH/USDT": "ETHUSDT"}


def refinement_days(document, *, maximum_candidates=23):
    """Treat label fields as data; select true ambiguity, then deduplicate days."""
    rows = document.get("outcomes")
    if not isinstance(rows, list):
        raise ValueError("barrier outcomes list required")
    candidates, targets = {}, {}
    for row in rows:
        if row.get("ambiguous") is not True:
            continue
        symbol = row.get("symbol", "").replace("-", "/")
        if symbol not in SYMBOL_IDS:
            raise ValueError("only registered BTC/ETH spot labels may request refinement")
        at = pd.Timestamp(row.get("exit_bar"))
        if pd.isna(at) or at.tzinfo is None:
            raise ValueError("explicit aware exit_bar required")
        day = at.tz_convert("UTC").normalize()
        if day+pd.Timedelta(days=1) > pd.Timestamp(utc_now()):
            raise ValueError("only completed historical UTC days may be imported")
        identity = row.get("candidate_id")
        if not isinstance(identity, str) or not identity:
            raise ValueError("candidate identity required")
        key = (symbol, day.isoformat())
        if identity in candidates and candidates[identity] != key:
            raise ValueError("conflicting candidate refinement identity")
        candidates[identity] = key
        targets.setdefault(key, set()).add(identity)
    if not candidates or len(candidates) > maximum_candidates:
        raise ValueError("refinement must contain 1 to 23 registered ambiguous candidates")
    return [{"symbol": symbol, "venue_symbol": SYMBOL_IDS[symbol], "utc_day": day,
             "start_ms": int(pd.Timestamp(day).timestamp()*1000),
             "end_exclusive_ms": int((pd.Timestamp(day)+pd.Timedelta(days=1)).timestamp()*1000),
             "candidate_ids": sorted(ids), "expected_minutes": 1440}
            for (symbol, day), ids in sorted(targets.items())]


def validate_minute_page(page, *, cursor, end_ms):
    if not isinstance(page, list) or not 0 < len(page) <= 1000:
        raise ValueError("nonempty public kline page of at most 1000 rows required")
    previous = cursor-MINUTE_MS
    for row in page:
        if not isinstance(row, list) or len(row) < 11:
            raise ValueError("invalid minute row")
        opened, closed = int(row[0]), int(row[6])
        if not cursor <= opened < end_ms or opened % MINUTE_MS or opened <= previous or not opened <= closed < opened+MINUTE_MS:
            raise ValueError("duplicate or out-of-range minute event time")
        o, h, l, c = map(float, row[1:5])
        values = [float(row[i]) for i in (5, 7, 9, 10)]
        if (not all(math.isfinite(p) and p > 0 for p in (o, h, l, c)) or
                not all(math.isfinite(v) and v >= 0 for v in values) or h < max(o, l, c) or l > min(o, h, c)):
            raise ValueError("invalid minute OHLCV")
        previous = opened
    return page


def download_refinement(labels_path, output, *, timeout_seconds=5., resume=False, client=None):
    labels_path, output = Path(labels_path), Path(output)
    content = labels_path.read_bytes()
    targets = refinement_days(json.loads(content))
    if not 0 < timeout_seconds <= 10:
        raise ValueError("timeout must be positive and bounded at 10 seconds")
    parameters = {"exchange_id": "binance", "environment": "live", "market_type": "spot", "timeframe": "1m",
        "source": SOURCE, "timeout_seconds": timeout_seconds, "max_retries": 2, "maximum_retry_wait_seconds": 10.,
        "max_pages": 2*len(targets), "targets": targets,
        "input_labels_sha256": sha256(content), "retrospective_only": True,
        "historical_pit_available_at": None, "availability_policy": "actual current response receipt; no historical PIT claim",
        "missing_or_short_minutes": "retain gaps and mark is_complete_bar=false; never synthesize",
        "public_read_only": True, "orders_submitted": 0, "credentials_used": False}
    sources = {str(path.relative_to(ROOT)): sha256(path.read_bytes()) for path in (
        Path(__file__).resolve(), ROOT/"scripts/fetch_spot_intraday.py", ROOT/"core/quote_observations.py")}
    registration_path = output/"registration.json"
    if resume:
        registered = json.loads(registration_path.read_text(encoding="utf-8"))
        if registered["parameters"] != parameters or registered["source_hashes"] != sources:
            raise ValueError("resume requires unchanged candidates, protocol and source")
    else:
        output.mkdir(parents=True, exist_ok=False)
        save_json(registration_path, {"schema": "targeted-minute-import/v1", "registered_at": utc_now().isoformat(),
            "input_labels": str(labels_path.resolve()), "parameters": parameters, "source_hashes": sources}, exclusive=True)
    owned, client = client is None, client or BinanceSpotKlineClient(timeout_seconds)
    session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    result = {"schema": "targeted-minute-import-result/v1", "started_at": utc_now().isoformat(),
        "parameters": parameters, "registration_sha256": sha256(registration_path.read_bytes()),
        "status": "started", "request_count": 0, "resumed_pages": 0, "days": [], "symbols": {}}
    by_symbol, page_count = {}, 0
    try:
        for target in targets:
            symbol, venue = target["symbol"], target["venue_symbol"]
            cursor, end_ms = target["start_ms"], target["end_exclusive_ms"]
            day_folder = output/"raw"/venue/target["utc_day"][:10]
            day_folder.mkdir(parents=True, exist_ok=True)
            candles, pages = [], []
            while cursor < end_ms:
                if page_count >= parameters["max_pages"]:
                    raise ValueError("registered targeted page budget exhausted")
                params = {"symbol": venue, "interval": "1m", "startTime": cursor, "endTime": end_ms-1, "limit": 1000}
                raw_path, meta_path = day_folder/f"{cursor}.json", day_folder/f"{cursor}.metadata.json"
                if raw_path.exists() or meta_path.exists():
                    if not (raw_path.exists() and meta_path.exists()):
                        raise ValueError("incomplete immutable page pair requires review")
                    raw = raw_path.read_bytes()
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    if meta["sha256"] != sha256(raw) or meta["params"] != params:
                        raise ValueError("immutable page identity changed")
                    page = json.loads(raw)
                    result["resumed_pages"] += 1
                else:
                    for attempt in range(3):
                        try:
                            sent = utc_now().isoformat()
                            result["request_count"] += 1
                            page = client.fetch(params, timeout_seconds)
                            observed = utc_now().isoformat()
                            validate_minute_page(page, cursor=cursor, end_ms=end_ms)
                            raw = json.dumps(page, separators=(",", ":"), allow_nan=False).encode("utf-8")
                            meta = {"source": SOURCE, "params": params, "sent_at": sent, "observed_at": observed,
                                "available_at": utc_now().isoformat(), "historical_pit_available_at": None,
                                "retrospective_only": True, "sha256": sha256(raw), "rows": len(page),
                                "serialization": "canonical JSON of unmodified decoded public response", "attempt": attempt+1}
                            with raw_path.open("xb") as stream:
                                stream.write(raw)
                            save_json(meta_path, meta, exclusive=True)
                            break
                        except Exception as exc:
                            if getattr(client, "status", None) != 429 or attempt == 2:
                                raise
                            wait = retry_after_seconds(getattr(client, "retry_after", None))
                            wait = float(attempt+1) if wait is None else wait
                            if wait > 10.:
                                raise RuntimeError("Retry-After exceeds bounded wait; resume later") from exc
                            time.sleep(wait)
                validate_minute_page(page, cursor=cursor, end_ms=end_ms)
                page_id = f"{venue}:{cursor}:{meta['sha256']}"
                for row in page:
                    opened, closed = int(row[0]), int(row[6])
                    candles.append({"timestamp": pd.Timestamp(opened, unit="ms", tz="UTC").isoformat(),
                        **dict(zip(("open", "high", "low", "close", "volume"), map(float, row[1:6]))),
                        "close_time": pd.Timestamp(closed, unit="ms", tz="UTC").isoformat(),
                        "bar_duration_ms": closed-opened+1, "is_complete_bar": closed == opened+MINUTE_MS-1,
                        "quote_volume": float(row[7]), "trade_count": int(row[8]),
                        "taker_buy_base_volume": float(row[9]), "taker_buy_quote_volume": float(row[10]),
                        "observed_at": meta["observed_at"], "available_at": meta["available_at"],
                        "historical_pit_available_at": None, "source_page_id": page_id})
                pages.append({"path": str(raw_path.relative_to(output)), "metadata_path": str(meta_path.relative_to(output)),
                              "sha256": meta["sha256"], "rows": len(page)})
                page_count += 1
                cursor = int(page[-1][0])+MINUTE_MS
                save_json(output/"checkpoint.json", {"session_id": session_id, "symbol": symbol,
                    "utc_day": target["utc_day"], "next_start_ms": cursor, "last_page_sha256": meta["sha256"]})
            frame = pd.DataFrame(candles)
            actual = pd.DatetimeIndex(pd.to_datetime(frame.timestamp, utc=True, format="ISO8601"))
            expected = pd.date_range(target["utc_day"], periods=1440, freq="min")
            gaps = expected.difference(actual)
            result["days"].append({**target, "rows": len(frame), "missing_minutes": [t.isoformat() for t in gaps],
                "incomplete_minutes": frame.loc[~frame.is_complete_bar, "timestamp"].tolist(),
                "duplicate_minutes": int(actual.duplicated().sum()), "pages": pages})
            by_symbol.setdefault(symbol, []).append(frame)
        for symbol, parts in by_symbol.items():
            frame = pd.concat(parts, ignore_index=True).sort_values("timestamp")
            csv_path = output/f"binance_{SYMBOL_IDS[symbol]}_spot_1m_targeted.csv"
            raw = frame.to_csv(index=False).encode("utf-8")
            if csv_path.exists():
                if csv_path.read_bytes() != raw:
                    raise ValueError("completed CSV changed")
            else:
                with csv_path.open("xb") as stream:
                    stream.write(raw)
            result["symbols"][symbol] = {"file": csv_path.name, "sha256": sha256(raw), "rows": len(frame),
                "utc_days": [r["utc_day"] for r in result["days"] if r["symbol"] == symbol],
                "first_observed_at": frame.observed_at.min(), "last_available_at": frame.available_at.max()}
        result["status"] = "completed" if all(not day["missing_minutes"] and not day["incomplete_minutes"]
            and not day["duplicate_minutes"] for day in result["days"]) else "incomplete_coverage"
    except Exception as exc:
        result.update(status="incomplete", error=quote_error_details(exc), http_status=getattr(client, "status", None))
    finally:
        if owned:
            client.close()
        result["completed_at"] = utc_now().isoformat()
        session_path = output/f"session_{session_id}.json"
        save_json(session_path, result, exclusive=True)
    if result["status"] in {"completed", "incomplete_coverage"}:
        ready = {"schema": "retrospective-fine-bars-ready/v1", "timeframe": "1m", "symbols": result["symbols"],
            "exchange_id": "binance", "environment": "live", "market_type": "spot", "retrospective_only": True,
            "historical_pit_available_at": None, "source_session": session_path.name,
            "source_session_sha256": sha256(session_path.read_bytes()), "registration_file": "registration.json",
            "registration_sha256": result["registration_sha256"], "retrieval_status": result["status"],
            "scope": "only still-ambiguous hourly candidates' complete UTC exit days; sparse across days"}
        ready_path = output/"ready_manifest.json"
        if not ready_path.exists():
            save_json(ready_path, ready, exclusive=True)
        result["ready_manifest"] = str(ready_path.resolve())
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=5.)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    result = download_refinement(args.labels, args.output, timeout_seconds=args.timeout_seconds, resume=args.resume)
    print(json.dumps({"status": result["status"], "target_days": len(result["parameters"]["targets"]),
        "request_count": result["request_count"], "resumed_pages": result["resumed_pages"],
        "rows": {s: r["rows"] for s, r in result["symbols"].items()},
        "ready_manifest": result.get("ready_manifest"), "error": result.get("error")}))
    return result


if __name__ == "__main__":
    main()
