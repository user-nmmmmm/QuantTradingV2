"""Audit registered Binance spot archives against official SHA256 CHECKSUM files.

This supplement never changes the validated REST candles. Missing archives,
failed checksums, incomplete bars, and source differences remain explicit gaps.
Only official data.binance.vision archives and Binance's own documentation are
requested. Raw bytes, request metadata and each archive result are immutable.
"""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from datetime import datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path
import re
import threading
import time
import zipfile

import pandas as pd
import requests


ROOT = Path(__file__).resolve().parents[1]
BASE = "https://data.binance.vision/data/spot"
DOCUMENTATION = "https://github.com/binance/binance-public-data"
README = "https://raw.githubusercontent.com/binance/binance-public-data/master/README.md"
SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT", "LTCUSDT")
DURATIONS = {"1d": 86_400_000, "4h": 14_400_000}
VALUE_COLUMNS = ("open", "high", "low", "close", "volume")


def sha256(payload):
    return hashlib.sha256(payload).hexdigest()


def _json(payload):
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _immutable(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != content:
            raise ValueError(f"Refusing to replace existing evidence: {path}")
        return
    with path.open("xb") as stream:
        stream.write(content)


def _ms(value):
    stamp = pd.Timestamp(value)
    stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
    return int(stamp.value // 1_000_000)


def _iso(milliseconds):
    return pd.Timestamp(milliseconds, unit="ms", tz="UTC").isoformat()


def registered_archives():
    jobs = []
    for symbol in SYMBOLS:
        for timeframe in DURATIONS:
            for period in pd.period_range("2021-09", "2026-08", freq="M"):
                label = str(period)
                start, end = _ms(period.start_time), _ms((period + 1).start_time)
                jobs.append(_job(symbol, timeframe, "monthly", label, start, end))
            for day in pd.date_range("2026-09-01", "2026-09-18", freq="D"):
                jobs.append(_job(symbol, timeframe, "daily", day.strftime("%Y-%m-%d"),
                                 _ms(day), _ms(day + pd.Timedelta(days=1))))
    return jobs


def _job(symbol, timeframe, kind, label, start, end):
    filename = f"{symbol}-{timeframe}-{label}.zip"
    return {"symbol": symbol, "timeframe": timeframe, "kind": kind, "period": label,
            "filename": filename, "archive_id": filename[:-4],
            "url": f"{BASE}/{kind}/klines/{symbol}/{timeframe}/{filename}",
            "start_ms": start, "end_exclusive_ms": end,
            "expected_bars": (end - start) // DURATIONS[timeframe]}


def verify_checksum(archive, checksum, expected_filename):
    """Bind the official digest to the exact requested ZIP basename."""
    lines = checksum.decode("utf-8-sig").strip().splitlines()
    if len(lines) != 1:
        raise ValueError("Official checksum must name exactly one archive")
    match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?([^\s]+)", lines[0].strip())
    if not match or match.group(2) != expected_filename:
        raise ValueError("Malformed checksum or mismatched archive filename")
    expected, actual = match.group(1).lower(), sha256(archive)
    if expected != actual:
        raise ValueError(f"Official checksum mismatch: expected {expected}, observed {actual}")
    return expected


def _timestamp(value):
    original = int(value)
    unit = "us" if original >= 100_000_000_000_000 else "ms"
    normalized = original // 1000 if unit == "us" else original
    if not _ms("2000-01-01") <= normalized < _ms("2100-01-01"):
        raise ValueError("Unsupported timestamp range or unit")
    return original, normalized, unit


def parse_archive(archive, job, asof_ms):
    """Parse only valid, closed, UTC-aligned complete candles; reject short bars."""
    expected_member = job["filename"][:-4] + ".csv"
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        members = [item for item in zipped.infolist() if not item.is_dir()]
        if len(members) != 1 or members[0].filename != expected_member:
            raise ValueError("Archive CSV member differs from the requested file")
        if members[0].file_size > 10_000_000:
            raise ValueError("Archive CSV exceeds registered kline size bound")
        text = zipped.read(members[0]).decode("utf-8-sig")
    valid, rejected, filtered, units = {}, [], [], Counter()
    duplicate_timestamps = set()
    duration = DURATIONS[job["timeframe"]]
    for line, row in enumerate(csv.reader(io.StringIO(text)), start=1):
        if not row:
            continue
        if line == 1 and row[0].strip().lower() in {"open_time", "open time"}:
            continue
        try:
            if len(row) != 12:
                raise ValueError("Archive kline requires twelve fields")
            opening_raw, opening, unit = _timestamp(row[0])
            closing_raw, _, closing_unit = _timestamp(row[6])
            units[unit] += 1
            if opening % duration:
                raise ValueError("Candle is not aligned to registered UTC boundary")
            if unit != closing_unit:
                raise ValueError("Open and close timestamp units differ")
            scale = 1000 if unit == "us" else 1
            if closing_raw != opening_raw + duration * scale - 1:
                raise ValueError("Binance close time does not describe a complete interval")
            if not job["start_ms"] <= opening < job["end_exclusive_ms"]:
                raise ValueError("Archive candle falls outside its requested period")
            if opening + duration > asof_ms:
                filtered.append({"line": line, "timestamp": _iso(opening), "reason": "not_closed_at_frozen_cutoff"})
                continue
            values = tuple(float(value) for value in row[1:6])
            if not all(math.isfinite(value) for value in values):
                raise ValueError("Non-finite candle price or volume")
            o, h, low, close, volume = values
            if min(o, h, low, close) <= 0 or volume < 0 or low > min(o, close) or h < max(o, close):
                raise ValueError("Invalid OHLC envelope or volume")
            if opening in valid:
                # An archive duplicate is ambiguous evidence, never last-row-wins.
                valid.pop(opening)
                duplicate_timestamps.add(opening)
                raise ValueError("Duplicate archive candle timestamp")
            if opening in duplicate_timestamps:
                raise ValueError("Duplicate archive candle timestamp")
            valid[opening] = values
        except (ValueError, OverflowError) as exc:
            rejected.append({"line": line, "open_time_raw": row[0] if row else None,
                             "close_time_raw": row[6] if len(row) > 6 else None,
                             "reason": str(exc), "raw_row": row})
    return valid, {"rows_retained": len(valid), "rejected_rows": rejected,
                   "filtered_rows": filtered, "timestamp_units": dict(units)}


def compare_rows(archive_rows, retained_rows, job):
    """Compare retained data, never fill or alter the existing candle CSV."""
    if retained_rows is None:
        return {"status": "validated_csv_missing", "matched_rows": None, "mismatched_rows": None,
                "mismatches": [], "archive_only_timestamps": [], "validated_only_timestamps": []}
    expected = {stamp: values for stamp, values in retained_rows.items()
                if job["start_ms"] <= stamp < job["end_exclusive_ms"]}
    common = set(archive_rows) & set(expected)
    mismatches, exact, tolerance_only = [], 0, 0
    for timestamp in sorted(common):
        left, right = archive_rows[timestamp], expected[timestamp]
        if left == right:
            exact += 1
        elif all(math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-10) for a, b in zip(left, right)):
            tolerance_only += 1
        else:
            fields = {field: {"archive": a, "validated_rest": b, "difference": a - b}
                      for field, a, b in zip(VALUE_COLUMNS, left, right)
                      if not math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-10)}
            mismatches.append({"timestamp": _iso(timestamp), "fields": fields})
    archive_only = sorted(set(archive_rows) - set(expected))
    rest_only = sorted(set(expected) - set(archive_rows))
    return {"status": "differences" if mismatches or archive_only or rest_only else "matched",
            "matched_rows": len(common), "exact_numeric_rows": exact,
            "roundtrip_tolerance_only_rows": tolerance_only, "mismatched_rows": len(mismatches),
            "validated_rows_in_period": len(expected), "mismatches": mismatches,
            "archive_only_timestamps": [_iso(stamp) for stamp in archive_only],
            "validated_only_timestamps": [_iso(stamp) for stamp in rest_only],
            "numeric_comparison_tolerance": {"relative": 1e-12, "absolute": 1e-10}}


class ArchiveClient:
    def __init__(self, root, *, timeout=20.0):
        self.root, self.timeout = Path(root), timeout
        self.local = threading.local()

    def get(self, url):
        if not (url.startswith(BASE + "/") or url == README):
            raise ValueError("Only registered official archive/documentation URLs are allowed")
        if not hasattr(self.local, "session"):
            self.local.session = requests.Session()
            self.local.session.headers.update({"User-Agent": "QuantTradingV1-archive-audit/1"})
        request_id = sha256(url.encode("utf-8"))
        for attempt in (1, 2):
            stem = self.root / "raw" / f"{request_id}.{attempt}"
            metadata_path = Path(str(stem) + ".json")
            body_path = Path(str(stem) + ".body")
            if metadata_path.exists():
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                body = body_path.read_bytes()
                if metadata["url"] != url or metadata["body_sha256"] != sha256(body):
                    raise ValueError("Cached official request evidence differs")
            else:
                body = b""
                metadata = {"url": url, "method": "GET", "attempt": attempt,
                            "accessed_at": datetime.now(timezone.utc).isoformat(),
                            "http_status": None, "error": None}
                try:
                    response = self.local.session.get(url, timeout=(5.0, self.timeout), allow_redirects=False)
                    body = response.content
                    metadata["http_status"] = response.status_code
                    metadata["response_headers"] = {key: response.headers.get(key) for key in
                                                    ("ETag", "Last-Modified", "Content-Type", "Content-Length")}
                    if response.status_code != 200:
                        metadata["error"] = f"HTTP {response.status_code}"
                except requests.RequestException as exc:
                    metadata["error"] = f"{type(exc).__name__}: {exc}"
                metadata["body_sha256"] = sha256(body)
                metadata["body_bytes"] = len(body)
                _immutable(body_path, body)
                _immutable(metadata_path, _json(metadata))
            record = {**metadata, "metadata_path": metadata_path.relative_to(self.root).as_posix(),
                      "body_path": body_path.relative_to(self.root).as_posix()}
            if metadata["error"] is None or metadata["http_status"] in {400, 401, 403, 404, 410, 451}:
                return body, record
            if attempt == 1:
                time.sleep(0.3)
        return body, record


def audit_one(root, client, identity, job, retained_rows, asof_ms):
    path = root / "archives" / (job["archive_id"] + ".json")
    if path.exists():
        result = json.loads(path.read_text(encoding="utf-8"))
        if result["identity"] != identity:
            raise ValueError("Archive result identity changed; retain prior evidence separately")
        return result
    zipped, archive_request = client.get(job["url"])
    checksum, checksum_request = client.get(job["url"] + ".CHECKSUM")
    result = {"schema": "binance_archive_audit/v1", "identity": identity, **job,
              "requests": {"archive": archive_request, "checksum": checksum_request},
              "official_checksum_verified": False, "status": "unknown",
              "comparison": None, "parse": None}
    if archive_request["error"] or checksum_request["error"]:
        result["status"] = "official_source_unavailable"
        result["errors"] = [request["error"] for request in (archive_request, checksum_request) if request["error"]]
    else:
        try:
            result["official_sha256"] = verify_checksum(zipped, checksum, job["filename"])
            result["official_checksum_verified"] = True
        except (ValueError, UnicodeError) as exc:
            result["status"], result["errors"] = "checksum_failed", [str(exc)]
        if result["official_checksum_verified"]:
            try:
                rows, parsed = parse_archive(zipped, job, asof_ms)
                result["parse"] = parsed
                result["comparison"] = compare_rows(rows, retained_rows, job)
                result["missing_valid_archive_bars"] = job["expected_bars"] - len(rows)
                result["status"] = "verified_archive"
            except (ValueError, UnicodeError, zipfile.BadZipFile, KeyError) as exc:
                result["status"], result["errors"] = "archive_parse_failed", [str(exc)]
    _immutable(path, _json(result))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, default=ROOT / "reports/strategy_review_20260919")
    parser.add_argument("--workers", type=int, default=4, choices=range(1, 5))
    args = parser.parse_args()
    batch = args.batch.resolve()
    root = batch / "public_data_archive_audit"
    root.mkdir(parents=True, exist_ok=True)
    protocol_bytes = (batch / "review_protocol.json").read_bytes()
    protocol = json.loads(protocol_bytes)
    scope = protocol["public_data"]
    if (_ms(scope["warmup_start"]) != _ms("2021-09-01")
            or _ms(scope["recent_end_exclusive"]) != _ms("2026-09-19")
            or tuple(symbol.replace("/", "") for symbol in scope["symbols"]) != SYMBOLS
            or set(scope["timeframes"]) != set(DURATIONS)):
        raise ValueError("Registered public scope differs from fixed archive audit")
    retained_root = batch / "public_data_validated"
    retained, inputs = {}, {}
    for symbol in SYMBOLS:
        for timeframe in DURATIONS:
            path = retained_root / f"binance_{symbol}_{timeframe}.csv"
            if not path.exists():
                retained[symbol, timeframe] = None
                inputs[path.name] = None
                continue
            raw = path.read_bytes()
            manifest = json.loads(path.with_suffix(".manifest.json").read_text(encoding="utf-8"))
            if sha256(raw) != manifest["csv_sha256"]:
                raise ValueError(f"Validated CSV checksum differs: {path.name}")
            frame = pd.read_csv(io.BytesIO(raw), float_precision="round_trip")
            timestamps = [_ms(value) for value in frame["timestamp"]]
            if len(timestamps) != len(set(timestamps)):
                raise ValueError("Validated CSV has ambiguous duplicate timestamps")
            retained[symbol, timeframe] = dict(zip(timestamps, frame[list(VALUE_COLUMNS)].itertuples(index=False, name=None)))
            inputs[path.name] = sha256(raw)
    client = ArchiveClient(root)
    documentation, documentation_request = client.get(README)
    identity = {"runner_sha256": sha256(Path(__file__).read_bytes()),
                "protocol_sha256": sha256(protocol_bytes), "validated_csv_sha256": inputs,
                "documentation_sha256": sha256(documentation)}
    _immutable(root / "identity.json", _json(identity))
    _immutable(root / "documentation.json", _json({"source": DOCUMENTATION, "request": documentation_request}))
    jobs = registered_archives()
    _immutable(root / "registered_archives.json", _json(jobs))
    asof_ms = min(_ms(scope["recent_end_exclusive"]), _ms(protocol["registered_at"]))
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(audit_one, root, client, identity, job,
                               retained[job["symbol"], job["timeframe"]], asof_ms): job for job in jobs}
        for future in as_completed(futures):
            results.append(future.result())
            if len(results) % 24 == 0 or len(results) == len(jobs):
                progress = {"completed": len(results), "registered": len(jobs),
                            "official_checksum_verified": sum(row["official_checksum_verified"] for row in results),
                            "statuses": dict(Counter(row["status"] for row in results))}
                (root / "progress.json").write_bytes(_json(progress))
                print(json.dumps(progress), flush=True)
    results.sort(key=lambda row: (row["symbol"], row["timeframe"], row["start_ms"]))
    streams = []
    for symbol in SYMBOLS:
        for timeframe in DURATIONS:
            rows = [row for row in results if row["symbol"] == symbol and row["timeframe"] == timeframe]
            comparisons = [row["comparison"] for row in rows if row["comparison"] is not None]
            streams.append({"symbol": symbol, "timeframe": timeframe, "registered_archives": len(rows),
                            "checksum_verified_archives": sum(row["official_checksum_verified"] for row in rows),
                            "source_unavailable_archives": sum(row["status"] == "official_source_unavailable" for row in rows),
                            "expected_bars": sum(row["expected_bars"] for row in rows),
                            "valid_verified_archive_bars": sum((row["parse"] or {}).get("rows_retained", 0) for row in rows),
                            "rejected_archive_rows": sum(len((row["parse"] or {}).get("rejected_rows", [])) for row in rows),
                            "compared_rows": sum(row.get("matched_rows") or 0 for row in comparisons),
                            "mismatched_numeric_rows": sum(row.get("mismatched_rows") or 0 for row in comparisons),
                            "archive_only_rows": sum(len(row["archive_only_timestamps"]) for row in comparisons),
                            "validated_only_rows": sum(len(row["validated_only_timestamps"]) for row in comparisons),
                            "unavailable_comparison_archives": sum(row["comparison"] is None or
                                row["comparison"]["status"] == "validated_csv_missing" for row in rows)})
    manifest = {"schema": "strategy_review_binance_archive_manifest/v1", "identity": identity,
                "registered_archives": len(jobs), "attempted_archives": len(results),
                "official_checksum_verified_archives": sum(row["official_checksum_verified"] for row in results),
                "statuses": dict(Counter(row["status"] for row in results)), "streams": streams,
                "status": "complete_audit", "archive_byte_coverage_complete": all(row["official_checksum_verified"] and row["parse"] is not None for row in results),
                "valid_closed_bar_coverage_complete": all(row["expected_bars"] == row["valid_verified_archive_bars"] for row in streams),
                "matched_retained_bars": sum(row["compared_rows"] for row in streams),
                "mismatched_numeric_rows": sum(row["mismatched_numeric_rows"] for row in streams),
                "rejected_archive_rows": sum(row["rejected_archive_rows"] for row in streams),
                "unknown_archive_bars": sum(row["expected_bars"] for row in results if row["parse"] is None),
                "retained_csvs_modified": False, "missing_values_filled": False,
                "substitution": None, "retrospective_only": True, "documentation": DOCUMENTATION,
                "interpretation": "Official byte integrity and retained closed-bar agreement are separate checks; rejected short intervals remain gaps.",
                "archive_manifests": [f"archives/{row['archive_id']}.json" for row in results]}
    # Recheck original validated bytes after the audit; comparison never writes them.
    for name, expected in inputs.items():
        if expected is not None and sha256((retained_root / name).read_bytes()) != expected:
            raise ValueError("Validated candles changed during archive audit")
    _immutable(root / "manifest.json", _json(manifest))
    pd.DataFrame(streams).to_csv(root / "stream_coverage.csv", index=False)
    print(json.dumps({key: value for key, value in manifest.items() if key not in {"identity", "streams", "archive_manifests"}}), flush=True)


if __name__ == "__main__":
    main()
