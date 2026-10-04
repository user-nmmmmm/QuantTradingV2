"""Episodic research on the original historical execution engine.

This adapter replays a complete episode rather than implementing another
broker or a Gym ``step`` loop.  A selector receives the engine's causal
candidate hook; its learning update happens after the episode is complete.
Configuration is process-global in the existing engine, so episodes in one
process must run sequentially (parallel workers need separate processes).
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

from backtest.engine import BacktestEngine
from backtest.reporting.operating_periods import split_execution_records
from config.config import config


REWARD_COLUMNS = (
    "equity", "net_log_return", "drawdown", "drawdown_penalty", "turnover",
    "turnover_penalty", "reward",
)


def _positive_finite(value: Any, name: str) -> float:
    number = float(value)
    if not np.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be finite and strictly positive")
    return number


def _penalty(value: Any, name: str) -> float:
    number = float(value)
    if not np.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be finite and nonnegative")
    return number


def _trade_records(trades: Iterable[Mapping[str, Any]] | pd.DataFrame | None) -> list[dict]:
    if trades is None:
        return []
    if isinstance(trades, pd.DataFrame):
        return trades.to_dict("records")
    return [dict(row) for row in trades]


def _actual_trades(trades, terminal_policy: str) -> list[dict]:
    if terminal_policy not in {"mark_to_market", "forced_liquidation", "valuation_only"}:
        raise ValueError("unsupported terminal policy")
    actual, _ = split_execution_records(
        _trade_records(trades), mark_to_market=terminal_policy == "mark_to_market",
    )
    return actual


def equity_rewards(
    equity_curve: pd.DataFrame,
    trades,
    *,
    initial_capital: float,
    drawdown_penalty: float = 0.5,
    turnover_penalty: float = 0.0,
    terminal_policy: str = "mark_to_market",
) -> pd.DataFrame:
    """Reward net account equity; never subtract fees a second time.

    The initial high-water mark is ``initial_capital``.  Drawdown is a
    nonnegative fraction and only its increase receives an extra penalty.
    Turnover is actual filled notional divided by the sampled net equity;
    partial fills count individually and unfilled targets count zero.
    Engine timestamps are bar OPEN timestamps and are preserved verbatim.
    A terminal settlement at +1 microsecond remains a separate reward row.
    Intrabar fills, if supplied, belong to the first sample at/after the fill.
    """
    capital = _positive_finite(initial_capital, "initial_capital")
    dd_weight = _penalty(drawdown_penalty, "drawdown_penalty")
    turnover_weight = _penalty(turnover_penalty, "turnover_penalty")
    actual = _actual_trades(trades, terminal_policy)
    if equity_curve.empty:
        if actual:
            raise ValueError("actual fills require an equity observation")
        return pd.DataFrame(index=equity_curve.index.copy(), columns=REWARD_COLUMNS, dtype=float)
    if "equity" not in equity_curve:
        raise ValueError("equity_curve must contain equity")
    stamps = pd.DatetimeIndex(pd.to_datetime(equity_curve.index, utc=True))
    if stamps.hasnans or not stamps.is_monotonic_increasing or stamps.has_duplicates:
        raise ValueError("equity observations require unique ordered timestamps")
    equity = equity_curve["equity"].to_numpy(dtype=float)
    if not np.isfinite(equity).all() or np.any(equity <= 0):
        raise ValueError("log rewards require finite strictly positive equity")
    previous = np.concatenate(([capital], equity[:-1]))
    net_return = np.log(equity / previous)
    high_water = np.maximum.accumulate(np.concatenate(([capital], equity)))[1:]
    drawdown = np.maximum(0.0, 1.0 - equity / high_water)
    drawdown_change = np.maximum(0.0, np.diff(np.concatenate(([0.0], drawdown))))
    filled_notional = np.zeros(len(equity), dtype=float)
    for trade in actual:
        quantity = _positive_finite(trade["qty"], "filled qty")
        price = _positive_finite(trade["fill_price"], "fill_price")
        fill = pd.Timestamp(trade["fill_time"])
        if pd.isna(fill):
            raise ValueError("actual fills require fill_time")
        fill = fill.tz_localize("UTC") if fill.tzinfo is None else fill.tz_convert("UTC")
        position = int(stamps.searchsorted(fill, side="left"))
        if position == len(stamps):
            raise ValueError("actual fill occurs after the last equity observation")
        filled_notional[position] += quantity * price
    turnover = filled_notional / equity
    dd_cost = dd_weight * drawdown_change
    turnover_cost = turnover_weight * turnover
    return pd.DataFrame({
        "equity": equity,
        "net_log_return": net_return,
        "drawdown": drawdown,
        "drawdown_penalty": dd_cost,
        "turnover": turnover,
        "turnover_penalty": turnover_cost,
        "reward": net_return - dd_cost - turnover_cost,
    }, index=equity_curve.index.copy())


def discounted_returns(rewards, gamma: float = 0.99) -> np.ndarray:
    """Return-to-go for rewards in chronological order, without normalization."""
    discount = float(gamma)
    if not np.isfinite(discount) or not 0 <= discount <= 1:
        raise ValueError("gamma must be in [0, 1]")
    if isinstance(rewards, pd.DataFrame):
        rewards = rewards["reward"]
    values = np.asarray(rewards, dtype=float)
    if values.ndim != 1 or not np.isfinite(values).all():
        raise ValueError("rewards must be a finite one-dimensional sequence")
    output = np.empty_like(values)
    future = 0.0
    for index in range(len(values) - 1, -1, -1):
        future = float(values[index]) + discount * future
        output[index] = future
    return output


def _evaluated_curve(result: Mapping[str, Any]) -> pd.DataFrame:
    """Exclude the existing engine's presentation-only frozen risk tail."""
    curve = result["equity_curve"]
    termination = (result.get("lifecycle") or {}).get("termination_timestamp")
    if curve.empty or termination is None:
        return curve.copy()
    stamps = pd.DatetimeIndex(pd.to_datetime(curve.index, utc=True))
    cutoff = pd.Timestamp(termination)
    cutoff = cutoff.tz_localize("UTC") if cutoff.tzinfo is None else cutoff.tz_convert("UTC")
    # Preserve an actual terminal settlement if the engine adds one.  Other
    # later rows are frozen presentation marks, including unresolved books.
    settled = [pd.Timestamp(row["fill_time"]) for row in _trade_records(result.get("trades"))
               if row.get("exit_reason") == "EndOfBacktest"]
    terminal_stamps = pd.DatetimeIndex(pd.to_datetime(settled, utc=True))
    return curve.loc[(stamps <= cutoff) | stamps.isin(terminal_stamps)].copy()


def episode_summary(result: Mapping[str, Any], initial_capital: float) -> dict[str, Any]:
    """Economic and lifecycle facts; no synthetic continuation after a halt."""
    capital = _positive_finite(initial_capital, "initial_capital")
    curve = _evaluated_curve(result)
    lifecycle = result.get("lifecycle") or {}
    valuation = result.get("terminal_valuation") or {}
    terminal_policy = result.get("terminal_policy", valuation.get("policy", "mark_to_market"))
    actual = _actual_trades(result.get("trades"), terminal_policy)
    ledger = equity_rewards(curve, actual, initial_capital=capital, terminal_policy=terminal_policy)
    final_equity = float(curve["equity"].iloc[-1]) if len(curve) else capital
    # The +1us synthetic settlement is not another day of market exposure.
    active_end = lifecycle.get("active_end")
    exposure_curve = curve
    if active_end is not None and not curve.empty:
        stamps = pd.DatetimeIndex(pd.to_datetime(curve.index, utc=True))
        end = pd.Timestamp(active_end)
        end = end.tz_localize("UTC") if end.tzinfo is None else end.tz_convert("UTC")
        exposure_curve = curve.loc[stamps <= end]
    exposure = (float(exposure_curve["gross_exposure_pct_equity"].mean())
                if len(exposure_curve) and "gross_exposure_pct_equity" in exposure_curve else 0.0)
    unresolved = deepcopy(lifecycle.get("unresolved_risk_positions") or {})
    if valuation.get("positions"):
        unresolved.update({row["symbol"]: row for row in valuation["positions"]})
    termination = lifecycle.get("termination_timestamp")
    return {
        "initial_capital": capital,
        "final_equity": final_equity,
        "net_return": final_equity / capital - 1.0,
        "return_pct": (final_equity / capital - 1.0) * 100.0,
        "max_drawdown": float(ledger["drawdown"].max()) if len(ledger) else 0.0,
        "actual_fill_count": len(actual),
        "fill_count": len(actual),
        "mean_gross_exposure_pct_equity": exposure,
        "exposure": exposure,
        "turnover": float(ledger["turnover"].sum()),
        "commission": sum(float(row.get("commission", 0.0) or 0.0) for row in actual),
        "net_log_return_sum": float(ledger["net_log_return"].sum()),
        "reward_sum": float(ledger["reward"].sum()),
        "evaluated_equity_rows": len(curve),
        "excluded_frozen_tail_rows": len(result["equity_curve"]) - len(curve),
        "status": lifecycle.get("status", "unknown"),
        "operating_status": lifecycle.get("operating_status"),
        "terminated_by_risk": termination is not None,
        "termination_timestamp": termination.isoformat() if isinstance(termination, pd.Timestamp) else termination,
        "termination_reason": lifecycle.get("termination_reason"),
        "risk_halt_started_at": lifecycle.get("risk_halt_started_at"),
        "unresolved_positions": unresolved,
        "pending_orders": deepcopy(valuation.get("pending_orders") or []),
        "terminal_policy": terminal_policy,
        "terminal_valuation_status": valuation.get("status"),
        "accounting_ok": (result.get("accounting_check") or {}).get("ok"),
    }


def _merge(base: dict, overrides: Mapping[str, Any]) -> dict:
    for key, value in overrides.items():
        if isinstance(value, Mapping) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = deepcopy(value)
    return base


@dataclass
class Episode:
    result: dict[str, Any]
    engine: BacktestEngine
    rewards: pd.DataFrame
    summary: dict[str, Any]
    reward_weights: dict[str, float] | None = None


class FullEngineEnvironment:
    """Daily spot episodes with fresh original-engine state on every replay."""

    def __init__(
        self,
        frames: Mapping[str, pd.DataFrame],
        *,
        engine_options: Mapping[str, Any] | None = None,
        parameters: Mapping[str, Any] | None = None,
        strategies: Mapping[str, Any] | None = None,
        drawdown_penalty: float = 0.5,
        turnover_penalty: float = 0.0,
        opening_delay_bars: int = 0,
    ) -> None:
        self.frames = {symbol: frame.copy(deep=True) for symbol, frame in frames.items()}
        self.engine_options = deepcopy(dict(engine_options or {}))
        self.parameters = _merge(deepcopy(config._config), parameters or {})
        self.strategies = deepcopy(dict(strategies)) if strategies is not None else None
        self.drawdown_penalty = _penalty(drawdown_penalty, "drawdown_penalty")
        self.turnover_penalty = _penalty(turnover_penalty, "turnover_penalty")
        if type(opening_delay_bars) is not int or opening_delay_bars < 0:
            raise ValueError("opening_delay_bars must be a nonnegative integer")
        self.opening_delay_bars = opening_delay_bars
        timeframe = self.engine_options.get("timeframe") or self.parameters["data"]["timeframe"]
        if timeframe != "1d":
            raise ValueError("ML selection environment currently requires timeframe='1d'")
        account_mode = self.engine_options.get("account_mode") or self.parameters["account"]["mode"]
        if account_mode != "spot":
            raise ValueError("ML selection environment currently requires account_mode='spot'")
        if "candidate_selector" in self.engine_options:
            raise ValueError("pass the selector to run_episode, not engine_options")
        if self.engine_options.get("portfolio_controller") is not None:
            raise ValueError("portfolio_controller bypasses candidate selection and is unsupported here")
        if self.parameters.get("portfolio_targets", {}).get("enabled", False):
            raise ValueError("portfolio_targets bypass candidate selection and are unsupported here")

    def run_episode(self, selector=None) -> Episode:
        previous = config._config
        try:
            config._config = deepcopy(self.parameters)
            options = deepcopy(self.engine_options)
            options["calculate_benchmarks"] = False
            options["candidate_selector"] = selector
            engine = BacktestEngine(**options)
            self.active_engine = engine
            from research.ml_selection.execution_stress import opening_delay
            with opening_delay(self.opening_delay_bars):
                result = engine.run(
                    self.frames,
                    strategies=deepcopy(self.strategies),
                    routing_log_enabled=False,
                )
            result["terminal_policy"] = engine.terminal_policy
            rewards = equity_rewards(
                _evaluated_curve(result), result.get("trades"),
                initial_capital=engine.initial_capital,
                drawdown_penalty=self.drawdown_penalty,
                turnover_penalty=self.turnover_penalty,
                terminal_policy=engine.terminal_policy,
            )
            summary = episode_summary(result, engine.initial_capital)
            summary["reward_sum"] = float(rewards["reward"].sum())
            summary["drawdown_penalty_sum"] = float(rewards["drawdown_penalty"].sum())
            summary["turnover_penalty_sum"] = float(rewards["turnover_penalty"].sum())
            return Episode(result=result, engine=engine, rewards=rewards, summary=summary,
                           reward_weights={"drawdown_penalty": self.drawdown_penalty,
                                           "turnover_penalty": self.turnover_penalty})
        finally:
            config._config = previous
