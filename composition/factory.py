"""Construct configured trading components at the application boundary."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Protocol
from dataclasses import asdict

from core.risk import RiskManager
from core.candidate_scoring import CandidateScorePolicy
from core.position_management import CapitalAllocationPolicy
from core.risk.portfolio_governor import CorrelationClusterPolicy, PortfolioRiskGovernor
from core.protective_stops import ProtectiveStopPolicy
from core.strategy_health import StrategyHealthPolicy
from core.state import MarketState, MarketStateMachine
from router.router import Router
from strategies.base import Strategy
from strategies.mean_reversion import RangeStrategy
from strategies.trend_breakout import TrendBreakdownStrategy, TrendBreakoutStrategy
from strategies.trend_portfolio_v2 import TrendPortfolioV2Strategy
from strategies.trend_portfolio_v3 import TrendPortfolioV3Strategy
from strategies.volatility import VolatilityReversionStrategy


class Configuration(Protocol):
    """Minimal interface accepted by component factories."""

    def get(self, section: str, key: Optional[str] = None) -> Any: ...

    def require(self, section: str, key: Optional[str] = None) -> Any: ...


DERIVATIVE_MARKET_TYPES = {"future", "futures", "swap", "margin"}


def build_portfolio_target_controller(configuration: Configuration, strategies, *, state_store=None):
    """Explicit application opt-in to the existing weekly V3 execution path.

    Membership facts must be supplied by the caller; present-day venue metadata
    is never manufactured into historical listing or publication evidence.
    """
    settings = configuration.get("portfolio_targets") or {}
    if not isinstance(settings, Mapping):
        raise ValueError("portfolio_targets must be a mapping")
    if not settings.get("enabled", False):
        return None
    allowed = {"enabled", "metadata", "cost_aware", "max_weekly_turnover", "relative_tolerance", "participation"}
    if set(settings) - allowed or settings.get("enabled") is not True:
        raise ValueError("invalid portfolio_targets settings")
    strategy = strategies.get("TrendPortfolioV3")
    if strategy is None:
        raise ValueError("portfolio_targets requires an explicitly routed TrendPortfolioV3")
    metadata = settings.get("metadata")
    if not isinstance(metadata, Mapping) or not metadata:
        raise ValueError("portfolio_targets requires explicit point-in-time membership metadata")
    from core.cost_aware_allocation import CostAwareAllocationPolicy
    from core.portfolio_target_controller import PortfolioTargetController
    from core.selection_v2 import select_all_qualified, size_portfolio_targets
    import pandas as pd

    cost_policy = CostAwareAllocationPolicy.from_mapping(settings.get("cost_aware"))
    clusters = build_correlation_cluster_policy(configuration)
    frozen = {"week": None, "weights": {}, "sizing": None}

    def targets(*, event, as_of, held_symbols, existing_weights):
        point = pd.Timestamp(as_of)
        week = (point.normalize() - pd.Timedelta(days=point.weekday())).isoformat()
        if frozen["week"] is None and controller._state.get("week") is not None:
            frozen.update(week=controller._state["week"], weights={
                s: row["weight"] for s, row in controller._state["targets"].items()})
        weekly = point.weekday() == 0 and frozen["week"] != week
        relevant = set(frozen["weights"]) | set(held_symbols)
        histories = event.histories if weekly else {s: f for s, f in event.histories.items() if s in relevant}
        selected = select_all_qualified(histories, metadata, as_of=as_of,
            held_symbols=held_symbols, policy=strategy.selection_policy)
        if weekly:
            sized = size_portfolio_targets(selected, existing_weights=existing_weights,
                clusters={s: clusters.cluster_for(s) for s in selected.selected_symbols},
                cost_aware_policy=cost_policy)
            frozen.update(week=week, weights=dict(sized.target_weights), sizing=sized.to_dict())
        rows = {s: row.to_dict() for s, row in selected.rows.items()}
        return {"target_weights": dict(frozen["weights"]), "as_of": as_of,
            "add_allowed": {s: bool(row.get("add_allowed", False)) for s, row in rows.items()},
            "stop_prices": {s: row["stop_price"] for s, row in rows.items() if row.get("stop_price") is not None},
            "forced_exits": [s for s, row in rows.items() if row.get("force_exit")],
            "selection_rows": rows, "sizing": frozen["sizing"]}

    controller = PortfolioTargetController(strategy=strategy, target_provider=targets,
        metadata=metadata, cost_aware_policy=cost_policy, state_store=state_store,
        **{key: settings[key] for key in ("max_weekly_turnover", "relative_tolerance", "participation") if key in settings})
    return controller


def build_strategy_registry(
    configuration: Optional[Configuration] = None,
) -> Dict[str, Strategy]:
    """Build every strategy, injecting the configured health policy (SR1-3).

    Strategies may not import config, so the ``strategy_health`` section is
    resolved here and pushed into each strategy that models a health
    lifecycle. Without a configuration the registered defaults apply.
    """
    breakout_parameters: Dict[str, int] = {}
    if configuration is not None:
        research = configuration.get("research") or {}
        if not isinstance(research, Mapping):
            raise ValueError("research must be a mapping")
        parameters = research.get("trend_breakout_parameters")
        if parameters is not None:
            experiment_id = research.get("experiment_id")
            if not isinstance(experiment_id, str) or not experiment_id.strip():
                raise ValueError("trend_breakout_parameters require research.experiment_id")
            if not isinstance(parameters, Mapping) or set(parameters) != {
                "entry_window", "exit_window",
            }:
                raise ValueError("trend_breakout_parameters require entry_window and exit_window")
            if any(type(value) is not int or value <= 0 for value in parameters.values()):
                raise ValueError("trend_breakout_parameters must be positive integers")
            if parameters["entry_window"] <= parameters["exit_window"]:
                raise ValueError("entry_window must exceed exit_window")
            breakout_parameters = dict(parameters)
    registry: Dict[str, Strategy] = {
        "TrendBreakout": TrendBreakoutStrategy(**breakout_parameters),
        "TrendBreakdown": TrendBreakdownStrategy(),
        "RangeMeanReversion": RangeStrategy(),
        "VolatilityReversion": VolatilityReversionStrategy(),
    }
    # Opt in through the effective routing configuration. Keeping V2 out of
    # the default registry also preserves the baseline's passive observers.
    if configuration is not None and "TrendPortfolioV2" in (
        configuration.get("routing") or {}
    ).values():
        v2_parameters = (configuration.get("research") or {}).get("trend_portfolio_v2", {})
        if not isinstance(v2_parameters, Mapping):
            raise ValueError("research.trend_portfolio_v2 must be a mapping")
        if v2_parameters and not str(
            (configuration.get("research") or {}).get("experiment_id") or ""
        ).strip():
            raise ValueError("trend_portfolio_v2 parameters require research.experiment_id")
        registry["TrendPortfolioV2"] = TrendPortfolioV2Strategy(
            **{**breakout_parameters, **dict(v2_parameters)}
        )
    if configuration is not None and "TrendPortfolioV3" in (
        configuration.get("routing") or {}
    ).values():
        research = configuration.get("research") or {}
        parameters = research.get("trend_portfolio_v3", {})
        if not isinstance(parameters, Mapping):
            raise ValueError("research.trend_portfolio_v3 must be a mapping")
        if not str(research.get("experiment_id") or "").strip():
            raise ValueError("TrendPortfolioV3 requires research.experiment_id")
        registry["TrendPortfolioV3"] = TrendPortfolioV3Strategy(**dict(parameters))
    if configuration is not None:
        health_policy = build_strategy_health_policy(configuration)
        stop_policy = build_protective_stop_policy(configuration)
        research = configuration.get("research") or {}
        health_overrides = research.get("strategy_health_overrides", {})
        regime_controls = research.get("regime_controls", {})
        eligible = research.get("strategy_eligible_symbols", {})
        for controls in (health_overrides, regime_controls, eligible):
            if not isinstance(controls, Mapping) or set(controls) - set(registry):
                raise ValueError("Research strategy controls must name registered strategies")
        if (health_overrides or regime_controls or eligible) and not str(research.get("experiment_id") or "").strip():
            raise ValueError("Strategy research controls require research.experiment_id")
        for strategy in registry.values():
            configure_health = getattr(strategy, "configure_health_policy", None)
            if callable(configure_health):
                override = health_overrides.get(strategy.name, {})
                if not isinstance(override, Mapping) or set(override) - set(StrategyHealthPolicy.__dataclass_fields__):
                    raise ValueError("Unknown per-strategy health policy field")
                configure_health(StrategyHealthPolicy.from_mapping({**asdict(health_policy), **override}))
            controls = regime_controls.get(strategy.name, {})
            if not isinstance(controls, Mapping) or set(controls) - {"entry_states", "exit_states", "exit_on_disallowed_state"}:
                raise ValueError("Unknown regime control")
            if controls:
                # Capture the original exit domain before widening entry admission.
                strategy.exit_allowed_states = set(strategy.allowed_states)
                for key, attr in (("entry_states", "allowed_states"), ("exit_states", "exit_allowed_states")):
                    if key in controls:
                        states = controls[key]
                        if not isinstance(states, (list, tuple)) or not states or set(states) - {s.name for s in MarketState}:
                            raise ValueError("Regime states must name valid market states")
                        setattr(strategy, attr, {MarketState[s] for s in states})
                if "exit_on_disallowed_state" in controls:
                    if type(controls["exit_on_disallowed_state"]) is not bool:
                        raise ValueError("exit_on_disallowed_state must be boolean")
                    strategy.exit_on_disallowed_state = controls["exit_on_disallowed_state"]
            configure_stops = getattr(strategy, "configure_stop_policy", None)
            if callable(configure_stops):
                configure_stops(stop_policy)
            configure_score = getattr(strategy, "configure_score_policy", None)
            if callable(configure_score):
                configure_score(build_candidate_score_policy(configuration))
        ablation = (configuration.get('research') or {}).get('strategy_ablation')
        if ablation not in (None, 'no_obv_confirmation', 'no_regime_restrictions', 'no_health'):
            raise ValueError('Unknown strategy ablation')
        if ablation == 'no_obv_confirmation':
            registry['TrendBreakout'].use_obv = False
        elif ablation == 'no_regime_restrictions':
            registry['TrendBreakout'].allowed_states = set(MarketState)
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
    budget_policy = configuration.get("drawdown_budget") or {}
    if (budget_policy.get("stop_basis", "original") != "original"
            or not budget_policy.get("reduction_enabled", True)
            or budget_policy.get("review_interval_bars", 1) != 1):
        research = configuration.get("research") or {}
        if not research.get("experiment_id") or not research.get("overlay_study"):
            raise ValueError("Drawdown overlay alternatives require a registered research study")
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
        recovery_policy=drawdown.get("recovery"),
        minimum_entry_policy=configuration.get("risk", "minimum_entry"),
        drawdown_budget_policy=configuration.get("drawdown_budget"),
        execution_costs=configuration.get("execution"),
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
    capital_policy: Optional[CapitalAllocationPolicy] = None,
) -> Router:
    routing_config = dict(configuration.require("routing"))
    for name, controls in (configuration.get("research") or {}).get("regime_controls", {}).items():
        if "entry_states" in controls:
            actual = {state for state, strategy in routing_config.items() if strategy == name}
            if actual != set(controls["entry_states"]):
                raise ValueError("Registered regime entry states must match explicit routing")
    # Preserve V2's explicit routing identity across normal regimes. Its
    # TREND_DOWN multiplier blocks new longs while owned positions retain exits.
    if not allow_short and routing_config.get("TREND_DOWN") != "TrendPortfolioV2":
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
        capital_policy=capital_policy if capital_policy is not None else
            CapitalAllocationPolicy.from_mapping((configuration.get("allocation") or {}).get("capital")),
    )


def market_type_supports_shorts(market_type: Optional[str]) -> bool:
    return (market_type or "").lower() in DERIVATIVE_MARKET_TYPES
