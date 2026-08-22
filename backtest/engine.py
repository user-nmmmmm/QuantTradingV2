"""Backtest engine composed from the shared runtime and historical adapters."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

import pandas as pd

from backtest.execution_adapter import SimulatedExecutionAdapter
from config.config import config
from core.broker import Broker
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
    ) -> None:
        self.initial_capital = initial_capital
        self.config_execution = config.require("execution")
        self.config_risk = config.require("risk")
        self.slippage = (
            self.config_execution["slippage_bps"] / 10000.0
            if slippage is None
            else slippage
        )
        self.random_slip = random_slip
        self.warmup_period = warmup_period
        self.market_data_adapter = None
        self.execution_adapter = None
        self.event_processor = None

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

        portfolio = Portfolio(self.initial_capital)
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

        market_data = HistoricalMarketDataAdapter(data_map)
        processed_data = market_data.data_map
        if not processed_data:
            logger.warning("No valid symbol data available after normalization")
            return {"trades": [], "equity_curve": pd.DataFrame()}
        timestamps = market_data.timestamps
        if len(timestamps) == 0:
            return {"trades": [], "equity_curve": pd.DataFrame()}

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
        for event in market_data.stream():
            result = processor.process(event)
            if result.circuit_breaker:
                for symbol, position in list(portfolio.positions.items()):
                    qty = position["qty"]
                    if qty == 0 or symbol not in event.bars:
                        continue
                    execution.submit_order(
                        symbol,
                        "sell" if qty > 0 else "cover",
                        abs(qty),
                        result.prices[symbol],
                        timestamp=event.timestamp,
                        strategy_id="CircuitBreaker",
                        exit_reason="MaxLoss",
                    )
            equity_curve.append(
                {
                    "timestamp": event.timestamp,
                    "equity": result.equity,
                    "cash": result.cash,
                }
            )
        router.save_log()
        logger.info("Backtest completed")

        benchmark_series = self._benchmark(processed_data, self.warmup_period)
        return {
            "trades": broker.trades,
            "equity_curve": pd.DataFrame(equity_curve).set_index("timestamp"),
            "benchmark": benchmark_series,
            # Per-strategy count of round trips each strategy actually saw
            # close. Diagnostics compares this against the reconstructed
            # closed-trade count to detect strategies whose on_trade_closed
            # hook never fires (e.g. positions liquidated by the router), which
            # silently disables their health/cooldown safeguards.
            "close_events": {
                name: int(getattr(strategy, "observed_close_events", 0))
                for name, strategy in strategies.items()
            },
        }

    def _benchmark(
        self, processed_data: Dict[str, pd.DataFrame], start_idx: int
    ) -> Optional[pd.Series]:
        try:
            closes = pd.DataFrame(
                {symbol: frame["close"] for symbol, frame in processed_data.items()}
            )
            returns = closes.pct_change(fill_method=None).fillna(0)
            benchmark = (1 + returns.mean(axis=1)).cumprod()
            if start_idx < len(benchmark):
                base = benchmark.iloc[start_idx]
                if base != 0:
                    benchmark = benchmark / base * self.initial_capital
                    benchmark.iloc[:start_idx] = self.initial_capital
                    return benchmark
            return benchmark * self.initial_capital
        except Exception as exc:
            logger.warning("Failed to calculate benchmark: %s", exc)
            return None

