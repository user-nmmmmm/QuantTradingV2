"""Live scheduler composed around the shared EventProcessor."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

import pandas as pd

from composition.factory import (
    Configuration,
    build_router,
    build_state_machine,
    market_type_supports_shorts,
)
from core.alerting import AlertSink, build_default_alert_sink
from core.clock import ClockLike, coerce_clock
from core.data_fetcher import DataFetcher
from core.health import DataHealthMonitor, DataHealthPolicy, HealthAssessment, HealthReason
from core.live_broker import LiveBroker
from core.logger import get_logger
from core.market_data import LiveMarketDataAdapter
from core.risk import RiskManager
from core.runtime import EventProcessor, MarketDataSlice
from core.state_store_v2 import StateStore, default_state_db_path
from core.sqlite_backup import SQLiteSnapshotManager
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
        configuration: Configuration,
        interval_seconds: int = 60,
        lookback_days: int = 30,
        timeframe: str = "1d",
        data_fetcher: Optional[DataFetcher] = None,
        clock: Optional[ClockLike] = None,
        health_policy: Optional[DataHealthPolicy] = None,
        state_file: str = "reports/live_status.json",
        state_store: Optional[StateStore] = None,
        close_grace_seconds: float = 2.0,
        bar_claim_lease_seconds: float = 300.0,
        alert_sink: Optional[AlertSink] = None,
        snapshot_interval_seconds: float = 3600,
        snapshot_retention: int = 24,
        snapshot_dir: Optional[str] = None,
        snapshot_manager: Optional[SQLiteSnapshotManager] = None,
        failure_backoff_base_seconds: float = 1.0,
        failure_backoff_max_seconds: float = 60.0,
        reconciliation_interval_seconds: float = 300.0,
        strategy_failure_threshold: int = 3,
        state_export_interval_ticks: int = 5,
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
        self.clock = coerce_clock(clock)
        self.health_monitor = DataHealthMonitor(health_policy)
        self.health_assessment: Optional[HealthAssessment] = None
        self.state_machine = build_state_machine(configuration)
        self._current_trading_day = None
        self.data_map: Dict[str, pd.DataFrame] = {}
        self.state_file = state_file
        self.state_store = state_store
        self._state_db_path = default_state_db_path(state_file)
        self._healthy = False
        self.snapshot_interval_seconds = snapshot_interval_seconds
        self.snapshot_retention = snapshot_retention
        self.snapshot_dir = snapshot_dir
        self.snapshot_manager = snapshot_manager
        self._operational_state = "STARTING"
        self._snapshot = None
        self._last_account_sync_at: Optional[datetime] = None
        self._last_order_sync_at: Optional[datetime] = None
        self._last_written_breaker: Optional[bool] = None
        self._last_written_breaker_day: Optional[str] = None
        self._last_exported_critical_state = None
        if failure_backoff_base_seconds < 0 or failure_backoff_max_seconds < 0:
            raise ValueError('failure backoff cannot be negative')
        self.failure_backoff_base_seconds = failure_backoff_base_seconds
        self.failure_backoff_max_seconds = failure_backoff_max_seconds
        self._consecutive_tick_crashes = 0
        self._next_retry_delay = 0.0
        if reconciliation_interval_seconds <= 0:
            raise ValueError("reconciliation_interval_seconds must be positive")
        if strategy_failure_threshold <= 0:
            raise ValueError("strategy_failure_threshold must be positive")
        if state_export_interval_ticks <= 0:
            raise ValueError("state_export_interval_ticks must be positive")
        self.reconciliation_interval_seconds = reconciliation_interval_seconds
        self.strategy_failure_threshold = strategy_failure_threshold
        self.state_export_interval_ticks = int(state_export_interval_ticks)
        self._tick_count = 0
        self._last_export_tick: Optional[int] = None
        self._last_reconciliation_at: Optional[datetime] = None
        self._reconciliation_status = {
            "last_run_at": None,
            "checked_count": 0,
            "discrepancy_count": 0,
            "ok": None,
        }
        self._consecutive_strategy_failures = 0
        self._last_strategy_error: Optional[str] = None
        self._strategies_state_bound = False
        self._unresolved_unknown_cache: Optional[bool] = None

        alert_path = os.path.join(
            os.path.dirname(os.path.abspath(state_file)), "live_alerts.jsonl"
        )
        self.alert_sink = alert_sink or build_default_alert_sink(logger, record_path=alert_path)
        allow_short = market_type_supports_shorts(getattr(broker, "market_type", "spot"))
        self.router = build_router(strategies, configuration, allow_short=allow_short)
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
            allocator=self.router.allocator,
            warmup_period=0,
            initial_equity=broker.portfolio.cash,
        )

    def _now(self) -> datetime:
        now = self.clock.now()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("live trading clock must return a timezone-aware datetime")
        return now.astimezone(timezone.utc)

    def _set_health_assessment(self, assessment: HealthAssessment) -> None:
        self.health_assessment = assessment
        self._healthy = assessment.healthy
        breaker_active = bool(
            getattr(self.risk_manager, "circuit_breaker_triggered", False)
        )
        self._operational_state = (
            "HEALTHY" if assessment.healthy and not breaker_active else "RISK_HALTED"
        )
        setter = getattr(self.risk_manager, "set_health_assessment", None)
        if callable(setter):
            setter(assessment)
        broker_setter = getattr(self.broker, "set_health_assessment", None)
        if callable(broker_setter):
            broker_setter(assessment)

    def _alert(self, level: str, event: str, context: Dict) -> None:
        try:
            self.alert_sink.notify(level, event, context)
        except Exception as exc:
            logger.error(
                "alert_delivery_failed event=%s category=%s",
                event, type(exc).__name__,
            )

    def _assess_health(self, now: datetime, *extra: HealthReason) -> HealthAssessment:
        assessment = self.health_monitor.assess(
            now=now,
            symbols=self.symbols,
            timeframe=self.timeframe,
            data_map=self.data_map,
            account_synced_at=self._last_account_sync_at,
            order_synced_at=self._last_order_sync_at,
            regressed_symbols=getattr(
                self.market_data_adapter, "regressed_symbols", set()
            ),
        )
        if extra:
            assessment = HealthAssessment(
                assessment.assessed_at, tuple(assessment.reasons) + tuple(extra)
            )
        self._set_health_assessment(assessment)
        if not assessment.healthy:
            logger.critical(
                "New risk halted by health assessment: %s",
                ",".join(assessment.reason_codes),
            )
            self._alert("critical", "risk_halt", {
                "reason_codes": assessment.reason_codes,
                "reasons": sorted(
                    {(r.code, r.subject) for r in assessment.reasons}
                ),
                "assessed_at": assessment.assessed_at,
            })
        else:
            acknowledge = getattr(self.alert_sink, "ack", None)
            if callable(acknowledge):
                acknowledge("risk_halt")
                acknowledge("tick_unhealthy")
        return assessment

    def _ensure_state_store(self) -> StateStore:
        if self.state_store is None:
            self.state_store = StateStore(self._state_db_path)
        if not self._strategies_state_bound:
            for strategy in self.strategies.values():
                binder = getattr(strategy, "bind_state_store", None)
                if callable(binder):
                    binder(self.state_store)
            self._strategies_state_bound = True
        if self.snapshot_manager is None:
            source_path = getattr(self.state_store, "path", self._state_db_path)
            if source_path != ":memory:":
                self.snapshot_manager = SQLiteSnapshotManager(
                    source_path,
                    snapshot_dir=self.snapshot_dir,
                    retention=self.snapshot_retention,
                    interval_seconds=self.snapshot_interval_seconds,
                )
        if self.snapshot_manager is not None:
            try:
                self.snapshot_manager.run_if_due()
            except Exception as exc:
                logger.error(
                    "state_snapshot_failed category=%s", type(exc).__name__,
                )
                self._alert("error", "state_snapshot_failed", {
                    "error": type(exc).__name__,
                })
        return self.state_store

    def _reset_daily_risk_if_needed(self, current_time: datetime) -> None:
        trading_day = current_time.date()
        if trading_day != self._current_trading_day:
            state_store = self._ensure_state_store()
            persisted_day = state_store.get("circuit_breaker_day")
            persisted_breaker = state_store.get("circuit_breaker", False)
            if persisted_day == trading_day.isoformat() and bool(persisted_breaker):
                # A process restart must not clear an intraday halt. The
                # operator-visible JSON file is deliberately not authoritative;
                # only the integrity-checked transactional store is restored.
                self.risk_manager.circuit_breaker_triggered = True
                self._operational_state = "RISK_HALTED"
                logger.critical(
                    "Restored active circuit breaker for trading_day=%s",
                    trading_day.isoformat(),
                )
                self._alert("critical", "circuit_breaker_restored", {
                    "trading_day": trading_day.isoformat(),
                })
            else:
                self.risk_manager.reset_daily_breaker()
                state_store.set("circuit_breaker", False)
                state_store.set("circuit_breaker_day", trading_day.isoformat())
            self._current_trading_day = trading_day

    def initialize(self):
        logger.info("Initializing Live Trading Engine...")
        self._ensure_state_store()
        recovery = self._recover_orders()
        if not self._has_unresolved_unknown(refresh=True):
            self._last_order_sync_at = self._now()
        if recovery:
            self._operational_state = "RECONCILING"
            logger.info("Recovered %s non-terminal order(s) during startup", len(recovery))
        sync_result = self.broker.sync()
        self._healthy = bool(getattr(sync_result, "ok", sync_result is None))
        if self._healthy:
            synced_at = getattr(sync_result, "synced_at", None)
            self._last_account_sync_at = (
                synced_at if isinstance(synced_at, datetime) else self._now()
            )
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
        self._assess_health(self._now())

    def run(self):
        logger.info("Starting Main Loop...")
        try:
            while True:
                healthy_tick = self._tick()
                delay = self.interval if healthy_tick else self._next_retry_delay
                time.sleep(delay)
        except KeyboardInterrupt:
            logger.info("Live Trading Stopped by User")

    def _recover_orders(self) -> Dict:
        recover = getattr(self.broker, "recover_open_orders", None)
        return recover() if callable(recover) else {}

    def _has_unresolved_unknown(self, *, refresh: bool = False) -> bool:
        if refresh or self._unresolved_unknown_cache is None:
            checker = getattr(self.broker, "has_unresolved_unknown", None)
            self._unresolved_unknown_cache = (
                bool(checker()) if callable(checker) else False
            )
        return self._unresolved_unknown_cache

    def _update_data(self):
        self.market_data_adapter.data_map = dict(self.data_map)
        self.data_map = self.market_data_adapter.refresh()

    def _tick(self) -> bool:
        '''Contain unexpected failures so one bad tick cannot kill the process.'''
        try:
            self._tick_once()
        except Exception as exc:
            self._consecutive_tick_crashes += 1
            if self._consecutive_tick_crashes == 1:
                self._next_retry_delay = min(
                    self.failure_backoff_base_seconds,
                    self.failure_backoff_max_seconds,
                )
            else:
                self._next_retry_delay = min(
                    self.failure_backoff_max_seconds,
                    max(
                        self.failure_backoff_base_seconds,
                        self._next_retry_delay * 2,
                    ),
                )
            self._healthy = False
            self._operational_state = 'HALTED'
            logger.exception(
                'Unexpected live tick failure; retrying in %.3fs',
                self._next_retry_delay,
            )
            self._alert('critical', 'tick_crashed', {
                'error': type(exc).__name__,
                'consecutive_failures': self._consecutive_tick_crashes,
                'retry_delay_seconds': self._next_retry_delay,
            })
            try:
                self._export_state()
            except Exception:
                logger.exception('Failed to export state after live tick crash')
            return False
        self._consecutive_tick_crashes = 0
        self._next_retry_delay = 0.0
        return True

    def _run_reconciliation_if_due(
        self, now: datetime, *, force: bool = False,
    ) -> Dict:
        due = (
            force
            or self._last_reconciliation_at is None
            or (now - self._last_reconciliation_at).total_seconds()
            >= self.reconciliation_interval_seconds
        )
        if not due:
            return dict(self._reconciliation_status)

        recovered = dict(self._recover_orders() or {})
        unresolved = [
            client_order_id
            for client_order_id, result in recovered.items()
            if str(getattr(getattr(result, "status", None), "value", ""))
            == "unknown"
        ]
        self._last_reconciliation_at = now
        self._reconciliation_status = {
            "last_run_at": now.isoformat(),
            "checked_count": len(recovered),
            "discrepancy_count": len(unresolved),
            "ok": not unresolved,
        }
        if not self._has_unresolved_unknown(refresh=True):
            self._last_order_sync_at = now
        if unresolved:
            self._alert("error", "reconcile_discrepancy", {
                "discrepancy_count": len(unresolved),
                "client_order_ids": unresolved,
            })
        return dict(self._reconciliation_status)

    def _tick_once(self):
        self._tick_count += 1
        self._unresolved_unknown_cache = None
        now = self._now()
        state_store = self._ensure_state_store()
        self._reset_daily_risk_if_needed(now)
        try:
            self._update_data()
        except Exception as exc:
            self._assess_health(now, HealthReason(
                "MARKET_DATA_UPDATE_FAILED", "market_data", "data",
                f"market data update failed: {type(exc).__name__}",
            ))
            self._alert("error", "tick_unhealthy", {
                "operation": "update_data", "error": type(exc).__name__,
            })
            self._maybe_export_state()
            return
        try:
            sync_result = self.broker.sync()
        except Exception as exc:
            self._assess_health(now, HealthReason(
                "ACCOUNT_SYNC_FAILED", "account_sync", "account",
                f"account synchronization raised: {type(exc).__name__}",
            ))
            self._alert("error", "tick_unhealthy", {
                "operation": "broker_sync", "error": type(exc).__name__,
            })
            self._maybe_export_state()
            return
        self._healthy = bool(getattr(sync_result, "ok", sync_result is None))
        if not self._healthy:
            self._assess_health(now, HealthReason(
                "ACCOUNT_SYNC_FAILED", "account_sync", "account",
                f"account synchronization failed: {getattr(sync_result, 'error', 'unknown')}",
            ))
            logger.error("Trading disabled: portfolio synchronization failed")
            self._maybe_export_state()
            return
        synced_at = getattr(sync_result, "synced_at", None)
        self._last_account_sync_at = (
            synced_at if isinstance(synced_at, datetime) else now
        )

        try:
            self._run_reconciliation_if_due(
                now, force=self._has_unresolved_unknown()
            )
        except Exception as exc:
            self._reconciliation_status = {
                "last_run_at": now.isoformat(),
                "checked_count": 0,
                "discrepancy_count": 1,
                "ok": False,
                "error": type(exc).__name__,
            }
            self._assess_health(now, HealthReason(
                "ORDER_SYNC_FAILED", "order_sync", "order",
                f"order synchronization failed: {type(exc).__name__}",
            ))
            self._alert("error", "reconcile_discrepancy", {
                "error": type(exc).__name__,
            })
            self._maybe_export_state()
            return
        if self._has_unresolved_unknown():
            self._assess_health(now, HealthReason(
                "ORDER_STATE_UNKNOWN", "order_sync", "order",
                "an order has unresolved exchange state",
            ))
            logger.critical("Trading halted: unresolved unknown order")
            self._maybe_export_state()
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

        self._assess_health(now)

        try:
            self._snapshot = build_portfolio_snapshot(
                self.broker.portfolio,
                prices,
                price_times,
                now if now.tzinfo else now.replace(tzinfo=timezone.utc),
            )
        except ValueError as exc:
            self._assess_health(now, HealthReason(
                "VALUATION_FACT_MISSING", "valuation", "portfolio",
                f"portfolio valuation is unavailable: {exc}",
            ))
            logger.error("Trading disabled: %s", exc)
            self._maybe_export_state()
            return

        self.event_processor.last_prices.update(self._snapshot.prices)
        day_key = f"daily_start_equity:{now.date().isoformat()}"
        daily_start = state_store.get(day_key)
        if daily_start is None:
            daily_start = self._snapshot.equity
            state_store.set(day_key, daily_start)
        was_already_triggered = bool(self.risk_manager.circuit_breaker_triggered)
        breaker = self.risk_manager.check_circuit_breaker(
            self._snapshot.equity, float(daily_start)
        )
        breaker_day = now.date().isoformat()
        if bool(breaker) != self._last_written_breaker:
            state_store.set("circuit_breaker", bool(breaker))
            self._last_written_breaker = bool(breaker)
        if breaker_day != self._last_written_breaker_day:
            state_store.set("circuit_breaker_day", breaker_day)
            self._last_written_breaker_day = breaker_day
        if breaker:
            self._operational_state = "RISK_HALTED"
            logger.critical("Trading disabled: daily circuit breaker active")
            # Alert only on the trip itself, not every tick the halt stays
            # active: equity moves every tick, which would otherwise defeat
            # HysteresisAlertSink's dedup key and page on every interval.
            if not was_already_triggered:
                self._alert("critical", "circuit_breaker_triggered", {
                    "equity": self._snapshot.equity,
                    "daily_start_equity": float(daily_start),
                })
            self._maybe_export_state()
            return

        strategy_failures = []
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
                if self._has_unresolved_unknown(refresh=True):
                    state_store.release_bar(bar_key)
                    self._assess_health(now, HealthReason(
                        "ORDER_STATE_UNKNOWN", "order_sync", "order",
                        "an order has unresolved exchange state",
                    ))
                    logger.critical("Bar released because order fact is unknown: %s", bar_key)
                    break
                state_store.complete_bar(bar_key, now.isoformat())
            except Exception as exc:
                state_store.release_bar(bar_key)
                strategy_failures.append((symbol, type(exc).__name__))
                logger.exception("Failed processing bar %s", bar_key)

        if strategy_failures:
            self._consecutive_strategy_failures += len(strategy_failures)
            self._last_strategy_error = strategy_failures[-1][1]
            halted = (
                self._consecutive_strategy_failures
                >= self.strategy_failure_threshold
            )
            self._healthy = False
            self._operational_state = "HALTED" if halted else "DEGRADED"
            self._alert("critical" if halted else "error", "strategy_processing_failed", {
                "failures": strategy_failures,
                "consecutive_failures": self._consecutive_strategy_failures,
                "threshold": self.strategy_failure_threshold,
                "operational_state": self._operational_state,
            })
        else:
            self._consecutive_strategy_failures = 0
            self._last_strategy_error = None
        self._maybe_export_state()

    def _critical_state_signature(self):
        return (
            self._healthy,
            self._operational_state,
            self._has_unresolved_unknown(),
            tuple(
                self.health_assessment.reason_codes
                if self.health_assessment else ()
            ),
        )

    def _maybe_export_state(self, *, force: bool = False) -> bool:
        critical_state = self._critical_state_signature()
        transition = critical_state != self._last_exported_critical_state
        due = (
            self._last_export_tick is None
            or self._tick_count - self._last_export_tick
            >= self.state_export_interval_ticks
        )
        if not (force or transition or due):
            return False
        if self._export_state():
            self._last_export_tick = self._tick_count
            self._last_exported_critical_state = critical_state
            return True
        return False

    def _export_state(self):
        tmp_path = f"{self.state_file}.{os.getpid()}.tmp"
        try:
            if self._snapshot is not None:
                # Reuse the authoritative valuation already produced this
                # tick instead of re-deriving equity from raw bar closes,
                # which can disagree with the price rule risk decisions used.
                cash = self._snapshot.cash
                equity = self._snapshot.equity
            else:
                current_prices = {
                    symbol: frame["close"].iloc[-1]
                    for symbol, frame in self.data_map.items()
                    if not frame.empty
                }
                cash = self.broker.portfolio.cash
                equity = self.broker.portfolio.get_equity(current_prices)
            state_data = {
                "schema_version": 1,
                "timestamp": self._now().strftime("%Y-%m-%d %H:%M:%S"),
                "cash": cash,
                "equity": equity,
                "positions": self.broker.portfolio.positions,
                "symbols": self.symbols,
                "last_update": self._now().isoformat(),
                "healthy": self._healthy,
                "operational_state": self._operational_state,
                "unresolved_unknown_order": self._has_unresolved_unknown(),
                "reconciliation": dict(self._reconciliation_status),
                "consecutive_strategy_failures": self._consecutive_strategy_failures,
                "last_strategy_error": self._last_strategy_error,
                "health_reason_codes": (
                    self.health_assessment.reason_codes
                    if self.health_assessment else []
                ),
                "health_assessment": (
                    self.health_assessment.to_dict()
                    if self.health_assessment else None
                ),
            }
            critical_state = (
                state_data["healthy"],
                state_data["operational_state"],
                state_data["unresolved_unknown_order"],
                tuple(state_data["health_reason_codes"]),
            )
            needs_fsync = critical_state != self._last_exported_critical_state
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(state_data, handle, indent=2)
                handle.flush()
                if needs_fsync:
                    os.fsync(handle.fileno())
            os.replace(tmp_path, self.state_file)
            self._last_exported_critical_state = critical_state
            return True
        except Exception as exc:
            logger.exception("Failed to export state")
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                logger.warning("Failed to remove incomplete state file: %s", tmp_path)
            return False
