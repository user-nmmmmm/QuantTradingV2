from typing import Dict, Optional

from config.config import config
from core.risk import RiskManager
from core.state import MarketStateMachine
from router.router import Router
from strategies.mean_reversion import RangeStrategy
from strategies.trend_breakout import TrendBreakdownStrategy, TrendBreakoutStrategy
from strategies.trend_following import TrendDownStrategy, TrendUpStrategy


DERIVATIVE_MARKET_TYPES = {"future", "futures", "swap", "margin"}


def build_strategy_registry() -> Dict[str, object]:
    return {
        "TrendUp": TrendUpStrategy(),
        "TrendDown": TrendDownStrategy(),
        "TrendBreakout": TrendBreakoutStrategy(),
        "TrendBreakdown": TrendBreakdownStrategy(),
        "RangeMeanReversion": RangeStrategy(),
    }


def build_risk_manager() -> RiskManager:
    risk_config = config.get("risk") or {}
    return RiskManager(
        risk_per_trade=risk_config.get("risk_per_trade", 0.01),
        max_leverage=risk_config.get("max_leverage", 3.0),
        max_drawdown_limit=risk_config.get("max_drawdown_limit", 0.20),
        liquidity_limit_pct=risk_config.get("liquidity_limit_pct", 0.01),
        max_pos_size_pct=risk_config.get("max_pos_size_pct", 0.20),
    )


def build_state_machine() -> MarketStateMachine:
    state_config = config.get("state") or {}
    return MarketStateMachine(
        stability_period=state_config.get("stability_period", 2),
        ma_fast=state_config.get("ma_fast", 20),
        ma_slow=state_config.get("ma_slow", 60),
        adx_period=state_config.get("adx_period", 14),
        adx_threshold=state_config.get("adx_threshold", 25),
        atr_period=state_config.get("atr_period", 14),
        atr_pct_threshold=state_config.get("atr_pct_threshold", 0.05),
    )


def build_router(
    strategies: Dict[str, object],
    log_path: Optional[str] = None,
    allow_short: bool = True,
) -> Router:
    router = Router(strategies, log_path=log_path)
    routing_config = dict(config.get("routing") or {})
    if not allow_short:
        routing_config["TREND_DOWN"] = "Cash"
    router.regime_map = routing_config

    router_config = config.get("router") or {}
    router.cooldown_bars = router_config.get(
        "cooldown_bars",
        getattr(router, "cooldown_bars", 3),
    )
    router.log_path = log_path
    if not hasattr(router, "log_buffer"):
        router.log_buffer = []
    return router


def market_type_supports_shorts(market_type: Optional[str]) -> bool:
    return (market_type or "").lower() in DERIVATIVE_MARKET_TYPES
