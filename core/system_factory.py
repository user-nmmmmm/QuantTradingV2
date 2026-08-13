from typing import Dict, Optional

from config.config import config
from core.risk import RiskManager
from core.state import MarketStateMachine
from router.router import Router
from strategies.mean_reversion import RangeStrategy
from strategies.trend_breakout import TrendBreakdownStrategy, TrendBreakoutStrategy


DERIVATIVE_MARKET_TYPES = {"future", "futures", "swap", "margin"}


def build_strategy_registry() -> Dict[str, object]:
    return {
        "TrendBreakout": TrendBreakoutStrategy(),
        "TrendBreakdown": TrendBreakdownStrategy(),
        "RangeMeanReversion": RangeStrategy(),
    }


def build_risk_manager() -> RiskManager:
    return RiskManager(
        risk_per_trade=config.require("risk", "risk_per_trade"),
        max_leverage=config.require("risk", "max_leverage"),
        max_drawdown_limit=config.require("risk", "max_drawdown_limit"),
        liquidity_limit_pct=config.require("risk", "liquidity_limit_pct"),
        max_pos_size_pct=config.require("risk", "max_pos_size_pct"),
    )


def build_state_machine() -> MarketStateMachine:
    return MarketStateMachine(
        stability_period=config.require("state", "stability_period"),
        ma_fast=config.require("state", "ma_fast"),
        ma_slow=config.require("state", "ma_slow"),
        adx_period=config.require("state", "adx_period"),
        adx_threshold=config.require("state", "adx_threshold"),
        atr_period=config.require("state", "atr_period"),
        atr_pct_threshold=config.require("state", "atr_pct_threshold"),
    )


def build_router(
    strategies: Dict[str, object],
    log_path: Optional[str] = None,
    allow_short: bool = True,
) -> Router:
    routing_config = dict(config.require("routing"))
    if not allow_short:
        routing_config["TREND_DOWN"] = "Cash"

    router = Router(
        strategies,
        regime_map=routing_config,
        cooldown_bars=config.require("router", "cooldown_bars"),
        log_path=log_path,
    )
    router.log_path = log_path
    if not hasattr(router, "log_buffer"):
        router.log_buffer = []
    return router


def market_type_supports_shorts(market_type: Optional[str]) -> bool:
    return (market_type or "").lower() in DERIVATIVE_MARKET_TYPES
