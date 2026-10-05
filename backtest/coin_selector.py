"""Load the frozen research selector only for explicitly enabled backtests.

The package contains weights and serving settings, not a training entry point.
Features are rebuilt from this backtest's own candles without building future
labels. Disabled callers need not import this module.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import shutil
import time
from typing import Any

import pandas as pd

from core.reproducibility import sha256_file


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUNDLE = ROOT / "config" / "ml_selector_current.json"


def read_bundle(bundle_path=None) -> tuple[Path, dict[str, Any]]:
    path = Path(bundle_path or DEFAULT_BUNDLE).resolve()
    package = json.loads(path.read_text(encoding="utf-8"))
    if package.get("schema") != "frozen-coin-selector/v1" or package.get("timeframe") != "1d":
        raise ValueError("Unsupported frozen coin selector package")
    candidate = package["candidate"]
    if candidate.get("selected_candidate") != "rl":
        raise ValueError("The frozen coin selector candidate must be RL")
    if set(package["files"]) != {"model", "policy"}:
        raise ValueError("Frozen selector model inventory is incomplete")
    for name, record in package["files"].items():
        source = (path.parent / record["path"]).resolve()
        if not source.is_relative_to(path.parent) or not source.is_file():
            raise ValueError(f"Frozen selector {name} file is unavailable or outside its package")
        if sha256_file(source) != record["sha256"]:
            raise ValueError(f"Frozen selector {name} file hash mismatch")
        payload = json.loads(source.read_text(encoding="utf-8"))
        if payload["model_id"] != record["model_id"]:
            raise ValueError(f"Frozen selector {name} identity mismatch")
    if (package["files"]["model"]["model_id"] != candidate["parent_model_id"]
            or package["files"]["policy"]["model_id"] != candidate["model_id"]):
        raise ValueError("Frozen selector candidate and weight identities differ")
    return path, package


def _account_compatibility(package, model, policy, deployment_mode):
    """Enforce explicit new contracts while preserving unverified old replays."""
    if deployment_mode is not None and (not isinstance(deployment_mode, str)
                                       or deployment_mode not in {"spot", "spot_margin"}):
        raise ValueError("Unsupported selector deployment account mode")
    contracts = [value for value in (package.get("account_contract"),
        model.metadata.get("account_contract"), policy.metadata.get("account_contract"))
        if value is not None]
    if not contracts:
        hints = []
        for metadata in (model.metadata, policy.metadata):
            scope = metadata.get("evaluation_scope") or {}
            scoped_contract = scope.get("account_contract") if isinstance(scope, dict) else None
            scoped_contract = scoped_contract if isinstance(scoped_contract, dict) else {}
            for value in (metadata.get("account_mode"), scoped_contract.get("training_account_mode")):
                if isinstance(value, str) and value in {"spot", "spot_margin"}:
                    hints.append(value)
        if len(set(hints)) > 1:
            raise ValueError("Frozen selector training account metadata disagree")
        return {"training_account_mode": hints[0] if hints else None,
                "deployment_account_mode": deployment_mode,
                "deployment_account_modes": [], "compatibility_verified": False,
                "status": "legacy_account_compatibility_unverified"}
    contract = contracts[0]
    if not isinstance(contract, dict) or any(value != contract for value in contracts[1:]):
        raise ValueError("Frozen selector account contracts disagree")
    training = contract.get("training_account_mode")
    supported = contract.get("deployment_account_modes")
    if (not isinstance(training, str) or training not in {"spot", "spot_margin"} or not isinstance(supported, list)
            or not supported or any(not isinstance(mode, str) or mode not in {"spot", "spot_margin"}
                                    for mode in supported)
            or len(set(supported)) != len(supported)
            or type(contract.get("compatibility_verified")) is not bool):
        raise ValueError("Frozen selector account contract is incomplete")
    if deployment_mode is None:
        raise ValueError("Explicit selector account contract requires a deployment account mode")
    if not contract["compatibility_verified"] or deployment_mode not in supported:
        raise ValueError("Frozen selector does not support the deployment account mode")
    if deployment_mode != training and contract.get("cross_account_transfer_verified") is not True:
        raise ValueError("Frozen selector cross-account transfer is unverified")
    return {**deepcopy(contract), "deployment_account_mode": deployment_mode,
            "status": "verified_supported_account"}


def create_selector(frames, *, initial_capital, bundle_path=None, account_mode=None):
    """Return deterministic serving and a JSON-safe per-run identity."""
    started = time.monotonic()
    from research.ml_selection.dataset import build_inference_dataset, FEATURE_COLUMNS
    from research.ml_selection.models import load_model
    from research.ml_selection.selector import ResearchSelector

    stage_started = time.monotonic()
    path, package = read_bundle(bundle_path)
    timings = {"bundle_validation_seconds": time.monotonic() - stage_started}
    stage_started = time.monotonic()
    model = load_model(path.parent / package["files"]["model"]["path"])
    policy = load_model(path.parent / package["files"]["policy"]["path"])
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
        "bundle_path": str(path), "bundle_sha256": sha256_file(path),
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


def snapshot_bundle(identity: dict[str, Any], directory: Path) -> Path:
    """Freeze the selected package alongside outputs, including compact reports."""
    source, package = read_bundle(identity["bundle_path"])
    if sha256_file(source) != identity["bundle_sha256"]:
        raise ValueError("Selector package changed during the backtest")
    directory.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(source, directory / "manifest.json")
    for record in package["files"].values():
        target = directory / record["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source.parent / record["path"], target)
    return directory / "manifest.json"


def write_selector_report(directory, selector, identity) -> dict[str, Any]:
    directory = Path(directory)
    report = deepcopy(identity)
    if selector is not None:
        snapshot = snapshot_bundle(identity, directory / "selector_inputs")
        audit = pd.DataFrame(selector.audit)
        if audit.empty:
            audit = pd.DataFrame(columns=["decision_id", "bar_time", "as_of", "symbol",
                                          "selected", "reason", "selection_probability"])
        audit.to_csv(directory / "coin_selection.csv", index=False)
        probability = pd.to_numeric(audit.get("selection_probability", pd.Series(dtype=float)),
                                    errors="coerce").dropna()
        report.update(candidate_count=len(audit),
            selected_count=int(audit["selected"].eq(True).sum()),
            reason_counts=audit["reason"].value_counts().to_dict(),
            scored_candidates=len(probability),
            probability_min=float(probability.min()) if len(probability) else None,
            probability_max=float(probability.max()) if len(probability) else None,
            selection_audit_sha256=sha256_file(directory / "coin_selection.csv"),
            snapshot_manifest="selector_inputs/manifest.json",
            snapshot_manifest_sha256=sha256_file(snapshot))
        report["selector_funnel"] = selector.summary()
    (directory / "coin_selector.json").write_text(
        json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
    return report


def disabled_identity() -> dict[str, Any]:
    return {"schema": "backtest-coin-selector/v1", "enabled": False, "candidate": None,
            "new_training_updates": 0, "new_threshold_search": 0}


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
