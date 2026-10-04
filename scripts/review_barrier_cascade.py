"""Refine only the remaining ambiguous labels, retaining prior verified paths."""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandas as pd

from analysis.paper_label_execution import refine_barrier_labels
from analysis.paper_labels import BarrierConfig, LabelCosts
from analysis.paper_study import write_json
from core.data_versions import DataVersionStore
from core.reproducibility import sha256_file
from scripts.run_paper_roadmap import load_fine_input


def cascade(previous, frames, candidates, fine, *, config, costs, fine_timeframe, as_of):
    remaining = {row["candidate_id"] for row in previous["outcomes"] if row["ambiguous"]}
    subset = [candidate for candidate in candidates if candidate["candidate_id"] in remaining]
    if len(subset) != len(remaining):
        raise ValueError("remaining label candidates are missing or duplicated")
    refined = refine_barrier_labels(frames, subset, config=config, costs=costs, as_of=as_of,
        fine_frames=fine, fine_timeframe=fine_timeframe)
    updates = {row["candidate_id"]: row for row in refined["outcomes"]}
    result = deepcopy(previous)
    result["outcomes"] = [deepcopy(updates.get(row["candidate_id"], row)) for row in previous["outcomes"]]
    result["as_of"] = as_of
    newly_resolved = refined["summary"]["resolved_with_fine_data"]
    result["summary"].update(ambiguous=sum(row["ambiguous"] for row in result["outcomes"]),
        training_eligible=sum(row["training_eligible"] for row in result["outcomes"]),
        resolved_with_fine_data=previous["summary"]["resolved_with_fine_data"]+newly_resolved)
    result["fine_data_contract"] = {"stages": [previous["fine_data_contract"], refined["fine_data_contract"]],
        "policy": "only unresolved candidates enter the next stage; prior proven rows remain byte-identical"}
    return result, {"input_ambiguous": len(remaining), "newly_resolved": newly_resolved,
        "remaining_ambiguous": result["summary"]["ambiguous"],
        "stage_statuses": dict(Counter(row.get("refinement_status") for row in refined["outcomes"]))}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous-review", type=Path, required=True)
    parser.add_argument("--fine-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    parent, output = args.previous_review.resolve(), args.output.resolve()
    prior_summary = json.loads((parent / "summary.json").read_text(encoding="utf-8"))
    for name, digest in prior_summary["artifacts"].items():
        if Path(name).name != name or sha256_file(parent / name) != digest:
            raise ValueError("prior review artifact identity changed")
    previous = json.loads((parent / "barrier_labels.json").read_text(encoding="utf-8"))
    snapshots = json.loads((parent / "input_snapshots.json").read_text(encoding="utf-8"))
    store = DataVersionStore(parent / "frozen_inputs")
    snapshot_id = snapshots["raw"]["snapshot_id"]
    store.verify_snapshot(snapshot_id)
    frames = {symbol: pd.read_csv(BytesIO(store.read_file(snapshot_id, entry["file"])),
        index_col="timestamp", parse_dates=True, float_precision="round_trip")
        for symbol, entry in snapshots["input_identity"]["symbols"].items()}
    observations = json.loads(store.read_file(snapshot_id, "signal_observation.json"))
    output.mkdir(parents=True, exist_ok=False)
    now = datetime.now(timezone.utc).isoformat()
    fine, timeframe, fine_snapshot = load_fine_input(args.fine_manifest, output, now)
    costs, config = LabelCosts(**prior_summary["costs"]), BarrierConfig(**prior_summary["barriers"])
    result, stage = cascade(previous, frames, observations["candidates"], fine,
        config=config, costs=costs, fine_timeframe=timeframe, as_of=now)
    write_json(output / "barrier_labels.json", result)
    rows = [{key: row.get(key) for key in ("candidate_id", "symbol", "exit_bar", "ambiguous", "barrier",
        "resolution", "refinement_status", "available_at", "net_return_bps")} for row in result["outcomes"]]
    pd.DataFrame(rows).to_csv(output / "first_touch_results.csv", index=False)
    evidence = DataVersionStore(output / "evidence")
    snapshot = evidence.freeze_files("first-touch-cascade-evidence", {
        "previous_summary.json": parent / "summary.json", "previous_barrier_labels.json": parent / "barrier_labels.json",
        "review_barrier_cascade.py": Path(__file__), "paper_label_execution.py": ROOT / "analysis/paper_label_execution.py",
        "paper_labels.py": ROOT / "analysis/paper_labels.py"}, observed_at=now)
    historical_end = pd.Timestamp(prior_summary["historical_end"])
    original_ambiguous = {row["candidate_id"] for row in previous["outcomes"] if row.get("coarse_path_ambiguous")}
    summary = {"schema": "paper-first-touch-cascade/v1", "scope": "retrospective_path_audit",
        "observed_at": now, "parent_review": str(parent), "parent_summary_sha256": sha256_file(parent / "summary.json"),
        "historical_end": historical_end.isoformat(), "before": prior_summary["before"], "after": result["summary"],
        "stage": stage, "costs": asdict(costs), "barriers": asdict(config),
        "newly_resolved_available_in_original_history": sum(row["candidate_id"] in original_ambiguous
            and not row["ambiguous"] and pd.Timestamp(row["available_at"]) <= historical_end for row in result["outcomes"]),
        "historical_point_in_time_complete": False, "new_strategy_net_performance_claim": False,
        "fine_snapshot": fine_snapshot["snapshot_id"], "evidence_snapshot": snapshot["snapshot_id"],
        "artifacts": {name: sha256_file(output / name) for name in ("barrier_labels.json", "first_touch_results.csv")}}
    write_json(output / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
