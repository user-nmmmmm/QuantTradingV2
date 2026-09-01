"""Live scheduler composed around the shared EventProcessor.

Split by change reason (A4) — see docs/architecture_review.md:
- live_trading/recovery.py         — order recovery, reconciliation-due checks
- live_trading/state_export.py     — durable JSON export of engine state
- live_trading/tick_orchestrator.py — the body of one tick (data/sync/risk/routing)

``LiveTradingEngine`` composes the three mixins below via inheritance rather
than holding separate collaborator objects, so every method still
reads/writes the exact same ``self`` attributes as before the split --
behavior-identical, mechanical. A true composition redesign is a bigger
change on this money-path-adjacent code and is deliberately left for a
dedicated pass, not bundled into this file-size cleanup.

``LiveTradingEngine`` itself keeps ``__init__``, lifecycle (``initialize``,
``run``), and the cross-cutting helpers (``_now``, ``_alert``,
``_assess_health``, ``_ensure_state_store``, ``_reset_daily_risk_if_needed``)
that the split modules call back into.
"""

from __future__ import annotations

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
from core.protective_stops import EntryRiskPolicy
from core.risk import RiskManager
from core.runtime import EventProcessor
from core.state_store_v2 import StateStore, default_state_db_path
from core.sqlite_backup import SQLiteSnapshotManager
from live_trading.execution_adapter import RecordedExecutionAdapter
from live_trading.recovery import RecoveryMixin
from live_trading.state_export import StateExportMixin
from live_trading.tick_orchestrator import TickOrchestratorMixin

logger = get_logger(__name__)


class LiveTradingEngine(TickOrchestratorMixin, RecoveryMixin, StateExportMixin):
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
        # SR2-5: venue-resident protective stops, reconciled every tick.
        self.protective_orders_enabled = bool(
            (configuration.get("protective_orders") or {}).get("enabled", True)
        )
        self._protective_order_manager = None
        # SR2-4: actual-fill risk is checked from the durable order ledger.
        self.entry_risk_policy = EntryRiskPolicy.from_mapping(
            configuration.get("entry_risk") or {}
        )
        self._live_fill_risk_audit = []

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
