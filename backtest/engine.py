"""Backtest engine composed from the shared runtime and historical adapters."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional
from uuid import uuid4

import pandas as pd

from backtest.execution_adapter import SimulatedExecutionAdapter
from backtest.protective_stops import CONSERVATIVE_BAR_PATH, ResidentStopSimulator
from composition.factory import (
    build_risk_manager,
    build_router,
    build_state_machine,
    build_strategy_registry,
)
from config.config import config
from core.accounting_check import AccountingReconciler
from core.account_cost_contract import validate_account_cost_contract
from core.accounts import AccountMode
from core.benchmarks import (
    dynamic_equal_weight_rebalanced,
    fixed_equal_weight_buy_hold,
)
from core.broker import Broker
from core.events import TradingEventPipeline
from core.logger import get_logger
from core.market_data import HistoricalMarketDataAdapter, normalize_market_frame
from core.metrics import calculate_exposure
from core.portfolio import Portfolio
from core.runtime import EventProcessor
from core.protective_stops import EntryRiskPolicy, evaluate_fill_risk
from core.strategy_health import cohort_rows, transition_rows

logger = get_logger(__name__)

DEFAULT_INITIAL_CAPITAL = 10000.0


class BacktestEngine:
    """Historical scheduler; trading decisions live in :class:`EventProcessor`."""

    def __init__(
        self,
        initial_capital: float = DEFAULT_INITIAL_CAPITAL,
        slippage: Optional[float] = None,
        random_slip: bool = False,
        warmup_period: int = 30,
        alignment_mode: Optional[str] = None,
        benchmark_mode: Optional[str] = None,
        benchmark_rebalance_cost_bps: Optional[float] = None,
        timeframe: Optional[str] = None,
        universe: Optional[object] = None,
        run_id: Optional[str] = None,
        account_mode: Optional[str] = None,
        breaker_policy: Optional[Dict[str, Any]] = None,
    ) -> None:
        config_data = config.require("data")
        config_benchmark = config.require("benchmark")
        alignment_mode = alignment_mode or config_data["alignment_mode"]
        benchmark_mode = benchmark_mode or config_benchmark["mode"]
        benchmark_rebalance_cost_bps = (
            config_benchmark["dynamic_rebalance_cost_bps"]
            if benchmark_rebalance_cost_bps is None
            else benchmark_rebalance_cost_bps
        )
        timeframe = timeframe or config_data["timeframe"]
        if alignment_mode not in {"union", "intersection"}:
            raise ValueError("alignment_mode must be 'union' or 'intersection'")
        if benchmark_mode not in {"fixed", "dynamic"}:
            raise ValueError("benchmark_mode must be 'fixed' or 'dynamic'")
        if benchmark_rebalance_cost_bps < 0:
            raise ValueError("benchmark_rebalance_cost_bps cannot be negative")
        self.initial_capital = initial_capital
        self.config_execution = config.require("execution")
        self.config_risk = config.require("risk")
        self.config_account = config.get("account") or {}
        configured_policy = dict((config.get("backtest") or {}).get("breaker_policy") or {})
        self.breaker_policy = {**configured_policy, **(breaker_policy or {})}
        self.breaker_policy.setdefault("on_reduce", "continue_reduced")
        self.breaker_policy.setdefault("on_block_new", "exit_only")
        self.breaker_policy.setdefault("on_liquidate", "terminate")
        self.breaker_policy.setdefault("on_locked", "terminate")
        self.breaker_policy.setdefault("shadow_diagnostics", True)
        self.breaker_policy.setdefault("recovery", {"mode": "none"})
        if self.breaker_policy["on_liquidate"] not in {"terminate", "cooldown"}:
            raise ValueError("breaker_policy.on_liquidate must be terminate or cooldown")
        self.account_mode = AccountMode(
            account_mode or self.config_account.get("mode", AccountMode.SPOT.value)
        )
        self.slippage = (
            self.config_execution["slippage_bps"] / 10000.0
            if slippage is None
            else slippage
        )
        self.random_slip = random_slip
        self.warmup_period = warmup_period
        self.alignment_mode = alignment_mode
        self.benchmark_mode = benchmark_mode
        self.benchmark_rebalance_cost_bps = benchmark_rebalance_cost_bps
        self.timeframe = timeframe
        self.universe = universe
        self.run_id = run_id or str(uuid4())
        self.market_data_adapter: Optional[HistoricalMarketDataAdapter] = None
        self.execution_adapter: Optional[SimulatedExecutionAdapter] = None
        self.event_processor: Optional[EventProcessor] = None
        self.stop_simulator: Optional[ResidentStopSimulator] = None

    @staticmethod
    def _prepare_dataframe(df: pd.DataFrame) -> pd.DataFrame:
        return normalize_market_frame(df)

    def run(
        self,
        data_map: Dict[str, pd.DataFrame],
        strategies: Optional[Dict[str, Any]] = None,
        routing_log_path: Optional[str] = None,
        routing_log_enabled: bool = True,
    ) -> Dict[str, Any]:
        if not data_map:
            return {}

        initial_margin_rate = float(
            self.config_account.get(
                "initial_margin_rate",
                1.0 / max(float(self.config_risk["max_leverage"]), 1.0),
            )
        )
        portfolio = Portfolio(
            self.initial_capital,
            account_mode=self.account_mode,
            initial_margin_rate=initial_margin_rate,
            maintenance_margin_rate=float(
                self.config_account.get("maintenance_margin_rate", 0.05)
            ),
        )
        event_pipeline = TradingEventPipeline(
            run_id=self.run_id,
            retention_limit=250000,
        )
        # SR3-4: one account mode, one cost model. Refuse to run a spot-margin
        # book on futures fees, or a perpetual book with optional funding.
        self.account_cost_contract = validate_account_cost_contract(
            config, account_mode=self.account_mode.value,
        )
        broker = Broker(
            portfolio,
            slippage=self.slippage,
            random_slip=self.random_slip,
            commission_rate=self.config_execution["commission_rate_taker"],
            commission_rate_maker=self.config_execution["commission_rate_maker"],
            use_impact_cost=self.config_execution["use_impact_cost"],
            max_participation_rate=self.config_execution.get(
                "max_participation_rate",
                self.config_risk["liquidity_limit_pct"],
            ),
            spread_bps=self.config_execution.get("spread_bps", 0.0),
            volatility_slippage_factor=self.config_execution.get(
                "volatility_slippage_factor", 0.0
            ),
            impact_coefficient=self.config_execution.get("impact_coefficient", 0.10),
            impact_exponent=self.config_execution.get("impact_exponent", 1.5),
            funding_interval_hours=self.config_account.get("funding_interval_hours", 8.0),
            funding_rate_required=self.config_account.get("funding_rate_required", True),
            default_borrow_rate_annual=self.config_account.get(
                "default_borrow_rate_annual", 0.0
            ),
            borrow_availability_required=self.config_account.get(
                "borrow_availability_required", False
            ),
            default_borrow_limit_qty=self.config_account.get(
                "default_borrow_limit_qty", float("inf")
            ),
            liquidation_penalty_bps=self.config_account.get(
                "liquidation_penalty_bps", 0.0
            ),
            opening_order_ttl_bars=self.config_execution.get(
                "opening_order_ttl_bars", 0
            ),
            event_pipeline=event_pipeline,
            timeframe=self.timeframe,
        )
        risk_manager = build_risk_manager(config)
        state_machine = build_state_machine(config)
        strategies = strategies or build_strategy_registry(config)
        for strategy in strategies.values():
            reset = getattr(strategy, "reset_runtime_state", None)
            if callable(reset):
                reset()

        if routing_log_enabled:
            if routing_log_path is None:
                routing_log_path = os.path.join(
                    os.getcwd(), "reports", "routing_log.csv"
                )
            log_dir = os.path.dirname(routing_log_path)
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)
        else:
            routing_log_path = None
        router = build_router(strategies, config, log_path=routing_log_path)

        market_data = HistoricalMarketDataAdapter(
            data_map,
            timeframe=self.timeframe,
            alignment_mode=self.alignment_mode,
            universe=self.universe,
        )
        processed_data = market_data.data_map
        # Early exits must keep the same result keys as a completed run, or
        # callers that read close_events/benchmark silently get None and the
        # diagnostics that depend on them are dropped without explanation.
        empty_result = {
            "trades": [],
            "equity_curve": pd.DataFrame(),
            "benchmark": None,
            "benchmark_fixed": None,
            "benchmark_dynamic": None,
            "benchmark_weights": pd.DataFrame(),
            "benchmark_turnover": pd.Series(dtype=float),
            "benchmark_costs": pd.Series(dtype=float),
            "benchmark_metadata": {},
            "close_events": {name: 0 for name in strategies},
            "accounting_check": AccountingReconciler(self.initial_capital).result().to_dict(),
            "event_log": tuple(event_pipeline.events),
            "run_id": self.run_id,
            "alignment_mode": self.alignment_mode,
            "account_mode": self.account_mode.value,
            "margin_ledger": [],
            "financing_ledger": [],
            "execution_audit": [],
            "breaker_audit": [],
            "breaker_state": {
                "action": "normal",
                "high_water_equity": None,
                "drawdown": 0.0,
                "daily_loss_triggered": False,
            },
            "lifecycle": {
                "status": "data_exhausted",
                "active_start": None,
                "active_end": None,
                "termination_timestamp": None,
                "termination_reason": None,
                "inactive_bars": 0,
                "inactive_days": 0,
                "resume_count": 0,
                "breaker_epochs": 1,
                "suppressed_setups_after_termination": None,
                "strategy_health_status": {},
                "disabled_or_cooldown_at": None,
                "health_gated_days": 0,
                "probation_periods": 0,
                "health_resume_count": 0,
                "health_transition_log": [],
            },
        }
        if not processed_data:
            logger.warning("No valid symbol data available after normalization")
            return empty_result
        timestamps = market_data.timestamps
        if len(timestamps) == 0:
            return empty_result

        execution = SimulatedExecutionAdapter(broker)
        # STR-P1-01: the backtest carries the same venue-resident protective
        # stop the live path carries, and fills it inside the bar on the
        # pre-registered conservative OHLC path instead of discovering the
        # breach at the close and exiting at the next open.
        protective_config = config.get("protective_orders") or {}
        stop_simulator = ResidentStopSimulator(
            broker, strategies,
            enabled=bool(protective_config.get("backtest_resident", True)),
        )
        self.stop_simulator = stop_simulator
        processor = EventProcessor(
            portfolio=portfolio,
            execution=execution,
            risk_manager=risk_manager,
            state_machine=state_machine,
            router=router,
            allocator=router.allocator,
            warmup_period=self.warmup_period,
            initial_equity=self.initial_capital,
        )
        self.market_data_adapter = market_data
        self.execution_adapter = execution
        self.event_processor = processor

        logger.info("Starting backtest on %s bars", len(timestamps))
        equity_curve = []
        # BM3: the book behind every equity point. Without it a Sharpe cannot
        # be read - a flat stretch of equity means one thing at 2x gross and
        # another thing flat in cash, and the equity curve alone cannot say
        # which. Only non-flat symbols and their marks are kept, which is what
        # calculate_exposure counts anyway.
        exposure_positions: Dict[Any, Dict[str, float]] = {}
        exposure_prices: Dict[Any, Dict[str, float]] = {}
        accounting = AccountingReconciler(self.initial_capital)
        bar_index = -1
        last_event_timestamp = None
        applied_breaker_actions: set[str] = set()
        entry_risk_policy = EntryRiskPolicy.from_mapping(
            config.get("entry_risk") if isinstance(config.get("entry_risk"), dict)
            else None
        )
        risk_budget_audit: list[Dict[str, Any]] = []
        checked_lot_ids: set[str] = set()
        lifecycle = {
            "status": "completed",
            "active_start": None,
            "active_end": None,
            "termination_timestamp": None,
            "termination_reason": None,
            "inactive_bars": 0,
            "inactive_days": 0,
            "resume_count": 0,
            "breaker_epochs": 1,
            # None explicitly means shadow evaluation was not enabled; it is
            # never misreported as a market with zero setups.
            "suppressed_setups_after_termination": None,
            "strategy_health_status": {},
            "disabled_or_cooldown_at": None,
            "health_gated_days": 0,
            "probation_periods": 0,
            "health_resume_count": 0,
            "health_transition_log": [],
        }
        recovery = dict(self.breaker_policy.get("recovery") or {})
        recovery_mode = str(recovery.get("mode", "none"))
        flat_bars = 0
        health_bars = 0
        waiting_for_recovery = False
        for bar_index, event in enumerate(market_data.stream()):
            last_event_timestamp = event.timestamp
            if lifecycle["active_start"] is None:
                lifecycle["active_start"] = event.timestamp
            if waiting_for_recovery and recovery_mode == "timed_rebase":
                flat_bars += 1
                assessment = getattr(risk_manager, "health_assessment", None)
                healthy = assessment is None or bool(
                    getattr(assessment, "allows_new_risk", False)
                )
                health_bars = health_bars + 1 if healthy else 0
                max_resumes = int(recovery.get("max_resumes", 1))
                if (
                    flat_bars >= int(recovery.get("flat_bars_required", 30))
                    and health_bars >= int(recovery.get("health_bars_required", 5))
                    and lifecycle["resume_count"] < max_resumes
                ):
                    current_equity = portfolio.get_total_value(processor.last_prices)
                    risk_manager.manual_resume(
                        approved_by=str(
                            recovery.get("approved_by", "backtest_protocol_v1")
                        ),
                        current_equity=current_equity,
                        rebase_high_water=bool(recovery.get("rebase_high_water", True)),
                        occurred_at=event.timestamp,
                        bar_index=bar_index,
                    )
                    lifecycle["resume_count"] += 1
                    lifecycle["breaker_epochs"] = risk_manager.breaker_epoch + 1
                    waiting_for_recovery = False
                    flat_bars = 0
                    health_bars = 0
            # One bar, one order: (1) the previous bar's queued orders fill at
            # this open, (2) protection is reconciled against the position that
            # now exists and matched against this bar, (3) strategy logic runs
            # on the close. Splitting the market event out of ``process`` is
            # what puts the resident stop between (1) and (3).
            execution.on_market_data(event)
            stop_simulator.step(event, bar_index=bar_index)
            result = processor.process(event, execute_market_event=False)
            # T-1.10: sample this bar's high/low against every open lot so
            # MAE/MFE (adverse/favorable excursion) are available at close,
            # not just entry/exit prices.
            for symbol, bar in event.bars.items():
                if symbol in portfolio.lot_books:
                    portfolio.update_lot_extremes(
                        symbol, float(bar["high"]), float(bar["low"])
                    )
            margin = portfolio.margin_snapshot(
                result.prices, timestamp=event.timestamp, record=True
            )
            forced_trades = []
            action_cost = 0.0
            positions_before = sum(
                1 for symbol in portfolio.positions
                if float(portfolio.get_position(symbol).get("qty", 0.0)) != 0.0
            )
            decision = result.risk_decision
            transition_id = (
                decision.transition_id if decision is not None else None
            )
            portfolio_action_id = getattr(
                risk_manager, "current_transition_id", None
            )
            if margin.liquidation_required:
                forced_trades.extend(
                    broker.force_liquidate(
                        dict(event.bars),
                        timestamp=event.timestamp,
                        reason="MarginLiquidation",
                        risk_action_id=(
                            f"epoch-{getattr(risk_manager, 'breaker_epoch', 0)}"
                            f"-margin-{bar_index}"
                        ),
                    )
                )
            elif result.breaker_action == "reduce" and (
                transition_id or f"epoch-{getattr(risk_manager, 'breaker_epoch', 0)}-reduce"
            ) not in applied_breaker_actions and not (
                decision is not None and decision.daily_loss_triggered
            ):
                forced_trades.extend(
                    broker.force_liquidate(
                        dict(event.bars),
                        timestamp=event.timestamp,
                        reason="DrawdownReduce",
                        remaining_fraction=risk_manager.reduced_risk_multiplier,
                        risk_action_id=(
                            transition_id
                            or f"epoch-{getattr(risk_manager, 'breaker_epoch', 0)}-reduce"
                        ),
                    )
                )
                applied_breaker_actions.add(
                    transition_id or f"epoch-{getattr(risk_manager, 'breaker_epoch', 0)}-reduce"
                )
            elif result.breaker_action in {"liquidate", "locked"}:
                action_key = transition_id or (
                    f"epoch-{getattr(risk_manager, 'breaker_epoch', 0)}-"
                    f"{result.breaker_action}"
                )
                if action_key not in applied_breaker_actions:
                    forced_trades.extend(
                        broker.force_liquidate(
                            dict(event.bars),
                            timestamp=event.timestamp,
                            reason="AccountLiquidation",
                            risk_action_id=action_key,
                        )
                    )
                    applied_breaker_actions.add(action_key)
            elif result.circuit_breaker and result.breaker_action in {"normal", "reduce"} and (
                transition_id or f"daily-{event.timestamp.date()}"
            ) not in applied_breaker_actions:
                forced_trades.extend(
                    broker.force_liquidate(
                        dict(event.bars),
                        timestamp=event.timestamp,
                        reason="DailyLossLimit",
                        # One daily-loss action = one health cohort, no matter
                        # how many correlated symbols it closes (STR-P0-02).
                        risk_action_id=(
                            getattr(risk_manager, "current_daily_action_id", None)
                            or transition_id
                            or f"daily-{event.timestamp.date()}"
                        ),
                    )
                )
                applied_breaker_actions.add(
                    transition_id or f"daily-{event.timestamp.date()}"
                )
            if forced_trades:
                for strategy in strategies.values():
                    for symbol in event.bars:
                        strategy._consume_execution_trades(
                            symbol, bar_index, portfolio, execution
                        )
                result.equity = portfolio.get_equity(result.prices)
                result.cash = float(portfolio.cash)
                portfolio.margin_snapshot(
                    result.prices, timestamp=event.timestamp, record=True
                )
                action_cost = sum(
                    float(item.get("commission", 0.0) or 0.0)
                    + abs(float(item.get("slip", 0.0) or 0.0))
                    for item in forced_trades
                )
                processor._previous_session_close_equity = result.equity
            # SR2-4: the real fill, not the signal close, decides how much is
            # at stake. A breakout that gapped through the open is resized down
            # under an explicit ``GapRiskResize``, never left silently over
            # budget (STR-P1-02).
            self._recheck_entry_risk(
                portfolio=portfolio,
                execution=execution,
                strategies=strategies,
                risk_manager=risk_manager,
                equity=result.equity,
                prices=result.prices,
                timestamp=event.timestamp,
                bar_index=bar_index,
                policy=entry_risk_policy,
                checked_lot_ids=checked_lot_ids,
                audit=risk_budget_audit,
            )
            risk_manager.record_breaker_action_result(
                transition_id,
                post_action_equity=result.equity,
                cost=action_cost,
                positions_before=positions_before,
                positions_after=sum(
                    1 for symbol in portfolio.positions
                    if float(portfolio.get_position(symbol).get("qty", 0.0)) != 0.0
                ),
            )
            if portfolio_action_id and portfolio_action_id != transition_id:
                risk_manager.record_breaker_action_result(
                    portfolio_action_id,
                    post_action_equity=result.equity,
                    cost=0.0,
                    positions_before=positions_before,
                    positions_after=sum(
                        1 for symbol in portfolio.positions
                        if float(portfolio.get_position(symbol).get("qty", 0.0)) != 0.0
                    ),
                    executed=False,
                    overridden_by=transition_id,
                )
            equity_curve.append(
                {
                    "timestamp": event.timestamp,
                    "equity": result.equity,
                    "cash": result.cash,
                }
            )
            self._sample_exposure(
                portfolio, result.prices, event.timestamp,
                exposure_positions, exposure_prices,
            )
            # T-1.8: accounting-identity check (Gate G2), one lightweight pass
            # per bar using the same equity/prices the loop already computed.
            accounting.check_bar(
                bar_index, event.timestamp, result.equity, portfolio,
                result.prices, broker.close_events,
            )
            terminal_action = result.breaker_action in {"liquidate", "locked"}
            policy_key = "on_locked" if result.breaker_action == "locked" else "on_liquidate"
            if terminal_action and self.breaker_policy.get(policy_key, "terminate") == "terminate":
                lifecycle.update({
                    "status": "locked" if result.breaker_action == "locked" else "terminated_by_risk",
                    "active_end": event.timestamp,
                    "termination_timestamp": event.timestamp,
                    "termination_reason": f"portfolio_{result.breaker_action}",
                    "inactive_bars": max(0, len(timestamps) - bar_index - 1),
                    "inactive_days": max(
                        0, (pd.Timestamp(timestamps[-1]) - event.timestamp).days
                    ),
                })
                break
            if terminal_action and self.breaker_policy.get(policy_key) == "cooldown":
                if lifecycle["resume_count"] >= int(recovery.get("max_resumes", 1)):
                    lifecycle.update({
                        "status": "terminated_by_risk",
                        "active_end": event.timestamp,
                        "termination_timestamp": event.timestamp,
                        "termination_reason": "recovery_limit_exhausted",
                        "inactive_bars": max(0, len(timestamps) - bar_index - 1),
                        "inactive_days": max(
                            0, (pd.Timestamp(timestamps[-1]) - event.timestamp).days
                        ),
                    })
                    break
                if not waiting_for_recovery:
                    waiting_for_recovery = True
                    flat_bars = 0
                    health_bars = 0
        if lifecycle["status"] not in {"terminated_by_risk", "locked"}:
            self._close_tail_positions(
                portfolio, broker, execution, strategies, equity_curve, accounting,
                last_event_timestamp, bar_index,
                exposure_positions=exposure_positions,
                exposure_prices=exposure_prices,
            )
            lifecycle["active_end"] = last_event_timestamp
        else:
            terminal_equity = float(equity_curve[-1]["equity"])
            terminal_cash = float(equity_curve[-1]["cash"])
            if bool(self.breaker_policy.get("shadow_diagnostics", True)):
                shadow_router = build_router(strategies, config, log_path=None)
                shadow_processor = EventProcessor(
                    portfolio=portfolio,
                    execution=execution,
                    risk_manager=risk_manager,
                    state_machine=state_machine,
                    router=shadow_router,
                    allocator=shadow_router.allocator,
                    warmup_period=self.warmup_period,
                    initial_equity=terminal_equity if equity_curve else self.initial_capital,
                )
                suppressed = 0
                termination_timestamp = pd.Timestamp(
                    lifecycle["termination_timestamp"]
                )
                for shadow_event in market_data.stream():
                    if shadow_event.timestamp <= termination_timestamp:
                        continue
                    for symbol, bar in shadow_event.bars.items():
                        close = float(bar["close"])
                        if pd.notna(close):
                            shadow_processor.last_prices[symbol] = close
                        candidate, _ = shadow_processor._collect_symbol_candidate(
                            shadow_event,
                            symbol,
                            allow_position_management=False,
                            allow_new_entries=True,
                        )
                        if candidate is not None:
                            suppressed += 1
                lifecycle["suppressed_setups_after_termination"] = suppressed
            # Preserve the full capital-period experience as an explicit flat
            # cash tail while active-strategy metrics stop at active_end.
            frozen_prices = (
                dict(self.event_processor.last_prices) if self.event_processor else {}
            )
            for inactive_timestamp in timestamps[bar_index + 1:]:
                equity_curve.append({
                    "timestamp": inactive_timestamp,
                    "equity": terminal_equity,
                    "cash": terminal_cash,
                })
                # The book is frozen too, so record it rather than leaving a
                # hole that would read as "exposure unknown" in equity.csv.
                self._sample_exposure(
                    portfolio, frozen_prices, inactive_timestamp,
                    exposure_positions, exposure_prices,
                )
        # SR1-4: fold the strategy health lifecycle into the run lifecycle so
        # a strategy that stopped trading years ago cannot be reported as an
        # uneventful "completed" run.
        health_machines = {
            name: strategy.health for name, strategy in strategies.items()
            if getattr(strategy, "health", None) is not None
        }
        for machine in health_machines.values():
            machine.evaluate(last_event_timestamp)
        lifecycle["strategy_health_status"] = {
            name: machine.status.value for name, machine in health_machines.items()
        }
        cooldown_starts = [
            machine.status_changed_at for machine in health_machines.values()
            if machine.status.value != "active" and machine.status_changed_at
        ]
        lifecycle["disabled_or_cooldown_at"] = (
            min(cooldown_starts).isoformat() if cooldown_starts else None
        )
        lifecycle["health_transition_log"] = transition_rows(
            list(health_machines.values())
        )
        lifecycle["suppressed_raw_setups"] = sum(
            int(getattr(strategy, "suppressed_setup_count", 0))
            for strategy in strategies.values()
        )
        lifecycle["shadow_setup_count"] = sum(
            int(getattr(strategy, "raw_setup_count", 0))
            for strategy in strategies.values()
        )
        lifecycle["probation_periods"] = sum(
            1 for machine in health_machines.values()
            for row in machine.transitions if row.get("to") == "probation"
        )
        lifecycle["health_resume_count"] = sum(
            machine.resume_count for machine in health_machines.values()
        )
        if lifecycle["disabled_or_cooldown_at"] and last_event_timestamp is not None:
            gate_start = pd.Timestamp(min(cooldown_starts))
            end_stamp = pd.Timestamp(last_event_timestamp)
            if gate_start.tzinfo is not None and end_stamp.tzinfo is None:
                end_stamp = end_stamp.tz_localize("UTC")
            elif gate_start.tzinfo is None and end_stamp.tzinfo is not None:
                gate_start = gate_start.tz_localize("UTC")
            lifecycle["health_gated_days"] = max(0, (end_stamp - gate_start).days)
        else:
            lifecycle["health_gated_days"] = 0

        router.save_log()
        logger.info("Backtest completed")

        fixed_benchmark = fixed_equal_weight_buy_hold(
            processed_data, self.initial_capital, start_idx=self.warmup_period
        )
        dynamic_benchmark = dynamic_equal_weight_rebalanced(
            processed_data,
            self.initial_capital,
            start_idx=self.warmup_period,
            cost_bps=self.benchmark_rebalance_cost_bps,
        )
        selected_benchmark = (
            dynamic_benchmark if self.benchmark_mode == "dynamic" else fixed_benchmark
        )
        return {
            "trades": broker.trades,
            "equity_curve": self._equity_frame(
                equity_curve, exposure_positions, exposure_prices
            ),
            "benchmark": selected_benchmark.equity if selected_benchmark else None,
            "benchmark_fixed": fixed_benchmark.equity if fixed_benchmark else None,
            "benchmark_dynamic": dynamic_benchmark.equity if dynamic_benchmark else None,
            "benchmark_weights": (
                selected_benchmark.weights if selected_benchmark else pd.DataFrame()
            ),
            "benchmark_turnover": (
                selected_benchmark.turnover if selected_benchmark else pd.Series(dtype=float)
            ),
            "benchmark_costs": (
                selected_benchmark.costs if selected_benchmark else pd.Series(dtype=float)
            ),
            "benchmark_metadata": {
                "selected": self.benchmark_mode,
                "fixed": fixed_benchmark.metadata if fixed_benchmark else None,
                "dynamic": dynamic_benchmark.metadata if dynamic_benchmark else None,
            },
            # Per-strategy count of round trips each strategy actually saw
            # close. Diagnostics compares this against the reconstructed
            # closed-trade count to detect strategies whose on_trade_closed
            # hook never fires (e.g. positions liquidated by the router), which
            # silently disables their health/cooldown safeguards.
            "close_events": {
                name: int(getattr(strategy, "observed_close_events", 0))
                for name, strategy in strategies.items()
            },
            # T-1.8 / Gate G2: equity(t) == initial_capital + realized + unrealized,
            # checked every bar and summarized here for the report/roadmap gate.
            "accounting_check": accounting.result().to_dict(),
            "event_log": tuple(event_pipeline.events),
            "run_id": self.run_id,
            "alignment_mode": self.alignment_mode,
            "account_mode": self.account_mode.value,
            "account_cost_contract": self.account_cost_contract.to_dict(),
            "margin_ledger": [item.to_dict() for item in portfolio.margin_ledger],
            "financing_ledger": [item.to_dict() for item in portfolio.financing_ledger],
            "execution_audit": list(broker.execution_audit),
            "breaker_audit": list(risk_manager.breaker_audit),
            "breaker_state": {
                "action": risk_manager.breaker_action.value,
                "high_water_equity": risk_manager.high_water_equity,
                "drawdown": risk_manager.last_drawdown,
                "daily_loss_triggered": bool(risk_manager.daily_loss_triggered),
                "blocks_new_risk": bool(risk_manager._blocks_new_risk()),
                "liquidation_triggered": risk_manager.breaker_action.value in {"liquidate", "locked"},
                "locked": risk_manager.breaker_action.value == "locked",
                "breaker_epoch": risk_manager.breaker_epoch,
            },
            "lifecycle": lifecycle,
            # SR1-4: the health lifecycle is a first-class run artifact, not a
            # value that only exists inside a strategy object.
            "strategy_health": {
                name: strategy.health_snapshot()
                for name, strategy in strategies.items()
                if strategy.health_snapshot()
            },
            "strategy_health_transitions": transition_rows([
                strategy.health for strategy in strategies.values()
                if getattr(strategy, "health", None) is not None
            ]),
            # SR2-4 deliverable: reserved vs actually-risked, per entry fill.
            "risk_budget_reconciliation": risk_budget_audit,
            # STR-P1-01 deliverable: every protective-stop intent and fill the
            # backtest produced, in the same shape the live path audits.
            "stop_order_audit": list(stop_simulator.audit),
            "protective_stop_summary": {
                "backtest_resident_enabled": stop_simulator.enabled,
                "intrabar_path": CONSERVATIVE_BAR_PATH,
                "triggered_stops": stop_simulator.triggered_stops,
                "unprotected_position_bars": (
                    stop_simulator.unprotected_position_bars
                ),
            },
            # SR3-1/SR3-2 deliverables: how every same-bar batch was ordered
            # (and whether the order was a real ranking), and what the
            # correlated-risk budget did to each candidate.
            "allocation_audit": [
                {
                    "symbol": decision.symbol,
                    "strategy": decision.strategy,
                    "score": decision.score,
                    "rank": decision.rank,
                    "accepted": decision.accepted,
                    "reason": decision.reason,
                    "ordering": decision.ordering,
                    **{
                        f"risk_budget_{key}": value
                        for key, value in (decision.risk_budget or {}).items()
                    },
                }
                for decision in router.allocator.audit
            ],
            "degenerate_ranking_batches": router.allocator.degenerate_batches,
            "correlated_risk_audit": list(router.allocator.risk_governor.audit),
            "strategy_health_cohorts": cohort_rows([
                strategy.health for strategy in strategies.values()
                if getattr(strategy, "health", None) is not None
            ]),
        }

    @staticmethod
    def _sample_exposure(
        portfolio: Portfolio,
        prices: Dict[str, float],
        timestamp: Any,
        positions_by_time: Dict[Any, Dict[str, float]],
        prices_by_time: Dict[Any, Dict[str, float]],
    ) -> None:
        """Record the non-flat book and its marks for one equity-curve row.

        Flat symbols are dropped because :func:`core.metrics.calculate_exposure`
        skips them anyway, and carrying the whole universe for every bar would
        make this the largest object the run holds.
        """
        held: Dict[str, float] = {}
        marks: Dict[str, float] = {}
        for symbol in portfolio.positions:
            qty = float(portfolio.get_position(symbol).get("qty", 0.0))
            if qty == 0.0:
                continue
            held[symbol] = qty
            price = prices.get(symbol)
            if price is not None:
                marks[symbol] = float(price)
        positions_by_time[timestamp] = held
        prices_by_time[timestamp] = marks

    @staticmethod
    def _equity_frame(
        equity_rows: list,
        positions_by_time: Dict[Any, Dict[str, float]],
        prices_by_time: Dict[Any, Dict[str, float]],
    ) -> pd.DataFrame:
        """The equity curve with its exposure columns joined on (BM3).

        Exposure ships as columns of the curve rather than as a separate
        result key so every consumer that already has the curve - equity.csv,
        the workbook's Equity sheet, the charts - gets it without a new
        parameter, and so a row can never be paired with another row's book.
        """
        frame = pd.DataFrame(equity_rows)
        if frame.empty:
            return frame
        frame = frame.set_index("timestamp")
        exposure = calculate_exposure(
            positions_by_time, prices_by_time, frame["equity"].to_dict()
        )
        if exposure.empty:
            return frame
        return frame.join(exposure)

    def _recheck_entry_risk(
        self,
        *,
        portfolio: Portfolio,
        execution: SimulatedExecutionAdapter,
        strategies: Dict[str, Any],
        risk_manager: Any,
        equity: float,
        prices: Dict[str, float],
        timestamp: Any,
        bar_index: int,
        policy: EntryRiskPolicy,
        checked_lot_ids: set,
        audit: list,
    ) -> None:
        """SR2-4: re-derive each entry's real risk and act when it exceeds budget.

        The reservation was made from the signal bar's close; the fill happens
        at the next bar's open and can gap. Every newly opened lot is measured
        once, against the equity at that moment and the strategy's current
        health multiplier, and a breach produces a named reduce order through
        the ordinary order path (filling on the next bar, which is the earliest
        a real system could act on a fill it has just learned about).
        """
        if not policy.enabled:
            return
        for symbol, lot_book in portfolio.lot_books.items():
            for lot in lot_book.open_lots:
                if lot.lot_id in checked_lot_ids:
                    continue
                checked_lot_ids.add(lot.lot_id)
                strategy = strategies.get(lot.strategy_id)
                multiplier = 1.0
                if strategy is not None:
                    getter = getattr(strategy, "health_risk_multiplier", None)
                    if callable(getter):
                        multiplier = float(getter())
                assessment = evaluate_fill_risk(
                    symbol=symbol,
                    lot_id=lot.lot_id,
                    side=lot.side,
                    fill_price=float(lot.entry_price),
                    protective_stop=lot.stop_price,
                    filled_qty=float(lot.qty_open),
                    equity_at_fill=float(equity),
                    base_risk_per_trade=float(
                        getattr(risk_manager, "risk_per_trade", 0.0)
                    ),
                    health_risk_multiplier=multiplier,
                    policy=policy,
                )
                if assessment is None:
                    continue
                row = assessment.to_dict()
                row.update({
                    "timestamp": timestamp,
                    "bar_index": bar_index,
                    "strategy_id": lot.strategy_id,
                    "health_risk_multiplier": multiplier,
                })
                audit.append(row)
                if assessment.action != "resize" or assessment.resize_qty <= 0:
                    continue
                mark = prices.get(symbol) or float(lot.entry_price)
                execution.submit_order(
                    symbol,
                    "sell" if lot.side == "long" else "cover",
                    assessment.resize_qty,
                    mark,
                    timestamp=timestamp,
                    strategy_id=lot.strategy_id,
                    exit_reason="GapRiskResize",
                )
                logger.warning(
                    "GapRiskResize %s lot=%s risk=%.2f budget=%.2f (%.2fx) "
                    "reducing %.8f",
                    symbol, lot.lot_id, assessment.actual_total_risk,
                    assessment.risk_budget, assessment.risk_ratio,
                    assessment.resize_qty,
                )

    def _close_tail_positions(
        self,
        portfolio: Portfolio,
        broker: Broker,
        execution: SimulatedExecutionAdapter,
        strategies: Dict[str, Any],
        equity_curve: list,
        accounting: AccountingReconciler,
        last_event_timestamp: Any,
        bar_index: int,
        exposure_positions: Optional[Dict[Any, Dict[str, float]]] = None,
        exposure_prices: Optional[Dict[Any, Dict[str, float]]] = None,
    ) -> None:
        """T-1.11: EndOfBacktest - close whatever is still open when the data
        runs out, through the same Broker/CloseEvent path as every other exit
        (T-1.3), so no trade silently falls out of trade-level analytics just
        because the run ended while it was open (I-37).
        """
        if last_event_timestamp is None:
            return
        open_symbols = [
            symbol
            for symbol, lot_book in portfolio.lot_books.items()
            if lot_book.open_lots and portfolio.get_position(symbol).get("qty", 0.0) != 0.0
        ]
        if not open_symbols:
            return
        # EndOfBacktest is now the authoritative close: a resident stop left
        # armed would otherwise also match the synthetic bar and sell twice.
        broker.cancel_protective_stops()

        eob_mode = config.get("backtest", "end_of_backtest_mode") or "mark_to_market"
        zero_cost = eob_mode == "mark_to_market"
        synthetic_time = last_event_timestamp + pd.Timedelta(microseconds=1)
        synthetic_bars: Dict[str, pd.Series] = {}
        for symbol in open_symbols:
            qty = portfolio.get_position(symbol)["qty"]
            mark_price = self.event_processor.last_prices.get(symbol) if self.event_processor else None
            if mark_price is None:
                continue
            side = "sell" if qty > 0 else "cover"
            execution.submit_order(
                symbol,
                side,
                abs(qty),
                mark_price,
                timestamp=last_event_timestamp,
                strategy_id="EndOfBacktest",
                exit_reason="EndOfBacktest",
                zero_cost=zero_cost,
            )
            synthetic_bars[symbol] = pd.Series(
                {
                    "open": mark_price, "high": mark_price, "low": mark_price,
                    "close": mark_price, "volume": 1e18,
                },
                name=synthetic_time,
            )
        if not synthetic_bars:
            return

        broker.process_orders(synthetic_bars)
        # No more routing will happen this run, so nothing else will ever
        # deliver these tail CloseEvents - flush them explicitly (T-1.4/T-1.5
        # still apply: dedup by close_event_id keeps this idempotent).
        for strategy in strategies.values():
            for symbol in synthetic_bars:
                strategy._consume_execution_trades(symbol, bar_index + 1, portfolio, execution)

        final_prices: Dict[str, float] = (
            dict(self.event_processor.last_prices) if self.event_processor else {}
        )
        final_equity = portfolio.get_equity(final_prices)
        equity_curve.append(
            {"timestamp": synthetic_time, "equity": final_equity, "cash": portfolio.cash}
        )
        if exposure_positions is not None and exposure_prices is not None:
            # Everything closed above, so this row is flat by construction -
            # sampling it anyway keeps the exposure columns hole-free.
            self._sample_exposure(
                portfolio, final_prices, synthetic_time,
                exposure_positions, exposure_prices,
            )
        accounting.check_bar(
            bar_index + 1, synthetic_time, final_equity, portfolio,
            final_prices, broker.close_events,
        )

    def _benchmark(
        self, processed_data: Dict[str, pd.DataFrame], start_idx: int
    ) -> Optional[pd.Series]:
        result = fixed_equal_weight_buy_hold(
            processed_data, self.initial_capital, start_idx=start_idx
        )
        return result.equity if result else None

