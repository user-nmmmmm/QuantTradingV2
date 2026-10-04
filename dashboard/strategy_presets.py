"""Explicit research strategy controls and immutable per-run configuration."""

from __future__ import annotations

import copy
import hashlib
import math
from pathlib import Path
from typing import Any

import yaml


_FAMILIES = [
    {"id": "configured", "name": "当前策略配置", "description": "使用项目当前的路由、策略与风控，运行时保存独立快照。", "parameters": []},
    {"id": "trend_breakout", "name": "趋势突破研究", "description": "仅在上涨趋势运行 Donchian 突破，其他状态持币；沿用当前风控与健康策略。", "parameters": [
        {"key": "entry_window", "label": "入场窗口", "type": "integer", "default": 20, "min": 10, "max": 200, "step": 1, "description": "突破过去 N 根 K 线高点；必须大于退出窗口。"},
        {"key": "exit_window", "label": "退出窗口", "type": "integer", "default": 10, "min": 2, "max": 100, "step": 1, "description": "跌破过去 N 根 K 线低点退出。"},
        {"key": "use_obv", "label": "OBV 成交量确认", "type": "boolean", "default": True, "description": "突破同时要求成交量累积确认；关闭属于消融研究。"},
    ]},
    {"id": "mean_reversion", "name": "均值回归研究", "description": "仅在震荡状态运行布林带均值回归；研究用途，不改变实盘准入。", "parameters": [
        {"key": "atr_threshold_pct", "label": "ATR / 价格上限", "type": "number", "default": 0.03, "min": 0.005, "max": 0.10, "step": 0.005, "description": "超过此波动阈值时跳过入场。"},
        {"key": "rsi_oversold", "label": "RSI 超卖阈值", "type": "number", "default": 30.0, "min": 5, "max": 49, "step": 1, "description": "多头入场的 RSI 上限。"},
        {"key": "rsi_overbought", "label": "RSI 超买阈值", "type": "number", "default": 70.0, "min": 51, "max": 95, "step": 1, "description": "空头入场的 RSI 下限，仍受账户与路由约束。"},
        {"key": "use_rsi", "label": "RSI 确认", "type": "boolean", "default": True, "description": "入场时额外检查 RSI。"},
    ]},
]


def strategy_catalog() -> dict[str, Any]:
    return {"families": copy.deepcopy(_FAMILIES), "default": {"family": "configured", "parameters": {}},
            "scope": "offline_research_only"}


def validate_strategy(value: Any = None) -> dict[str, Any]:
    if value is None:
        return {"family": "configured", "parameters": {}}
    if not isinstance(value, dict) or set(value) - {"family", "parameters"} or "family" not in value:
        raise ValueError("strategy must contain family and parameters only")
    family = next((item for item in _FAMILIES if item["id"] == value["family"]), None)
    if family is None:
        raise ValueError("Unknown strategy family")
    parameters = value.get("parameters", {})
    if not isinstance(parameters, dict) or set(parameters) - {item["key"] for item in family["parameters"]}:
        raise ValueError("Unsupported strategy parameter")
    normalized = {}
    for field in family["parameters"]:
        number = parameters.get(field["key"], field["default"])
        if field["type"] == "boolean":
            if not isinstance(number, bool):
                raise ValueError(f"{field['key']} must be boolean")
        else:
            expected = int if field["type"] == "integer" else (int, float)
            if isinstance(number, bool) or not isinstance(number, expected):
                raise ValueError(f"{field['key']} must be {field['type']}")
            if not field["min"] <= number <= field["max"] or not math.isfinite(number):
                raise ValueError(f"{field['key']} must be between {field['min']} and {field['max']}")
        normalized[field["key"]] = number
    if family["id"] == "trend_breakout" and normalized["entry_window"] <= normalized["exit_window"]:
        raise ValueError("entry_window must exceed exit_window")
    return {"family": family["id"], "parameters": normalized}


def load_base_config(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    if len(raw) > 1024 * 1024:
        raise ValueError("Configuration exceeds size limit")
    try:
        configuration = yaml.safe_load(raw.decode("utf-8"))
    except (yaml.YAMLError, UnicodeError) as exc:
        raise ValueError("Invalid backtest YAML configuration") from exc
    if not isinstance(configuration, dict):
        raise ValueError("Invalid backtest configuration")
    return configuration, hashlib.sha256(raw).hexdigest()


def configuration_for_strategy(base: dict[str, Any], value: Any, *, experiment_id: str) -> dict[str, Any]:
    strategy = validate_strategy(value)
    configuration = copy.deepcopy(base)
    if strategy["family"] == "configured":
        return configuration
    research = configuration.setdefault("research", {})
    if not isinstance(research, dict):
        raise ValueError("Invalid research configuration")
    research["experiment_id"] = experiment_id
    research["dashboard_strategy"] = strategy
    research.pop("strategy_ablation", None)
    if strategy["family"] == "trend_breakout":
        configuration["routing"] = {"TREND_UP": "TrendBreakout", "TREND_DOWN": "Cash", "SIDEWAYS": "Cash", "VOLATILE": "Cash"}
        research["trend_breakout_parameters"] = {key: strategy["parameters"][key] for key in ("entry_window", "exit_window")}
        if not strategy["parameters"]["use_obv"]:
            research["strategy_ablation"] = "no_obv_confirmation"
    else:
        configuration["routing"] = {"TREND_UP": "Cash", "TREND_DOWN": "Cash", "SIDEWAYS": "RangeMeanReversion", "VOLATILE": "Cash"}
        research.pop("trend_breakout_parameters", None)
    return configuration


def config_diff(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    result = []

    def compare(left: Any, right: Any, path: str):
        if left is None and isinstance(right, dict):
            left = {}
        if right is None and isinstance(left, dict):
            right = {}
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(set(left) | set(right)):
                compare(left.get(key), right.get(key), f"{path}.{key}" if path else str(key))
        elif left != right:
            result.append({"path": path, "before": copy.deepcopy(left), "after": copy.deepcopy(right)})
    compare(before, after, "")
    return result


def write_config_snapshot(path: Path, configuration: dict[str, Any]) -> str:
    rendered = yaml.safe_dump(configuration, allow_unicode=True, sort_keys=True).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(rendered)
    return hashlib.sha256(rendered).hexdigest()
