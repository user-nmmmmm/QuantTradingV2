"""Freeze the current repaired candidate and its bounded public intake contract.

The prior protocol stays unopened. This command registers a fresh observation
clock; it never inherits elapsed days or treats historical trials as unseen.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core.data_versions import DataVersionStore
from core.reproducibility import sha256_file
from scripts.register_strategy_successor import register_successor


def prepare(*, historical_batch, output, source_root=ROOT):
    historical_batch, output, source_root = map(Path, (historical_batch, output, source_root))
    registry = json.loads((historical_batch / "review_protocol.json").read_text(encoding="utf-8"))
    symbols = registry.get("public_data", {}).get("symbols")
    if not isinstance(symbols, list) or not symbols or len(set(symbols)) != len(symbols):
        raise ValueError("the frozen research registry must declare a distinct public symbol set")
    if any(not isinstance(s, str) or s.count("/") != 1 or not s.endswith("/USDT") for s in symbols):
        raise ValueError("registered spot USDT symbols required")
    receipt = register_successor(source_root=source_root, historical_batch=historical_batch, output=output)
    protocol = json.loads((output / "prospective_protocol.json").read_text(encoding="utf-8"))
    contract = {"schema": "strategy-forward-intake/v1", "candidate": protocol["candidate"],
        "protocol_hash": protocol["protocol_hash"], "code_hash": receipt["code_hash"],
        "source_manifest_sha256": sha256_file(output / "source_manifest.json"),
        "config_hash": receipt["config_hash"], "registry_hash": protocol["experiment_registry_hash"],
        "venue": "binance", "market_type": "spot", "symbols": symbols, "timeframe": "1d",
        "source": "https://api.binance.com/api/v3/klines", "max_pages_per_session": 12,
        "max_bars_per_symbol_per_session": 1000, "timeout_seconds": 5.0,
        "observation_boundary": protocol["observation_boundary"], "test_start": protocol["test_start"],
        "test_end_exclusive": protocol["test_end_exclusive"], "mature_after": protocol["mature_after"],
        "availability_policy": "actual local receipt; no publication backdating",
        "intake_stop_exclusive": protocol["mature_after"],
        "scope": "underlying public spot candles only; account financing and membership evidence remain separate",
        "gap_policy": "retain missing and shortened bars; no interpolation",
        "access_policy": "capture raw evidence and coverage only; no strategy evaluation before maturity",
        "public_read_only": True, "orders_submitted": 0, "production_approved": False}
    with (output / "intake_contract.json").open("x", encoding="utf-8") as handle:
        json.dump(contract, handle, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
    store = DataVersionStore(output / "intake_snapshots")
    snapshot = store.freeze_files("strategy-forward-registration", {
        name: output / name for name in ("intake_contract.json", "prospective_protocol.json",
            "review_protocol.json", "source_manifest.json", "parent_protocol.json", "acceptance.json")},
        observed_at=datetime.now(timezone.utc).isoformat(), code_refs={"source_hash": receipt["code_hash"]},
        metadata={"opens_final_sample": False, "inherits_old_elapsed_time": False})
    with (output / "intake_registration_ref.json").open("x", encoding="utf-8") as handle:
        json.dump({"snapshot_id": snapshot["snapshot_id"]}, handle)
    return {**receipt, "intake_registration_snapshot": snapshot["snapshot_id"],
        "symbols": symbols, "observation_boundary": protocol["observation_boundary"],
        "output": str(output.resolve())}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--historical-batch", type=Path, default=ROOT / "reports/strategy_review_20260919")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = prepare(historical_batch=args.historical_batch, output=args.output)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
