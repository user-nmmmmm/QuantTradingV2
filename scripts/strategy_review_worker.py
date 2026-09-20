"""One review job, importing only its frozen implementation snapshot."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import logging
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", type=Path, required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--job", type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.source_root.resolve()))
    import numpy as np
    import pandas as pd
    from core.reproducibility import canonical_json, sha256_file
    from scripts.run_revalidation60 import load_inputs, run_one, save

    logging.disable(logging.CRITICAL)
    study = json.loads((args.batch / "review_protocol.json").read_text(encoding="utf-8"))
    reference = json.loads((args.batch / "reference_protocol.json").read_text(encoding="utf-8"))
    job = json.loads(args.job.read_text(encoding="utf-8"))
    spec = dict(job)
    parameters = deepcopy(study["parameters"])
    if args.phase == "matrix":
        arm_name = spec.pop("arm")
        parameters = deepcopy(next(arm["parameters"] for arm in study["arms"] if arm["arm"] == arm_name))
    parameters.setdefault("research", {})["entry_audit"] = True
    if args.phase != "baseline":
        parameters["research"]["experiment_id"] = f"strategy-review-20260919:{job['name']}"
    frozen = {**reference, "parameters": parameters}
    manifest_name = "baseline_manifest.json" if args.phase == "baseline" else "revised_manifest.json"
    manifest = json.loads((args.batch / manifest_name).read_text(encoding="utf-8"))
    for rel, expected in manifest["source_hashes"].items():
        if sha256_file(args.source_root / rel) != expected:
            raise ValueError("Frozen source changed: " + rel)
    identity = dict(source=manifest["source_hashes"], inputs=manifest["input_hashes"],
                    parameters=parameters, job=job, protocol_sha256=sha256_file(args.batch / "review_protocol.json"),
                    worker_sha256=sha256_file(Path(__file__)))
    identity_hash = hashlib.sha256(canonical_json(identity).encode()).hexdigest()
    destination = args.batch / (args.phase + "_results")
    folder = destination / "runs" / job["name"]
    if (folder / "summary.json").exists():
        if not (folder / "review_identity.json").exists():
            raise ValueError("Summary without completed identity; cannot trust cached run")
        old = json.loads((folder / "review_identity.json").read_text(encoding="utf-8"))
        if old["sha256"] != identity_hash:
            raise ValueError("Cached run identity mismatch")
        for rel, expected in old.get("artifacts", {}).items():
            if sha256_file(folder / rel) != expected:
                raise ValueError("Cached artifact checksum mismatch: " + rel)
        print("VERIFIED CACHE " + job["name"])
        return 0
    frames, _, _ = load_inputs(args.batch / "frozen_inputs")
    transform = spec.pop("transform", None)
    seed = spec.pop("seed", 42)
    if transform == "reversed_symbols":
        frames = dict(reversed(list(frames.items())))
    elif transform:
        rng = np.random.default_rng(seed)
        changed = {}
        for symbol, original in frames.items():
            frame = original.copy()
            if transform == "liquidity_quarter":
                frame["volume"] *= .25
            elif transform == "missing_one_percent":
                remove = (rng.random(len(frame)) < .01) & ~frame["scheduled_exit"].to_numpy(dtype=bool)
                frame = frame.loc[~remove].copy()
            elif transform == "market_outage_7d":
                frame = frame.loc[~((frame.index >= "2023-12-23") & (frame.index <= "2023-12-29"))].copy()
            else:
                raise ValueError("Unknown transform")
            changed[symbol] = frame
        frames = changed
    result, summary = run_one(root=destination, all_frames=frames, frozen=frozen, **spec)
    if args.phase != "baseline":
        from analysis.strategy_review import cohort_evidence, evaluate_gates
        from analysis.strategy_review_diagnostics import write_review_diagnostics
        evidence = cohort_evidence(result["close_event_records"], mark_to_market=not spec.get("forced", False))
        save(folder / "review_cohort_evidence.json", evidence)
        save(folder / "review_research_gates.json", evaluate_gates(summary, evidence))
        write_review_diagnostics(folder, frames, result, parameters)
    artifacts = {p.name: sha256_file(p) for p in folder.iterdir() if p.is_file() and p.name != "review_identity.json"}
    save(folder / "review_identity.json", {"sha256": identity_hash, "identity": identity,
                                           "artifacts": artifacts,
                                           "resolved_initial_stop_mode": parameters.get("stops", {}).get("initial_stop_mode") or
                                           ("hybrid" if parameters["stops"].get("use_atr_initial_stop") else "structural_donchian")})
    print(f"VERIFIED {job['name']}: {summary['return_pct']:.5f}%", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
