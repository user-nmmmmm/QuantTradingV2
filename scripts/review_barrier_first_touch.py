"""Review frozen historical ambiguous labels using newly observed fine bars.

This is a retrospective path audit. The new labels retain the fine feed's real
availability, so they cannot enter training at the old historical cutoff.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

import pandas as pd

from analysis.paper_label_execution import refine_barrier_labels
from analysis.paper_labels import BarrierConfig, LabelCosts
from analysis.paper_study import write_json
from core.reproducibility import sha256_file
from scripts.run_paper_roadmap import freeze_inputs, load_fine_input, validate_frozen_observations


def review(frames, candidates, fine, *, fine_timeframe, observed_at, config=None, costs=None):
    config, costs = config or BarrierConfig(), costs or LabelCosts()
    historical_end = max(pd.to_datetime(frame.index, utc=True).max() for frame in frames.values()) + pd.Timedelta(config.timeframe)
    previous = refine_barrier_labels(frames, candidates, config=config, costs=costs, as_of=historical_end)
    reviewed = refine_barrier_labels(frames, candidates, config=config, costs=costs,
        as_of=observed_at, fine_frames=fine, fine_timeframe=fine_timeframe)
    original = {row["candidate_id"]: row for row in previous["outcomes"]}
    changes = []
    for row in reviewed["outcomes"]:
        before = original[row["candidate_id"]]
        if not before["ambiguous"]:
            continue
        changes.append({"candidate_id": row["candidate_id"], "symbol": row["symbol"],
            "exit_bar": row["exit_bar"], "status": row.get("refinement_status"),
            "previous_barrier": before["barrier"], "reviewed_barrier": row["barrier"],
            "previous_net_bps": before["net_return_bps"], "reviewed_net_bps": row["net_return_bps"],
            "available_at": row["available_at"], "ambiguous": row["ambiguous"],
            "usable_at_historical_end": bool(not row["ambiguous"] and pd.Timestamp(row["available_at"]) <= historical_end)})
    summary = {"schema": "paper-first-touch-review/v1", "scope": "retrospective_path_audit",
        "observed_at": observed_at, "historical_end": historical_end.isoformat(),
        "before": previous["summary"], "after": reviewed["summary"],
        "refinement_statuses": dict(Counter(row["status"] for row in changes)),
        "barrier_changed": sum(row["previous_barrier"] != row["reviewed_barrier"] for row in changes),
        "newly_resolved_available_in_original_history": sum(row["usable_at_historical_end"] for row in changes),
        "historical_point_in_time_complete": False, "new_strategy_net_performance_claim": False,
        "costs": asdict(costs), "barriers": asdict(config),
        "limitation": "finer OHLC establishes interval first-touch order, not tick-level fills or execution costs"}
    return reviewed, changes, summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=ROOT / "data/binance/1d/_manifest.json")
    parser.add_argument("--observations", type=Path, default=ROOT / "reports/paper_applications_20261003_v3/signal_observation.json")
    parser.add_argument("--fine-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    now = datetime.now(timezone.utc).isoformat()
    frames, observations, snapshots, _ = freeze_inputs(args.manifest, args.observations, output, now)
    validation = validate_frozen_observations(frames, observations)
    fine, timeframe, fine_snapshot = load_fine_input(args.fine_manifest, output, now)
    reviewed, changes, summary = review(frames, observations["candidates"], fine,
        fine_timeframe=timeframe, observed_at=now,
        costs=LabelCosts(commission_bps_per_side=10, slippage_bps_per_side=5, spread_bps_per_side=1))
    write_json(output / "barrier_labels.json", reviewed)
    pd.DataFrame(changes).to_csv(output / "first_touch_changes.csv", index=False)
    summary.update(input_snapshot=snapshots["raw"]["snapshot_id"], fine_snapshot=fine_snapshot["snapshot_id"],
        validation=validation, artifacts={name: sha256_file(output / name) for name in (
            "barrier_labels.json", "first_touch_changes.csv", "input_snapshots.json")},
        implementation={name: sha256_file(ROOT / name) for name in (
            "scripts/review_barrier_first_touch.py", "analysis/paper_label_execution.py", "analysis/paper_labels.py")})
    write_json(output / "summary.json", summary)
    print(__import__("json").dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
