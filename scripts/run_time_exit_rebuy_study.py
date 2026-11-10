"""SR-EXIT-001: count TrendBreakout re-entries right after a MaxHoldingPeriod exit.

Protocol: docs/research/time_exit_rebuy_study_20261110.md. Runs the default
configuration on train60 and validation20 only; final20 is refused. Counting
study: it selects nothing and changes no configuration.
"""
from __future__ import annotations

import argparse
import ast
from copy import deepcopy
import json
import logging
from pathlib import Path
from typing import Any, Dict, List

import pandas as pd

from config.config import config
from core.foundation.reproducibility import sha256_file, sha256_frame
from scripts.run_revalidation60 import load_inputs, run_one, save, source_hashes

ROOT = Path(__file__).resolve().parents[1]

EXPERIMENT_ID = "SR-EXIT-001"
PROTOCOL = ROOT / "docs/research/time_exit_rebuy_study_20261110.md"
WINDOWS = ("train60", "validation20")
FORBIDDEN_WINDOW = "final20"
PRIMARY_K, REPORTED_K = 3, (1, 3, 5)
MAX_COUNT, MAX_IMPACT_SHARE, CAPITAL = 3, 0.0025, 10_000.0
TIME_EXIT, STRATEGY = "MaxHoldingPeriod", "TrendBreakout"


def modeled_cost(value: Any) -> float:
    """``costs.modeled_total`` from a trades.csv cell (a dict literal or a dict)."""
    if isinstance(value, dict):
        costs = value
    elif isinstance(value, str) and value.strip():
        costs = ast.literal_eval(value)
    else:
        return 0.0
    return float(costs.get("modeled_total") or 0.0)


def rebuy_pairs(trades: pd.DataFrame, k: int) -> List[Dict[str, Any]]:
    """Pair each time-exit sell with the first strategy buy of that symbol within ``k`` days."""
    if trades.empty:
        return []
    frame = trades.copy()
    frame["fill_time"] = pd.to_datetime(frame["fill_time"])
    exits = frame[(frame["exit_reason"] == TIME_EXIT) & (frame["side"] == "sell")]
    buys = frame[(frame["strategy_id"] == STRATEGY) & (frame["side"] == "buy")]
    pairs = []
    for _, sold in exits.sort_values("fill_time").iterrows():
        window = buys[(buys["symbol"] == sold["symbol"])
                      & (buys["fill_time"] > sold["fill_time"])
                      & (buys["fill_time"] <= sold["fill_time"] + pd.Timedelta(days=k))]
        if window.empty:
            continue
        bought = window.sort_values("fill_time").iloc[0]
        cost = modeled_cost(sold.get("costs")) + modeled_cost(bought.get("costs"))
        gap = (float(bought["fill_price"]) - float(sold["fill_price"])) * float(bought["qty"])
        pairs.append({"symbol": sold["symbol"], "exit_fill_time": str(sold["fill_time"]),
                      "rebuy_fill_time": str(bought["fill_time"]),
                      "days": (bought["fill_time"] - sold["fill_time"]).days,
                      "round_trip_cost": cost, "price_gap": gap, "impact": cost + gap})
    return pairs


def summarize(trades: pd.DataFrame) -> Dict[str, Any]:
    time_exits = int(((trades["exit_reason"] == TIME_EXIT) & (trades["side"] == "sell")).sum()) if len(trades) else 0
    by_k = {}
    for k in REPORTED_K:
        pairs = rebuy_pairs(trades, k)
        by_k[str(k)] = {"count": len(pairs), "impact_total": float(sum(p["impact"] for p in pairs)),
                        "round_trip_cost_total": float(sum(p["round_trip_cost"] for p in pairs)),
                        "price_gap_total": float(sum(p["price_gap"] for p in pairs)), "pairs": pairs}
    return {"time_exits": time_exits, "by_k": by_k}


def verdict(summaries: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Apply protocol section 3: negligible only if every window passes both limits."""
    limit = MAX_IMPACT_SHARE * CAPITAL
    checks = {}
    for window, summary in summaries.items():
        primary = summary["by_k"][str(PRIMARY_K)]
        checks[window] = {"count_le_limit": primary["count"] <= MAX_COUNT,
                          "impact_le_limit": primary["impact_total"] <= limit}
    negligible = all(all(window.values()) for window in checks.values())
    return {"primary_k": PRIMARY_K, "max_count": MAX_COUNT, "max_impact": limit, "checks": checks,
            "negligible": negligible,
            "conclusion": "close_F06_retain_current" if negligible else "candidate_for_stage_C"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "reports/time_exit_rebuy_study_20261110")
    parser.add_argument("--review-root", type=Path, default=ROOT / "reports/strategy_review_20260919",
                        help="folder holding frozen_inputs/ and reference_protocol.json")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    if FORBIDDEN_WINDOW in WINDOWS:
        raise ValueError("final holdout must not be used")
    inputs = args.review_root / "frozen_inputs"
    reference = json.loads((args.review_root / "reference_protocol.json").read_text(encoding="utf-8"))
    frames, _, _ = load_inputs(inputs)
    params = deepcopy(config._config)
    params.setdefault("research", {}).update(entry_audit=True, experiment_id=EXPERIMENT_ID)
    identity = {"schema": "time-exit-rebuy-study/v1", "experiment_id": EXPERIMENT_ID,
                "protocol_sha256": sha256_file(PROTOCOL), "source_hashes": source_hashes(),
                "config_sha256": sha256_file(ROOT / "config/params.yaml"),
                "data_manifest_sha256": sha256_file(inputs / "data_manifest.json"),
                "engine_input_hashes": {s: sha256_frame(f) for s, f in frames.items()},
                "windows": {w: reference["segments"][w] for w in WINDOWS},
                "forbidden_window": FORBIDDEN_WINDOW, "forced_end_liquidation": True,
                "rule": {"primary_k": PRIMARY_K, "reported_k": list(REPORTED_K), "max_count": MAX_COUNT,
                         "max_impact_share": MAX_IMPACT_SHARE, "capital": CAPITAL}}
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = args.output / "registration.json"
    if manifest.exists():
        if json.loads(manifest.read_text(encoding="utf-8")) != json.loads(json.dumps(identity)):
            raise ValueError("Protocol/source/config/data identity changed; use a new study output directory")
    else:
        save(manifest, identity)
    if (args.output / "result.json").exists():
        raise ValueError("The registered count is single-use and already recorded")

    summaries = {}
    for window in WINDOWS:
        start, end = reference["segments"][window]
        run_one(f"baseline_{window}", args.output, frames, {**reference, "parameters": params},
                start=start, end=end, forced=True)
        trades = pd.read_csv(args.output / "runs" / f"baseline_{window}" / "trades.csv")
        summaries[window] = summarize(trades)
        primary = summaries[window]["by_k"][str(PRIMARY_K)]
        print(f"{window}: time exits={summaries[window]['time_exits']} "
              f"rebuys(K={PRIMARY_K})={primary['count']} impact={primary['impact_total']:.2f}", flush=True)
    if source_hashes() != identity["source_hashes"] or sha256_file(PROTOCOL) != identity["protocol_sha256"]:
        raise ValueError("Source/protocol changed during study; results cannot be compared")
    result = {"summaries": summaries, "verdict": verdict(summaries)}
    save(args.output / "result.json", result)
    print(json.dumps(result["verdict"], ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
