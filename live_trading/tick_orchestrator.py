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

from core.domain import OrderStatus
from core.health import HealthReason
from core.logger import get_logger
from core.protective_orders import (
    ProtectiveAction,
    ProtectiveOrder,
    ProtectiveOrderManager,
)
from core.protective_stops import evaluate_fill_risk
from core.runtime import MarketDataSlice
from core.risk.actions import plan_risk_action
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
        data_failure = None
        try:
            self._update_data()
        except Exception as exc:
            data_failure = type(exc).__name__
            self._assess_health(now, HealthReason(
                "MARKET_DATA_UPDATE_FAILED", "market_data", "data",
                f"market data update failed: {type(exc).__name__}",
            ))
            self._alert("error", "tick_unhealthy", {
                "operation": "update_data", "error": type(exc).__name__,
            })
            # Continue account/order recovery even when signal data is unavailable.
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
            logger.critical("New risk blocked: unresolved unknown order; reconciling protection")

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
        if data_failure:
            self._assess_health(now, HealthReason("MARKET_DATA_UPDATE_FAILED", "market_data", "data", data_failure))
        mark_loader = getattr(type(self.broker), "risk_price_facts", None)
        if callable(mark_loader):
            try:
                facts = self.broker.risk_price_facts(set(self.symbols) | set(self.broker.portfolio.positions))
                prices = {symbol: fact["price"] for symbol, fact in facts.items()}
                price_times = {symbol: fact["timestamp"] for symbol, fact in facts.items()}
            except Exception as exc:
                self._assess_health(now, HealthReason("RISK_MARK_UNAVAILABLE", "valuation", "portfolio", type(exc).__name__))
                self._reconcile_protective_orders()
                self._maybe_export_state()
                return

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
            self._snapshot.equity, float(daily_start), occurred_at=now
        )
        state_store.set("portfolio_breaker_checkpoint", self.risk_manager.breaker_checkpoint())
        action_plan = plan_risk_action(breaker, now.date())
        if breaker:
            for pending in self.broker.order_store.list_non_terminal():
                if pending["side"] in {"buy", "short"}:
                    self.broker.cancel_order(pending["client_order_id"])
        if action_plan is not None:
            key = f"risk_targets:{action_plan.action_id}"
            targets = state_store.get(key)
            if targets is None:
                targets = {symbol: abs(float(pos["qty"])) * action_plan.remaining_fraction
                           for symbol, pos in self.broker.portfolio.positions.items()}
                state_store.set(key, targets)
            for symbol, target in targets.items():
                self._submit_risk_exit(symbol, action_plan.reason, target, action_plan.action_id,
                                       self._snapshot.prices.get(symbol))
        self._reconcile_protective_orders()
        if self._operational_state == "DEGRADED" or not self._recheck_live_entry_risk():
            self._maybe_export_state(force=True)
            return
        breaker_day = now.date().isoformat()
        if bool(breaker) != self._last_written_breaker:
            state_store.set("circuit_breaker", bool(breaker))
            self._last_written_breaker = bool(breaker)
        if breaker_day != self._last_written_breaker_day:
            state_store.set("circuit_breaker_day", breaker_day)
            self._last_written_breaker_day = breaker_day
        if breaker:
            self._operational_state = "RISK_HALTED"
            logger.critical("New entries disabled by circuit breaker: %s", breaker.reason_codes)
            # Alert only on the trip itself, not every tick the halt stays
            # active: equity moves every tick, which would otherwise defeat
            # HysteresisAlertSink's dedup key and page on every interval.
            if not was_already_triggered:
                self._alert("critical", "circuit_breaker_triggered", {
                    "equity": self._snapshot.equity,
                    "daily_start_equity": float(daily_start),
                })
            # A portfolio BLOCK_NEW cooldown must keep strategy exits running.
            # Daily/terminal halts retain their existing execution behavior.
            if breaker.action.value != "block_new" or breaker.daily_loss_triggered:
                self._maybe_export_state()
                return

        strategy_failures = []
        batches = {}
        for symbol in sorted(self.symbols):
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
                candidate, _ = self.event_processor._collect_symbol_candidate(
                    event, symbol, allow_position_management=True,
                    allow_new_entries=not bool(breaker) and self._healthy and not data_failure,
                )
                batch = batches.setdefault(close_time, {"candidates": [], "keys": []})
                batch["keys"].append(bar_key)
                if candidate is not None:
                    batch["candidates"].append(candidate)
                if self._has_unresolved_unknown(refresh=True):
                    state_store.release_bar(bar_key)
                    self._assess_health(now, HealthReason(
                        "ORDER_STATE_UNKNOWN", "order_sync", "order",
                        "an order has unresolved exchange state",
                    ))
                    logger.critical("Bar released because order fact is unknown: %s", bar_key)
                    break
            except Exception as exc:
                state_store.release_bar(bar_key)
                strategy_failures.append((symbol, type(exc).__name__))
                logger.exception("Failed processing bar %s", bar_key)

        for close_time, batch in sorted(batches.items()):
            try:
                if not self._has_unresolved_unknown(refresh=True):
                    self.broker.set_bar_context(self.timeframe, close_time)
                    self.event_processor.allocator.allocate(
                        batch["candidates"], portfolio=self.broker.portfolio,
                        broker=self.execution_adapter, risk_manager=self.risk_manager,
                        current_prices=self._snapshot.prices,
                    )
                for key in batch["keys"]:
                    if self._has_unresolved_unknown(refresh=True):
                        state_store.release_bar(key)
                    else:
                        state_store.complete_bar(key, now.isoformat())
            except Exception as exc:
                for key in batch["keys"]:
                    state_store.release_bar(key)
                strategy_failures.append(("allocation_batch", type(exc).__name__))
        for strategy in self.strategies.values():
            state_store.set(f"strategy_runtime:{strategy.name}", {
                "context": strategy.context,
                "consumed_close_event_ids": sorted(strategy._consumed_close_event_ids),
            })

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
            if getattr(self.broker, "market_type", None) in {"spot", "margin"}:
                held = float(self.broker.portfolio.get_position(str(record["symbol"]))["qty"])
                remaining = float(record.get("remaining_qty", record["requested_qty"]))
                reduce_only = (str(record["side"]) == ("sell" if held > 0 else "cover")
                               and 0 < remaining <= abs(held) + 1e-9)
            orders.append(ProtectiveOrder(
                order_id=str(record["client_order_id"]),
                symbol=str(record["symbol"]),
                side=str(record["side"]),
                qty=float(record.get("remaining_qty", record["requested_qty"])),
                stop_price=float(intent.get("trigger_price") or record.get("price") or 0.0),
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
        portfolio = getattr(self.broker, "portfolio", None)
        if portfolio is not None:
            lots = portfolio.open_lots(symbol)
            levels = [lot.stop_price for lot in lots if lot.stop_price is not None]
            if levels:
                return max(levels) if portfolio.get_position(symbol)["qty"] > 0 else min(levels)
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
                    action_id = f"gap:{client_order_id}:{filled_qty}"
                    target_key = f"gap_target:{action_id}"
                    target = state_store.get(target_key)
                    if target is None:
                        target = max(0.0, held_qty - resize_qty)
                        state_store.set(target_key, target)
                    accepted = self._submit_risk_exit(
                        symbol, "GapRiskResize", target, action_id, average_fill_price
                    )
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
            self._operational_state = "DEGRADED"
            logger.exception("Protective order reconciliation could not read orders")
            self._alert("critical", "protective_orders_unreadable", {
                "error": type(exc).__name__,
            })
            return
        symbols = (set(portfolio.positions) | {order.symbol for order in venue_orders}
                   | manager.tracked_symbols)
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

    def _risk_sequence(self, key: str) -> int:
        store = getattr(self.broker, "order_store", None)
        allocate = getattr(store, "action_sequence", None)
        if not callable(allocate):
            raise RuntimeError("durable risk action identity is unavailable")
        return allocate(key)

    def _apply_protective_intent(self, symbol: str, intent) -> None:
        action = intent.action
        try:
            if action in (ProtectiveAction.CANCEL, ProtectiveAction.REPLACE):
                result = self.broker.cancel_order(intent.cancel_order_id)
                if result.status not in {OrderStatus.CANCELED, OrderStatus.FILLED, OrderStatus.EXPIRED}:
                    self._operational_state = "DEGRADED"
                    return  # No replacement until cancellation is authoritative.
                if action is ProtectiveAction.REPLACE:
                    if not self.broker.sync():
                        self._operational_state = "DEGRADED"
                        return
                    held = abs(self.broker.portfolio.get_position(symbol)["qty"])
                    if held <= 1e-12:
                        return
            snapshot = getattr(self, "_snapshot", None)
            reference = (getattr(snapshot, "prices", {}) or {}).get(symbol) or intent.stop_price
            key = f"{symbol}:{action.value}:{intent.cancel_order_id}:{intent.qty}:{intent.stop_price}:{getattr(self.broker, '_bar_time', '')}"
            if action in (ProtectiveAction.PLACE, ProtectiveAction.REPLACE):
                result = self.broker.submit_order(
                    symbol, intent.side, min(intent.qty, abs(self.broker.portfolio.get_position(symbol)["qty"])),
                    trigger_price=intent.stop_price, reference_price=reference, order_type="stop",
                    timestamp=self._now(), strategy_id="ProtectiveStop",
                    exit_reason="protective_stop", reduce_only=True,
                    sequence=self._risk_sequence(key), risk_action_id=key,
                )
                if not result.accepted:
                    self._operational_state = "DEGRADED"
                    self._alert("critical", "protective_order_unconfirmed", {
                        "symbol": symbol, "status": result.status.value,
                        "client_order_id": result.client_order_id,
                    })
                    # UNKNOWN may already own inventory. The broker will prevent
                    # over-selling until that outstanding exit is reconciled.
                    self._submit_risk_exit(symbol, "unprotected_flatten", 0.0, key, reference)
            if action is ProtectiveAction.FLATTEN:
                self._operational_state = "DEGRADED"
                self._alert("critical", "position_unprotected", {
                    "symbol": symbol, "reason": intent.reason, "qty": intent.qty,
                })
                self._submit_risk_exit(symbol, "unprotected_flatten", 0.0, key, reference)
        except Exception as exc:
            logger.exception("Protective intent failed: %s %s", symbol, action)
            self._operational_state = "DEGRADED"
            self._alert("critical", "protective_intent_failed", {
                "symbol": symbol, "action": action.value,
                "reason": intent.reason, "error": type(exc).__name__,
            })

    def _submit_risk_exit(self, symbol, reason, target_qty, action_id, reference):
        # Cancel every competing exit and opening request before a market risk
        # action. Unknown cancellation keeps the inventory reserved.
        store = self.broker.order_store
        own_pending = []
        for row in store.list_non_terminal():
            if row["symbol"] == symbol:
                if (row.get("intent") or {}).get("risk_action_id") == action_id:
                    own_pending.append(row)
                    self.broker.reconcile_order(row["client_order_id"])
                    continue
                result = self.broker.cancel_order(row["client_order_id"])
                if result.status not in {OrderStatus.CANCELED, OrderStatus.FILLED, OrderStatus.EXPIRED}:
                    self._operational_state = "DEGRADED"
                    return False
        if not self.broker.sync():
            return False
        held = float(self.broker.portfolio.get_position(symbol)["qty"])
        qty = max(abs(held) - target_qty, 0.0)
        if qty <= 1e-12:
            return True
        for pending in own_pending:
            if store.get(pending["client_order_id"])["status"] not in {s.value for s in (OrderStatus.CANCELED, OrderStatus.FILLED, OrderStatus.EXPIRED, OrderStatus.REJECTED)}:
                self._operational_state = "DEGRADED"
                return False
        previous = [row["client_order_id"] for row in store.list_all()
                    if row["symbol"] == symbol and (row.get("intent") or {}).get("risk_action_id") == action_id]
        key = f"{action_id}:{symbol}:{held}:{target_qty}:{','.join(sorted(previous))}"
        result = self.broker.submit_order(
            symbol, "sell" if held > 0 else "cover", qty,
            reference_price=reference, order_type="market", timestamp=self._now(),
            strategy_id="GapRiskResize" if reason == "GapRiskResize" else "PortfolioRiskExit", exit_reason=reason, reduce_only=True,
            sequence=self._risk_sequence(key), risk_action_id=action_id,
        )
        if not result.accepted or not self.broker.sync():
            self._operational_state = "DEGRADED"
            return False
        remaining = abs(float(self.broker.portfolio.get_position(symbol)["qty"]))
        if remaining > target_qty + 1e-9:
            self._operational_state = "DEGRADED"
            return False
        return True
