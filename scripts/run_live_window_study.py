"""SR-DATA-001: do rolling live windows reproduce backtest states and entry candidates?

Protocol: docs/research/live_window_study_20261110.md. Compares bars of
train60 and validation20 only; every frame is cut at the validation20 end
before anything is computed, so final20 is never read. Changes no config.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import json
import logging
import math
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

import pandas as pd

from composition.factory import build_state_machine, build_strategy_registry
from config.config import config
from core.foundation.reproducibility import sha256_file, sha256_frame
from core.trading.state import MarketState
from scripts.run_revalidation60 import load_inputs, save, source_hashes

ROOT = Path(__file__).resolve().parents[1]

EXPERIMENT_ID = "SR-DATA-001"
PROTOCOL = ROOT / "docs/research/live_window_study_20261110.md"
WINDOWS = (100, 200, 400)
COMPARED_SEGMENTS = ("train60", "validation20")
FORBIDDEN_WINDOW = "final20"
MAX_STATE_MISMATCH_RATE = 0.001
MAX_CANDIDATE_DIFF_SHARE = 0.01
STRATEGY = "TrendBreakout"


def _observe(machine, strategy, symbol: str, frame: pd.DataFrame, i: int,
             states: Optional[pd.Series] = None) -> Tuple[str, Optional[float]]:
    """State name at ``i`` and the candidate score (``None`` when not a candidate)."""
    state = (states if states is not None else machine.calculate_states(frame)).iloc[i]
    if state is not MarketState.TREND_UP:
        return state.name, None
    signal = strategy.raw_entry_signal(symbol, i, frame)
    if not signal:
        return state.name, None
    score = signal.get("score")
    return state.name, float(score) if score is not None and math.isfinite(float(score)) else 0.0


def compare_symbol(symbol: str, frame: pd.DataFrame, compare_from: Any, compare_to: Any,
                   windows: Iterable[int]) -> Dict[str, Any]:
    """Full-history versus trailing-window states and candidates for one symbol."""
    logging.disable(logging.CRITICAL)
    machine = build_state_machine(config)
    strategy = build_strategy_registry(config)[STRATEGY]
    frame = frame.loc[:compare_to, ["open", "high", "low", "close", "volume"]].copy()
    full = frame.copy()
    full_states = machine.calculate_states(full)
    positions = [i for i, stamp in enumerate(frame.index) if stamp >= compare_from]
    reference = {i: _observe(machine, strategy, symbol, full, i, full_states) for i in positions}
    out: Dict[str, Any] = {}
    for w in windows:
        compared = state_diff = 0
        ref_candidates = win_candidates = only_ref = only_win = 0
        max_score_diff = 0.0
        mismatches = []
        for i in positions:
            if i + 1 < w:
                continue
            window = frame.iloc[i + 1 - w:i + 1].copy()
            state, score = _observe(machine, strategy, symbol, window, w - 1)
            ref_state, ref_score = reference[i]
            compared += 1
            if state != ref_state:
                state_diff += 1
            ref_candidates += ref_score is not None
            win_candidates += score is not None
            if (ref_score is None) != (score is None):
                only_ref += ref_score is not None
                only_win += score is not None
                mismatches.append({"time": str(frame.index[i]), "reference": ref_state,
                                   "window": state, "candidate_in": "reference" if score is None else "window"})
            elif score is not None:
                max_score_diff = max(max_score_diff, abs(score - ref_score))
        out[str(w)] = {"compared_bars": compared, "state_mismatches": state_diff,
                       "reference_candidates": ref_candidates, "window_candidates": win_candidates,
                       "only_reference": only_ref, "only_window": only_win,
                       "max_score_abs_diff": max_score_diff, "candidate_mismatches": mismatches}
    return out


def aggregate(per_symbol: Dict[str, Dict[str, Any]], windows: Iterable[int]) -> Dict[str, Any]:
    totals = {}
    for w in windows:
        rows = [symbol_result[str(w)] for symbol_result in per_symbol.values()]
        compared = sum(r["compared_bars"] for r in rows)
        state = sum(r["state_mismatches"] for r in rows)
        reference = sum(r["reference_candidates"] for r in rows)
        symmetric = sum(r["only_reference"] + r["only_window"] for r in rows)
        totals[str(w)] = {"compared_bars": compared, "state_mismatches": state,
                          "state_mismatch_rate": state / compared if compared else 0.0,
                          "reference_candidates": reference,
                          "window_candidates": sum(r["window_candidates"] for r in rows),
                          "candidate_symmetric_diff": symmetric,
                          "max_score_abs_diff": max((r["max_score_abs_diff"] for r in rows), default=0.0),
                          "symbols_with_state_mismatch": sorted(
                              s for s, res in per_symbol.items() if res[str(w)]["state_mismatches"])}
    return totals


def verdict(totals: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Protocol section 3: smallest W within both limits, else full-history seeding."""
    checks = {}
    for w, row in totals.items():
        allowed = math.floor(MAX_CANDIDATE_DIFF_SHARE * row["reference_candidates"])
        checks[w] = {"state_rate_ok": row["state_mismatch_rate"] <= MAX_STATE_MISMATCH_RATE,
                     "candidate_diff_ok": row["candidate_symmetric_diff"] <= allowed,
                     "allowed_candidate_diff": allowed}
    passing = sorted(int(w) for w, c in checks.items() if c["state_rate_ok"] and c["candidate_diff_ok"])
    if passing:
        chosen = passing[0]
        return {"checks": checks, "selected_closed_bars": chosen, "live_lookback": chosen + 1,
                "conclusion": "set_live_lookback_in_stage_C2"}
    return {"checks": checks, "selected_closed_bars": None, "live_lookback": None,
            "conclusion": "rolling_window_insufficient_seed_from_full_history"}


def _job(args):
    return args[0], compare_symbol(*args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "reports/live_window_study_20261110")
    parser.add_argument("--review-root", type=Path, default=ROOT / "reports/strategy_review_20260919",
                        help="folder holding frozen_inputs/ and reference_protocol.json")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    if FORBIDDEN_WINDOW in COMPARED_SEGMENTS:
        raise ValueError("final holdout must not be used")
    inputs = args.review_root / "frozen_inputs"
    reference = json.loads((args.review_root / "reference_protocol.json").read_text(encoding="utf-8"))
    segments = reference["segments"]
    compare_from = pd.Timestamp(segments[COMPARED_SEGMENTS[0]][0]).tz_localize(None)
    compare_to = pd.Timestamp(segments[COMPARED_SEGMENTS[-1]][1]).tz_localize(None)
    frames, _, _ = load_inputs(inputs)
    frames = {s: f.loc[:compare_to] for s, f in frames.items() if not f.loc[compare_from:compare_to].empty}
    identity = {"schema": "live-window-study/v1", "experiment_id": EXPERIMENT_ID,
                "protocol_sha256": sha256_file(PROTOCOL), "source_hashes": source_hashes(),
                "config_sha256": sha256_file(ROOT / "config/params.yaml"),
                "data_manifest_sha256": sha256_file(inputs / "data_manifest.json"),
                "truncated_input_hashes": {s: sha256_frame(f) for s, f in frames.items()},
                "compared": [str(compare_from), str(compare_to)], "windows": list(WINDOWS),
                "forbidden_window": FORBIDDEN_WINDOW,
                "rule": {"max_state_mismatch_rate": MAX_STATE_MISMATCH_RATE,
                         "max_candidate_diff_share": MAX_CANDIDATE_DIFF_SHARE}}
    args.output.mkdir(parents=True, exist_ok=True)
    manifest = args.output / "registration.json"
    if manifest.exists():
        if json.loads(manifest.read_text(encoding="utf-8")) != json.loads(json.dumps(identity)):
            raise ValueError("Protocol/source/config/data identity changed; use a new study output directory")
    else:
        save(manifest, identity)
    if (args.output / "result.json").exists():
        raise ValueError("The registered comparison is single-use and already recorded")
    jobs = [(s, f, compare_from, compare_to, WINDOWS) for s, f in sorted(frames.items())]
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        per_symbol = dict(pool.map(_job, jobs))
    if source_hashes() != identity["source_hashes"] or sha256_file(PROTOCOL) != identity["protocol_sha256"]:
        raise ValueError("Source/protocol changed during study; results cannot be compared")
    totals = aggregate(per_symbol, WINDOWS)
    result = {"totals": totals, "verdict": verdict(totals)}
    save(args.output / "per_symbol.json", per_symbol)
    save(args.output / "result.json", result)
    print(json.dumps({w: {k: v for k, v in row.items() if k != "symbols_with_state_mismatch"}
                      for w, row in totals.items()}, indent=2), flush=True)
    print(json.dumps(result["verdict"], indent=2), flush=True)


if __name__ == "__main__":
    main()
