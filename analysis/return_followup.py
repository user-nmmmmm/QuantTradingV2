"""现货历史重放的因果目标权重与年度选参，不直接执行订单。

本模块返回目标权重和诊断；调用方使用同一个 Broker 模拟账户执行目标，
在实际调仓时计入模型内的切换成本，不拼接候选账户收益冒充连续账户。
历史回放不证明当时已获取这些数据，也不是实盘成交或未来收益证据。
"""
from __future__ import annotations

from dataclasses import replace
import math

import numpy as np
import pandas as pd

from analysis.paper_portfolio import PaperSpec
from analysis.paper_validation import block_bootstrap_indices


def followup_specs():
    base = PaperSpec()
    return [replace(base, name="cash", signal="cash"),
        replace(base, name="equal_weight", signal="equal_weight"),
        replace(base, name="trend_20", lookback=20), base,
        replace(base, name="trend_120", lookback=120),
        replace(base, name="trend_60_no_trade", rebalance="no_trade"),
        replace(base, name="trend_20_no_trade", lookback=20, rebalance="no_trade"),
        replace(base, name="ensemble_equal", lookback=120),
        replace(base, name="ensemble_equal_no_trade", lookback=120, rebalance="no_trade")]


def trend_targets(frames, *, horizons=(20, 60, 120), max_gross=.9, warmup=120):
    """以共同收盘历史生成多周期多头/现金权重，预热期保持现金。

    每个索引时点的目标只使用该时点及之前的收盘价。调用方须在后续开盘
    执行；本函数既不平移执行时间，也不计算成交与费用。
    """
    if not horizons or len(set(horizons)) != len(horizons) or any(type(n) is not int or n < 1 for n in horizons):
        raise ValueError("distinct positive integer horizons required")
    if not math.isfinite(max_gross) or not 0 < max_gross <= 1:
        raise ValueError("cash-only gross cap required")
    closes = pd.DataFrame({s: frame.close for s, frame in sorted(frames.items())})
    closes.index = pd.to_datetime(closes.index, utc=True)
    if (closes.empty or closes.index.has_duplicates or not closes.index.is_monotonic_increasing
            or closes.isna().any().any() or not np.isfinite(closes.to_numpy()).all() or (closes <= 0).any().any()):
        raise ValueError("complete positive common close history required")
    # 对传入周期的多头/现金权重取均值，保留部分仓位；默认是三个周期。
    votes = sum((closes > closes.shift(h)).astype(float) for h in horizons)/len(horizons)
    targets = votes*max_gross/len(closes.columns)
    targets.iloc[:max(warmup, max(horizons))] = 0.
    return targets


def annual_selection_targets(frames, training_returns, *, start="2021-01-01", end="2026-09-19",
                             embargo_days=35, minimum_days=250, rebalance_every=7):
    """按年度固定选择趋势周期，返回收盘目标及每年的信息截止日志。

    首年固定 trend_60；以后只评价上一日历年内、收益可用时间严格早于
    年初减去 embargo_days 的样本。足量且 Sharpe 为正时选最高者，同分按
    登记顺序选择，否则持有现金。输入收益按日线口径在索引次日可用。

    目标归属由下一次观测开盘的年份决定。rebalance_every 仅用于记录首个
    计划执行日，调用方仍须按同一调仓日程执行，不能在年界免费换仓。
    """
    names = ("trend_20", "trend_60", "trend_120")
    if list(training_returns.columns) != list(names):
        raise ValueError("registered ordered horizon family required")
    panel = training_returns.copy()
    panel.index = pd.to_datetime(panel.index, utc=True)
    if (panel.index.has_duplicates or not panel.index.is_monotonic_increasing or panel.isna().any().any()
            or not np.isfinite(panel.to_numpy()).all()):
        raise ValueError("complete chronological training panel required")
    targets_by_name = {name: trend_targets(frames, horizons=(horizon,))
                       for name, horizon in zip(names, (20, 60, 120))}
    first, last = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")
    result = targets_by_name["trend_60"].copy()
    execution_index = result.index[(result.index >= first) & (result.index <= last)
                                  & (np.arange(len(result.index)) > 0)]
    schedule = execution_index[::rebalance_every]
    logs = []
    for year in range(first.year, last.year+1):
        year_start = pd.Timestamp(year=year, month=1, day=1, tz="UTC")
        cutoff = year_start-pd.Timedelta(days=embargo_days)
        train = panel.loc[(panel.index >= year_start-pd.DateOffset(years=1))
                          & (panel.index+pd.Timedelta(days=1) < cutoff)]
        scores = {}
        selected, reason = "trend_60", "predeclared_initial_year"
        if year != first.year:
            for name in names:
                std = float(train[name].std(ddof=1))
                scores[name] = float(train[name].mean()/std*np.sqrt(365.)) if len(train) >= minimum_days and std > 0 else None
            eligible = [n for n in names if scores[n] is not None and scores[n] > 0]
            selected = max(eligible, key=lambda name: scores[name]) if eligible else "cash"
            reason = "prior_year_net_sharpe" if eligible else "nonpositive_or_insufficient_prior_year"
        # A target at prior close is for the next observed open. Existing common
        # weekly schedule can delay a switch; no free Jan 1 liquidation is added.
        next_execution_year = pd.Series(result.index, index=result.index).shift(-1).dt.year
        mask = next_execution_year.eq(year)
        result.loc[mask] = 0. if selected == "cash" else targets_by_name[selected].loc[mask]
        first_scheduled = schedule[schedule.year == year]
        logs.append({"year": year, "selected": selected, "reason": reason,
            "selection_cutoff": cutoff.isoformat() if year != first.year else None,
            "training_observations": len(train) if year != first.year else 0,
            "last_training_return_available_at": (train.index[-1]+pd.Timedelta(days=1)).isoformat() if len(train) and year != first.year else None,
            "scores": scores, "first_scheduled_execution": first_scheduled[0].isoformat() if len(first_scheduled) else None,
            "selected_using_future_year_returns": False})
    return result, logs


def paired_excess_diagnostic(candidate, baseline, *, block_lengths=(5, 20, 60), iterations=1000):
    """对同日期日收益差做循环块重采样，报告年化算术超额均值区间。

    365 日年化是本研究的日线约定；区间不表示累计收益、独立留出集通过
    或未来盈利概率。
    """
    if not candidate.index.equals(baseline.index):
        raise ValueError("paired return dates must be identical")
    difference = candidate.to_numpy()-baseline.to_numpy()
    if not np.isfinite(difference).all():
        raise ValueError("finite paired returns required")
    scenarios = {}
    for block in block_lengths:
        indices = block_bootstrap_indices(len(difference), iterations=iterations, block_length=block, seed=42, method="circular")
        means = difference[indices].mean(axis=1)*365*100
        scenarios[str(block)] = {"annual_arithmetic_excess_pct_ci95": np.quantile(means, [.025, .975]).tolist(),
            "block_length": block, "iterations": iterations}
    return {"annual_arithmetic_excess_pct": float(difference.mean()*365*100), "scenarios": scenarios,
        "scope": "paired dependent historical mean differences; not cumulative-return or future-profit confidence",
        "independent_holdout_passed": False}
