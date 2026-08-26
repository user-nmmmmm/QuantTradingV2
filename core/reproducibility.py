"""Reproducible backtest manifests and deterministic artifact identities.

Phase 2 treats a backtest as a bundle, not merely a set of headline metrics.
The bundle records the exact code/config/data/execution identity and includes
immutable input snapshots so the same run can be reconstructed later.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

import numpy as np
import pandas as pd


MANIFEST_SCHEMA_VERSION = "2.0"
_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not np.isfinite(value):
            return None
        return value
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, (np.integer, np.floating)):
        return _json_value(value.item())
    if isinstance(value, (datetime, pd.Timestamp)):
        timestamp = pd.Timestamp(value)
        if timestamp.tzinfo is None:
            timestamp = timestamp.tz_localize("UTC")
        else:
            timestamp = timestamp.tz_convert("UTC")
        return timestamp.isoformat().replace("+00:00", "Z")
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Mapping):
        return {str(key): _json_value(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    return str(value)


def canonical_json(value: Any) -> str:
    """Stable JSON representation used by every Phase 2 digest."""

    return json.dumps(
        _json_value(value), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: os.PathLike[str] | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_frame_csv(frame: pd.DataFrame) -> str:
    """Serialize a market frame deterministically without locale dependence."""

    normalized = frame.copy()
    normalized.index = pd.to_datetime(normalized.index, errors="raise")
    if normalized.index.tz is not None:
        normalized.index = normalized.index.tz_convert("UTC").tz_localize(None)
    normalized = normalized.sort_index().sort_index(axis=1)
    return normalized.to_csv(
        index=True,
        index_label="timestamp",
        date_format="%Y-%m-%dT%H:%M:%S.%f",
        float_format="%.17g",
        lineterminator="\n",
    )


def sha256_frame(frame: pd.DataFrame) -> str:
    return sha256_bytes(canonical_frame_csv(frame).encode("utf-8"))


def _git(repo_root: Path, *args: str) -> Optional[str]:
    try:
        completed = subprocess.run(
            ["git", *args], cwd=repo_root, check=True, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        )
        return completed.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def code_identity(repo_root: os.PathLike[str] | str) -> Dict[str, Any]:
    root = Path(repo_root).resolve()
    status = _git(root, "status", "--porcelain", "--untracked-files=all")
    return {
        "git_sha": _git(root, "rev-parse", "HEAD"),
        "branch": _git(root, "branch", "--show-current"),
        "dirty": None if status is None else bool(status),
        "dirty_status": [] if not status else status.splitlines(),
    }


def dependency_identity(repo_root: os.PathLike[str] | str) -> Dict[str, Any]:
    root = Path(repo_root).resolve()
    candidates = [
        root / "requirements.lock.txt",
        root / "requirements.txt",
        root / "pyproject.toml",
    ]
    files = {
        path.name: {"sha256": sha256_file(path), "size": path.stat().st_size}
        for path in candidates if path.exists()
    }
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "platform": platform.platform(),
        "lock_files": files,
    }


def config_identity(config_path: os.PathLike[str] | str) -> Dict[str, Any]:
    path = Path(config_path).resolve()
    return {
        "path": path.name,
        "sha256": sha256_file(path),
        "content": path.read_text(encoding="utf-8"),
    }


def data_identity(
    data_map: Mapping[str, pd.DataFrame],
    *,
    source: str,
    exchange: Optional[str],
    market_type: str,
    timeframe: str,
    timezone_name: str,
    downloaded_at: datetime,
) -> Dict[str, Any]:
    symbols: Dict[str, Any] = {}
    for symbol in sorted(data_map):
        frame = data_map[symbol]
        symbols[symbol] = {
            "rows": int(len(frame)),
            "start": None if frame.empty else frame.index.min(),
            "end": None if frame.empty else frame.index.max(),
            "sha256": sha256_frame(frame),
            "columns": list(frame.columns),
        }
    return {
        "source": source,
        "exchange": exchange,
        "market_type": market_type,
        "timeframe": timeframe,
        "timezone": timezone_name,
        "downloaded_at": downloaded_at,
        "symbols": symbols,
    }


def save_data_snapshots(
    data_map: Mapping[str, pd.DataFrame], output_dir: os.PathLike[str] | str
) -> Dict[str, Dict[str, Any]]:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    result: Dict[str, Dict[str, Any]] = {}
    used_names: set[str] = set()
    for symbol in sorted(data_map):
        stem = _SAFE_NAME.sub("_", symbol).strip("._") or "symbol"
        candidate = stem
        counter = 2
        while candidate.lower() in used_names:
            candidate = f"{stem}_{counter}"
            counter += 1
        used_names.add(candidate.lower())
        path = directory / f"{candidate}.csv"
        content = canonical_frame_csv(data_map[symbol])
        path.write_text(content, encoding="utf-8", newline="")
        result[symbol] = {
            "path": path.name,
            "sha256": sha256_bytes(content.encode("utf-8")),
            "rows": len(data_map[symbol]),
        }
    return result


def load_data_snapshots(
    snapshot_dir: os.PathLike[str] | str,
    entries: Mapping[str, Mapping[str, Any]],
    *,
    verify: bool = True,
) -> Dict[str, pd.DataFrame]:
    directory = Path(snapshot_dir)
    result: Dict[str, pd.DataFrame] = {}
    for symbol, entry in entries.items():
        path = directory / str(entry["path"])
        if verify and sha256_file(path) != entry["sha256"]:
            raise ValueError(f"Data snapshot hash mismatch for {symbol}: {path}")
        frame = pd.read_csv(
            path,
            index_col="timestamp",
            parse_dates=["timestamp"],
            float_precision="round_trip",
        )
        result[symbol] = frame
    return result


def artifact_hashes(
    output_dir: os.PathLike[str] | str,
    names: Iterable[str],
) -> Dict[str, Dict[str, Any]]:
    directory = Path(output_dir)
    result = {}
    for name in names:
        path = directory / name
        if path.exists() and path.is_file():
            result[name] = {"sha256": sha256_file(path), "size": path.stat().st_size}
    return result


def build_run_manifest(
    *,
    run_id: str,
    repo_root: os.PathLike[str] | str,
    config_path: os.PathLike[str] | str,
    requested_period: Mapping[str, Any],
    effective_period: Mapping[str, Any],
    data: Mapping[str, Any],
    snapshots: Mapping[str, Any],
    execution: Mapping[str, Any],
    artifacts: Mapping[str, Any],
    audit: Mapping[str, Any],
) -> Dict[str, Any]:
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc),
        "code": code_identity(repo_root),
        "dependencies": dependency_identity(repo_root),
        "config": config_identity(config_path),
        "period": {
            "requested": dict(requested_period),
            "effective": dict(effective_period),
        },
        "data": dict(data),
        "data_snapshots": dict(snapshots),
        "execution": dict(execution),
        "artifacts": dict(artifacts),
        "audit": dict(audit),
    }


def write_manifest(path: os.PathLike[str] | str, manifest: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(_json_value(manifest), ensure_ascii=False, sort_keys=True, indent=2)
        + "\n",
        encoding="utf-8",
        newline="",
    )


def load_manifest(path: os.PathLike[str] | str) -> Dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if value.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError(
            f"Unsupported manifest schema: {value.get('schema_version')!r}"
        )
    return value


def deterministic_result_digest(result: Mapping[str, Any]) -> Dict[str, str]:
    """Digest trades/equity/benchmarks for same-process or replay comparison."""

    trades = result.get("trades") or []
    equity = result.get("equity_curve")
    benchmark = result.get("benchmark")
    return {
        "trades": sha256_bytes(canonical_json(trades).encode("utf-8")),
        "equity": sha256_bytes(canonical_frame_csv(equity).encode("utf-8")),
        "benchmark": (
            sha256_bytes(canonical_frame_csv(benchmark.to_frame("benchmark")).encode("utf-8"))
            if isinstance(benchmark, pd.Series) else sha256_bytes(b"")
        ),
        "report_payload": sha256_bytes(canonical_json({
            "close_events": result.get("close_events"),
            "accounting_check": result.get("accounting_check"),
            "benchmark_metadata": result.get("benchmark_metadata"),
            "account_mode": result.get("account_mode"),
            "margin_ledger": result.get("margin_ledger"),
            "financing_ledger": result.get("financing_ledger"),
            "execution_audit": result.get("execution_audit"),
            "breaker_audit": result.get("breaker_audit"),
            "breaker_state": result.get("breaker_state"),
        }).encode("utf-8")),
    }


def runtime_identity() -> Dict[str, Any]:
    return {
        "python_executable": Path(sys.executable).name,
        "process_id": os.getpid(),
    }


__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "artifact_hashes",
    "build_run_manifest",
    "canonical_frame_csv",
    "canonical_json",
    "code_identity",
    "config_identity",
    "data_identity",
    "dependency_identity",
    "deterministic_result_digest",
    "load_data_snapshots",
    "load_manifest",
    "runtime_identity",
    "save_data_snapshots",
    "sha256_file",
    "sha256_frame",
    "write_manifest",
]
