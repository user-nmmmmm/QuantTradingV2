"""Replay of a recorded backtest manifest, and the temporal-input restoration it needs."""

from pathlib import Path
import json
import numpy as np
import random
import sys

from backtest.engine import BacktestEngine
from core.foundation.reproducibility import capital_allocation_digest, deterministic_result_digest, load_data_snapshots, load_manifest, sha256_file
from core.market.temporal_data import temporal_policy as parse_temporal_policy

ROOT = Path(__file__).resolve().parents[1]   # repository root (this file is cli/<module>.py)


def _temporal_report_payload(engine, results, data_map):
    """Freeze the evidence needed to restore temporal semantics after CSV load."""
    payload = {**(results.get("temporal_data") or {}), "schema": "temporal-run/v1",
               "policy": vars(parse_temporal_policy(getattr(engine, "temporal_policy", None))),
               "inputs": {}}
    adapter = getattr(engine, "market_data_adapter", None)
    histories = getattr(adapter, "_temporal_histories", {})
    for symbol, versions in histories.items():
        entry = {"identity": versions.identity}
        if not versions.store:
            entry["records"] = list(versions.records)
        raw_id = data_map[symbol].attrs.get("temporal_raw_file_snapshot")
        if raw_id:
            entry["raw_file_snapshot"] = raw_id
        payload["inputs"][symbol] = entry
    financing = getattr(engine, "temporal_financing", None)
    if financing is not None:
        from signal_meta.temporal_financing import TemporalFinancing
        if isinstance(financing, TemporalFinancing):
            payload["financing_evidence"] = financing.export()
    return payload


def _restore_temporal_financing(execution, report_dir):
    """Restore the exact frozen cost revisions, never a mutable external head."""
    expected = execution.get("temporal_financing_sha256")
    if expected is None:
        return None
    from signal_meta.signal_observation_types import fingerprint
    path = Path(report_dir) / "temporal_data.json"
    if sha256_file(path) != execution.get("temporal_data_sha256"):
        raise ValueError("temporal financing report hash mismatch")
    evidence = json.loads(path.read_text(encoding="utf-8")).get("financing_evidence")
    if evidence is None or fingerprint(evidence) != expected:
        raise ValueError("frozen temporal financing evidence missing or changed")
    return evidence


def _restore_temporal_replay_inputs(data_map, execution, report_dir):
    from core.market.temporal_data import TemporalDataError, TemporalOHLCV

    # Explicit compatibility policy prevents an old manifest inheriting today's
    # config. Strict replay must restore identical evidence, never re-import it.
    policy = parse_temporal_policy(execution.get("temporal_policy") or "retrospective")
    expected = execution.get("temporal_data_sha256")
    if not expected:
        if policy.mode == "strict" or policy.store_path:
            raise TemporalDataError("recorded temporal policy has no evidence digest")
        return vars(policy)
    evidence_path = Path(report_dir) / "temporal_data.json"
    if sha256_file(evidence_path) != expected:
        raise TemporalDataError("temporal report hash mismatch")
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    if payload.get("schema") != "temporal-run/v1" or payload.get("policy") != vars(policy):
        raise TemporalDataError("temporal policy differs from recorded evidence")
    inputs = payload.get("inputs", {})
    if policy.mode == "strict" and set(inputs) != set(data_map):
        raise TemporalDataError("strict input evidence is incomplete")
    for symbol, entry in inputs.items():
        frame = data_map[symbol]
        identity = entry["identity"]
        store_path = identity.get("store_path")
        if store_path and not Path(store_path).is_dir():
            raise TemporalDataError("referenced immutable version store is missing")
        frame.attrs["temporal_identity"] = identity
        if "records" in entry:
            frame.attrs["temporal_records"] = entry["records"]
        reader = TemporalOHLCV(identity["dataset_id"], timeframe=identity["timeframe"],
                               policy={**vars(policy), "store_path": None})
        if not reader.import_identity(frame):
            raise TemporalDataError("frozen temporal records are missing")
        if entry.get("raw_file_snapshot"):
            if reader.store is None:
                raise TemporalDataError("raw CSV snapshot store is missing")
            reader.store.verify_snapshot(entry["raw_file_snapshot"])
            frame.attrs["temporal_raw_file_snapshot"] = entry["raw_file_snapshot"]
    return vars(policy)


def replay_manifest(manifest_path: str) -> int:
    """Re-run a saved manifest and compare trades/equity/report payload exactly."""

    path = Path(manifest_path).resolve()
    manifest = load_manifest(path)
    expected_code = manifest["code"]
    config_path = ROOT / "config" / "params.yaml"
    if manifest["config"]["sha256"] != sha256_file(config_path):
        print("Replay refused: current config hash differs from manifest.", file=sys.stderr)
        return 7
    snapshots = load_data_snapshots(
        path.parent / "data_inputs", manifest["data_snapshots"], verify=True
    )
    execution = manifest["execution"]
    try:
        replay_temporal_policy = _restore_temporal_replay_inputs(snapshots, execution, path.parent)
        replay_temporal_financing = _restore_temporal_financing(execution, path.parent)
    except (OSError, ValueError, KeyError) as exc:
        print(f"Replay refused: frozen temporal evidence unavailable or changed: {exc}", file=sys.stderr)
        return 7
    # An old manifest must not inherit a subsequently enabled research model.
    meta_policy = execution.get("signal_meta_layer") or {"enabled": False}
    if meta_policy.get("enabled", False) and not execution.get("signal_meta_layer_digest"):
        print("Replay refused: enabled P1 policy has no recorded research digest.", file=sys.stderr)
        return 7
    from signal_meta.signal_adaptive_types import AdaptiveEVPolicy, MetaReplayPolicy
    try:
        adaptive_policy = AdaptiveEVPolicy.from_mapping(execution.get("signal_adaptive") or {"enabled": False})
        meta_replay_policy = MetaReplayPolicy.from_mapping(execution.get("signal_meta_replay") or {"enabled": False})
    except (TypeError, ValueError) as exc:
        print(f"Replay refused: invalid P2/P3 research policy: {exc}", file=sys.stderr)
        return 7
    for name, policy in (("signal_adaptive", adaptive_policy), ("signal_meta_replay", meta_replay_policy)):
        if policy.enabled and not execution.get(f"{name}_digest"):
            print(f"Replay refused: enabled {name} policy has no recorded research digest.", file=sys.stderr)
            return 7
    if ((meta_replay_policy.enabled and not adaptive_policy.enabled)
            or (adaptive_policy.enabled and not meta_policy.get("enabled", False))):
        print("Replay refused: P2/P3 research dependencies were not recorded as enabled.", file=sys.stderr)
        return 7
    recorded_observation = execution.get("signal_observation") or {}
    if adaptive_policy.enabled and (not recorded_observation.get("enabled", False)
                                    or not execution.get("signal_observation_digest")):
        print("Replay refused: P2 requires a recorded enabled P0 policy and digest.", file=sys.stderr)
        return 7
    if (meta_replay_policy.enabled
            and meta_replay_policy.horizon_bars not in recorded_observation.get("horizons", [])):
        print("Replay refused: P3 horizon is absent from the recorded P0 horizons.", file=sys.stderr)
        return 7
    symbol_order = execution.get("data_symbol_order")
    if symbol_order is not None:
        if len(symbol_order) != len(snapshots) or set(symbol_order) != set(snapshots):
            print("Replay refused: input symbol order does not match snapshots.", file=sys.stderr)
            return 7
        # Snapshot filenames/JSON keys are canonicalised alphabetically, but
        # the original event stream and audit lists preserve input order.
        snapshots = {symbol: snapshots[symbol] for symbol in symbol_order}
    seed = int(execution["seed"])
    np.random.seed(seed)
    random.seed(seed)
    try:
        from research.ml_selection.serving import restore_selector
        candidate_selector = restore_selector(execution, snapshots, path.parent)
    except (OSError, ValueError, KeyError, TypeError, ImportError) as exc:
        print(f"Replay refused: frozen coin selector unavailable or changed: {exc}", file=sys.stderr)
        return 7
    engine = BacktestEngine(
        # Preserve the recorded numeric type too: early flat-account audit
        # rows distinguish JSON 10000 from 10000.0 in byte-exact digests.
        initial_capital=execution["capital"],
        slippage=execution.get("slippage"),
        random_slip=bool(execution.get("random_slip", False)),
        warmup_period=int(execution.get("warmup_period", 30)),
        alignment_mode=execution["alignment_mode"],
        benchmark_mode=execution["benchmark_mode"],
        benchmark_rebalance_cost_bps=float(execution["benchmark_rebalance_cost_bps"]),
        timeframe=execution["timeframe"],
        run_id=manifest["run_id"],
        account_mode=execution.get("account_mode"),
        signal_observation=execution.get("signal_observation") or {"enabled": False},
        signal_meta_layer=meta_policy,
        signal_adaptive=adaptive_policy.to_dict(),
        signal_meta_replay=meta_replay_policy.to_dict(),
        temporal_policy=replay_temporal_policy,
        temporal_financing=replay_temporal_financing,
        capital_allocation=execution.get("capital_allocation") or {"enabled": False},
        candidate_selector=candidate_selector,
    )
    result = engine.run(snapshots, routing_log_enabled=False)
    observed = deterministic_result_digest(result)
    expected = execution["result_digest"]
    research_expected = execution.get("signal_observation_digest")
    research_observed = None
    if research_expected is not None:
        from backtest.reporting.signal_observation import signal_observation_digest
        research_observed = signal_observation_digest(result.get("signal_observation"))
    meta_expected = execution.get("signal_meta_layer_digest")
    meta_observed = None
    if meta_expected is not None or result.get("signal_meta_layer") is not None:
        from backtest.reporting.signal_meta_layer import signal_meta_layer_digest
        meta_observed = signal_meta_layer_digest(result.get("signal_meta_layer"))
    additional_research = {}
    capital_expected = execution.get("capital_allocation_digest")
    capital_observed = capital_allocation_digest(result) if capital_expected is not None else None
    for name in ("signal_adaptive", "signal_meta_replay"):
        expected_digest = execution.get(f"{name}_digest")
        observed_digest = None
        if expected_digest is not None or result.get(name) is not None:
            from backtest.reporting.signal_adaptive import research_digest
            observed_digest = research_digest(result.get(name))
        additional_research[name] = (expected_digest, observed_digest)
    report = {
        "status": "passed" if (observed == expected and research_expected == research_observed
                               and meta_expected == meta_observed
                               and capital_expected == capital_observed
                               and all(pair[0] == pair[1] for pair in additional_research.values())) else "failed",
        "expected": expected,
        "observed": observed,
        "signal_observation_expected": research_expected,
        "signal_observation_observed": research_observed,
        "signal_meta_layer_expected": meta_expected,
        "signal_meta_layer_observed": meta_observed,
        "code_identity_expected": expected_code,
        "capital_allocation_expected": capital_expected,
        "capital_allocation_observed": capital_observed,
    }
    for name, (expected_digest, observed_digest) in additional_research.items():
        report[f"{name}_expected"] = expected_digest
        report[f"{name}_observed"] = observed_digest
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "passed" else 8
