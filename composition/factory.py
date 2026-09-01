"""Construct configured trading components at the application boundary."""

from __future__ import annotations

from typing import Any, Dict, Optional, Protocol

from core.risk import RiskManager
from core.candidate_scoring import CandidateScorePolicy
from core.risk.portfolio_governor import CorrelationClusterPolicy, PortfolioRiskGovernor
from core.protective_stops import ProtectiveStopPolicy
from core.strategy_health import StrategyHealthPolicy
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


def build_strategy_registry(
    configuration: Optional[Configuration] = None,
) -> Dict[str, Strategy]:
    """Build every strategy, injecting the configured health policy (SR1-3).

    Strategies may not import config, so the ``strategy_health`` section is
    resolved here and pushed into each strategy that models a health
    lifecycle. Without a configuration the registered defaults apply.
    """
    registry: Dict[str, Strategy] = {
        "TrendBreakout": TrendBreakoutStrategy(),
        "TrendBreakdown": TrendBreakdownStrategy(),
        "RangeMeanReversion": RangeStrategy(),
        "VolatilityReversion": VolatilityReversionStrategy(),
    }
    if configuration is not None:
        health_policy = build_strategy_health_policy(configuration)
        stop_policy = build_protective_stop_policy(configuration)
        for strategy in registry.values():
            configure_health = getattr(strategy, "configure_health_policy", None)
            if callable(configure_health):
                configure_health(health_policy)
            configure_stops = getattr(strategy, "configure_stop_policy", None)
            if callable(configure_stops):
                configure_stops(stop_policy)
            configure_score = getattr(strategy, "configure_score_policy", None)
            if callable(configure_score):
                configure_score(build_candidate_score_policy(configuration))
    return registry


def build_candidate_score_policy(
    configuration: Configuration,
) -> CandidateScorePolicy:
    """SR3-1 ranking weights, resolved at the composition boundary."""
    section = configuration.get("candidate_scoring")
    return CandidateScorePolicy.from_mapping(
        section if isinstance(section, dict) else None
    )


def build_correlation_cluster_policy(
    configuration: Configuration,
) -> CorrelationClusterPolicy:
    """SR3-2 correlated-exposure and correlated-risk budgets."""
    section = configuration.get("portfolio_risk")
    return CorrelationClusterPolicy.from_mapping(
        section if isinstance(section, dict) else None
    )


def build_protective_stop_policy(
    configuration: Configuration,
) -> ProtectiveStopPolicy:
    """SR2-2/SR2-3 stop parameters, resolved at the composition boundary."""
    section = configuration.get("stops")
    return ProtectiveStopPolicy.from_mapping(
        section if isinstance(section, dict) else None
    )


def build_strategy_health_policy(
    configuration: Configuration,
) -> StrategyHealthPolicy:
    section = configuration.get("strategy_health")
    return StrategyHealthPolicy.from_mapping(
        section if isinstance(section, dict) else None
    )


def build_risk_manager(configuration: Configuration) -> RiskManager:
    drawdown = configuration.get("drawdown") or {}
    manager = RiskManager(
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
    # SR3-2: the cluster budgets live inside _entry_notional_caps, the single
    # source both clamp_entry_qty and check_entry_risk read.
    manager.cluster_policy = build_correlation_cluster_policy(configuration)
    return manager


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
        max_holding_days=configuration.require("router", "max_holding_days"),
        risk_governor=PortfolioRiskGovernor(
            build_correlation_cluster_policy(configuration)
        ),
    )


def market_type_supports_shorts(market_type: Optional[str]) -> bool:
    return (market_type or "").lower() in DERIVATIVE_MARKET_TYPES
