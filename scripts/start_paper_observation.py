"""Register and seal bounded prospective public observations; never place orders.

Each invocation is an explicit finite session. A resume verifies the original
protocol, scoped source files, sealed session artifacts and durable quote prefix
before collecting anything. This is observation provenance, not unseen-strategy
validation, an execution calibration or an authorization to trade.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]

from analysis.research_evidence import ResearchEvidenceRun
from core.data_versions import DataVersionStore
from core.quote_observations import QuoteObservationStore, aware_utc, read_quote_observations
from core.reproducibility import canonical_json, sha256_bytes, sha256_file


# Hash the observer and its evidence path, not unrelated strategy development.
OBSERVATION_SOURCES = (
    "scripts/start_paper_observation.py", "scripts/collect_execution_quotes.py",
    "core/quote_observations.py", "core/request_budget.py", "core/data_versions.py",
    "core/reproducibility.py", "core/metrics/execution.py",
    "analysis/research_evidence.py", "analysis/execution_calibration.py",
    "analysis/paper_study.py", "analysis/paper_validation.py", "requirements.lock.txt",
)
DEFAULTS = {"exchange_id": "binance", "symbols": ["BTC/USDT", "ETH/USDT"],
    "market_type": "spot", "environment": "live", "interval_seconds": 1.,
    "timeout_seconds": 5., "stale_after_seconds": 5.}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _digest(value):
    return sha256_bytes(canonical_json(value).encode())


def _write(path, value):
    """Exclusive complete receipt; existing evidence is never overwritten."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True,
                               indent=2, allow_nan=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def observation_source_hashes(source_root=ROOT):
    root = Path(source_root)
    return {name: sha256_file(root / name) for name in OBSERVATION_SOURCES}


def _configuration(values):
    config = {**DEFAULTS, **values}
    config["symbols"] = list(config["symbols"])
    if (not isinstance(config["exchange_id"], str) or not config["exchange_id"].strip()
            or not config["symbols"] or len(set(config["symbols"])) != len(config["symbols"])
            or any(not isinstance(s, str) or "/" not in s for s in config["symbols"])):
        raise ValueError("explicit exchange and distinct slash-delimited symbols required")
    if config["environment"] not in {"live", "sandbox"} or config["market_type"] not in {
            "spot", "spot_margin", "swap", "future", "perpetual"}:
        raise ValueError("unsupported public observation environment or market")
    for key in ("interval_seconds", "timeout_seconds", "stale_after_seconds"):
        value = config[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError("finite positive sampling times required")
        config[key] = float(value)
    return config


def _seal(store, dataset, files, *, source_hashes, protocol_id=None, metadata=None):
    return store.freeze_files(dataset, files, observed_at=_now(),
        code_refs=source_hashes, config_refs={"parent_protocol_snapshot_id": protocol_id},
        metadata=metadata or {})["snapshot_id"]


def _verify_files(store, snapshot_id, files):
    manifest = store.verify_snapshot(snapshot_id)
    if manifest["kind"] != "files" or set(manifest["files"]) != set(files):
        raise ValueError("sealed artifact set changed")
    for name, path in files.items():
        if Path(path).read_bytes() != store.read_file(snapshot_id, name):
            raise ValueError("sealed artifact changed: " + name)


def _parent(root, config, hashes, source_root, duration):
    root.mkdir(parents=True, exist_ok=False)
    store = DataVersionStore(root / "snapshots")
    source_files = {f"source-{i:03d}.txt": Path(source_root) / name
                    for i, name in enumerate(OBSERVATION_SOURCES)}
    code_snapshot = _seal(store, "public-observer-code", source_files, source_hashes=hashes,
        metadata={"source_paths": dict(zip(source_files, OBSERVATION_SOURCES))})
    if observation_source_hashes(source_root) != hashes:
        raise ValueError("observation source changed while registering")
    parameters = {"schema": "prospective-public-observation/v1", "configuration": config,
        "code_snapshot_id": code_snapshot, "initial_session_duration_seconds": duration,
        "session_duration_policy": "explicit per invocation; positive and at most 86400 seconds",
        "time_policy": "new public requests only after this registration; no historical backfill",
        "unknown_exchange_time_policy": "raw evidence only; never substitute receipt time",
        "public_read_only": True, "credentials_used": False, "orders_submitted": 0,
        "system_service_installed": False, "independent_holdout": False,
        "real_venue_calibration": False, "source_scope": list(OBSERVATION_SOURCES)}
    registration = ResearchEvidenceRun(candidates=config["symbols"], parameters=parameters,
        data_map={}, output=root / "registration", source_hashes=hashes)
    _write(root / "protocol.json", registration.registration)
    protocol_id = _seal(store, "public-observation-protocol", {
        "protocol.json": root / "protocol.json",
        "registration.json": root / "registration" / "registration.json"}, source_hashes=hashes)
    _write(root / "protocol_ref.json", {"schema": "public-observation-protocol-ref/v1",
        "snapshot_id": protocol_id, "registration_identity": registration.identity})
    QuoteObservationStore(root / "execution_quotes.sqlite3")
    return store, registration.registration, protocol_id


def _verify_parent(root, hashes, overrides):
    if not root.is_dir() or not (root / "snapshots").is_dir():
        raise ValueError("resume requires an existing registered observation directory")
    store = DataVersionStore(root / "snapshots")
    reference = _read(root / "protocol_ref.json")
    protocol_id = reference["snapshot_id"]
    _verify_files(store, protocol_id, {"protocol.json": root / "protocol.json",
        "registration.json": root / "registration" / "registration.json"})
    protocol = _read(root / "protocol.json")
    if _digest(protocol) != reference["registration_identity"]:
        raise ValueError("protocol registration identity changed")
    if protocol["parameters"]["schema"] != "prospective-public-observation/v1":
        raise ValueError("unsupported observation protocol")
    if protocol["source_hashes"] != hashes:
        raise ValueError("observation source changed; register a new protocol")
    config = _configuration(protocol["parameters"]["configuration"])
    if any(config.get(key) != value for key, value in overrides.items()):
        raise ValueError("resume configuration differs from immutable protocol")
    store.verify_snapshot(protocol["parameters"]["code_snapshot_id"])
    if not (root / "execution_quotes.sqlite3").is_file():
        raise ValueError("registered quote store is missing")
    return store, protocol, protocol_id


def _history(root, store, quotes, protocol_id):
    """Verify sealed prior sessions and their exact durable quote prefix."""
    expected, parents = [], []
    sessions = root / "sessions"
    for folder in sorted(sessions.iterdir()) if sessions.exists() else ():
        if not folder.is_dir():
            raise ValueError("unexpected session entry")
        if not (folder / "receipt.json").is_file():
            raise ValueError("prior session is unsealed; inspect interruption before resuming")
        receipt = _read(folder / "receipt.json")
        mapping = receipt["sealed_files"]
        if any(not isinstance(p, str) or Path(p).is_absolute() or ".." in Path(p).parts
               or not (folder / p).resolve().is_relative_to(folder.resolve()) for p in mapping.values()):
            raise ValueError("invalid sealed session path")
        _verify_files(store, receipt["snapshot_id"], {k: folder / p for k, p in mapping.items()})
        result = _read(folder / "session_result.json")
        if (result["parent_protocol_snapshot_id"] != protocol_id
                or result["starting_sequence"] != (expected[-1]["sequence"] if expected else 0)):
            raise ValueError("session protocol or sequence chain mismatch")
        if result["status"] == "stop_pending":
            raise ValueError("previous collector termination is unverified")
        captured = _read(folder / "captured_quotes.json")
        expected.extend(captured)
        if result["last_sequence"] != (expected[-1]["sequence"] if expected else 0):
            raise ValueError("session ending sequence changed")
        parents.append(receipt["snapshot_id"])
    if quotes != expected:
        raise ValueError("durable quote history differs from sealed session prefix")
    return parents


def _collector_args(config, duration, output, quote_store):
    args = ["--exchange", config["exchange_id"], "--symbols", *config["symbols"],
        "--market-type", config["market_type"], "--environment", config["environment"],
        "--duration-seconds", str(duration), "--output", str(output),
        "--resume-store", str(quote_store)]
    for name in ("interval_seconds", "timeout_seconds", "stale_after_seconds"):
        args += ["--" + name.replace("_", "-"), str(config[name])]
    return args


def _check_capture(folder, quotes, config, starting_sequence, registered_at, ended_at, duration):
    collection = folder / "collection"
    manifest, health, summary = (_read(collection / name) for name in
                                ("manifest.json", "sampler_health.json", "summary.json"))
    if (manifest["starting_sequence"] != starting_sequence
            or manifest.get("orders_submitted") != 0 or manifest.get("credentials_used") is not False
            or manifest.get("public_read_only") is not True
            or manifest.get("duration_seconds") != duration
            or manifest.get("source") != config["environment"]
            or any(manifest.get(key) != value for key, value in config.items() if key != "environment")
            or summary.get("fills") != 0 or summary.get("real_venue_calibration") is not False):
        raise ValueError("collector contract differs from registered public-only observation")
    if (Path(manifest["quote_store"]).resolve() != folder.parent.parent / "execution_quotes.sqlite3"
            or not aware_utc(registered_at) <= aware_utc(manifest["started_at"]) <= aware_utc(ended_at)
            or summary.get("observations") != len(quotes)):
        raise ValueError("collector window or durable store differs from registration")
    if _read(collection / "quote_observations.json") != quotes:
        raise ValueError("export differs from durable session quote facts")
    counts = Counter()
    for row in quotes:
        if (row["collector_run_id"] != manifest["collector_run_id"]
                or row["symbol"] not in config["symbols"]
                or any(row[k] != config[k] for k in ("exchange_id", "environment", "market_type"))):
            raise ValueError("quote source differs from registered session")
        if not (aware_utc(registered_at) <= aware_utc(row["request_started_at"])
                <= aware_utc(row["observed_at"]) <= aware_utc(row["available_at"]) <= aware_utc(ended_at)):
            raise ValueError("quote was not prospectively requested and received in this session")
        counts[row["symbol"]] += 1
    if health.get("lifecycle") != "stopped":
        return "stop_pending", dict(counts)
    if (not quotes or set(counts) != set(config["symbols"])
            or health.get("status") != "ok" or summary.get("status") != "observed_quotes"):
        return "insufficient", dict(counts)
    return "completed", dict(counts)


def run_observation(*, output=None, resume=None, duration_seconds, collector=None,
                    source_root=ROOT, **configuration):
    """Run one finite session. Injected collectors are for deterministic tests."""
    if (output is None) == (resume is None):
        raise ValueError("choose exactly one fresh output or existing resume directory")
    if (isinstance(duration_seconds, bool) or not isinstance(duration_seconds, (int, float))
            or not math.isfinite(duration_seconds) or not 0 < duration_seconds <= 86400):
        raise ValueError("explicit duration must be positive and at most one day")
    if set(configuration) - set(DEFAULTS):
        raise ValueError("unknown observation configuration")
    overrides = {k: v for k, v in configuration.items() if v is not None}
    root = Path(output if output is not None else resume).resolve()
    hashes = observation_source_hashes(source_root)
    if resume is None:
        config = _configuration(overrides)
        store, protocol, protocol_id = _parent(root, config, hashes, source_root, duration_seconds)
    else:
        store, protocol, protocol_id = _verify_parent(root, hashes, overrides)
        config = _configuration(protocol["parameters"]["configuration"])
    # Only a lock created by this invocation is removed. A crash leaves an
    # explicit stale lock; automatic recovery never erases another writer.
    lock = root / "active_session.lock"
    with lock.open("x", encoding="utf-8") as handle:
        handle.write(_now())
    try:
        before = read_quote_observations(root / "execution_quotes.sqlite3")
        previous = _history(root, store, before, protocol_id)
        start_sequence = before[-1]["sequence"] if before else 0
        session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ_") + uuid4().hex[:8]
        folder = root / "sessions" / session_id
        parameters = {"parent_protocol_snapshot_id": protocol_id, "previous_session_snapshots": previous,
            "configuration": config, "duration_seconds": float(duration_seconds),
            "starting_sequence": start_sequence, "prior_quote_prefix_sha256": _digest(before),
            "public_read_only": True, "orders_submitted": 0, "historical_backfill": False}
        run = ResearchEvidenceRun(candidates=config["symbols"], parameters=parameters,
            data_map={"prior_quotes": before}, output=folder / "registration", source_hashes=hashes)
        registration_snapshot = _seal(store, "public-observation-session-registration", {
            "registration.json": folder / "registration" / "registration.json"},
            source_hashes=hashes, protocol_id=protocol_id)
        _write(folder / "registration_ref.json", {"snapshot_id": registration_snapshot})
        run.record(phase="collection", status="started", registration_snapshot_id=registration_snapshot)
        status, errors, counts = "failed", [], {}
        collection_started_at, clock_start = _now(), time.monotonic()
        try:
            if observation_source_hashes(source_root) != hashes:
                raise ValueError("source identity changed before collection")
            if collector is None:
                from scripts.collect_execution_quotes import main as collector
            collector(_collector_args(config, duration_seconds, folder / "collection",
                                      root / "execution_quotes.sqlite3"))
        except (Exception, KeyboardInterrupt) as exc:
            errors.append({"phase": "collection", "category": type(exc).__name__})
            status = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        ended_at, elapsed = _now(), time.monotonic() - clock_start
        # Even an interrupted collector may have persisted observations before
        # failing to export its JSON. Preserve the durable facts independently.
        after, readable = [], True
        try:
            after = read_quote_observations(root / "execution_quotes.sqlite3")
        except Exception as exc:
            readable, status = False, "failed"
            errors.append({"phase": "durable_read", "category": type(exc).__name__})
        quotes = [r for r in after if r["sequence"] > start_sequence]
        _write(folder / "captured_quotes.json", quotes)
        if not errors:
            try:
                status, counts = _check_capture(folder, quotes, config, start_sequence,
                    run.registration["registered_at"], ended_at, duration_seconds)
                # The collection CLI handles Ctrl+C by stopping cleanly. A
                # short but nonempty capture must not claim the requested full
                # observation interval. Allow only timer rounding (10 ms).
                if status == "completed" and elapsed + .01 < duration_seconds:
                    status = "incomplete_duration"
            except Exception as exc:
                errors.append({"phase": "capture_validation", "category": type(exc).__name__})
                status = "failed"
        identity = run.verify_identity({"prior_quotes": [r for r in after if r["sequence"] <= start_sequence]})
        try:
            source_unchanged = observation_source_hashes(source_root) == hashes
        except Exception as exc:
            source_unchanged = False
            errors.append({"phase": "source_read", "category": type(exc).__name__})
        identity.update(data_unchanged=readable and identity["data_unchanged"], source_unchanged=source_unchanged,
            source_verification="scoped_observer_files_before_and_after")
        identity["identity_changed"] = not identity["data_unchanged"] or not identity["source_unchanged"]
        if identity["identity_changed"]:
            status = "failed"
            errors.append({"phase": "identity", "category": "IdentityChanged"})
        run.record(phase="observer_identity", status="changed" if identity["identity_changed"] else "checked",
                   **identity)
        result = {"schema": "prospective-public-session/v1", "status": status, "session_id": session_id,
            "parent_protocol_snapshot_id": protocol_id, "registration_snapshot_id": registration_snapshot,
            "registered_at": run.registration["registered_at"], "collection_started_at": collection_started_at,
            "ended_at": ended_at, "elapsed_seconds": elapsed,
            "duration_seconds": duration_seconds, "starting_sequence": start_sequence,
            "last_sequence": quotes[-1]["sequence"] if quotes else start_sequence,
            "observations": len(quotes), "observations_by_symbol": counts,
            "unknown_exchange_timestamps": sum(r["occurred_at"] is None for r in quotes),
            "identity": identity, "errors": errors, "public_read_only": True,
            "orders_submitted": 0, "real_venue_calibration": False, "independent_holdout": False,
            "production_approved": False, "system_service_installed": False,
            "scope": "prospective public observation provenance; no strategy or execution validation"}
        run.record(phase="collection", status=status, observations=len(quotes), errors=errors)
        _write(folder / "session_result.json", result)
        files = {"session_result.json": folder / "session_result.json",
            "captured_quotes.json": folder / "captured_quotes.json",
            "registration.json": folder / "registration" / "registration.json",
            "registration_ref.json": folder / "registration_ref.json",
            "attempts.jsonl": folder / "registration" / "attempts.jsonl"}
        for name in ("manifest.json", "sampler_health.json", "summary.json",
                     "quote_observations.json", "calibration_without_fills.json"):
            path = folder / "collection" / name
            if path.is_file():
                files["collector_" + name] = path
        snapshot_id = _seal(store, "public-observation-session-evidence", files,
            source_hashes=hashes, protocol_id=protocol_id,
            metadata={"session_id": session_id, "status": status,
                      "unknown_exchange_time_policy": "raw JSON only; no imputed event_time"})
        _verify_files(store, snapshot_id, files)
        _write(folder / "receipt.json", {"schema": "public-observation-session-receipt/v1",
            "snapshot_id": snapshot_id, "sealed_files": {k: p.relative_to(folder).as_posix() for k, p in files.items()}})
        return {**result, "snapshot_id": snapshot_id, "output": str(root), "session": str(folder)}
    finally:
        lock.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    destination = parser.add_mutually_exclusive_group(required=True)
    destination.add_argument("--output", type=Path, help="New experiment directory; never overwrite evidence")
    destination.add_argument("--resume", type=Path, help="Existing immutable protocol; append one new finite session")
    parser.add_argument("--duration-seconds", type=float, required=True,
                        help="Explicit bounded duration for this invocation, at most 86400 seconds")
    parser.add_argument("--exchange", dest="exchange_id")
    parser.add_argument("--symbols", nargs="+")
    parser.add_argument("--market-type")
    parser.add_argument("--environment", choices=("live", "sandbox"))
    parser.add_argument("--interval-seconds", type=float)
    parser.add_argument("--timeout-seconds", type=float)
    parser.add_argument("--stale-after-seconds", type=float)
    args = vars(parser.parse_args(argv))
    result = run_observation(**args)
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
