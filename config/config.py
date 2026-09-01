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
        "spread_bps", "volatility_slippage_factor", "use_impact_cost",
        "impact_coefficient", "impact_exponent", "max_participation_rate",
        "reconciliation_interval_seconds",
        "strategy_failure_threshold",
        "state_export_interval_ticks",
    ),
    "risk": ("max_leverage", "risk_per_trade", "max_drawdown_limit", "liquidity_limit_pct", "max_pos_size_pct"),
    "drawdown": (
        "daily_loss_limit", "reduce_threshold", "block_threshold",
        "liquidate_threshold", "lock_threshold", "reduced_risk_multiplier",
    ),
    "account": (
        "mode", "initial_margin_rate", "maintenance_margin_rate",
        "funding_interval_hours", "funding_rate_required",
        "default_borrow_rate_annual", "borrow_availability_required",
        "default_borrow_limit_qty", "liquidation_penalty_bps",
    ),
    "state": ("stability_period", "stability_candidates", "ma_fast", "ma_slow", "adx_period", "adx_threshold", "atr_period", "atr_pct_threshold"),
    "routing": ("TREND_UP", "TREND_DOWN", "SIDEWAYS", "VOLATILE"),
    "router": ("cooldown_bars", "transition_action", "max_holding_days"),
    "allocation": ("order",),
}

LEGACY_PHASE4_KEYS = {
    "transition_action": ("router", "transition_action"),
    "max_holding_days": ("router", "max_holding_days"),
    "allocation_order": ("allocation", "order"),
    "stability_candidates": ("state", "stability_candidates"),
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
        self._migrate_legacy_phase4(loaded)
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
    def _migrate_legacy_phase4(loaded: Dict[str, Any]) -> None:
        """Accept the old roadmap-named section while normalizing ownership.

        A mixed file is accepted only when old and new values agree.  This
        makes upgrades backwards compatible without silently choosing between
        contradictory runtime contracts.
        """
        legacy = loaded.get("phase4")
        if legacy is None:
            return
        if not isinstance(legacy, dict):
            raise ConfigLoadError("legacy phase4 configuration must be a mapping")
        migrated = []
        for old_key, (section, key) in LEGACY_PHASE4_KEYS.items():
            if old_key not in legacy:
                continue
            target = loaded.setdefault(section, {})
            if not isinstance(target, dict):
                raise ConfigLoadError(f"configuration section must be a mapping: {section}")
            if key in target and target[key] != legacy[old_key]:
                raise ConfigLoadError(
                    f"conflicting legacy and current configuration: "
                    f"phase4.{old_key} != {section}.{key}"
                )
            if key not in target:
                target[key] = legacy[old_key]
                migrated.append(f"phase4.{old_key}->{section}.{key}")
        if migrated:
            logger.warning(
                "Migrated deprecated Phase 4 configuration keys: %s",
                ", ".join(migrated),
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
