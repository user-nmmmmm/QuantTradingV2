"""Fail-closed YAML configuration loader."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

import yaml

from core.logger import get_logger


logger = get_logger(__name__)

REQUIRED_CONFIG: Dict[str, Tuple[str, ...]] = {
    "execution": (
        "commission_rate_taker", "commission_rate_maker", "slippage_bps",
        "use_impact_cost", "reconciliation_interval_seconds",
        "strategy_failure_threshold",
    ),
    "risk": ("max_leverage", "risk_per_trade", "max_drawdown_limit", "liquidity_limit_pct", "max_pos_size_pct"),
    "state": ("stability_period", "ma_fast", "ma_slow", "adx_period", "adx_threshold", "atr_period", "atr_pct_threshold"),
    "routing": ("TREND_UP", "TREND_DOWN", "SIDEWAYS", "VOLATILE"),
    "router": ("cooldown_bars",),
}


class ConfigLoadError(RuntimeError):
    """Raised when the authoritative YAML configuration cannot be loaded."""


class ConfigLoader:
    """Load the authoritative params.yaml, with isolated paths available to tests."""

    _instance = None

    def __new__(cls, config_path: Optional[str] = None):
        if config_path is not None:
            return super().__new__(cls)
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, config_path: Optional[str] = None) -> None:
        requested_path = config_path or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "params.yaml"
        )
        if getattr(self, "_initialized", False) and self.config_path == requested_path:
            return
        self.config_path = requested_path
        self._config = None
        self._load_config()
        self._initialized = True

    def _load_config(self) -> None:
        if not os.path.exists(self.config_path):
            raise ConfigLoadError(f"params.yaml not found at {self.config_path}")
        try:
            with open(self.config_path, "r", encoding="utf-8") as handle:
                loaded = yaml.safe_load(handle)
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigLoadError(
                f"failed to load params.yaml at {self.config_path}: {type(exc).__name__}"
            ) from exc
        if not isinstance(loaded, dict):
            raise ConfigLoadError("params.yaml is empty or not a valid mapping")
        self._validate_required(loaded)
        self._config = loaded

        execution = loaded["execution"]
        risk = loaded.get("risk") or {}
        routing = loaded.get("routing") or {}
        logger.info(
            "Config loaded from %s: taker=%.4f maker=%.4f slippage_bps=%.2f "
            "risk_per_trade=%.4f max_leverage=%.1f max_dd=%.2f "
            "liquidity_limit_pct=%.4f max_pos_size_pct=%.4f routing=%s",
            self.config_path,
            float(execution["commission_rate_taker"]),
            float(execution["commission_rate_maker"]),
            float(execution["slippage_bps"]),
            float(risk["risk_per_trade"]),
            float(risk["max_leverage"]),
            float(risk["max_drawdown_limit"]),
            float(risk["liquidity_limit_pct"]),
            float(risk["max_pos_size_pct"]),
            routing,
        )

    @staticmethod
    def _validate_required(loaded: Dict[str, Any]) -> None:
        missing = []
        for section, keys in REQUIRED_CONFIG.items():
            values = loaded.get(section)
            if not isinstance(values, dict):
                missing.append(section)
                continue
            missing.extend(f"{section}.{key}" for key in keys if key not in values)
        if missing:
            raise ConfigLoadError(
                "params.yaml is missing required configuration: " + ", ".join(missing)
            )

    def get(self, section: str, key: Optional[str] = None) -> Any:
        if section not in self._config:
            return None
        if key is not None:
            values = self._config[section]
            return values.get(key) if isinstance(values, dict) else None
        return self._config[section]

    def require(self, section: str, key: Optional[str] = None) -> Any:
        if section not in self._config:
            raise ConfigLoadError(f"required configuration section is missing: {section}")
        values = self._config[section]
        if key is None:
            return values
        if not isinstance(values, dict) or key not in values:
            raise ConfigLoadError(f"required configuration key is missing: {section}.{key}")
        return values[key]


config = ConfigLoader()
