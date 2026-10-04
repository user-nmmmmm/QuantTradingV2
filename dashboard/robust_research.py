"""Validated, bounded walk-forward jobs using the shared offline scheduler.

No engine or numerical libraries are imported into the HTTP process.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

from dashboard.backtest_jobs import PROJECT_ROOT
from dashboard.strategy_presets import load_base_config


BASE_FIELDS = {"source", "symbols", "start", "end", "capital", "slippage_bps", "seed"}
RESEARCH_FIELDS = {"train_bars", "validation_bars", "test_bars", "purge_bars", "windows",
                   "entry_windows", "exit_windows", "selection_metric"}
DEFAULTS = {"train_bars": 90, "validation_bars": 30, "test_bars": 30, "purge_bars": 5,
            "windows": 3, "entry_windows": [20, 30, 50], "exit_windows": [5, 10],
            "selection_metric": "TotalReturn"}
LIMITS = {"min_candidates": 3, "max_candidates": 6, "max_windows": 4,
          "train_bars": [30, 180], "validation_bars": [10, 90], "test_bars": [10, 90],
          "purge_bars": [1, 30], "entry_window": [10, 120], "exit_window": [2, 60]}


def _integer(value: Any, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be an integer between {minimum} and {maximum}")
    return value


def window_geometry(parameters: dict[str, Any], configuration: dict[str, Any]) -> dict[str, int]:
    """First split supplies history; every following split has full warmup."""
    state = configuration.get("state") or {}
    stops = configuration.get("stops") or {}
    warmup = max(30, *parameters["entry_windows"], int(state.get("ma_slow", 60)),
                 int(state.get("ma_fast", 20)), int(state.get("adx_period", 14)) * 2,
                 int(state.get("atr_period", 14)), int(stops.get("atr_period", 14)))
    step = max(warmup, parameters["test_bars"])
    base = sum(parameters[key] for key in ("train_bars", "validation_bars", "purge_bars", "test_bars"))
    return {"warmup_bars": warmup, "step_bars": step,
            "required_bars": base + parameters["windows"] * step}


class RobustResearch:
    """Offline trend-breakout candidate search with immutable job snapshots."""

    def __init__(self, jobs: Any) -> None:
        self.jobs = jobs
        jobs.register_task_type("robust", self._command, success_file="result.json")

    def options(self) -> dict[str, Any]:
        base = self.jobs.options()
        configuration, _ = load_base_config(PROJECT_ROOT / "config" / "params.yaml")
        return {**base, "defaults": {**base["defaults"], **DEFAULTS},
                "limits": {**base["limits"], **LIMITS},
                "geometry": window_geometry(DEFAULTS, configuration),
                "selection_metrics": [{"id": "TotalReturn", "label": "验证期总收益"},
                                      {"id": "SharpeRatio", "label": "验证期夏普比率"}],
                "strategy": "趋势突破入场 / 退出窗口搜索；上涨趋势交易，其他状态持币。沿用快照中的风控、费用与健康策略。"}

    def validate(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict) or set(payload) != BASE_FIELDS | RESEARCH_FIELDS:
            raise ValueError("Research requires backtest parameters and the complete walk-forward geometry")
        result = self.jobs._validate({key: payload[key] for key in BASE_FIELDS})
        for key in ("train_bars", "validation_bars", "test_bars", "purge_bars"):
            result[key] = _integer(payload[key], key, *LIMITS[key])
        result["windows"] = _integer(payload["windows"], "windows", 1, LIMITS["max_windows"])
        for key, maximum, bounds in (("entry_windows", 3, LIMITS["entry_window"]),
                                      ("exit_windows", 2, LIMITS["exit_window"])):
            values = payload[key]
            if not isinstance(values, list) or not 1 <= len(values) <= maximum:
                raise ValueError(f"{key} must contain 1 to {maximum} distinct integer values")
            checked = [_integer(value, key, *bounds) for value in values]
            if len(set(checked)) != len(checked):
                raise ValueError(f"{key} must not contain duplicates")
            result[key] = sorted(checked)
        candidates = len(result["entry_windows"]) * len(result["exit_windows"])
        if not LIMITS["min_candidates"] <= candidates <= LIMITS["max_candidates"]:
            raise ValueError("Research requires 3 to 6 parameter combinations")
        if min(result["entry_windows"]) <= max(result["exit_windows"]):
            raise ValueError("Every entry window must exceed every exit window")
        if payload["selection_metric"] not in ("TotalReturn", "SharpeRatio"):
            raise ValueError("selection_metric must be TotalReturn or SharpeRatio")
        result["selection_metric"] = payload["selection_metric"]
        configuration, _ = load_base_config(PROJECT_ROOT / "config" / "params.yaml")
        geometry = window_geometry(result, configuration)
        available = (date.fromisoformat(result["end"]) - date.fromisoformat(result["start"])).days + 1
        if available < geometry["required_bars"]:
            raise ValueError(f"Research needs at least {geometry['required_bars']} daily bars including warmup; selected {available}")
        # An explicit family guarantees that every searched parameter affects
        # the traded strategy even when the project's current routing changes.
        result["strategy"] = {"family": "trend_breakout", "parameters": {
            "entry_window": result["entry_windows"][0], "exit_window": result["exit_windows"][0], "use_obv": True}}
        return result

    def submit(self, payload: Any) -> dict[str, Any]:
        return self.jobs.submit_task("robust", self.validate(payload))

    def _command(self, job_id: str, parameters: dict[str, Any], run_dir: Path, config_path: Path) -> list[str]:
        # Use the digest registered at admission, never re-sign a snapshot
        # that may have changed while the job was queued.
        digest = self.jobs.store.get(job_id)["config_sha256"]
        return [sys.executable, "-u", "-m", "dashboard.robust_worker", "--id", job_id,
                "--parameters", json.dumps(parameters, ensure_ascii=False, allow_nan=False),
                "--output-dir", str(run_dir), "--data-dir", str(self.jobs.data_dir),
                "--config", str(config_path), "--config-sha256", digest]

    def result(self, identifier: Any) -> dict[str, Any]:
        if not isinstance(identifier, str) or not re.fullmatch(r"research_\d{8}_\d{6}_[a-f0-9]{8}", identifier):
            raise ValueError("Invalid research identifier")
        job = self.jobs.store.get(identifier)
        if job.get("kind") != "robust" or job.get("status") != "succeeded":
            raise FileNotFoundError("A completed research result is not available")
        root = self.jobs.reports_dir.resolve()
        directory = root / identifier
        path = directory / "result.json"
        if directory.is_symlink() or path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            raise FileNotFoundError("Research result not found")
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("Research result exceeds size limit")
        result = json.loads(path.read_text(encoding="utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError("Nonfinite research value")))
        if not isinstance(result, dict) or result.get("id") != identifier:
            raise ValueError("Invalid research result")
        return result
