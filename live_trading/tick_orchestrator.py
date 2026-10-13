"""编排单次实盘轮询：保护检查、行情、账户对账、风险动作和策略处理。

LiveTradingEngine 负责 initialize()/run() 生命周期；本协作类与恢复、导出
协作类通过 host 引用共享同一引擎状态（B4，ADR 0018）。新增风险门控与已有持仓保护分别判断：拒绝新开仓
不等于跳过账户恢复或保护单对账，具体路径仍取决于可用账户事实和风险动作。
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from typing import Any, Dict

import pandas as pd

from core.foundation.domain import OrderStatus
from ops.health import HealthReason
from core.foundation.logger import get_logger
from core.market.market_data_errors import MarketDataRefreshError
from core.trading.protective_orders import (
    ProtectiveAction,
    ProtectiveOrder,
    ProtectiveOrderManager,
    authoritative_position_ids,
    parse_protective_position_reference,
    protective_position_reference,
)
from core.trading.protective_stops import evaluate_fill_risk
from core.trading.entry_risk import resolve_approved_risk
from core.foundation.runtime import MarketDataSlice
from core.risk.actions import plan_risk_action
from core.market.timeframes import as_utc_timestamp, closed_bars, timeframe_delta
from core.trading.valuation import build_portfolio_snapshot
from time import perf_counter
from live_trading.recovery import (
    account_new_risk_gate, balance_sync_succeeded, supports_account_reconciliation,
)
from live_trading.catchup import runtime_checkpoint
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from live_trading.engine import LiveTradingEngine

# Same logger name as live_trading.engine (logging.getLogger caches by name,
# so this is the identical object) -- tests patch "live_trading.engine.logger"
# and must keep catching exceptions logged from this module too.
logger = get_logger("live_trading.engine")


class TickOrchestrator:
    """The body of one live tick: data, sync, reconciliation, risk, routing.

    Needs from the host: the full ``LiveTradingEngine`` attribute set (broker, risk_manager, event_processor,
    state helpers) plus the ``Recovery``/``StateExport``/``PortfolioRiskActions`` methods it calls into.
    """

    def __init__(self, host: "LiveTradingEngine") -> None:
        self._host = host


    def _update_data(self):
        host = self._host
        controls = getattr(host, "runtime_controls", None)
        if controls is not None and controls.policy.enabled:
            controls.update_data(host)
            return
        host.market_data_adapter.data_map = dict(host.data_map)
        host.data_map = host.market_data_adapter.refresh()
        if host.market_data_adapter.failed_symbols:
            raise MarketDataRefreshError(host.market_data_adapter.failed_symbols)

    def _tick(self) -> bool:
        """隔离意外异常并更新退避；返回 True 仅表示本轮未抛出未处理异常。

        行情、账户或风险门控可以在 _tick_once 内结束本轮，此时不能把 True
        解释为允许交易或账户健康。
        """
        host = self._host
        started = perf_counter()
        try:
            host._tick_once()
        except Exception as exc:
            host._consecutive_tick_crashes += 1
            if host._consecutive_tick_crashes == 1:
                host._next_retry_delay = min(
                    host.failure_backoff_base_seconds,
                    host.failure_backoff_max_seconds,
                )
            else:
                host._next_retry_delay = min(
                    host.failure_backoff_max_seconds,
                    max(
                        host.failure_backoff_base_seconds,
                        host._next_retry_delay * 2,
                    ),
                )
            host._healthy = False
            host._operational_state = 'HALTED'
            logger.exception(
                'Unexpected live tick failure; retrying in %.3fs',
                host._next_retry_delay,
            )
            host._alert('critical', 'tick_crashed', {
                'error': type(exc).__name__,
                'consecutive_failures': host._consecutive_tick_crashes,
                'retry_delay_seconds': host._next_retry_delay,
            })
            try:
                host._export_state()
            except Exception:
                logger.exception('Failed to export state after live tick crash')
            return False
        finally:
            host._last_tick_seconds = perf_counter() - started
        host._consecutive_tick_crashes = 0
        host._next_retry_delay = 0.0
        return True

    def _tick_once(self):
        """先建立账户和风险事实，再按收盘时间批量分配通过门控的候选。

        可选 runtime_controls 在行情刷新前运行到期保护，并在认领最新 bar
        前仅补齐历史状态。bar 租约、订单对账及最终入场复核分别承担不同责任。
        """
        host = self._host
        host._tick_count += 1
        host._unresolved_unknown_cache = None
        now = host._now()
        state_store = host._ensure_state_store()
        host._reset_daily_risk_if_needed(now)
        controls = getattr(host, "runtime_controls", None)
        if controls is not None and controls.policy.enabled:
            controls.protect_if_due(host)
        data_failure = None
        try:
            host._update_data()
        except Exception as exc:
            data_failure = type(exc).__name__
            host._assess_health(now, HealthReason(
                "MARKET_DATA_UPDATE_FAILED", "market_data", "data",
                f"market data update failed: {type(exc).__name__}",
            ))
            host._alert("error", "tick_unhealthy", {
                "operation": "update_data", "error": type(exc).__name__,
            })
            # Continue account/order recovery even when signal data is unavailable.
        now = host._now()
        try:
            sync_result = host.broker.sync()
        except Exception as exc:
            host._assess_health(now, HealthReason(
                "ACCOUNT_SYNC_FAILED", "account_sync", "account",
                f"account synchronization raised: {type(exc).__name__}",
            ))
            host._alert("error", "tick_unhealthy", {
                "operation": "broker_sync", "error": type(exc).__name__,
            })
            host._maybe_export_state()
            return
        host._healthy = bool(getattr(sync_result, "ok", sync_result is None))
        # 余额同步成功与独立账户核验通过是两个条件。前者可为保护/退出提供
        # 真实持仓，后者仍可拒绝新增风险；网络或格式错误则连持仓事实也不可信。
        if not balance_sync_succeeded(host.broker, sync_result):
            host._assess_health(now, HealthReason(
                "ACCOUNT_SYNC_FAILED", "account_sync", "account",
                f"account synchronization failed: {getattr(sync_result, 'error', 'unknown')}",
            ))
            logger.error("Trading disabled: portfolio synchronization failed")
            host._maybe_export_state()
            return
        synced_at = getattr(sync_result, "synced_at", None)
        host._last_account_sync_at = (
            synced_at if isinstance(synced_at, datetime) else now
        )

        try:
            host._run_reconciliation_if_due(
                now, force=host._has_unresolved_unknown()
            )
        except Exception as exc:
            host._reconciliation_status = {
                "last_run_at": now.isoformat(),
                "checked_count": 0,
                "discrepancy_count": 1,
                "ok": False,
                "error": type(exc).__name__,
            }
            host._assess_health(now, HealthReason(
                "ORDER_SYNC_FAILED", "order_sync", "order",
                f"order synchronization failed: {type(exc).__name__}",
            ))
            host._alert("error", "reconcile_discrepancy", {
                "error": type(exc).__name__,
            })
            host._maybe_export_state()
            return
        if host._has_unresolved_unknown():
            host._assess_health(now, HealthReason(
                "ORDER_STATE_UNKNOWN", "order_sync", "order",
                "an order has unresolved exchange state",
            ))
            logger.critical("New risk blocked: unresolved unknown order; reconciling protection")

        # Network reads may cross a close or UTC day boundary. Never classify
        # closed bars or freshness using the tick's pre-fetch wall clock.
        now = host._now()
        host._reset_daily_risk_if_needed(now)
        prices: Dict[str, float] = {}
        price_times = {}
        closed_map: Dict[str, pd.DataFrame] = {}
        for symbol, data in host.data_map.items():
            eligible = closed_bars(data, host.timeframe, now, host.close_grace_seconds)
            if not eligible.empty:
                closed_map[symbol] = eligible.copy()
                prices[symbol] = float(eligible["close"].iloc[-1])
                price_times[symbol] = as_utc_timestamp(eligible.index[-1]).to_pydatetime()

        account_gate = account_new_risk_gate(host.broker, host._now(),
            persistence_failed=getattr(host, "_account_reconciliation_persistence_failed", False))
        host._reconciliation_status["account_entry_gate"] = account_gate
        host._reconciliation_status["allows_new_risk"] = account_gate["allows_new_risk"] and not host._has_unresolved_unknown()
        if supports_account_reconciliation(host.broker):
            host._reconciliation_status["account_reconciliation"] = getattr(host.broker, "account_reconciliation_report", {})
        extra_health = []
        if data_failure:
            extra_health.append(HealthReason("MARKET_DATA_UPDATE_FAILED", "market_data", "data", data_failure))
        if not account_gate["allows_new_risk"]:
            extra_health.append(HealthReason("ACCOUNT_FACTS_UNVERIFIED", "account_sync", "account", account_gate["reason"]))
        host._assess_health(now, *extra_health)
        if host._reconcile_external_positions():
            host._reconcile_protective_orders()
            host._maybe_export_state(force=True)
            return
        mark_loader = getattr(type(host.broker), "risk_price_facts", None)
        if callable(mark_loader):
            try:
                facts = host.broker.risk_price_facts(set(host.symbols) | set(host.broker.portfolio.positions))
                prices = {symbol: fact["price"] for symbol, fact in facts.items()}
                price_times = {symbol: fact["timestamp"] for symbol, fact in facts.items()}
            except Exception as exc:
                host._assess_health(now, HealthReason("RISK_MARK_UNAVAILABLE", "valuation", "portfolio", type(exc).__name__))
                host._reconcile_protective_orders()
                host._maybe_export_state()
                return

        now = host._now()
        host._reset_daily_risk_if_needed(now)
        host._assess_health(now, *extra_health)
        try:
            host._snapshot = build_portfolio_snapshot(
                host.broker.portfolio,
                prices,
                price_times,
                now if now.tzinfo else now.replace(tzinfo=timezone.utc),
            )
        except ValueError as exc:
            host._assess_health(now, HealthReason(
                "VALUATION_FACT_MISSING", "valuation", "portfolio",
                f"portfolio valuation is unavailable: {exc}",
            ))
            logger.error("Trading disabled: %s", exc)
            host._maybe_export_state()
            return

        host.event_processor.last_prices.update(host._snapshot.prices)
        day_key = f"daily_start_equity:{now.date().isoformat()}"
        daily_start = state_store.get(day_key)
        if daily_start is None:
            daily_start = host._snapshot.equity
            state_store.set(day_key, daily_start)
        was_already_triggered = bool(host.risk_manager.circuit_breaker_triggered)
        breaker = host.risk_manager.check_circuit_breaker(
            host._snapshot.equity, float(daily_start), occurred_at=now
        )
        host._save_breaker_checkpoint(now.date().isoformat())
        # Refresh only the breaker-derived state using this tick's existing
        # health assessment, preserving account/data failures already found.
        if host.health_assessment is not None:
            host._set_health_assessment(host.health_assessment)
        budget = getattr(host.risk_manager, "drawdown_budget", None)
        if budget is not None:
            budget.update(host._snapshot.prices,
                {symbol: frame.iloc[-1] for symbol, frame in closed_map.items() if not frame.empty}, now)
        action_plan = plan_risk_action(breaker, now.date())
        # REDUCE owns opening cancellation and freezes targets only after the
        # cancellation facts are durable. A plain BLOCK_NEW still cancels opens.
        if breaker and action_plan is None:
            for pending in host.broker.order_store.list_non_terminal():
                if pending["side"] in {"buy", "short"}:
                    host.broker.cancel_order(pending["client_order_id"])
        risk_progress = host._reconcile_portfolio_risk_action(action_plan)
        budget_ready = host._reconcile_drawdown_budget(execute=not risk_progress.blocks_entries)
        if risk_progress.blocks_entries or not budget_ready:
            host._reconcile_protective_orders()
            host._maybe_export_state(force=True)
            return
        host._reconcile_protective_orders()
        if host._operational_state == "DEGRADED" or not host._recheck_live_entry_risk():
            host._maybe_export_state(force=True)
            return
        if breaker:
            host._operational_state = "RISK_HALTED"
            logger.critical("New entries disabled by circuit breaker: %s", breaker.reason_codes)
            # Alert only on the trip itself, not every tick the halt stays
            # active: equity moves every tick, which would otherwise defeat
            # HysteresisAlertSink's dedup key and page on every interval.
            if not was_already_triggered:
                host._alert("critical", "circuit_breaker_triggered", {
                    "equity": host._snapshot.equity,
                    "daily_start_equity": float(daily_start),
                })
            # A portfolio BLOCK_NEW cooldown must keep strategy exits running.
            # Daily/terminal halts retain their existing execution behavior.
            if breaker.action.value != "block_new" or breaker.daily_loss_triggered:
                host._maybe_export_state()
                return

        strategy_failures = []
        batches: Dict[Any, Dict[str, list]] = {}
        for symbol in sorted(host.symbols):
            frame = closed_map.get(symbol)
            if frame is None or frame.empty:
                continue
            timestamp = frame.index[-1]
            if controls is not None and controls.policy.enabled:
                timestamp = controls.prepare_bar(host, symbol, frame, state_store)
                if timestamp is None:
                    continue
            close_time = as_utc_timestamp(timestamp) + timeframe_delta(host.timeframe)
            bar_key = (
                f"{getattr(host.broker, 'exchange_id', 'exchange')}|"
                f"{getattr(host.broker, 'account_id', getattr(host.broker, 'market_type', 'spot'))}|"
                f"{symbol}|{host.timeframe}|{close_time.isoformat()}"
            )
            if not state_store.claim_bar(
                bar_key, now.isoformat(), lease_seconds=host.bar_claim_lease_seconds
            ):
                if controls is not None and controls.policy.enabled and state_store.status(bar_key) == "processed":
                    controls.complete_bar(host, symbol, timestamp, state_store)
                continue
            set_context = getattr(host.broker, "set_bar_context", None)
            if callable(set_context):
                set_context(host.timeframe, close_time)
            event = MarketDataSlice(
                timestamp=timestamp,
                bars={symbol: frame.iloc[-1]},
                histories={symbol: frame},
                timeframe=host.timeframe,
                source="live",
            )
            try:
                candidate, _ = host.event_processor._collect_symbol_candidate(
                    event, symbol, allow_position_management=True,
                    allow_new_entries=not bool(breaker) and host._healthy and not data_failure
                        and account_gate["allows_new_risk"],
                )
                batch = batches.setdefault(close_time, {"candidates": [], "keys": [], "cursors": {}})
                batch["keys"].append(bar_key)
                batch["cursors"][bar_key] = (symbol, timestamp)
                if candidate is not None and account_gate["allows_new_risk"]:
                    batch["candidates"].append(candidate)
                if host._has_unresolved_unknown(refresh=True):
                    state_store.release_bar(bar_key)
                    host._assess_health(now, HealthReason(
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
                # 持仓管理可能耗时或引入 UNKNOWN；分配前复核账户证据新鲜度和订单状态。
                allocation_gate = account_new_risk_gate(host.broker, host._now(),
                    persistence_failed=getattr(host, "_account_reconciliation_persistence_failed", False))
                host._reconciliation_status["account_entry_gate"] = allocation_gate
                host._reconciliation_status["allows_new_risk"] = allocation_gate["allows_new_risk"] and not host._has_unresolved_unknown()
                if not allocation_gate["allows_new_risk"] and account_gate["allows_new_risk"]:
                    host._assess_health(host._now(), HealthReason(
                        "ACCOUNT_FACTS_UNVERIFIED", "account_sync", "account", allocation_gate["reason"]))
                if not host._has_unresolved_unknown(refresh=True) and allocation_gate["allows_new_risk"]:
                    host.broker.set_bar_context(host.timeframe, close_time)
                    host.event_processor.allocator.allocate(
                        batch["candidates"], portfolio=host.broker.portfolio,
                        broker=host.execution_adapter, risk_manager=host.risk_manager,
                        current_prices=host._snapshot.prices,
                    )
                for key in batch["keys"]:
                    if host._has_unresolved_unknown(refresh=True):
                        state_store.release_bar(key)
                    else:
                        if controls is not None and controls.policy.enabled:
                            symbol, timestamp = batch["cursors"][key]
                            # bar 完成事实、游标与运行状态一并提交，避免崩溃后跳过未落盘状态。
                            state_store.complete_bar(key, now.isoformat(), state_values={
                                **runtime_checkpoint(host),
                                f"catchup_cursor:{symbol}:{host.timeframe}": timestamp.isoformat()})
                        else:
                            state_store.complete_bar(key, now.isoformat())
            except Exception as exc:
                for key in batch["keys"]:
                    state_store.release_bar(key)
                strategy_failures.append(("allocation_batch", type(exc).__name__))
        runtime_states = {"router_runtime": host.router.checkpoint()}
        for strategy in host.strategies.values():
            runtime_states[f"strategy_runtime:{strategy.name}"] = {
                "context": strategy.context,
                "consumed_close_event_ids": sorted(strategy._consumed_close_event_ids),
                "trade_state": getattr(strategy, "trade_state", None),
            }
        state_store.set_many(runtime_states)

        if strategy_failures:
            host._consecutive_strategy_failures += len(strategy_failures)
            host._last_strategy_error = strategy_failures[-1][1]
            halted = (
                host._consecutive_strategy_failures
                >= host.strategy_failure_threshold
            )
            host._healthy = False
            host._operational_state = "HALTED" if halted else "DEGRADED"
            host._alert("critical" if halted else "error", "strategy_processing_failed", {
                "failures": strategy_failures,
                "consecutive_failures": host._consecutive_strategy_failures,
                "threshold": host.strategy_failure_threshold,
                "operational_state": host._operational_state,
            })
        else:
            host._consecutive_strategy_failures = 0
            host._last_strategy_error = None
        host._reconcile_protective_orders()
        host._alert_strategy_health_transitions()
        host._maybe_export_state()

    def _alert_strategy_health_transitions(self) -> None:
        """SR1-4: a health transition is an operator event, not a log line.

        Every new ACTIVE/COOLDOWN/PROBATION/MANUAL_LOCK transition raises one
        alert exactly once. MANUAL_LOCK is critical - it will not clear on its
        own and needs a human.
        """
        host = self._host
        seen = getattr(host, "_alerted_health_transitions", None)
        if seen is None:
            seen = set()
            host._alerted_health_transitions = seen
        for name, strategy in getattr(host, "strategies", {}).items():
            machine = getattr(strategy, "health", None)
            if machine is None:
                continue
            for index, row in enumerate(machine.transitions):
                key = (name, index, row.get("at"), row.get("to"))
                if key in seen:
                    continue
                seen.add(key)
                host._alert(
                    "critical" if row.get("to") == "manual_lock" else "warning",
                    "strategy_health_transition",
                    {"strategy": name, **row},
                )

    # ------------------------------------------------------------ SR2-5

    def _protective_manager(self):
        host = self._host
        manager = getattr(host, "_protective_order_manager", None)
        if manager is None:
            manager = ProtectiveOrderManager()
            # A crash may follow an acknowledged stop cancellation but precede
            # replacement. Its immutable attributed intent retains the ratchet.
            store = getattr(host.broker, "order_store", None)
            if store is not None:
                for record in store.list_all():
                    if str(record.get("order_type", "")).lower() != "stop":
                        continue
                    fact = record.get("intent") or {}
                    ids = parse_protective_position_reference(fact.get("causation_id"))
                    if ids is None:
                        continue
                    symbol = str(record["symbol"])
                    held = float(host.broker.portfolio.get_position(symbol)["qty"])
                    current_ids = authoritative_position_ids(host.broker.portfolio, symbol)
                    manager.restore_confirmed_stop(ProtectiveOrder(
                        str(record["client_order_id"]), symbol, str(record["side"]),
                        float(record.get("remaining_qty", record["requested_qty"])),
                        float(fact.get("trigger_price") or record.get("price") or 0),
                        str(record["status"]), position_ids=ids),
                        position_ids=current_ids, position_qty=held)
            host._protective_order_manager = manager
        return manager

    def _venue_protective_orders(self):
        """Protective orders as the order store currently knows them."""
        host = self._host
        store = getattr(host.broker, "order_store", None)
        if store is None:
            return []
        orders = []
        for record in store.list_non_terminal():
            if str(record.get("order_type", "")).lower() != "stop":
                continue
            intent = record.get("intent") or {}
            reduce_only = True
            if isinstance(intent, dict):
                reduce_only = bool(intent.get("reduce_only", True))
            if getattr(host.broker, "market_type", None) in {"spot", "margin"}:
                held = float(host.broker.portfolio.get_position(str(record["symbol"]))["qty"])
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
                position_ids=parse_protective_position_reference(intent.get("causation_id")),
            ))
        return orders

    def _desired_protective_stop(self, symbol: str, strategy_id: str = ""):
        """The level the owning strategy currently wants enforced."""
        host = self._host
        if strategy_id:
            strategy = getattr(host, "strategies", {}).get(strategy_id)
            context = getattr(strategy, "context", {}).get(symbol) or {}
            stop = context.get("effective_stop", context.get("stop_loss"))
            if stop:
                return float(stop)
        for strategy in getattr(host, "strategies", {}).values():
            context = getattr(strategy, "context", {}).get(symbol) or {}
            stop = context.get("effective_stop", context.get("stop_loss"))
            if stop:
                return float(stop)
        portfolio = getattr(host.broker, "portfolio", None)
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
        host = self._host

        policy = getattr(host, "entry_risk_policy", None)
        if policy is None or not policy.enabled or host._snapshot is None:
            return True
        order_store = getattr(host.broker, "order_store", None)
        list_with_fills = getattr(order_store, "list_with_fills", None)
        if not callable(list_with_fills):
            return True
        state_store = host._ensure_state_store()
        audit = getattr(host, "_live_fill_risk_audit", None)
        if audit is None:
            audit = []
            host._live_fill_risk_audit = audit

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

            symbol = str(record["symbol"])
            owned_lots = [lot for lot in host.broker.portfolio.open_lots(symbol)
                          if lot.order_id == client_order_id]
            if not owned_lots:
                # An old closed entry must never resize a newer position in
                # the same symbol when a checkpoint is absent or upgraded.
                continue

            checkpoint_key = f"entry_risk_check:{client_order_id}"
            checkpoint = state_store.get(checkpoint_key) or {}
            checked_qty = float(checkpoint.get("checked_filled_qty", 0.0) or 0.0)
            if (checkpoint.get("budget_contract_version") == 1
                    and abs(filled_qty - checked_qty) <= 1e-12
                    and checkpoint.get("checked_average_fill_price") == average_fill_price):
                continue

            stop = intent.get("initial_stop")
            if not stop:
                # Old closed orders need no action. An open position without
                # its original stop cannot manufacture an approval from today.
                if abs(float(host.broker.portfolio.get_position(symbol).get("qty", 0.0))) > 1e-12:
                    all_accepted = False
                    host._operational_state = "DEGRADED"
                    host._alert("critical", "entry_risk_approval_missing", {
                        "client_order_id": client_order_id, "symbol": symbol,
                        "reason": "original_initial_stop_missing",
                    })
                continue
            try:
                budget, budget_source = resolve_approved_risk(intent)
            except ValueError as exc:
                all_accepted = False
                host._operational_state = "DEGRADED"
                host._alert("critical", "entry_risk_approval_missing", {
                    "client_order_id": client_order_id, "symbol": symbol,
                    "reason": str(exc),
                })
                continue
            assessment = evaluate_fill_risk(
                symbol=symbol,
                lot_id=client_order_id,
                side="long" if side == "buy" else "short",
                fill_price=average_fill_price,
                protective_stop=stop,
                filled_qty=filled_qty,
                approved_risk_amount=budget,
                policy=policy,
            )
            if assessment is None:
                continue
            row = assessment.to_dict()
            row.update({
                "timestamp": host._now().isoformat(),
                "client_order_id": client_order_id,
                "strategy_id": strategy_id,
                "approved_risk_amount": budget,
                "budget_source": budget_source,
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
                    host.broker.portfolio.get_position(symbol).get("qty", 0.0)
                ))
                resize_qty = min(additional_resize, held_qty,
                                 sum(lot.qty_open for lot in owned_lots))
                if resize_qty > 1e-12:
                    action_id = f"gap:v1:{client_order_id}:{filled_qty}:{average_fill_price}"
                    target_key = f"gap_target:{action_id}"
                    target = state_store.get(target_key)
                    if target is None:
                        target = max(0.0, held_qty - resize_qty)
                        state_store.set(target_key, target)
                    accepted = host._submit_risk_exit(
                        symbol, "GapRiskResize", target, action_id, average_fill_price
                    )
                    if accepted:
                        # Reaching a persisted target also completes reductions
                        # filled before a retry/restart. Counting only today's
                        # remaining quantity would forget those fills and
                        # over-reduce when a later opening fill arrives.
                        requested_total = max(requested_total, float(assessment.resize_qty))
                    else:
                        all_accepted = False
                        host._operational_state = "DEGRADED"
                        host._alert("critical", "gap_risk_resize_failed", {
                            "symbol": symbol,
                            "client_order_id": client_order_id,
                            "requested_qty": resize_qty,
                            "risk_ratio": assessment.risk_ratio,
                        })

            if accepted:
                checkpoint = {
                    "budget_contract_version": 1,
                    "checked_filled_qty": filled_qty,
                    "checked_average_fill_price": average_fill_price,
                    "requested_resize_qty": requested_total,
                    "last_assessment": row,
                }
                state_store.set(checkpoint_key, checkpoint)
                state_store.set("live_fill_risk_audit", list(audit))
        return all_accepted

    def _reconcile_external_positions(self) -> bool:
        """Persist a named, zero-target exit before touching unknown inventory.

        No historical owner, entry time, cost basis or risk is invented. A
        mismatched symbol is flattened as a whole: mixed owned/unowned exposure
        cannot safely be treated as independent inventory at a netted venue.
        """
        host = self._host
        unowned = getattr(host.broker, "unowned_positions", {})
        if not isinstance(unowned, dict):
            return False
        state = host._ensure_state_store()
        key = "external_position_actions"
        actions = state.get(key, {})
        active = bool(unowned)
        for symbol, record in list(actions.items()):
            if record["status"] != "completed" and symbol not in unowned:
                qty = float(host.broker.portfolio.get_position(symbol).get("qty", 0.0))
                record.update(status="completed", completed_at=host._now().isoformat(),
                              completion="flat" if abs(qty) <= 1e-12 else "ownership_restored")
                state.set(key, actions)
        if not active:
            return False
        # This policy applies even when ordinary protective stops are disabled.
        host._operational_state = "DEGRADED"
        for symbol, observation in sorted(unowned.items()):
            record = actions.get(symbol)
            if record is None or record["status"] == "completed":
                previous_action = record.get("action_id") if record else None
                sequence = host._risk_sequence(
                    f"external-position:{symbol}:{previous_action}:{host._now().isoformat()}"
                )
                record = {
                    "schema_version": 1, **observation,
                    "action_id": f"external-position:{symbol}:{sequence}",
                    "first_detected_at": host._now().isoformat(),
                    "previous_action_id": previous_action,
                    "target_qty": 0.0, "status": "pending",
                }
                actions[symbol] = record
                state.set(key, actions)
                host._alert("critical", "external_position_detected", dict(record, symbol=symbol))
            reference = None
            try:
                if callable(getattr(type(host.broker), "risk_price_facts", None)):
                    reference = host.broker.risk_price_facts({symbol})[symbol]["price"]
                completed = host._submit_risk_exit(
                    symbol, "ExternalPositionExit", 0.0, record["action_id"], reference,
                )
                record.update(status="completed" if completed else "pending",
                              remaining_qty=float(host.broker.portfolio.get_position(symbol).get("qty", 0.0)),
                              last_checked_at=host._now().isoformat(),
                              last_error=None if completed else "exit_unconfirmed")
                if completed:
                    record.update(completed_at=host._now().isoformat(), completion="flat")
            except Exception as exc:
                record.update(status="pending", last_error=type(exc).__name__,
                              last_checked_at=host._now().isoformat())
                host._alert("critical", "external_position_exit_failed", {
                    "symbol": symbol, "action_id": record["action_id"], "error": type(exc).__name__,
                })
            state.set(key, actions)
        return True

    def _reconcile_protective_orders(self) -> None:
        """按真实持仓及 position_ids 核对驻场保护，避免旧持仓止损影响新持仓。

        保护数量扣除在途非止损退出已占用的数量，止损价只沿收紧方向移动。
        无法读取订单或确认持仓身份时降级告警；需替换的订单先确认撤单终态。
        不能确认保护时可请求退出，但退出仍受撤单、账户同步和持仓归属约束，
        不能把“已请求退出”视为“已平仓”。
        """
        host = self._host
        if not getattr(host, "protective_orders_enabled", True):
            return
        broker = host.broker
        portfolio = getattr(broker, "portfolio", None)
        if portfolio is None:
            return
        try:
            manager = host._protective_manager()
            venue_orders = host._venue_protective_orders()
        except Exception as exc:  # order store unreadable: do not guess
            host._operational_state = "DEGRADED"
            logger.exception("Protective order reconciliation could not read orders")
            host._alert("critical", "protective_orders_unreadable", {
                "error": type(exc).__name__,
            })
            return
        symbols = (set(portfolio.positions) | {order.symbol for order in venue_orders}
                   | manager.tracked_symbols)
        for symbol in sorted(symbols):
            qty = float(portfolio.get_position(symbol).get("qty", 0.0))
            try:
                position_ids = authoritative_position_ids(portfolio, symbol)
            except (ValueError, AttributeError, TypeError) as exc:
                host._operational_state = "DEGRADED"
                host._alert("critical", "protective_position_unverifiable", {
                    "symbol": symbol, "error": str(exc),
                })
                continue
            # In-flight market exits already reserve inventory. Protect only
            # the remainder, so protection cannot cancel or compete with an
            # authoritative partial risk reduction (including UNKNOWN orders).
            reserved = sum(float(row.get("remaining_qty") or 0.0)
                           for row in broker.order_store.list_non_terminal()
                           if row["symbol"] == symbol
                           and row["side"] == ("sell" if qty > 0 else "cover")
                           and str(row.get("order_type", "")).lower() != "stop")
            available = max(abs(qty) - reserved, 0.0)
            plan = manager.evaluate(
                symbol=symbol,
                position_qty=available if qty >= 0 else -available,
                desired_stop=host._desired_protective_stop(symbol),
                open_protective_orders=venue_orders,
                position_ids=position_ids,
                total_position_qty=qty,
            )
            for intent in plan.intents:
                host._apply_protective_intent(symbol, intent)

    def _risk_sequence(self, key: str) -> int:
        host = self._host
        store = getattr(host.broker, "order_store", None)
        allocate = getattr(store, "action_sequence", None)
        if not callable(allocate):
            raise RuntimeError("durable risk action identity is unavailable")
        return allocate(key)

    def _apply_protective_intent(self, symbol: str, intent) -> None:
        host = self._host
        action = intent.action
        try:
            if action in (ProtectiveAction.CANCEL, ProtectiveAction.REPLACE):
                result = host.broker.cancel_order(intent.cancel_order_id)
                if result.status not in {OrderStatus.CANCELED, OrderStatus.FILLED, OrderStatus.EXPIRED}:
                    host._operational_state = "DEGRADED"
                    return  # No replacement until cancellation is authoritative.
                if action is ProtectiveAction.REPLACE:
                    if not balance_sync_succeeded(host.broker, host.broker.sync()):
                        host._operational_state = "DEGRADED"
                        return
                    held = abs(host.broker.portfolio.get_position(symbol)["qty"])
                    if held <= 1e-12:
                        return
            snapshot = getattr(host, "_snapshot", None)
            reference = (getattr(snapshot, "prices", {}) or {}).get(symbol) or intent.stop_price
            key = f"{symbol}:{action.value}:{intent.cancel_order_id}:{intent.qty}:{intent.stop_price}:{getattr(host.broker, '_bar_time', '')}"
            if action in (ProtectiveAction.PLACE, ProtectiveAction.REPLACE):
                current_ids = authoritative_position_ids(host.broker.portfolio, symbol)
                current_qty = float(host.broker.portfolio.get_position(symbol)["qty"])
                if (not intent.position_ids or not set(current_ids).intersection(intent.position_ids)
                        or (current_qty > 0) != (intent.side == "sell")):
                    host._operational_state = "DEGRADED"
                    host._alert("critical", "protective_position_changed_before_submit", {
                        "symbol": symbol, "position_ids": list(current_ids),
                    })
                    return
                position_reference = protective_position_reference(intent.position_ids)
                key = f"{key}:{position_reference}"
                if action is ProtectiveAction.PLACE:
                    # A canceled predecessor in the same bar must not resolve
                    # to its old immutable client ID after process restart.
                    predecessors = sorted(str(row["client_order_id"])
                        for row in host.broker.order_store.list_all()
                        if row["symbol"] == symbol and str(row.get("order_type", "")).lower() == "stop")
                    lineage = hashlib.sha256("\n".join(predecessors).encode()).hexdigest()[:24]
                    key = f"{key}:predecessors:{lineage}"
                result = host.broker.submit_order(
                    symbol, intent.side, min(intent.qty, abs(host.broker.portfolio.get_position(symbol)["qty"])),
                    trigger_price=intent.stop_price, reference_price=reference, order_type="stop",
                    timestamp=host._now(), strategy_id="ProtectiveStop",
                    exit_reason="protective_stop", reduce_only=True,
                    sequence=host._risk_sequence(key), risk_action_id=key,
                    causation_id=position_reference,
                )
                if not result.accepted:
                    host._operational_state = "DEGRADED"
                    host._alert("critical", "protective_order_unconfirmed", {
                        "symbol": symbol, "status": result.status.value,
                        "client_order_id": result.client_order_id,
                    })
                    # UNKNOWN may already own inventory. The broker will prevent
                    # over-selling until that outstanding exit is reconciled.
                    host._submit_risk_exit(symbol, "unprotected_flatten", 0.0, key, reference)
            if action is ProtectiveAction.FLATTEN:
                host._operational_state = "DEGRADED"
                host._alert("critical", "position_unprotected", {
                    "symbol": symbol, "reason": intent.reason, "qty": intent.qty,
                })
                host._submit_risk_exit(symbol, "unprotected_flatten", 0.0, key, reference,
                    original_position_ids=intent.position_ids,
                    original_side="long" if intent.side == "sell" else "short")
        except Exception as exc:
            logger.exception("Protective intent failed: %s %s", symbol, action)
            host._operational_state = "DEGRADED"
            host._alert("critical", "protective_intent_failed", {
                "symbol": symbol, "action": action.value,
                "reason": intent.reason, "error": type(exc).__name__,
            })

    def _reconcile_drawdown_budget(self, *, execute=True):
        host = self._host
        budget = getattr(getattr(host, "risk_manager", None), "drawdown_budget", None)
        if budget is None or not budget.policy.enabled:
            return True
        # Observe delayed fills before comparing the lot ledger with the venue
        # balance. A balance-only sync can otherwise look like missing lots.
        for order in host.broker.order_store.list_non_terminal():
            if (order.get('intent') or {}).get('exit_reason') == 'DrawdownBudgetReduce':
                host.broker.reconcile_order(order['client_order_id'])
        snap = budget.snapshot()
        row = {"timestamp": str(host._now()), "event": "budget_review", **snap.to_dict()}
        budget.audit.append(row)
        host.state_store.set("drawdown_budget_snapshot", row)
        if snap.issues:
            host._operational_state = "DEGRADED"
            host._alert("error", "drawdown_budget_unverifiable", {"issues": snap.issues})
            return False
        if not execute:
            row["deferred_to_portfolio_risk_action"] = True
            host.state_store.set("drawdown_budget_snapshot", row)
            return False
        checkpoint = host.state_store.get("drawdown_budget_action")
        if snap.over_budget or checkpoint:
            for order in host.broker.order_store.list_non_terminal():
                if order['side'] in {'buy', 'short'}:
                    result = host.broker.cancel_order(order['client_order_id'])
                    if result.status not in {OrderStatus.CANCELED, OrderStatus.FILLED, OrderStatus.EXPIRED, OrderStatus.REJECTED}:
                        host._operational_state = "DEGRADED"
                        return False
            if not balance_sync_succeeded(host.broker, host.broker.sync()):
                host._operational_state = "DEGRADED"
                return False
            snap = budget.snapshot()
            if snap.issues:
                host._operational_state = "DEGRADED"
                return False
        if checkpoint is None and snap.over_budget:
            fraction = budget.reduction_fraction(snap)
            sequence = int(host.state_store.get('drawdown_budget_sequence') or 0) + 1
            host.state_store.set('drawdown_budget_sequence', sequence)
            checkpoint = {'action_id': f'drawdown-budget-{host._now().isoformat()}-{sequence}',
                'targets': {s: abs(float(p['qty'])) * fraction
                            for s, p in host.broker.portfolio.positions.items() if p['qty']},
                'positions': {s: [lot.position_id for lot in book.open_lots]
                              for s, book in host.broker.portfolio.lot_books.items()},
                'snapshot': snap.to_dict()}
            host.state_store.set("drawdown_budget_action", checkpoint)
        if checkpoint:
            done = True
            for symbol, target in checkpoint['targets'].items():
                book = host.broker.portfolio.lot_books.get(symbol)
                current_ids = {lot.position_id for lot in book.open_lots} if book else set()
                original_ids = set(checkpoint['positions'].get(symbol, []))
                if current_ids and not current_ids.issubset(original_ids):
                    host._operational_state = "DEGRADED"
                    host._alert('error', 'drawdown_budget_position_changed', {'symbol': symbol})
                    done = False
                    continue
                if not host._submit_risk_exit(symbol, 'DrawdownBudgetReduce', target,
                        checkpoint['action_id'], host._snapshot.prices.get(symbol)):
                    done = False
            row.update(action='reduce', action_id=checkpoint['action_id'], completed=done,
                       after=budget.snapshot().to_dict())
            host.state_store.set("drawdown_budget_snapshot", row)
            if done:
                host.state_store.set("drawdown_budget_action", None)
            # A risk-action tick never also opens new risk, including when all
            # fills completed immediately. The next tick recomputes the budget.
            return False
        return True

    def _submit_risk_exit(self, symbol, reason, target_qty, action_id, reference,
                          *, original_position_ids=None, original_side=None):
        # First reconcile this action's own orders. Completed actions must not
        # cancel an already-correct protective stop on every subsequent tick.
        host = self._host
        store = host.broker.order_store
        for row in store.list_non_terminal():
            if row["symbol"] == symbol and (row.get("intent") or {}).get("risk_action_id") == action_id:
                host.broker.reconcile_order(row["client_order_id"])
        if not balance_sync_succeeded(host.broker, host.broker.sync()):
            return False

        def owns_position():
            if original_position_ids is None:
                return True
            try:
                current = host._risk_action_position(symbol)
            except ValueError:
                host._operational_state = "DEGRADED"
                return None
            ids = set(current["position_ids"])
            if not ids.intersection(original_position_ids):
                return False
            if not ids.issubset(original_position_ids) or current["side"] != original_side:
                host._operational_state = "DEGRADED"
                return None
            return True

        def pending_owned():
            return [row for row in store.list_non_terminal()
                    if row["symbol"] == symbol
                    and (row.get("intent") or {}).get("risk_action_id") == action_id]

        ownership = owns_position()
        if ownership is None:
            return False
        held = float(host.broker.portfolio.get_position(symbol)["qty"])
        if not ownership or abs(held) <= target_qty + 1e-12:
            # A partially filled own order can still overshoot a target reached
            # by another exit. Cancel only that remainder, never normal stops.
            if not host._cancel_risk_orders(pending_owned()):
                return False
            ownership = owns_position()
            return ownership is False or (ownership is True and
                abs(float(host.broker.portfolio.get_position(symbol)["qty"])) <= target_qty + 1e-12)
        if pending_owned():
            host._operational_state = "DEGRADED"
            return False

        competing = [row for row in store.list_non_terminal() if row["symbol"] == symbol]
        if not host._cancel_risk_orders(competing):
            return False
        # Cancellation may itself reveal fills or close the original position.
        ownership = owns_position()
        if ownership is None:
            return False
        if not ownership:
            return True
        held = float(host.broker.portfolio.get_position(symbol)["qty"])
        qty = max(abs(held) - target_qty, 0.0)
        if qty <= 1e-12:
            return True
        previous = [row["client_order_id"] for row in store.list_all()
                    if row["symbol"] == symbol and (row.get("intent") or {}).get("risk_action_id") == action_id]
        key = f"{action_id}:{symbol}:{held}:{target_qty}:{','.join(sorted(previous))}"
        result = host.broker.submit_order(
            symbol, "sell" if held > 0 else "cover", qty,
            reference_price=reference, order_type="market", timestamp=host._now(),
            strategy_id="GapRiskResize" if reason == "GapRiskResize" else "PortfolioRiskExit", exit_reason=reason, reduce_only=True,
            sequence=host._risk_sequence(key), risk_action_id=action_id,
        )
        if not result.accepted or not balance_sync_succeeded(host.broker, host.broker.sync()):
            host._operational_state = "DEGRADED"
            return False
        remaining = abs(float(host.broker.portfolio.get_position(symbol)["qty"]))
        if remaining > target_qty + 1e-9:
            host._operational_state = "DEGRADED"
            return False
        if not host._cancel_risk_orders(pending_owned()):
            return False
        return True
