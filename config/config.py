"""Fail-closed YAML configuration loader."""

from __future__ import annotations

import os
from typing import Any, Optional

import yaml

from core.logger import get_logger


logger = get_logger(__name__)


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
        self._config = loaded

        execution = loaded.get("execution") or {}
        risk = loaded.get("risk") or {}
        routing = loaded.get("routing") or {}
        logger.info(
            "Config loaded from %s: taker=%.4f maker=%.4f max_leverage=%.1f "
            "max_dd=%.2f routing=%s",
            self.config_path,
            float(execution.get("commission_rate_taker", 0.0)),
            float(execution.get("commission_rate_maker", 0.0)),
            float(risk.get("max_leverage", 0.0)),
            float(risk.get("max_drawdown_limit", 0.0)),
            routing,
        )

    def get(self, section: str, key: Optional[str] = None) -> Any:
        if section not in self._config:
            return None
        if key is not None:
            values = self._config[section]
            return values.get(key) if isinstance(values, dict) else None
        return self._config[section]


config = ConfigLoader()
