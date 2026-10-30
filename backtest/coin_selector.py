"""Frozen coin-selector package: validation, snapshot and report, with no dependency on ``research``.

The package contains weights and serving settings, not a training entry point. Building the selector from a
package (``create_selector`` / ``restore_selector``) needs the research feature builder and models, so it lives
in ``research.ml_selection.serving`` (B3, ADR 0017); importing this module never imports ``research``.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import pandas as pd

from core.foundation.reproducibility import sha256_file


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BUNDLE = ROOT / "config" / "ml_selector_current.json"
_WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul", "conin$", "conout$"}
    | {f"{prefix}{number}" for prefix in ("com", "lpt") for number in "123456789¹²³"}
)


@dataclass(frozen=True)
class BundleSnapshot:
    path: Path
    package: dict[str, Any]
    manifest_bytes: bytes
    artifact_bytes: dict[str, bytes]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.manifest_bytes).hexdigest()


def _portable_member_component(component: str) -> bool:
    """Apply the same Windows filename rules on every snapshot-building OS."""
    return not (component.endswith((".", " "))
                or component.partition(".")[0].rstrip(" ").casefold() in _WINDOWS_RESERVED_NAMES
                or any(ord(character) < 32 or character in '<>:"|?*' for character in component))


def _member_path(value: Any) -> str:
    if (not isinstance(value, str) or not value or "\\" in value
            or PureWindowsPath(value).drive or PurePosixPath(value).is_absolute()
            or not PurePosixPath(value).parts
            or ".." in PurePosixPath(value).parts or PurePosixPath(value).as_posix() != value
            or PurePosixPath(value).parts[0].casefold() == "manifest.json"
            or not all(_portable_member_component(part) for part in PurePosixPath(value).parts)):
        raise ValueError("Frozen selector members require portable relative paths")
    return value


def read_bundle_snapshot(bundle_path=None) -> BundleSnapshot:
    """Validate one immutable read of the manifest and each model artifact."""
    path = Path(bundle_path or DEFAULT_BUNDLE).resolve()
    manifest_bytes = path.read_bytes()
    package = json.loads(manifest_bytes)
    if (not isinstance(package, dict) or package.get("schema") != "frozen-coin-selector/v1"
            or package.get("timeframe") != "1d"):
        raise ValueError("Unsupported frozen coin selector package")
    candidate = package["candidate"]
    if not isinstance(candidate, dict) or candidate.get("selected_candidate") != "rl":
        raise ValueError("The frozen coin selector candidate must be RL")
    if not isinstance(package["files"], dict) or set(package["files"]) != {"model", "policy"}:
        raise ValueError("Frozen selector model inventory is incomplete")
    artifacts = {}
    member_names = set()
    for name, record in package["files"].items():
        if not isinstance(record, dict):
            raise ValueError(f"Frozen selector {name} record must be a mapping")
        relative = _member_path(record["path"])
        if relative.casefold() in member_names:
            raise ValueError("Frozen selector model paths must be distinct")
        member_names.add(relative.casefold())
        source = (path.parent / relative).resolve()
        if not source.is_relative_to(path.parent) or not source.is_file():
            raise ValueError(f"Frozen selector {name} file is unavailable or outside its package")
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != record["sha256"]:
            raise ValueError(f"Frozen selector {name} file hash mismatch")
        payload = json.loads(data)
        if payload["model_id"] != record["model_id"]:
            raise ValueError(f"Frozen selector {name} identity mismatch")
        artifacts[relative] = data
    if (package["files"]["model"]["model_id"] != candidate["parent_model_id"]
            or package["files"]["policy"]["model_id"] != candidate["model_id"]):
        raise ValueError("Frozen selector candidate and weight identities differ")
    return BundleSnapshot(path, package, manifest_bytes, artifacts)


def read_bundle(bundle_path=None) -> tuple[Path, dict[str, Any]]:
    snapshot = read_bundle_snapshot(bundle_path)
    return snapshot.path, snapshot.package


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


def snapshot_bundle(identity: dict[str, Any], directory: Path) -> Path:
    """Freeze the selected package alongside outputs, including compact reports."""
    captured = read_bundle_snapshot(identity["bundle_path"])
    if captured.sha256 != identity["bundle_sha256"]:
        raise ValueError("Selector package changed during the backtest")
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "manifest.json").write_bytes(captured.manifest_bytes)
    for relative, data in captured.artifact_bytes.items():
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
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
