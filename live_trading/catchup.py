"""按持久化游标选择有限数量的补帧；历史帧只恢复状态，不补发订单。"""
from dataclasses import dataclass
import pandas as pd

from core.timeframes import as_utc_timestamp, timeframe_delta, observed_bars_after


@dataclass(frozen=True)
class CatchupPlan:
    """补帧结果：历史回放时间、可处理的最新 bar、剩余已观察帧数及缺口标记。

    pending 不估算缺失 bar 的数量；无缺口时也不包含预留给实盘的最新 bar。
    live_time 为 None 表示本轮尚不能进入正常策略处理。
    """

    replay_times: tuple
    live_time: object
    pending: int
    gap: bool


def plan_catchup(frame, last_processed, *, timeframe, max_bars=100):
    """为已收盘且索引有序、唯一的行情生成补帧计划。

    首次启动直接采用最新 bar；重启后仅回放游标之后的历史帧。每轮最多
    回放 max_bars 根，且只走到第一个时间缺口之前；补帧未完或存在缺口时
    不放行最新 bar。时间统一按 UTC 比较，返回值保留原始索引类型。
    """
    if type(max_bars) is not int or max_bars < 1:
        raise ValueError("catchup limit must be a positive integer")
    if frame.empty:
        return CatchupPlan((), None, 0, False)
    if not frame.index.is_monotonic_increasing or not frame.index.is_unique:
        raise ValueError("catchup history must be sorted and unique")
    latest = frame.index[-1]
    # 无恢复游标时只接纳当前最新帧，不执行启动前的历史信号。
    if last_processed is None:
        return CatchupPlan((), latest, 0, False)
    last = as_utc_timestamp(last_processed)
    index = pd.DatetimeIndex(frame.index)
    comparable = index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")
    unseen = list(index[comparable > last])
    if not unseen:
        return CatchupPlan((), None, 0, False)
    step = timeframe_delta(timeframe)
    points = [last, *[as_utc_timestamp(point) for point in unseen]]
    gap = any(b - a > step for a, b in zip(points, points[1:]))
    contiguous = []
    previous = last
    for point in unseen:
        stamp = as_utc_timestamp(point)
        if stamp - previous > step:
            break
        contiguous.append(point)
        previous = stamp
    # 连续历史补齐后才把最后一帧交给正常策略；存在缺口时不得越过缺口。
    historical = contiguous if gap else unseen[:-1]
    replay = tuple(historical[:max_bars])
    pending = max(0, len(unseen) - len(replay) - (0 if gap else 1))
    return CatchupPlan(replay, None if pending or gap else latest, pending, gap)


def replay_state_only(engine, symbol, frame, timestamp):
    """恢复市场状态、路由及策略冷却进度，不调用 broker 或候选分配器。

    冷却进度按已观察到的 bar 推进；此处不重新运行历史持仓管理、撤单或退出。
    """
    location = frame.index.get_loc(timestamp)
    state = engine.state_machine.get_state(frame, location)
    engine.event_processor._last_market_states[symbol] = state
    # 状态切换可启动或结束冷却，但不能触发历史订单副作用。
    cooling = False
    if symbol in engine.router.cooldowns:
        started = engine.router._cooldown_started_at.get(symbol)
        if started is not None:
            last, elapsed = engine.router._cooldown_progress.get(symbol, (started, 0))
            if as_utc_timestamp(timestamp) > as_utc_timestamp(last):
                elapsed += observed_bars_after(frame, location, last)
                engine.router._cooldown_progress[symbol] = (timestamp, elapsed)
            cooling = elapsed <= engine.router.cooldown_bars
        else:
            cooling = location <= engine.router.cooldowns[symbol]
        if not cooling:
            engine.router.cooldowns.pop(symbol, None)
            engine.router._cooldown_started_at.pop(symbol, None)
            engine.router._cooldown_progress.pop(symbol, None)
    previous = engine.router.symbol_states.get(symbol)
    if not cooling and previous is not None and engine.router._map_state_to_strategy(previous) != engine.router._map_state_to_strategy(state):
        engine.router.cooldowns[symbol] = location + engine.router.cooldown_bars
        engine.router._cooldown_started_at[symbol] = timestamp
        engine.router._cooldown_progress[symbol] = (timestamp, 0)
    if not cooling:
        engine.router.symbol_states[symbol] = state
    for strategy in engine.strategies.values():
        context = strategy.context.get(symbol)
        if context:
            strategy._just_entered(location, frame, context)
        trade_state = getattr(strategy, "trade_state", {}).get(symbol, {})
        started = trade_state.get("cooldown_started_at")
        if started is not None:
            last = trade_state.get("cooldown_last_timestamp", started)
            if as_utc_timestamp(timestamp) > as_utc_timestamp(last):
                trade_state["cooldown_bar_count"] = trade_state.get("cooldown_bar_count", 0) + observed_bars_after(frame, location, last)
                trade_state["cooldown_last_timestamp"] = timestamp.isoformat()
            if trade_state.get("cooldown_bar_count", 0) > 24:
                trade_state.pop("cooldown_started_at", None)
                trade_state["cooldown_until"] = -1


def runtime_checkpoint(engine):
    """收集与补帧游标一同持久化的路由、策略上下文及成交消费进度。"""
    values = {"router_runtime": engine.router.checkpoint()}
    for strategy in engine.strategies.values():
        values[f"strategy_runtime:{strategy.name}"] = {
            "context": strategy.context,
            "consumed_close_event_ids": sorted(strategy._consumed_close_event_ids),
            "trade_state": getattr(strategy, "trade_state", None),
        }
    return values
