"""Replay registered controls from frozen forecasts in a new delivery run.

No fitting, parameter selection, or official full-history engine run occurs.
The explicit field projection is sufficient for the frozen P3 input contract;
its new identity is never represented as the original full P2 payload digest.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import csv
import gc
import hashlib
import importlib.util
import json
import logging
from pathlib import Path
import sys


PREDICTION_FIELDS = ("candidate_id", "horizon_bars", "strategy", "symbol", "direction",
    "signal_version", "snapshot_version", "timeframe", "available_at", "status", "estimate_bps",
    "lower_bound_bps", "fold_id", "training_cutoff", "model_version", "max_label_available_at")


def decode_prediction(row):
    value = {key: row.get(key) or None for key in PREDICTION_FIELDS}
    value["horizon_bars"] = int(value["horizon_bars"])
    for key in ("estimate_bps", "lower_bound_bps"):
        if value[key] is not None:
            value[key] = float(value[key])
    return value


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def verify_frozen(batch):
    manifest = read(batch / "revised_manifest.json")
    for field, folder in (("source_hashes", "revised_source"), ("input_hashes", "frozen_inputs")):
        for relative, expected in manifest[field].items():
            if sha(batch / folder / relative) != expected:
                raise ValueError(f"Frozen {field} mismatch: {relative}")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    batch, output = args.batch.resolve(), args.output.resolve()
    if output == batch or output.is_relative_to(batch):
        raise ValueError("Control replay must use a new directory outside the historical batch")
    output.mkdir(parents=True, exist_ok=True)
    meta = batch / "meta_review"
    manifest = verify_frozen(batch)
    identity = read(meta / "identity.json")
    if identity["source_hashes"] != manifest["source_hashes"] or identity["input_hashes"] != manifest["input_hashes"]:
        raise ValueError("Cached producer differs from frozen source/input manifest")
    if sha(batch / "review_protocol.json") != identity["protocol"]:
        raise ValueError("Protocol identity mismatch")
    sealed = read(meta / "report_reuse_manifest.json")["files"]
    amendment = read(batch / "meta_export_resume_amendment.json")
    completed = amendment["interrupted_report_attempt"]["completed_exports"]
    used = ("signal_candidate_events.jsonl", "signal_decisions.csv", "signal_observation_summary.json",
            "p2_predictions.csv", "p2_folds.csv")
    inputs = {}
    for name in used:
        actual = sha(meta / name)
        if actual != {**sealed, **completed}[name]:
            raise ValueError(f"Sealed export hash mismatch: {name}")
        inputs[name] = actual
    projection = read(output / "official_p0_projection.json")
    p2_metadata = projection["payload"]["p2"]
    if projection["cache"]["sha256"] != (meta / "official_p0.sha256").read_text().strip():
        raise ValueError("Projection does not refer to the sealed on cache")
    source = batch / "revised_source"
    sys.path.insert(0, str(source))
    from config.config import config
    from core.signal_observation_types import fingerprint
    from core.signal_adaptive_types import MetaReplayPolicy
    from core.market_data import HistoricalMarketDataAdapter
    from backtest.engine import BacktestEngine
    from backtest.signal_meta_replay import replay_signal_meta
    from backtest.reporting.signal_adaptive import write_signal_meta_replay_report
    from scripts.run_revalidation60 import load_inputs

    for module in ("config.config", "backtest.engine", "backtest.signal_meta_replay", "scripts.run_revalidation60"):
        if not Path(sys.modules[module].__file__).resolve().is_relative_to(source):
            raise ValueError(f"Non-frozen implementation imported: {module}")
    adapter_path = batch / "performance_diagnostics/streaming_report_export.py"
    spec = importlib.util.spec_from_file_location("frozen_report_adapter", adapter_path)
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    adapter.install_report_adapter()
    logging.disable(logging.CRITICAL)
    config._config = deepcopy(read(batch / "review_protocol.json")["parameters"])
    p0 = read(meta / "signal_observation_summary.json")
    with (meta / "signal_candidate_events.jsonl").open(encoding="utf-8") as stream:
        all_candidates = [json.loads(line) for line in stream]
    if fingerprint(sorted(all_candidates, key=lambda item: item["candidate_id"])) != p2_metadata["input_identity"]["candidates_sha256"]:
        raise ValueError("Restored candidates differ from original frozen P2 candidate identity")
    # Registered membership uses only the strategy and direction at decision time.
    p0["candidates"] = [row for row in all_candidates if row["strategy"] == "TrendBreakout" and row["direction"] == "long"]
    del all_candidates
    ids = {row["candidate_id"] for row in p0["candidates"]}
    with (meta / "signal_decisions.csv").open(encoding="utf-8-sig", newline="") as stream:
        p0["decisions"] = [{"candidate_id": row["candidate_id"], "veto_stage": row["veto_stage"]}
                           for row in csv.DictReader(stream) if row["candidate_id"] in ids]
    p0["outcomes"] = []  # The frozen P3 validator deliberately does not inspect outcomes.
    with (meta / "p2_predictions.csv").open(encoding="utf-8-sig", newline="") as stream:
        predictions = [decode_prediction(row) for row in csv.DictReader(stream) if row["candidate_id"] in ids]
    with (meta / "p2_folds.csv").open(encoding="utf-8-sig", newline="") as stream:
        folds = list(csv.DictReader(stream))
    p2 = {**p2_metadata, "folds": folds, "predictions": predictions,
          "input_identity": {**p2_metadata["input_identity"],
            "candidates_sha256": fingerprint(sorted(p0["candidates"], key=lambda item: item["candidate_id"]))}}
    provenance = {"schema": "frozen_forecast_control_replay/v1", "original_producer": identity,
        "delivery_runner_sha256": sha(__file__), "streaming_adapter_sha256": sha(adapter_path),
        "input_exports_sha256": inputs, "source_manifest_sha256": sha(batch / "revised_manifest.json"),
        "new_policy_replay_calls": 2, "official_full_history_engine_reruns": 0,
        "template_one_bar_initializations": 1, "registered_626_engine_reruns": 0,
        "model_fits": 0, "parameter_searches": 0, "selector": {"strategy": "TrendBreakout", "direction": "long"},
        "candidate_count": len(ids), "prediction_count": len(predictions),
        "prediction_field_projection": list(PREDICTION_FIELDS),
        "decision_field_projection": ["candidate_id", "veto_stage"],
        "projection_identity": "new derived P3 inputs; no claim of identical full P2 payload digest"}
    (output / "control_replay_identity.json").write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    print(f"INPUTS VERIFIED: {len(ids)} candidates, {len(predictions)} frozen predictions", flush=True)
    frames, _, _ = load_inputs(batch / "frozen_inputs")
    template = BacktestEngine(initial_capital=10000, timeframe="1d", alignment_mode="union", benchmark_mode="fixed",
        signal_observation={"enabled": False}, signal_meta_layer={"enabled": False},
        signal_adaptive={"enabled": False}, signal_meta_replay={"enabled": False})
    template.run({symbol: frame.head(1) for symbol, frame in frames.items()}, routing_log_enabled=False)
    market = HistoricalMarketDataAdapter(frames, timeframe="1d", alignment_mode="union")
    summaries = {}
    for name, notional in (("primary", 1000.0), ("quarter_control", 250.0)):
        print(f"REPLAY {name}", flush=True)
        payload = replay_signal_meta(market, p0, p2, template.execution_adapter.broker,
                                     MetaReplayPolicy(enabled=True, reference_notional=notional))
        summaries[name] = write_signal_meta_replay_report(payload, output / name)
        print(f"DONE {name}: {summaries[name]['status']}", flush=True)
        del payload
        gc.collect()
    (output / "control_replay_results.json").write_text(json.dumps(summaries, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
