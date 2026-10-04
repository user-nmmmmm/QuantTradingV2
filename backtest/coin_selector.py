"""Load the frozen research selector only for explicitly enabled backtests.

The package contains weights and serving settings, not a training entry point.
Features are rebuilt from this backtest's own candles. ResearchSelector removes
all future labels before inference. Disabled callers need not import this module.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import shutil
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


def create_selector(frames, *, initial_capital, bundle_path=None):
    """Return deterministic serving and a JSON-safe per-run identity."""
    from research.ml_selection.dataset import build_dataset, FEATURE_COLUMNS
    from research.ml_selection.models import load_model
    from research.ml_selection.selector import ResearchSelector

    path, package = read_bundle(bundle_path)
    model = load_model(path.parent / package["files"]["model"]["path"])
    policy = load_model(path.parent / package["files"]["policy"]["path"])
    if model.kind != "lightgbm" or policy.kind != "bernoulli_policy":
        raise ValueError("Frozen selector requires its LightGBM parent and RL policy")
    if (model.model_id != package["candidate"]["parent_model_id"]
            or policy.model_id != package["candidate"]["model_id"]):
        raise ValueError("Loaded selector identity differs from the frozen candidate")
    # The shared research builder preserves availability and liquidity rules;
    # labels never reach the serving table and label maturity is not an entry gate.
    dataset = build_dataset(frames, **package["dataset"])
    selector = ResearchSelector(dataset, mode="policy", model=model, policy=policy,
        deterministic=True, initial_capital=initial_capital, **package["selection"])
    if selector.policy_threshold != policy.metadata["evaluation_threshold"]:
        raise ValueError("Frozen selector threshold differs from model metadata")
    identity = {
        "schema": "backtest-coin-selector/v1", "enabled": True,
        "candidate": "rl", "model_id": policy.model_id, "parent_model_id": model.model_id,
        "policy_threshold": selector.policy_threshold, "deterministic": True,
        "bundle_path": str(path), "bundle_sha256": sha256_file(path),
        "source_protocol_id": package["source_protocol_id"],
        "model_files": deepcopy(package["files"]),
        "training_end_exclusive": package["training_end_exclusive"],
        "validation_end_exclusive": package["validation_end_exclusive"],
        "evaluation_kind": "retrospective_research_only", "new_training_updates": 0,
        "new_threshold_search": 0, "production_enabled": False,
        "feature_rows": len(dataset), "eligible_rows": int(dataset.eligible.sum()),
        "features": list(FEATURE_COLUMNS), "future_labels_used_for_serving": False,
        "label_maturity_required_for_serving": False,
        "quote_volume_basis": {symbol: "exchange_quote_volume" if "quote_volume" in frame
                               else "close_times_base_volume_proxy" for symbol, frame in frames.items()},
    }
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
    selector, observed = create_selector(frames, initial_capital=execution["capital"], bundle_path=path)
    for name in ("model_id", "parent_model_id", "policy_threshold", "source_protocol_id"):
        if observed[name] != identity[name]:
            raise ValueError(f"Recorded coin selector {name} differs from its snapshot")
    return selector
