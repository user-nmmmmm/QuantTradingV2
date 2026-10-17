"""Build the frozen coin selector from its package for explicitly enabled backtests.

Moved from ``backtest.coin_selector`` (B3, ADR 0017): constructing the selector needs the research feature
builder, models and selector, and ``backtest`` must not import ``research``. The package contract itself
(``read_bundle``, account compatibility, snapshot, report) stays in ``backtest.coin_selector``.
Features are rebuilt from this backtest's own candles without building future labels.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import time

from backtest.coin_selector import _account_compatibility, read_bundle_snapshot
from core.foundation.reproducibility import sha256_file
from research.ml_selection.dataset import build_inference_dataset, FEATURE_COLUMNS
from research.ml_selection.models import load_model_bytes
from research.ml_selection.selector import ResearchSelector


def create_selector(frames, *, initial_capital, bundle_path=None, account_mode=None):
    """Return deterministic serving and a JSON-safe per-run identity."""
    started = time.monotonic()
    stage_started = time.monotonic()
    captured = read_bundle_snapshot(bundle_path)
    path, package = captured.path, captured.package
    timings = {"bundle_validation_seconds": time.monotonic() - stage_started}
    stage_started = time.monotonic()
    model = load_model_bytes(captured.artifact_bytes[package["files"]["model"]["path"]])
    policy = load_model_bytes(captured.artifact_bytes[package["files"]["policy"]["path"]])
    if model.kind != "lightgbm" or policy.kind != "bernoulli_policy":
        raise ValueError("Frozen selector requires its LightGBM parent and RL policy")
    if (model.model_id != package["candidate"]["parent_model_id"]
            or policy.model_id != package["candidate"]["model_id"]):
        raise ValueError("Loaded selector identity differs from the frozen candidate")
    account_contract = _account_compatibility(package, model, policy, account_mode)
    timings["model_load_seconds"] = time.monotonic() - stage_started
    stage_started = time.monotonic()
    # The shared research builder preserves availability and liquidity rules;
    # Future labels are not built at all; maturity cannot become an entry gate.
    dataset = build_inference_dataset(frames, **package["dataset"])
    timings["feature_build_seconds"] = time.monotonic() - stage_started
    stage_started = time.monotonic()
    selector = ResearchSelector(dataset, mode="policy", model=model, policy=policy,
        deterministic=True, initial_capital=initial_capital, **package["selection"])
    if selector.policy_threshold != policy.metadata["evaluation_threshold"]:
        raise ValueError("Frozen selector threshold differs from model metadata")
    timings["selector_initialization_seconds"] = time.monotonic() - stage_started
    data_identity = deepcopy(dataset.attrs["data_identity"])
    identity = {
        "schema": "backtest-coin-selector/v1", "enabled": True,
        "candidate": "rl", "model_id": policy.model_id, "parent_model_id": model.model_id,
        "policy_threshold": selector.policy_threshold, "deterministic": True,
        "gate_contract": selector.gate_contract,
        "gate_contract_source": selector.gate_contract_source,
        "policy_gate_mode": selector.policy_gate_mode,
        "selector_contract": deepcopy(selector.selector_contract),
        "capital_score_source": selector.capital_score_source,
        "probability_semantics": "policy_action_probability_not_profit_probability",
        "account_contract": account_contract,
        "deployment_account_mode": account_mode,
        "training_account_mode": account_contract["training_account_mode"],
        "bundle_path": str(path), "bundle_sha256": captured.sha256,
        "source_protocol_id": package["source_protocol_id"],
        "model_files": deepcopy(package["files"]),
        "training_end_exclusive": package["training_end_exclusive"],
        "validation_end_exclusive": package["validation_end_exclusive"],
        "training_latest_label_available_at": model.metadata.get("train_latest_label_available_at"),
        "training_latest_decision_at": model.metadata.get("train_max_as_of"),
        "validation_latest_decision_at": model.metadata.get("validation_max_as_of"),
        "data_identity": data_identity,
        "data_identity_sha256": data_identity["data_identity_sha256"],
        "market_watermarks": data_identity["symbols"],
        "evaluation_kind": "retrospective_research_only", "new_training_updates": 0,
        "new_threshold_search": 0, "production_enabled": False,
        "feature_rows": len(dataset), "eligible_rows": int(dataset.eligible.sum()),
        "features": list(FEATURE_COLUMNS), "future_labels_used_for_serving": False,
        "future_labels_computed": False,
        "label_maturity_required_for_serving": False,
        "quote_volume_basis": {symbol: next(iter(record["quote_volume_basis_counts"]))
                                if len(record["quote_volume_basis_counts"]) == 1 else "mixed_or_empty"
                               for symbol, record in data_identity["symbols"].items()},
    }
    timings["total_seconds"] = time.monotonic() - started
    identity["timing_seconds"] = timings
    return selector, identity


def restore_selector(execution, frames, report_dir):
    """Older manifests remain off; an enabled replay must have frozen inputs."""
    identity = execution.get("coin_selector") or {"enabled": False}
    if type(identity.get("enabled")) is not bool:
        raise ValueError("Recorded coin selector enabled flag must be boolean")
    if not identity["enabled"]:
        return None
    root = Path(report_dir).resolve()
    path = (root / identity["snapshot_manifest"]).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("Recorded selector snapshot is unavailable")
    if sha256_file(path) != identity["snapshot_manifest_sha256"]:
        raise ValueError("Recorded selector snapshot hash mismatch")
    selector, observed = create_selector(frames, initial_capital=execution["capital"], bundle_path=path,
                                         account_mode=execution.get("account_mode"))
    for name in ("model_id", "parent_model_id", "policy_threshold", "source_protocol_id"):
        if observed[name] != identity[name]:
            raise ValueError(f"Recorded coin selector {name} differs from its snapshot")
    for name in ("data_identity_sha256", "policy_gate_mode", "gate_contract"):
        if name in identity and observed[name] != identity[name]:
            raise ValueError(f"Recorded coin selector {name} differs from its inputs or contract")
    if (identity.get("deployment_account_mode") is not None
            and observed["deployment_account_mode"] != identity["deployment_account_mode"]):
        raise ValueError("Recorded coin selector deployment account mode differs from its execution")
    return selector
