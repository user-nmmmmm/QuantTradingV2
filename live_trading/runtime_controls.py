"""显式启用的行情限时读取、保护调度与恢复补帧控制。

工作线程只读取行情；行情缓存提交、状态落盘和 broker 调用均留在引擎线程。
限时等待不强制终止底层网络请求，也不使保护任务抢占当前同步调用。
"""
from types import SimpleNamespace
import time

import pandas as pd

from core.market_data import LiveMarketDataAdapter
from core.market_data_errors import MarketDataRefreshError
from core.market_read_batch import BoundedMarketReads
from core.data_fetcher import DataFetcher
from core.timeframes import as_utc_timestamp, timeframe_delta
from live_trading.catchup import plan_catchup, replay_state_only, runtime_checkpoint
from live_trading.protection_schedule import ProtectionSchedule, RuntimeSchedulePolicy


class RuntimeControls:
    """协调可选调度组件；调用方负责在退出时关闭行情读取池。"""

    def __init__(self, policy=None, *, workers=4):
        self.policy = policy or RuntimeSchedulePolicy()
        self._reads = BoundedMarketReads(workers) if self.policy.enabled else None
        self.protection = ProtectionSchedule(self.policy.protection_interval_seconds)
        self.catchup_status = {}

    def update_data(self, engine):
        """并行读取行情，再由调用线程合并成功标的；失败标的保留旧缓存。

        读取等待使用总预算的 80%，余下时间用于启动逐标的指标刷新。截止
        检查发生在各标的刷新之前，不会中断已经开始的同步计算。任何失败
        都在合并可用结果后抛出 MarketDataRefreshError，交由 tick 禁新增风险。
        """
        if not self.policy.enabled:
            raise RuntimeError("runtime controls require explicit enablement")
        started = time.monotonic()
        deadline = started + self.policy.market_timeout_seconds
        adapter = engine.market_data_adapter
        store = getattr(engine, "state_store", None)
        cursors = {symbol: store.get(f"catchup_cursor:{symbol}:{engine.timeframe}")
                   for symbol in engine.symbols} if store is not None else {}
        def fetch(symbol):
            frame = engine.fetcher.fetch_ccxt(symbol, timeframe=engine.timeframe,
                limit=adapter.lookback,
                **({"exchange_id": adapter.exchange_id} if adapter.exchange_id else {}),
                **({"market_type": adapter.market_type} if adapter.market_type not in {"spot", "margin"} else {}),
                **({"max_retries": 1} if type(engine.fetcher) is DataFetcher else {}))
            cursor = cursors.get(symbol)
            # 内置 DataFetcher 每轮只补取一个有界历史窗口；仍有缺口则由补帧门控等待。
            if (type(engine.fetcher) is DataFetcher and cursor is not None and not frame.empty
                    and as_utc_timestamp(frame.index[0]) > as_utc_timestamp(cursor) + timeframe_delta(engine.timeframe)):
                since = as_utc_timestamp(cursor) - adapter.lookback * timeframe_delta(engine.timeframe)
                backfill = engine.fetcher.fetch_ccxt_since(symbol, since,
                    exchange_id=adapter.exchange_id, timeframe=engine.timeframe,
                    market_type=adapter.market_type,
                    limit=min(1000, adapter.lookback + self.policy.catchup_max_bars + 1))
                frame = pd.concat([backfill, frame])
                frame = frame.loc[~frame.index.duplicated(keep="last")].sort_index()
            return frame
        batch = self._reads.read(engine.symbols, fetch,
            timeout_seconds=self.policy.market_timeout_seconds * .8)
        # 暂存适配器隔离本轮结果；超时线程的迟到结果不能回写引擎缓存。
        snapshot = SimpleNamespace(fetch_ccxt=lambda symbol, **_: batch.values.get(symbol, pd.DataFrame()))
        staged = LiveMarketDataAdapter([], snapshot, timeframe=adapter.timeframe,
            lookback=adapter.lookback, exchange_id=adapter.exchange_id, market_type=adapter.market_type)
        staged.data_map = dict(engine.data_map)
        staged._raw_data_map = dict(adapter._raw_data_map)
        staged._last_fetched_latest = dict(adapter._last_fetched_latest)
        failures, regressions = dict(batch.failures), set()
        for symbol in engine.symbols:
            if symbol in failures:
                continue
            if time.monotonic() >= deadline:
                failures[symbol] = "compute_timeout"
                continue
            staged.symbols = [symbol]
            staged.lookback = max(adapter.lookback, min(adapter.lookback * 2 + self.policy.catchup_max_bars,
                                                       len(batch.values.get(symbol, ()))))
            staged.refresh()
            failures.update(staged.failed_symbols)
            regressions.update(staged.regressed_symbols)
        adapter.data_map, adapter._raw_data_map = staged.data_map, staged._raw_data_map
        adapter._last_fetched_latest = staged._last_fetched_latest
        adapter.failed_symbols, adapter.regressed_symbols = failures, regressions
        adapter.refresh_metrics = {"total_seconds": time.monotonic() - started,
            "fetch_seconds": batch.elapsed_seconds, "workers": adapter.max_workers,
            "symbols": len(engine.symbols), "failed_symbols": failures,
            "timeout_seconds": self.policy.market_timeout_seconds}
        engine.data_map = adapter.data_map
        if failures:
            raise MarketDataRefreshError(failures)

    def prepare_bar(self, engine, symbol, frame, state_store):
        """推进有限历史补帧并保存进度，返回可以正常处理的最新 bar。"""
        key = f"catchup_cursor:{symbol}:{engine.timeframe}"
        last = state_store.get(key)
        plan = plan_catchup(frame, last, timeframe=engine.timeframe,
            max_bars=self.policy.catchup_max_bars)
        self.catchup_status[symbol] = {"replayed": len(plan.replay_times),
            "pending": plan.pending, "gap": plan.gap}
        for timestamp in plan.replay_times:
            replay_state_only(engine, symbol, frame, timestamp)
            # 游标与恢复状态原子写入，避免重启后游标领先于实际状态。
            state_store.set_many({**runtime_checkpoint(engine), key: timestamp.isoformat()})
        if plan.gap:
            engine._alert("error", "catchup_gap", {"symbol": symbol, "pending": plan.pending})
        return plan.live_time

    @staticmethod
    def complete_bar(engine, symbol, timestamp, state_store):
        """为已经完成认领处理的 bar 补齐游标与运行状态；此处不完成 bar 租约。"""
        state_store.set_many({**runtime_checkpoint(engine),
            f"catchup_cursor:{symbol}:{engine.timeframe}": timestamp.isoformat()})

    def protect_if_due(self, engine):
        """到期时串行同步账户、恢复未知订单并核对外部持仓及保护单。

        余额事实不可用时仅告警并结束本次保护回调；返回值不是保护成功标记。
        """
        def protect():
            from live_trading.recovery import balance_sync_succeeded
            sync = engine.broker.sync()
            if not balance_sync_succeeded(engine.broker, sync):
                engine._alert("critical", "protection_sync_failed", {})
                return
            engine._last_account_sync_at = getattr(sync, "synced_at", None) or engine._now()
            engine._unresolved_unknown_cache = None
            engine._run_reconciliation_if_due(engine._now(), force=engine._has_unresolved_unknown())
            engine._reconcile_external_positions()
            engine._reconcile_protective_orders()
        return self.protection.run_if_due(protect)

    def close(self):
        if self._reads is not None:
            self._reads.close()
