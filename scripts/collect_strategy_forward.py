"""Capture closed post-registration bars; never run or open the strategy holdout.

Run the copy inside the frozen candidate's source directory. Each bounded
session verifies the immutable candidate, records actual receipt timestamps,
and seals raw responses. No performance or candidate-selection metrics exist
in this entry point.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]

import pandas as pd

from analysis.strategy_review import validate_prospective
from core.data_versions import DataVersionStore
from core.reproducibility import sha256_file
from scripts.roadmap_baseline import verify_source


def _write(path, value):
    with Path(path).open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _now():
    return datetime.now(timezone.utc).isoformat()


def verify_registration(batch, *, source_root=ROOT):
    batch = Path(batch).resolve()
    store = DataVersionStore(batch / "intake_snapshots")
    ref = _read(batch / "intake_registration_ref.json")["snapshot_id"]
    manifest = store.verify_snapshot(ref)
    for name in manifest["files"]:
        if (batch / name).read_bytes() != store.read_file(ref, name):
            raise ValueError("forward registration artifact changed: " + name)
    protocol = validate_prospective(_read(batch / "prospective_protocol.json"))
    if protocol["status"] != "pending_unseen_evidence" or (batch / "prospective_protocol.json.opened").exists():
        raise PermissionError("capture requires the unopened candidate protocol")
    sources = _read(batch / "source_manifest.json")
    verify_source(Path(source_root), sources["files"])
    verify_source(batch / "source", sources["files"])
    contract = _read(batch / "intake_contract.json")
    if contract["protocol_hash"] != protocol["protocol_hash"] or contract["source_manifest_sha256"] != sha256_file(batch / "source_manifest.json"):
        raise ValueError("intake contract differs from registered candidate")
    return store, contract, protocol


def _history(batch, store, contract):
    boundary = pd.Timestamp(contract["observation_boundary"])
    cursor = {symbol: boundary for symbol in contract["symbols"]}
    sessions = batch / "forward_capture"
    # The immutable store is an independent catalog. Enumerating only extant
    # session folders would silently restart history after a lost tail folder.
    expected = {}
    for path in (store.root / "snapshots").glob("*.json"):
        manifest = _read(path)
        if manifest.get("dataset_id") != "strategy-forward-session":
            continue
        manifest = store.verify_snapshot(path.stem)
        if manifest["config_refs"].get("protocol_hash") != contract["protocol_hash"]:
            raise ValueError("foreign forward session in the registered store")
        summary = json.loads(store.read_file(path.stem, "summary.json"))
        session_id = summary["session_id"]
        if not isinstance(session_id, str) or Path(session_id).name != session_id or session_id in expected:
            raise ValueError("invalid or duplicate forward session identity")
        expected[session_id] = path.stem
    folders = list(sessions.iterdir()) if sessions.exists() else []
    if {folder.name for folder in folders} != set(expected):
        raise ValueError("forward session missing from disk or not independently sealed")
    refs = []
    for folder in sorted(folders):
        if not folder.is_dir() or not (folder / "receipt.json").is_file():
            raise ValueError("previous forward session is not sealed")
        receipt = _read(folder / "receipt.json")
        if receipt["snapshot_id"] != expected[folder.name]:
            raise ValueError("forward session differs from immutable catalog")
        snapshot = store.verify_snapshot(receipt["snapshot_id"])
        for name in snapshot["files"]:
            if (folder / name).read_bytes() != store.read_file(receipt["snapshot_id"], name):
                raise ValueError("sealed forward session changed")
        result = _read(folder / "summary.json")
        store.verify_snapshot(result["record_snapshot"])
        if result["protocol_hash"] != contract["protocol_hash"] or result["prior_snapshots"] != refs:
            raise ValueError("forward capture chain mismatch")
        if result["status"] == "complete":
            for symbol, stop in result["next_start"].items():
                cursor[symbol] = pd.Timestamp(stop)
        refs.append(receipt["snapshot_id"])
    return cursor, refs


def collect(batch, *, source_root=ROOT, client=None):
    batch = Path(batch).resolve()
    store, contract, protocol = verify_registration(batch, source_root=source_root)
    point = pd.Timestamp(_now())
    if pd.isna(point) or point.tzinfo is None or point < pd.Timestamp(protocol["registered_at"]):
        raise ValueError("current aware observation time must follow registration")
    point = point.tz_convert("UTC")
    stop = min(point.normalize(), pd.Timestamp(contract["intake_stop_exclusive"]))
    lock = batch / "forward_capture.lock"
    with lock.open("x", encoding="utf-8") as handle:
        handle.write(_now())
    owned = False
    try:
        cursor, previous = _history(batch, store, contract)
        folder = batch / "forward_capture" / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ_")+uuid4().hex[:8])
        folder.mkdir(parents=True, exist_ok=False)
        session = {"schema": "strategy-forward-capture/v1", "registered_at": _now(),
            "session_id": folder.name,
            "protocol_hash": contract["protocol_hash"], "prior_snapshots": previous,
            "start": {s: t.isoformat() for s, t in cursor.items()}, "stop_exclusive": stop.isoformat(),
            "public_read_only": True, "orders_submitted": 0}
        _write(folder / "registration.json", session)
        result = {**session, "status": "complete", "coverage": {}, "requests": 0,
            "next_start": dict(session["start"]), "errors": [], "strategy_evaluated": False,
            "independent_holdout_complete": False, "performance_disclosed": False,
            "phase": ("before_observation" if point < pd.Timestamp(contract["observation_boundary"]) else
                      "embargo" if point < pd.Timestamp(contract["test_start"]) else
                      "test_capture" if point < pd.Timestamp(contract["test_end_exclusive"]) else "label_maturation")}
        records = []
        try:
            for symbol in contract["symbols"]:
                start = cursor[symbol]
                if start >= stop:
                    result["coverage"][symbol] = {"expected": 0, "received": 0, "missing": [], "shortened": []}
                    continue
                if result["requests"] >= contract["max_pages_per_session"]:
                    raise ValueError("registered session request budget exhausted")
                end = min(stop, start+pd.Timedelta(days=contract["max_bars_per_symbol_per_session"]))
                if client is None:
                    from scripts.fetch_spot_intraday import BinanceSpotKlineClient
                    client = BinanceSpotKlineClient(contract["timeout_seconds"])
                    owned = True
                sent = _now()
                params = {"symbol": symbol.replace("/", ""), "interval": contract["timeframe"],
                    "startTime": int(start.timestamp()*1000), "endTime": int(end.timestamp()*1000)-1, "limit": 1000}
                result["requests"] += 1
                page = client.fetch(params, contract["timeout_seconds"])
                observed = _now()
                if not point <= pd.Timestamp(sent) <= pd.Timestamp(observed):
                    raise ValueError("collector clock regressed")
                raw_name = symbol.replace("/", "_")+".json"
                _write(folder / raw_name, {"source": contract["source"], "params": params,
                    "request_started_at": sent, "observed_at": observed, "available_at": observed, "response": page})
                if not isinstance(page, list):
                    raise ValueError("invalid public candle response")
                received, shortened = [], []
                for item in page:
                    if not isinstance(item, list) or len(item) < 8:
                        raise ValueError("invalid public candle row")
                    opened, closed = pd.Timestamp(int(item[0]), unit="ms", tz="UTC"), pd.Timestamp(int(item[6]), unit="ms", tz="UTC")
                    if opened != opened.normalize() or not start <= opened < end or not opened <= closed < opened+pd.Timedelta(days=1):
                        raise ValueError("candle outside the registered closed observation range")
                    if received and opened <= received[-1]:
                        raise ValueError("duplicate or unordered candle")
                    values = dict(zip(("open", "high", "low", "close", "volume"), map(float, item[1:6])))
                    if (not all(math.isfinite(v) for v in values.values()) or values["volume"] < 0
                            or min(values[k] for k in ("open", "high", "low", "close")) <= 0
                            or values["high"] < max(values["open"], values["low"], values["close"])
                            or values["low"] > min(values["open"], values["high"], values["close"])):
                        raise ValueError("invalid public OHLCV")
                    received.append(opened)
                    complete = closed+pd.Timedelta(milliseconds=1) == opened+pd.Timedelta(days=1)
                    if not complete:
                        shortened.append(opened.isoformat())
                    records.append({"record_id": symbol+":"+opened.isoformat(), "revision_id": sha256_file(folder / raw_name),
                        "event_time": opened.isoformat(), "observed_at": observed, "available_at": observed,
                        "availability_evidence": {"kind": "local_receipt", "reference": raw_name},
                        "data": {"symbol": symbol, **values, "close_time": closed.isoformat(), "is_complete_bar": complete}})
                missing = pd.date_range(start, end, freq="D", inclusive="left").difference(pd.DatetimeIndex(received))
                result["coverage"][symbol] = {"expected": len(pd.date_range(start, end, freq="D", inclusive="left")),
                    "received": len(received), "missing": [t.isoformat() for t in missing], "shortened": shortened}
                result["next_start"][symbol] = end.isoformat()
                if len(missing) or shortened:
                    result["status"] = "incomplete_coverage"
        except Exception as exc:
            result["status"] = "failed"
            result["errors"].append({"category": type(exc).__name__})
        if result["requests"] == 0 and result["status"] == "complete":
            result["status"] = "awaiting_closed_forward_bars"
        try:
            verify_registration(batch, source_root=source_root)
        except (ValueError, PermissionError, OSError) as exc:
            result["status"] = "failed"
            result["errors"].append({"phase": "identity_after_capture", "category": type(exc).__name__})
        result["ended_at"] = _now()
        result["record_snapshot"] = store.create_snapshot("strategy-forward-candles", records,
            config_refs={"protocol_hash": contract["protocol_hash"]}, metadata={"public_read_only": True})["snapshot_id"]
        _write(folder / "summary.json", result)
        files = {path.name: path for path in folder.iterdir() if path.is_file()}
        snapshot = store.freeze_files("strategy-forward-session", files, observed_at=_now(),
            config_refs={"protocol_hash": contract["protocol_hash"]})
        _write(folder / "receipt.json", {"snapshot_id": snapshot["snapshot_id"]})
        return {**result, "output": str(folder), "snapshot_id": snapshot["snapshot_id"]}
    finally:
        if owned:
            client.close()
        lock.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, required=True)
    args = parser.parse_args(argv)
    result = collect(args.batch)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] in {"complete", "awaiting_closed_forward_bars"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
