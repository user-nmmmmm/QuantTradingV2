"""Expanded fixed-policy P0/P1/P2/P3 evidence, with an independent quarter-size control."""
from __future__ import annotations

import argparse
from copy import deepcopy
import gc
import hashlib
import json
import logging
from pathlib import Path
import pickle
import sys


def required_replay_checks(components):
    """Every required replay must retain its complete three-account result.

    An inactive gate remains a valid engineering result, but incomplete or
    absent primary/control accounts must not disappear from batch acceptance.
    Research reporters deliberately return incomplete payloads instead of
    raising, so the driver must check their status and metric validity.
    """
    checks = {}
    expected = {"baseline", "gate", "sizing"}
    for name in ("p3", "p3_primary", "p3_fixed_quarter"):
        summary = components.get(name) or {}
        accounts = summary.get("accounts") or []
        arms = [row.get("arm") for row in accounts]
        checks[name] = {
            "report_complete": summary.get("status") == "complete" and not summary.get("errors"),
            "account_partition_complete": len(arms) == len(expected) and set(arms) == expected,
            "account_metrics_valid": bool(accounts) and all(row.get("metrics_valid") is True for row in accounts),
        }
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, required=True)
    args = parser.parse_args()
    batch = args.batch.resolve()
    source = batch / "revised_source"
    sys.path.insert(0, str(source))
    import numpy as np
    import pandas as pd
    from config.config import config
    from backtest.engine import BacktestEngine
    from backtest.signal_meta_replay import replay_signal_meta
    from backtest.reporting.signal_observation import write_signal_observation_report
    from backtest.reporting.signal_meta_layer import write_signal_meta_layer_report
    from backtest.reporting.signal_adaptive import write_signal_adaptive_report, write_signal_meta_replay_report
    from core.market_data import HistoricalMarketDataAdapter
    from core.reproducibility import canonical_json, sha256_file, deterministic_result_digest
    from core.signal_adaptive_types import MetaReplayPolicy
    from core.signal_observation_types import fingerprint
    from scripts.run_revalidation60 import load_inputs

    logging.disable(logging.CRITICAL)
    output = batch / "meta_review"
    output.mkdir(exist_ok=True)
    protocol = json.loads((batch / "review_protocol.json").read_text(encoding="utf-8"))
    manifest = json.loads((batch / "revised_manifest.json").read_text(encoding="utf-8"))
    for rel, expected in manifest["input_hashes"].items():
        if sha256_file(batch / "frozen_inputs" / rel) != expected:
            raise ValueError("Frozen input changed: " + rel)
    for rel, expected in manifest["source_hashes"].items():
        if sha256_file(source / rel) != expected:
            raise ValueError("Frozen implementation changed")
    identity = dict(source_hashes=manifest["source_hashes"], input_hashes=manifest["input_hashes"],
                    protocol=sha256_file(batch / "review_protocol.json"), runner=sha256_file(Path(__file__)))
    identity_path = output / "identity.json"
    if identity_path.exists() and json.loads(identity_path.read_text(encoding="utf-8")) != identity:
        raise ValueError("Meta cache identity mismatch")
    identity_path.write_text(canonical_json(identity) + "\n", encoding="utf-8")

    def save(name, payload):
        (output / name).write_text(canonical_json(payload) + "\n", encoding="utf-8")

    def stage(name, build):
        path = output / (name + ".pickle")
        if path.exists():
            # Only this batch's locally generated, identity-checked caches are read.
            checksum = path.with_suffix(".sha256").read_text(encoding="ascii").strip()
            if hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
                raise ValueError("Meta stage cache checksum mismatch: " + name)
            with path.open("rb") as stream:
                return pickle.load(stream)
        print("START " + name, flush=True)
        payload = build()
        temporary = path.with_suffix(".pending")
        with temporary.open("wb") as stream:
            pickle.dump(payload, stream, protocol=pickle.HIGHEST_PROTOCOL)
        temporary.replace(path)
        path.with_suffix(".sha256").write_text(hashlib.sha256(path.read_bytes()).hexdigest() + "\n", encoding="ascii")
        save("progress.json", dict(last_completed=name))
        print("DONE " + name, flush=True)
        return payload

    config._config = deepcopy(protocol["parameters"])
    frames, _, _ = load_inputs(batch / "frozen_inputs")

    def official(observe):
        np.random.seed(42)
        import random
        random.seed(42)
        engine = BacktestEngine(initial_capital=10000, timeframe="1d", alignment_mode="union", benchmark_mode="fixed",
                               run_id="strategy-review-meta", signal_observation={"enabled": observe},
                               signal_meta_layer={"enabled": observe}, signal_adaptive={"enabled": observe},
                               signal_meta_replay={"enabled": observe})
        result = engine.run(frames, routing_log_enabled=False)
        return dict(digest=deterministic_result_digest(result), health=result["strategy_health"],
                    allocation=result["allocation_audit"], p0=result["signal_observation"],
                    p1=result["signal_meta_layer"], p2=result["signal_adaptive"], p3=result["signal_meta_replay"])

    off = stage("official_off", lambda: official(False))
    on = stage("official_p0", lambda: official(True))
    isolation = dict(digest=off["digest"] == on["digest"], health=off["health"] == on["health"],
                     allocation=off["allocation"] == on["allocation"])
    save("official_isolation.json", {**isolation, "scope": "full_60_coin_history_real_engine_all_P0_P1_P2_P3_switches"})
    if not all(isolation.values()):
        raise ValueError("Passive observation changed official account")
    p0 = on["p0"]
    integrated = {name: on[name] for name in ("p1", "p2", "p3")}
    del on, off
    gc.collect()
    p0_summary = write_signal_observation_report(p0, output)
    # Persist the actual integrated-engine payloads. They are not rebuilt with
    # different inputs, templates or policies after the isolation comparison.
    p1 = stage("p1", lambda payloads=integrated: payloads.pop("p1"))
    p1_summary = write_signal_meta_layer_report(p1, output)
    p2 = stage("p2", lambda payloads=integrated: payloads.pop("p2"))
    p2_summary = write_signal_adaptive_report(p2, output, p1_payload=p1)
    del p1
    gc.collect()
    # A fresh one-bar engine supplies execution settings only; clone_research_broker
    # creates each account from initial cash, without importing any positions.
    template = BacktestEngine(initial_capital=10000, timeframe="1d", alignment_mode="union", benchmark_mode="fixed",
                             signal_observation={"enabled": False}, signal_meta_layer={"enabled": False},
                             signal_adaptive={"enabled": False}, signal_meta_replay={"enabled": False})
    template.run({symbol: frame.head(1) for symbol, frame in frames.items()}, routing_log_enabled=False)
    broker = template.execution_adapter.broker
    market = HistoricalMarketDataAdapter(frames, timeframe="1d", alignment_mode="union")
    p3 = stage("p3_all", lambda payloads=integrated: payloads.pop("p3"))
    p3_summary = write_signal_meta_replay_report(p3, output)
    del integrated
    gc.collect()

    # Predeclared strategy/direction subset of the already frozen predictions.
    # No label, realised return, execution flag or model decision selects membership.
    primary_p0 = {**p0, "candidates": [c for c in p0["candidates"] if c["strategy"] == "TrendBreakout" and c["direction"] == "long"]}
    ids = {c["candidate_id"] for c in primary_p0["candidates"]}
    primary_p0["decisions"] = [d for d in p0["decisions"] if d["candidate_id"] in ids]
    primary_p0["outcomes"] = [r for r in p0["outcomes"] if r["candidate_id"] in ids]
    primary_p2 = {**p2, "predictions": [p for p in p2["predictions"] if p["candidate_id"] in ids],
                  "input_identity": {**p2.get("input_identity", {}),
                    "candidates_sha256": fingerprint(sorted(primary_p0["candidates"], key=lambda c: c["candidate_id"]))}}
    primary = stage("p3_primary", lambda: replay_signal_meta(market, primary_p0, primary_p2, broker, MetaReplayPolicy(enabled=True)))
    quarter = stage("p3_fixed_quarter", lambda: replay_signal_meta(market, primary_p0, primary_p2, broker,
                    MetaReplayPolicy(enabled=True, reference_notional=250.0)))
    primary_dir, quarter_dir = output / "primary", output / "quarter_control"
    primary_dir.mkdir(exist_ok=True); quarter_dir.mkdir(exist_ok=True)
    primary_summary = write_signal_meta_replay_report(primary, primary_dir)
    quarter_summary = write_signal_meta_replay_report(quarter, quarter_dir)
    accounts = [dict(row, comparison_scope="TrendBreakout_long_h5") for row in primary_summary.get("accounts", [])]
    accounts += [dict(row, arm="fixed_quarter", comparison_scope="TrendBreakout_long_h5")
                 for row in quarter_summary.get("accounts", []) if row["arm"] == "baseline"]
    pd.DataFrame(accounts).to_csv(output / "primary_policy_comparison.csv", index=False)
    coverage = []
    predictions = pd.DataFrame(p2.get("predictions", []))
    if not predictions.empty:
        for keys, rows in predictions.groupby(["strategy", "direction", "horizon_bars", "fold_id"], dropna=False):
            coverage.append(dict(zip(("strategy", "direction", "horizon_bars", "fold_id"), keys)) |
                            dict(count=len(rows), **{s: int(rows.status.eq(s).sum()) for s in ("allow", "veto", "abstain")}))
    pd.DataFrame(coverage).to_csv(output / "support_by_book_fold.csv", index=False)
    components = dict(p0=p0_summary, p1=p1_summary, p2=p2_summary, p3=p3_summary,
                      p3_primary=primary_summary, p3_fixed_quarter=quarter_summary)
    replay_checks = required_replay_checks(components)
    complete = (all(isolation.values())
                and all(s.get("status") == "complete" and not s.get("errors") for s in components.values())
                and all(all(checks.values()) for checks in replay_checks.values()))
    save("acceptance.json", dict(engineering_isolation=all(isolation.values()),
        components={name: {"status": s.get("status"), "errors": s.get("errors", [])} for name, s in components.items()},
        required_replay_checks=replay_checks,
        primary_accounts=accounts, status="complete" if complete else "incomplete",
        evidence="retrospective_diagnostic_only", admission="paused_revalidation",
        interpretation="Trade support and equal-capital quarter-size controls must be inspected; no automatic causal/profit claim",
        primary_selector=protocol["meta_primary"], quarter_notional=250.0, parameters_relaxed=False))
    print("Meta evidence saved: " + str(output), flush=True)


if __name__ == "__main__":
    main()
