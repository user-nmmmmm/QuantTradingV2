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
from core.protective_orders import (
    ProtectiveAction,
    ProtectiveOrder,
    ProtectiveOrderManager,
)
from core.protective_stops import evaluate_fill_risk
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

        # A venue fill can gap away from the signal reference used for sizing.
        # Recheck it before any new strategy work is allowed this tick.
        # Establish or recover venue protection before strategy evaluation.
        self._reconcile_protective_orders()
        if self._operational_state == "DEGRADED":
            self._maybe_export_state(force=True)
            return
        if not self._recheck_live_entry_risk():
            self._maybe_export_state(force=True)
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
        self._reconcile_protective_orders()
        self._alert_strategy_health_transitions()
        self._maybe_export_state()

    def _alert_strategy_health_transitions(self) -> None:
        """SR1-4: a health transition is an operator event, not a log line.

        Every new ACTIVE/COOLDOWN/PROBATION/MANUAL_LOCK transition raises one
        alert exactly once. MANUAL_LOCK is critical - it will not clear on its
        own and needs a human.
        """
        seen = getattr(self, "_alerted_health_transitions", None)
        if seen is None:
            seen = set()
            self._alerted_health_transitions = seen
        for name, strategy in getattr(self, "strategies", {}).items():
            machine = getattr(strategy, "health", None)
            if machine is None:
                continue
            for index, row in enumerate(machine.transitions):
                key = (name, index, row.get("at"), row.get("to"))
                if key in seen:
                    continue
                seen.add(key)
                self._alert(
                    "critical" if row.get("to") == "manual_lock" else "warning",
                    "strategy_health_transition",
                    {"strategy": name, **row},
                )

    # ------------------------------------------------------------ SR2-5

    def _protective_manager(self):
        manager = getattr(self, "_protective_order_manager", None)
        if manager is None:
            manager = ProtectiveOrderManager()
            self._protective_order_manager = manager
        return manager

    def _venue_protective_orders(self):
        """Protective orders as the order store currently knows them."""
        store = getattr(self.broker, "order_store", None)
        if store is None:
            return []
        orders = []
        for record in store.list_non_terminal():
            if str(record.get("order_type", "")).lower() != "stop":
                continue
            intent = record.get("intent")
            reduce_only = True
            if isinstance(intent, dict):
                reduce_only = bool(intent.get("reduce_only", True))
            orders.append(ProtectiveOrder(
                order_id=str(record["client_order_id"]),
                symbol=str(record["symbol"]),
                side=str(record["side"]),
                qty=float(record.get("remaining_qty") or record["requested_qty"]),
                stop_price=float(record.get("price") or 0.0),
                status=str(record["status"]).lower(),
                reduce_only=reduce_only,
            ))
        return orders

    def _desired_protective_stop(self, symbol: str, strategy_id: str = ""):
        """The level the owning strategy currently wants enforced."""
        if strategy_id:
            strategy = getattr(self, "strategies", {}).get(strategy_id)
            context = getattr(strategy, "context", {}).get(symbol) or {}
            stop = context.get("effective_stop", context.get("stop_loss"))
            if stop:
                return float(stop)
        for strategy in getattr(self, "strategies", {}).values():
            context = getattr(strategy, "context", {}).get(symbol) or {}
            stop = context.get("effective_stop", context.get("stop_loss"))
            if stop:
                return float(stop)
        return None

    def _recheck_live_entry_risk(self) -> bool:
        """SR2-4: verify durable opening fills against the real risk budget.

        The order ledger, not an in-memory callback, is authoritative.  This
        catches fills learned during reconciliation and fills that happened
        immediately before a restart.  State-store checkpoints make partial
        fills idempotent and ensure that only the incremental resize is sent.
        """

        policy = getattr(self, "entry_risk_policy", None)
        if policy is None or not policy.enabled or self._snapshot is None:
            return True
        order_store = getattr(self.broker, "order_store", None)
        list_with_fills = getattr(order_store, "list_with_fills", None)
        if not callable(list_with_fills):
            return True
        state_store = self._ensure_state_store()
        audit = getattr(self, "_live_fill_risk_audit", None)
        if audit is None:
            audit = []
            self._live_fill_risk_audit = audit

        all_accepted = True
        for record in list_with_fills():
            intent = record.get("intent") or {}
            side = str(record.get("side") or intent.get("action") or "").lower()
            if side not in {"buy", "short"} or bool(intent.get("reduce_only", False)):
                continue
            strategy_id = str(intent.get("strategy_id") or "")
            if strategy_id == "GapRiskResize":
                continue
            client_order_id = str(record["client_order_id"])
            filled_qty = float(record.get("filled_qty") or 0.0)
            average_fill_price = float(record.get("average_fill_price") or 0.0)
            if filled_qty <= 0 or average_fill_price <= 0:
                continue

            checkpoint_key = f"entry_risk_check:{client_order_id}"
            checkpoint = state_store.get(checkpoint_key) or {}
            checked_qty = float(checkpoint.get("checked_filled_qty", 0.0) or 0.0)
            if filled_qty <= checked_qty + 1e-12:
                continue

            symbol = str(record["symbol"])
            stop = self._desired_protective_stop(symbol, strategy_id)
            if not stop:
                # The protective-order reconciler owns the fail-closed action
                # for an open position without a usable stop.
                continue
            strategy = getattr(self, "strategies", {}).get(strategy_id)
            multiplier_getter = getattr(strategy, "health_risk_multiplier", None)
            multiplier = (
                float(multiplier_getter()) if callable(multiplier_getter) else 1.0
            )
            assessment = evaluate_fill_risk(
                symbol=symbol,
                lot_id=client_order_id,
                side="long" if side == "buy" else "short",
                fill_price=average_fill_price,
                protective_stop=stop,
                filled_qty=filled_qty,
                equity_at_fill=float(self._snapshot.equity),
                base_risk_per_trade=float(
                    getattr(self.risk_manager, "risk_per_trade", 0.0)
                ),
                health_risk_multiplier=multiplier,
                policy=policy,
            )
            if assessment is None:
                continue
            row = assessment.to_dict()
            row.update({
                "timestamp": self._now().isoformat(),
                "client_order_id": client_order_id,
                "strategy_id": strategy_id,
                "health_risk_multiplier": multiplier,
                "source": "live",
            })
            audit.append(row)
            if len(audit) > 1000:
                del audit[:-1000]

            requested_total = float(
                checkpoint.get("requested_resize_qty", 0.0) or 0.0
            )
            additional_resize = max(
                0.0, float(assessment.resize_qty) - requested_total
            )
            accepted = True
            if assessment.action == "resize" and additional_resize > 1e-12:
                held_qty = abs(float(
                    self.broker.portfolio.get_position(symbol).get("qty", 0.0)
                ))
                resize_qty = min(additional_resize, held_qty)
                if resize_qty > 1e-12:
                    result = self.broker.submit_order(
                        symbol,
                        "sell" if side == "buy" else "cover",
                        resize_qty,
                        order_type="market",
                        timestamp=self._now(),
                        strategy_id="GapRiskResize",
                        exit_reason="GapRiskResize",
                        reduce_only=True,
                    )
                    accepted = bool(getattr(result, "accepted", False))
                    if accepted:
                        requested_total += resize_qty
                    else:
                        all_accepted = False
                        self._operational_state = "DEGRADED"
                        self._alert("critical", "gap_risk_resize_failed", {
                            "symbol": symbol,
                            "client_order_id": client_order_id,
                            "requested_qty": resize_qty,
                            "risk_ratio": assessment.risk_ratio,
                        })

            if accepted:
                checkpoint = {
                    "checked_filled_qty": filled_qty,
                    "requested_resize_qty": requested_total,
                    "last_assessment": row,
                }
                state_store.set(checkpoint_key, checkpoint)
                state_store.set("live_fill_risk_audit", list(audit))
        return all_accepted

    def _reconcile_protective_orders(self) -> None:
        """Keep venue-resident protection in step with the real position.

        SR2-5: the entry fill, not the signal, is what creates protection; the
        protective quantity tracks the net position; the level only ratchets;
        and anything the venue cannot confirm fails closed into a flatten
        rather than being carried as if a stop existed.
        """
        if not getattr(self, "protective_orders_enabled", True):
            return
        broker = self.broker
        portfolio = getattr(broker, "portfolio", None)
        if portfolio is None:
            return
        manager = self._protective_manager()
        try:
            venue_orders = self._venue_protective_orders()
        except Exception as exc:  # order store unreadable: do not guess
            logger.exception("Protective order reconciliation could not read orders")
            self._alert("critical", "protective_orders_unreadable", {
                "error": type(exc).__name__,
            })
            return
        symbols = set(portfolio.positions) | {order.symbol for order in venue_orders}
        for symbol in sorted(symbols):
            qty = float(portfolio.get_position(symbol).get("qty", 0.0))
            plan = manager.evaluate(
                symbol=symbol,
                position_qty=qty,
                desired_stop=self._desired_protective_stop(symbol),
                open_protective_orders=venue_orders,
            )
            for intent in plan.intents:
                self._apply_protective_intent(symbol, intent)

    def _apply_protective_intent(self, symbol: str, intent) -> None:
        action = intent.action
        try:
            if action in (ProtectiveAction.CANCEL, ProtectiveAction.REPLACE):
                if intent.cancel_order_id:
                    self.broker.cancel_order(intent.cancel_order_id)
            if action in (ProtectiveAction.PLACE, ProtectiveAction.REPLACE):
                self.broker.submit_order(
                    symbol, intent.side, intent.qty,
                    price=intent.stop_price, order_type="stop",
                    timestamp=self._now(), strategy_id="ProtectiveStop",
                    exit_reason="protective_stop", reduce_only=True,
                )
            if action is ProtectiveAction.FLATTEN:
                # Fail closed: an unprotected position is flattened, and this
                # is always an operator-visible event.
                self._operational_state = "DEGRADED"
                self._alert("critical", "position_unprotected", {
                    "symbol": symbol, "reason": intent.reason, "qty": intent.qty,
                })
                self.broker.submit_order(
                    symbol, intent.side, intent.qty,
                    order_type="market", timestamp=self._now(),
                    strategy_id="ProtectiveStop",
                    exit_reason="unprotected_flatten", reduce_only=True,
                )
        except Exception as exc:
            logger.exception("Protective intent failed: %s %s", symbol, action)
            self._operational_state = "DEGRADED"
            self._alert("critical", "protective_intent_failed", {
                "symbol": symbol, "action": action.value,
                "reason": intent.reason, "error": type(exc).__name__,
            })
