"""Registered strategy review: immutable inputs, isolated workers, no admission changes."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BATCH = ROOT / "reports/strategy_review_20260919"


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    temporary.replace(path)


def matrix(parameters):
    """The approved 52 economically distinct configurations, before identity tags."""
    arms = {}

    def add(name, family, changes):
        values = deepcopy(parameters)
        for section, patch in changes.items():
            values.setdefault(section, {}).update(deepcopy(patch))
        key = digest(values)
        if key in arms:
            arms[key]["families"].append(family)
            arms[key]["aliases"].append(name)
        else:
            arms[key] = dict(arm=name, families=[family], aliases=[], parameters=values, parameters_sha256=key)

    add("baseline", "baseline", {})
    for stability in (2, 3, 5, 10):
        for cooldown in (0, 2):
            add(f"timing_s{stability}_c{cooldown}", "timing",
                {"state": {"stability_period": stability}, "router": {"cooldown_bars": cooldown}})
    for ablation in ("no_obv_confirmation", "no_regime_restrictions", "no_health"):
        patch = {"research": {"strategy_ablation": ablation}}
        if ablation == "no_health":
            patch["strategy_health"] = {"enabled": False}
        if ablation == "no_regime_restrictions":
            patch["routing"] = {key: "TrendBreakout" for key in parameters["routing"]}
        add(ablation, "ablation", patch)
    initial = [("structural_donchian", 2.0)] + [(mode, k) for mode in ("atr", "hybrid") for k in (1.5, 2.0, 2.5, 3.0)]
    for mode, initial_k in initial:
        for trailing in (None, 2.5, 3.0, 4.0):
            stops = dict(use_trailing_stop=trailing is not None,
                         trailing_atr_multiple=3.0 if trailing is None else trailing,
                         initial_atr_multiple=initial_k)
            # Preserve the exact legacy default as the shared baseline.
            if mode != "structural_donchian":
                stops["initial_stop_mode"] = mode
            add(f"stop_{mode}_{initial_k:g}_trail_{'off' if trailing is None else f'{trailing:g}'}", "stops", {"stops": stops})
    for name, weights in (("manual", parameters["candidate_scoring"]["weights"]),
                          ("alphabetical_control", {key: 0.0 for key in parameters["candidate_scoring"]["weights"]}),
                          ("liquidity_only", {key: float(key == "liquidity") for key in parameters["candidate_scoring"]["weights"]})):
        add("score_" + name, "scoring", {"candidate_scoring": {"enabled": True, "weights": weights}})
    for entry, exit_window in ((10, 5), (20, 10), (30, 15), (40, 20), (55, 20)):
        patch = {} if (entry, exit_window) == (20, 10) else {"research": {"trend_breakout_parameters": {"entry_window": entry, "exit_window": exit_window}}}
        add(f"donchian_{entry}_{exit_window}", "donchian", patch)
    result = list(arms.values())
    if len(result) != 52:
        raise ValueError(f"Expected exactly 52 unique configurations, got {len(result)}")
    return result


def windows(reference):
    timeline = reference["timeline"]
    return [dict(name=f"rolling_{index:02d}", start=timeline[row["test_start"]],
                 end=timeline[row["test_end"] - 1], forced=True)
            for index, row in enumerate(reference["rolling_windows"])]


def baseline_specs(reference):
    return [dict(name="main", start=reference["requested_start"], end=reference["requested_end_inclusive_utc"])] + windows(reference) + [
        dict(name=key, start=value[0], end=value[1], forced=True) for key, value in reference["segments"].items()]


def validation_specs(reference):
    start, end = reference["requested_start"], reference["requested_end_inclusive_utc"]
    rows = [dict(name=f"main_{i}") for i in (1, 2, 3)]
    rows += [dict(name=k, start=v[0], end=v[1], forced=True) for k, v in reference["segments"].items()]
    rows += [dict(name=f"cost_{m}", multiplier=m) for m in (1.5, 2, 3)]
    rows += [dict(name=f"fresh_{y}", start=f"{y}-01-01") for y in (2022, 2023, 2024, 2025)]
    rows += [dict(name="reversed_symbols", transform="reversed_symbols"),
             dict(name="prefix_2023", end="2023-12-21"), dict(name="prefix_2024", end="2024-01-06"),
             dict(name="end_exit_sensitivity", forced=True),
             dict(name="liquidity_quarter", transform="liquidity_quarter"),
             dict(name="market_outage_7d", transform="market_outage_7d")]
    rows += [dict(name=f"missing_1pct_seed_{seed}", transform="missing_one_percent", seed=seed) for seed in range(42, 62)]
    return [{"start": start, "end": end, **row} for row in rows]


def register(batch):
    import yaml
    reference = json.loads((batch / "reference_protocol.json").read_text(encoding="utf-8"))
    parameters = yaml.safe_load((batch / "baseline_source/config/params.yaml").read_text(encoding="utf-8"))
    arms = matrix(parameters)
    study = dict(schema="strategy_review_protocol/v1", registered_at=datetime.now(timezone.utc).isoformat(),
                 parameters=parameters, arms=arms, rolling_windows=windows(reference),
                 baseline_specs=baseline_specs(reference), validation_specs=validation_specs(reference),
                 reference_protocol_sha256=digest(reference), matrix_runs=572, baseline_runs=15,
                 retrospective=True, admission="paused_revalidation", missing_bar_seeds=list(range(42, 62)),
                 meta_primary={"strategy": "TrendBreakout", "direction": "long", "horizon_bars": 5},
                 public_data={"venues": ["binance", "okx"], "symbols": ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "ADA/USDT", "LTC/USDT"],
                              "timeframes": ["1d", "4h"], "start": "2022-01-01", "end": "2026-06-30", "warmup_start": "2021-09-01",
                              "recent_end_exclusive": datetime.now(timezone.utc).date().isoformat()},
                 gates={"minimum_cohorts": 30, "pf_strictly_above": 1.15, "pf_ci_lower_strictly_above": 1.0,
                        "max_drawdown_pct": 20.0, "positive_net_return": True, "positive_remove_top": [5, 10],
                        "cost_rerun_multipliers_required": [1.5, 2.0], "cost_disclosure": 3.0,
                        "positive_windows_at_least": 6, "rolling_median_positive": True,
                        "bootstrap": {"block_groups": 5, "seed": 42, "iterations": 2000}})
    path = batch / "review_protocol.json"
    if path.exists():
        raise ValueError("Protocol already registered; never overwrite after observing results")
    save(path, study)
    print(f"Registered {len(arms)} configurations, 572 matrix runs, {len(study['validation_specs'])} validation runs", flush=True)


def work_items(batch, phase):
    protocol = json.loads((batch / "review_protocol.json").read_text(encoding="utf-8"))
    if phase == "matrix":
        return [dict(spec, name=f"{arm['arm']}__{spec['name']}", arm=arm["arm"])
                for arm in protocol["arms"] for spec in protocol["rolling_windows"]]
    return protocol[phase + "_specs"]


def run_batch(batch, phase, workers=2):
    verify_frozen_inputs(batch)
    seal_path = batch / "baseline_artifact_seal.json"
    if phase == "baseline" and seal_path.exists():
        seal = json.loads(seal_path.read_text(encoding="utf-8"))
        for relative, expected in seal["artifacts"].items():
            if hashlib.sha256((batch / relative).read_bytes()).hexdigest() != expected:
                raise ValueError("Sealed baseline output changed: " + relative)
    tasks = work_items(batch, phase)
    logs = batch / "logs" / phase
    logs.mkdir(parents=True, exist_ok=True)
    source = batch / ("baseline_source" if phase == "baseline" else "revised_source")
    worker_script = (batch / "tool_snapshots/strategy_review_worker_baseline.py" if phase == "baseline"
                     else source / "scripts/strategy_review_worker.py")
    if not source.exists():
        raise ValueError("An immutable source snapshot is required")

    def launch(spec):
        selected_worker = worker_script
        amended_worker = batch / "tool_snapshots/strategy_review_worker_v2.py"
        if phase != "baseline" and amended_worker.exists() and spec.get("arm") != "baseline":
            # Operational driver repair only; immutable runtime and completed
            # baseline-arm identities are retained without relabeling hashes.
            selected_worker = amended_worker
        job_path = batch / "jobs" / phase / (spec["name"] + ".json")
        if not job_path.exists():
            save(job_path, spec)
        elif json.loads(job_path.read_text(encoding="utf-8")) != spec:
            raise ValueError("Job identity changed")
        with (logs / (spec["name"] + ".log")).open("w", encoding="utf-8") as log:
            proc = subprocess.run([sys.executable, str(selected_worker), "--batch", str(batch),
                                   "--phase", phase, "--source-root", str(source), "--job", str(job_path)],
                                  cwd=source, stdout=log, stderr=subprocess.STDOUT)
        return spec["name"], proc.returncode

    complete, failed = [], []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(launch, spec) for spec in tasks]
        for future in as_completed(futures):
            name, status = future.result()
            (complete if status == 0 else failed).append(name)
            save(batch / f"{phase}_progress.json", dict(completed=len(complete), expected=len(tasks), failed=failed,
                 last=name, updated_at=datetime.now(timezone.utc).isoformat()))
            print(f"{phase}: {len(complete)}/{len(tasks)}, failures={len(failed)}, last={name}", flush=True)
    save(batch / f"{phase}_completion.json", dict(completed=complete, failed=failed, expected=len(tasks)))
    return bool(failed)


def verify_frozen_inputs(batch):
    """Check actual bytes against the original freeze before trusting caches."""
    manifest = json.loads((batch / "baseline_manifest.json").read_text(encoding="utf-8"))
    for relative, expected in manifest["input_hashes"].items():
        actual = hashlib.sha256((batch / "frozen_inputs" / relative).read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError("Frozen input changed: " + relative)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("register", "baseline", "matrix", "validation"))
    parser.add_argument("--batch", type=Path, default=DEFAULT_BATCH)
    parser.add_argument("--workers", type=int, choices=(1, 2), default=2)
    args = parser.parse_args()
    if args.action == "register":
        register(args.batch.resolve())
        return 0
    return int(run_batch(args.batch.resolve(), args.action, args.workers))


if __name__ == "__main__":
    raise SystemExit(main())
