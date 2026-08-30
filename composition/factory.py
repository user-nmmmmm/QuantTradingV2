"""Construct configured trading components at the application boundary."""

from __future__ import annotations

from typing import Any, Dict, Optional, Protocol

from core.risk import RiskManager
from core.state import MarketStateMachine
from router.router import Router
from strategies.base import Strategy
from strategies.mean_reversion import RangeStrategy
from strategies.trend_breakout import TrendBreakdownStrategy, TrendBreakoutStrategy
from strategies.volatility import VolatilityReversionStrategy


class Configuration(Protocol):
    """Minimal interface accepted by component factories."""

    def get(self, section: str, key: Optional[str] = None) -> Any: ...

    def require(self, section: str, key: Optional[str] = None) -> Any: ...


DERIVATIVE_MARKET_TYPES = {"future", "futures", "swap", "margin"}


def build_strategy_registry() -> Dict[str, Strategy]:
    return {
        "TrendBreakout": TrendBreakoutStrategy(),
        "TrendBreakdown": TrendBreakdownStrategy(),
        "RangeMeanReversion": RangeStrategy(),
        "VolatilityReversion": VolatilityReversionStrategy(),
    }


def build_risk_manager(configuration: Configuration) -> RiskManager:
    drawdown = configuration.get("drawdown") or {}
    return RiskManager(
        risk_per_trade=configuration.require("risk", "risk_per_trade"),
        max_leverage=configuration.require("risk", "max_leverage"),
        max_drawdown_limit=configuration.require("risk", "max_drawdown_limit"),
        liquidity_limit_pct=configuration.require("risk", "liquidity_limit_pct"),
        max_pos_size_pct=configuration.require("risk", "max_pos_size_pct"),
        daily_loss_limit=drawdown.get("daily_loss_limit"),
        portfolio_drawdown_reduce=drawdown.get("reduce_threshold"),
        portfolio_drawdown_block=drawdown.get("block_threshold"),
        portfolio_drawdown_liquidate=drawdown.get("liquidate_threshold"),
        portfolio_drawdown_lock=drawdown.get("lock_threshold"),
        reduced_risk_multiplier=drawdown.get("reduced_risk_multiplier", 0.5),
    )


def build_state_machine(configuration: Configuration) -> MarketStateMachine:
    return MarketStateMachine(
        stability_period=configuration.require("state", "stability_period"),
        ma_fast=configuration.require("state", "ma_fast"),
        ma_slow=configuration.require("state", "ma_slow"),
        adx_period=configuration.require("state", "adx_period"),
        adx_threshold=configuration.require("state", "adx_threshold"),
        atr_period=configuration.require("state", "atr_period"),
        atr_pct_threshold=configuration.require("state", "atr_pct_threshold"),
    )


def build_router(
    strategies: Dict[str, Strategy],
    configuration: Configuration,
    log_path: Optional[str] = None,
    allow_short: bool = True,
) -> Router:
    routing_config = dict(configuration.require("routing"))
    if not allow_short:
        routing_config["TREND_DOWN"] = "Cash"

    return Router(
        strategies,
        regime_map=routing_config,
        cooldown_bars=configuration.require("router", "cooldown_bars"),
        log_path=log_path,
        max_holding_days=(configuration.get("phase4") or {}).get("max_holding_days"),
    )


def market_type_supports_shorts(market_type: Optional[str]) -> bool:
    return (market_type or "").lower() in DERIVATIVE_MARKET_TYPES
