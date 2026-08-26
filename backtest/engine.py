"""Backtest engine composed from the shared runtime and historical adapters."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional
from uuid import uuid4

import pandas as pd

from backtest.execution_adapter import SimulatedExecutionAdapter
from config.config import config
from core.accounting_check import AccountingReconciler
from core.accounts import AccountMode
from core.benchmarks import (
    dynamic_equal_weight_rebalanced,
    fixed_equal_weight_buy_hold,
)
from core.broker import Broker
from core.events import TradingEventPipeline
from core.logger import get_logger
from core.market_data import HistoricalMarketDataAdapter, normalize_market_frame
from core.portfolio import Portfolio
from core.runtime import EventProcessor
from core.system_factory import (
    build_risk_manager,
    build_router,
    build_state_machine,
    build_strategy_registry,
)

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
            event_pipeline=event_pipeline,
            timeframe=self.timeframe,
        )
        risk_manager = build_risk_manager()
        state_machine = build_state_machine()
        strategies = strategies or build_strategy_registry()
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
        router = build_router(strategies, log_path=routing_log_path)

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
        }
        if not processed_data:
            logger.warning("No valid symbol data available after normalization")
            return empty_result
        timestamps = market_data.timestamps
        if len(timestamps) == 0:
            return empty_result

        execution = SimulatedExecutionAdapter(broker)
        processor = EventProcessor(
            portfolio=portfolio,
            execution=execution,
            risk_manager=risk_manager,
            state_machine=state_machine,
            router=router,
            warmup_period=self.warmup_period,
            initial_equity=self.initial_capital,
        )
        self.market_data_adapter = market_data
        self.execution_adapter = execution
        self.event_processor = processor

        logger.info("Starting backtest on %s bars", len(timestamps))
        equity_curve = []
        accounting = AccountingReconciler(self.initial_capital)
        bar_index = -1
        last_event_timestamp = None
        applied_breaker_actions: set[str] = set()
        for bar_index, event in enumerate(market_data.stream()):
            last_event_timestamp = event.timestamp
            result = processor.process(event)
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
            if margin.liquidation_required:
                forced_trades.extend(
                    broker.force_liquidate(
                        dict(event.bars),
                        timestamp=event.timestamp,
                        reason="MarginLiquidation",
                    )
                )
            elif result.breaker_action == "reduce" and "reduce" not in applied_breaker_actions:
                forced_trades.extend(
                    broker.force_liquidate(
                        dict(event.bars),
                        timestamp=event.timestamp,
                        reason="DrawdownReduce",
                        remaining_fraction=risk_manager.reduced_risk_multiplier,
                    )
                )
                applied_breaker_actions.add("reduce")
            elif result.breaker_action in {"liquidate", "locked"}:
                if result.breaker_action not in applied_breaker_actions:
                    forced_trades.extend(
                        broker.force_liquidate(
                            dict(event.bars),
                            timestamp=event.timestamp,
                            reason="AccountLiquidation",
                        )
                    )
                    applied_breaker_actions.add(result.breaker_action)
            elif result.circuit_breaker and result.breaker_action == "normal":
                forced_trades.extend(
                    broker.force_liquidate(
                        dict(event.bars),
                        timestamp=event.timestamp,
                        reason="DailyLossLimit",
                    )
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
            equity_curve.append(
                {
                    "timestamp": event.timestamp,
                    "equity": result.equity,
                    "cash": result.cash,
                }
            )
            # T-1.8: accounting-identity check (Gate G2), one lightweight pass
            # per bar using the same equity/prices the loop already computed.
            accounting.check_bar(
                bar_index, event.timestamp, result.equity, portfolio,
                result.prices, broker.close_events,
            )
        self._close_tail_positions(
            portfolio, broker, execution, strategies, equity_curve, accounting,
            last_event_timestamp, bar_index,
        )
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
            "equity_curve": pd.DataFrame(equity_curve).set_index("timestamp"),
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
            "margin_ledger": [item.to_dict() for item in portfolio.margin_ledger],
            "financing_ledger": [item.to_dict() for item in portfolio.financing_ledger],
            "execution_audit": list(broker.execution_audit),
            "breaker_audit": list(risk_manager.breaker_audit),
            "breaker_state": {
                "action": risk_manager.breaker_action.value,
                "high_water_equity": risk_manager.high_water_equity,
                "drawdown": risk_manager.last_drawdown,
                "daily_loss_triggered": bool(risk_manager.circuit_breaker_triggered),
            },
        }

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

