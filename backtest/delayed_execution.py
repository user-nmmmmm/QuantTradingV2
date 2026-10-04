"""按决策可用时间回放现货市价单的独立研究工具。

调用方预先声明决策数量、参考价和 available_at；这里只选择该标的在
available_at 当时或之后实际存在的执行 bar 开盘，不用未来价格生成决策。
本工具不改变 BacktestEngine 对 decision_delay_seconds 必须为零的限制。
"""
from __future__ import annotations

from copy import deepcopy
import math

import pandas as pd

from core.broker import Broker
from core.domain import OrderIntent
from core.portfolio import Portfolio
from core.signal_observation_types import fingerprint, iso
from core.timeframes import as_utc_timestamp, timeframe_delta


def replay_delayed_decisions(frames, decisions, *, initial_capital=10000., execution_timeframe="1h",
                             commission_rate=.001, slippage=.0005, spread_bps=2., max_participation_rate=.05):
    """复制输入后回放 buy/sell 决策，并返回成交、成本和时序审计。

    frames 的索引表示执行 bar 的开盘时间；available_at 不得早于信号 bar
    收盘。每条决策仅提交一次 IOC 市价单，未成交余量不会移到下一根 bar。
    unexecuted_decisions 只列出尚无可执行 bar、因而尚未提交的决策；已提交
    但未成交的结果需查看 order_statuses 和 execution_audit。

    OHLCV、参与率和成本属于回放假设，不构成历史发布时点或真实场所校准
    证据。限价单、止损单、融资计提和实盘下单均不在本函数范围内。
    """
    for value in (initial_capital, commission_rate, slippage, spread_bps, max_participation_rate):
        if isinstance(value, bool) or not math.isfinite(value) or value < 0:
            raise ValueError("finite nonnegative account and cost inputs required")
    if initial_capital <= 0 or not 0 < max_participation_rate <= 1:
        raise ValueError("positive capital and a valid participation fraction required")
    delta = pd.Timedelta(timeframe_delta(execution_timeframe))
    normalized = {}
    for symbol, raw in frames.items():
        frame = raw.copy(deep=True)
        frame.attrs = {}
        frame.index = pd.to_datetime(frame.index, utc=True)
        if frame.index.has_duplicates or frame.index.hasnans or not frame.index.is_monotonic_increasing:
            raise ValueError("execution bars must be unique and sorted")
        if any(at.value % delta.value for at in frame.index):
            raise ValueError("execution bars must lie on the declared grid")
        for _, bar in frame.iterrows():
            values = [float(bar[name]) for name in ("open", "high", "low", "close", "volume")]
            if (not all(math.isfinite(v) for v in values) or min(values[:4]) <= 0 or values[4] < 0
                    or values[1] < max(values[0], values[2], values[3])
                    or values[2] > min(values[0], values[1], values[3])):
                raise ValueError("invalid execution OHLCV")
        frame.index = frame.index.tz_localize(None)
        normalized[symbol] = frame
    queue, seen = [], set()
    for raw in decisions:
        row = deepcopy(raw)
        if not isinstance(row.get("decision_id"), str) or not row["decision_id"] or row["decision_id"] in seen:
            raise ValueError("unique decision identities required")
        seen.add(row["decision_id"])
        if row.get("side") not in {"buy", "sell"} or row.get("order_type", "market") != "market":
            raise ValueError("only spot market buy/sell decisions are supported")
        if row["symbol"] not in normalized:
            raise ValueError("declared execution data required for every decision symbol")
        signal = as_utc_timestamp(row["signal_time"])
        available = as_utc_timestamp(row["available_at"])
        if pd.isna(signal) or pd.isna(available) or available < signal+timeframe_delta(row["signal_timeframe"]):
            raise ValueError("decision cannot precede its signal bar close")
        for key in ("quantity", "reference_price"):
            if isinstance(row[key], bool) or not math.isfinite(float(row[key])) or float(row[key]) <= 0:
                raise ValueError("decision quantity and reference price must be known positive values")
        row["available_at"], row["signal_time"] = iso(available), iso(signal)
        queue.append(row)
    queue.sort(key=lambda row: (as_utc_timestamp(row["available_at"]), row["decision_id"]))
    portfolio = Portfolio(initial_capital, account_mode="spot")
    broker = Broker(portfolio, commission_rate=commission_rate, slippage=slippage,
        spread_bps=spread_bps, max_participation_rate=max_participation_rate, random_slip=False,
        exchange_id="binance", account_id="delayed-research", timeframe=execution_timeframe)
    timeline = sorted(set().union(*(set(frame.index) for frame in normalized.values())))
    pending, rows, marks = list(queue), [], {}
    for at in timeline:
        bars = {symbol: frame.loc[at] for symbol, frame in normalized.items() if at in frame.index}
        now = as_utc_timestamp(at)
        # 全市场时间轴出现新 bar 不代表本标的可成交；缺 bar 时等到真实开盘。
        eligible = [row for row in pending if as_utc_timestamp(row["available_at"]) <= now and row["symbol"] in bars]
        for row in eligible:
            available = as_utc_timestamp(row["available_at"]).tz_localize(None)
            intent = OrderIntent(exchange="binance", account="delayed-research", symbol=row["symbol"],
                timeframe=row["signal_timeframe"], bar_time=row["signal_time"], strategy_id="DelayedResearch",
                action=row["side"], sequence=len(rows), requested_qty=float(row["quantity"]),
                order_type="market", reference_price=float(row["reference_price"]), price=float(row["reference_price"]),
                created_at=row["available_at"], signal_id=row["decision_id"], time_in_force="IOC")
            # 显式匹配时钟允许在可用时点对应的开盘撮合；普通订单仍遵循下一 bar 规则。
            order = broker.submit_order(row["symbol"], row["side"], float(row["quantity"]),
                price=float(row["reference_price"]), timestamp=available, match_not_before=available,
                strategy_id="DelayedResearch", time_in_force="IOC", _intent=intent)
            rows.append({"decision_id": row["decision_id"], "order_id": order.id,
                "signal_time": row["signal_time"], "decision_available_at": row["available_at"],
                "first_executable_open": iso(at), "reference_price": row["reference_price"]})
            pending.remove(row)
        broker.process_orders(bars)
        marks.update({symbol: float(bar["close"]) for symbol, bar in bars.items()})
    orders = {row["order_id"]: row for row in rows}
    temporal_ok = all(as_utc_timestamp(fill["fill_time"]) >= as_utc_timestamp(
        orders[fill["order_id"]]["decision_available_at"]) for fill in broker.trades)
    equity = portfolio.get_equity(marks)
    out = {"schema": "delayed-market-replay/v1", "scope": "independent_spot_market_execution_diagnostic",
        "decisions_sha256": fingerprint(queue), "execution_timeframe": execution_timeframe,
        "initial_capital": initial_capital, "final_equity": equity, "cash": portfolio.cash,
        "orders": rows, "fills": deepcopy(broker.trades), "execution_audit": deepcopy(broker.execution_audit),
        "unexecuted_decisions": [row["decision_id"] for row in pending],
        "positions": deepcopy(portfolio.positions), "no_fill_before_decision": temporal_ok,
        "order_statuses": {row["order_id"]: broker.orders_by_id[row["order_id"]].status.value for row in rows},
        "cost_policy": {"commission_rate": commission_rate, "slippage": slippage, "spread_bps": spread_bps,
            "max_participation_rate": max_participation_rate},
        "intrabar_order_path_inferred": False, "historical_point_in_time_certified": False,
        "real_venue_calibration": False, "live_orders_submitted": 0}
    if not temporal_ok:
        raise AssertionError("a delayed order filled before its decision")
    return out
