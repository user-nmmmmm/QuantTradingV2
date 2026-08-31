"""Per-tick execution: data refresh, account sync, risk gate, bar routing.

Split out of live_trading/engine.py (A4) — see docs/architecture_review.md.
See live_trading/recovery.py's module docstring for why this is a mixin
rather than a standalone collaborator object. ``LiveTradingEngine`` keeps
``run()``/``initialize()`` as the lifecycle shell; this module is the body
of a single tick.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict

import pandas as pd

from core.health import HealthReason
from core.logger import get_logger
from core.runtime import MarketDataSlice
from core.timeframes import as_utc_timestamp, closed_bars, timeframe_delta
from core.valuation import build_portfolio_snapshot

# Same logger name as live_trading.engine (logging.getLogger caches by name,
# so this is the identical object) -- tests patch "live_trading.engine.logger"
# and must keep catching exceptions logged from this mixin too.
logger = get_logger("live_trading.engine")


class TickOrchestratorMixin:
    """The body of one live tick: data, sync, reconciliation, risk, routing.

    Expects ``self`` to carry the full ``LiveTradingEngine`` attribute set
    (broker, risk_manager, event_processor, state helpers) plus the
    ``RecoveryMixin``/``StateExportMixin`` methods it calls into.
    """

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
