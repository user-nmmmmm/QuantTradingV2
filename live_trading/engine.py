"""Live scheduler composed around the shared EventProcessor."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

import pandas as pd

from core.data_fetcher import DataFetcher
from core.live_broker import LiveBroker
from core.logger import get_logger
from core.market_data import LiveMarketDataAdapter
from core.risk import RiskManager
from core.runtime import EventProcessor, MarketDataSlice
from core.state_store_v2 import StateStore, default_state_db_path
from core.system_factory import build_router, build_state_machine, market_type_supports_shorts
from core.timeframes import as_utc_timestamp, closed_bars, timeframe_delta
from core.valuation import build_portfolio_snapshot
from live_trading.execution_adapter import RecordedExecutionAdapter

logger = get_logger(__name__)


class LiveTradingEngine:
    """Live durability/scheduling shell with mode-independent event processing."""

    def __init__(
        self,
        symbols: List[str],
        strategies: Dict,
        broker: LiveBroker,
        risk_manager: RiskManager,
        interval_seconds: int = 60,
        lookback_days: int = 30,
        timeframe: str = "1d",
        data_fetcher: Optional[DataFetcher] = None,
        clock: Optional[Callable[[], datetime]] = None,
        state_file: str = "reports/live_status.json",
        state_store: Optional[StateStore] = None,
        close_grace_seconds: float = 2.0,
        bar_claim_lease_seconds: float = 300.0,
    ) -> None:
        self.symbols = symbols
        self.strategies = strategies
        self.broker = broker
        self.risk_manager = risk_manager
        self.interval = interval_seconds
        self.lookback_days = lookback_days
        self.timeframe = timeframe
        self.close_grace_seconds = close_grace_seconds
        self.bar_claim_lease_seconds = bar_claim_lease_seconds
        self.fetcher = data_fetcher or DataFetcher()
        self._clock = clock or datetime.now
        self.state_machine = build_state_machine()
        self._current_trading_day = None
        self.data_map: Dict[str, pd.DataFrame] = {}
        self.state_file = state_file
        self.state_store = state_store
        self._state_db_path = default_state_db_path(state_file)
        self._healthy = False
        self._operational_state = "STARTING"
        self._snapshot = None

        allow_short = market_type_supports_shorts(getattr(broker, "market_type", "spot"))
        self.router = build_router(strategies, allow_short=allow_short)
        if not allow_short:
            logger.warning(
                "Live broker market_type=%s does not support short routing; TREND_DOWN is mapped to Cash",
                getattr(broker, "market_type", "spot"),
            )
        state_dir = os.path.dirname(state_file)
        if state_dir:
            os.makedirs(state_dir, exist_ok=True)

        self.market_data_adapter = LiveMarketDataAdapter(
            symbols,
            self.fetcher,
            timeframe=timeframe,
            lookback=max(lookback_days, 100),
            close_grace_seconds=close_grace_seconds,
        )
        self.execution_adapter = RecordedExecutionAdapter(broker)
        self.event_processor = EventProcessor(
            portfolio=broker.portfolio,
            execution=self.execution_adapter,
            risk_manager=risk_manager,
            state_machine=self.state_machine,
            router=self.router,
            warmup_period=0,
            initial_equity=broker.portfolio.cash,
        )

    def _now(self) -> datetime:
        return self._clock()

    def _ensure_state_store(self) -> StateStore:
        if self.state_store is None:
            self.state_store = StateStore(self._state_db_path)
        return self.state_store

    def _reset_daily_risk_if_needed(self, current_time: datetime) -> None:
        trading_day = current_time.date()
        if trading_day != self._current_trading_day:
            self.risk_manager.reset_daily_breaker()
            self._current_trading_day = trading_day

    def initialize(self):
        logger.info("Initializing Live Trading Engine...")
        self._ensure_state_store()
        recovery = self._recover_orders()
        if recovery:
            self._operational_state = "RECONCILING"
            logger.info("Recovered %s non-terminal order(s) during startup", len(recovery))
        sync_result = self.broker.sync()
        self._healthy = bool(getattr(sync_result, "ok", sync_result is None))
        self._operational_state = "HEALTHY" if self._healthy else "HALTED"
        self._reset_daily_risk_if_needed(self._now())
        for symbol in self.symbols:
            logger.info("Warming up data for %s...", symbol)
            frame = self.fetcher.fetch_ccxt(
                symbol, timeframe=self.timeframe, limit=max(self.lookback_days, 100)
            )
            if not frame.empty:
                self.data_map[symbol] = frame
                logger.info("Loaded %s bars for %s", len(frame), symbol)
            else:
                logger.warning("Failed to load data for %s", symbol)
        self.market_data_adapter.data_map = dict(self.data_map)

    def run(self):
        logger.info("Starting Main Loop...")
        try:
            while True:
                self._tick()
                time.sleep(self.interval)
        except KeyboardInterrupt:
            logger.info("Live Trading Stopped by User")

    def _recover_orders(self) -> Dict:
        recover = getattr(self.broker, "recover_open_orders", None)
        return recover() if callable(recover) else {}

    def _has_unresolved_unknown(self) -> bool:
        checker = getattr(self.broker, "has_unresolved_unknown", None)
        return bool(checker()) if callable(checker) else False

    def _update_data(self):
        self.market_data_adapter.data_map = dict(self.data_map)
        self.data_map = self.market_data_adapter.refresh()

    def _tick(self):
        now = self._now()
        state_store = self._ensure_state_store()
        self._reset_daily_risk_if_needed(now)
        self._recover_orders()
        if self._has_unresolved_unknown():
            self._healthy = False
            self._operational_state = "HALTED"
            logger.critical("Trading halted: unresolved unknown order")
            self._export_state()
            return

        self._update_data()
        sync_result = self.broker.sync()
        self._healthy = bool(getattr(sync_result, "ok", sync_result is None))
        if not self._healthy:
            logger.error("Trading disabled: portfolio synchronization failed")
            self._export_state()
            return

        prices: Dict[str, float] = {}
        price_times = {}
        closed_map: Dict[str, pd.DataFrame] = {}
        for symbol, data in self.data_map.items():
            eligible = closed_bars(data, self.timeframe, now, self.close_grace_seconds)
            if not eligible.empty:
                closed_map[symbol] = eligible
                prices[symbol] = float(eligible["close"].iloc[-1])
                price_times[symbol] = as_utc_timestamp(eligible.index[-1]).to_pydatetime()

        try:
            self._snapshot = build_portfolio_snapshot(
                self.broker.portfolio,
                prices,
                price_times,
                now if now.tzinfo else now.replace(tzinfo=timezone.utc),
            )
        except ValueError as exc:
            self._healthy = False
            self._operational_state = "HALTED"
            logger.error("Trading disabled: %s", exc)
            self._export_state()
            return

        self.event_processor.last_prices.update(self._snapshot.prices)
        day_key = f"daily_start_equity:{now.date().isoformat()}"
        daily_start = state_store.get(day_key)
        if daily_start is None:
            daily_start = self._snapshot.equity
            state_store.set(day_key, daily_start)
        breaker = self.risk_manager.check_circuit_breaker(
            self._snapshot.equity, float(daily_start)
        )
        state_store.set("circuit_breaker", bool(breaker))
        if breaker:
            logger.critical("Trading disabled: daily circuit breaker active")
            self._export_state()
            return

        for symbol in self.symbols:
            frame = closed_map.get(symbol)
            if frame is None or frame.empty:
                continue
            timestamp = frame.index[-1]
            close_time = as_utc_timestamp(timestamp) + timeframe_delta(self.timeframe)
            bar_key = (
                f"{getattr(self.broker, 'exchange_id', 'exchange')}|"
                f"{getattr(self.broker, 'account_id', getattr(self.broker, 'market_type', 'spot'))}|"
                f"{symbol}|{self.timeframe}|{close_time.isoformat()}"
            )
            if not state_store.claim_bar(
                bar_key, now.isoformat(), lease_seconds=self.bar_claim_lease_seconds
            ):
                continue
            set_context = getattr(self.broker, "set_bar_context", None)
            if callable(set_context):
                set_context(self.timeframe, close_time)
            event = MarketDataSlice(
                timestamp=timestamp,
                bars={symbol: frame.iloc[-1]},
                histories={symbol: frame},
                timeframe=self.timeframe,
                source="live",
            )
            try:
                self.event_processor.process_symbol(event, symbol)
                if self._has_unresolved_unknown():
                    state_store.release_bar(bar_key)
                    self._healthy = False
                    self._operational_state = "HALTED"
                    logger.critical("Bar released because order fact is unknown: %s", bar_key)
                    break
                state_store.complete_bar(bar_key, now.isoformat())
            except Exception:
                state_store.release_bar(bar_key)
                logger.exception("Failed processing bar %s", bar_key)
        self._export_state()

    def _export_state(self):
        try:
            current_prices = {
                symbol: frame["close"].iloc[-1]
                for symbol, frame in self.data_map.items()
                if not frame.empty
            }
            state_data = {
                "timestamp": self._now().strftime("%Y-%m-%d %H:%M:%S"),
                "cash": self.broker.portfolio.cash,
                "equity": self.broker.portfolio.get_equity(current_prices),
                "positions": self.broker.portfolio.positions,
                "symbols": self.symbols,
                "last_update": self._now().isoformat(),
                "healthy": self._healthy,
                "operational_state": self._operational_state,
                "unresolved_unknown_order": self._has_unresolved_unknown(),
            }
            with open(self.state_file, "w") as handle:
                json.dump(state_data, handle, indent=2)
        except Exception as exc:
            logger.error("Failed to export state: %s", type(exc).__name__)

