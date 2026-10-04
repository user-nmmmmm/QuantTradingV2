"""Empirical tail diagnostics and transparent joint stress accounting.

Stress outputs are hypothetical scenarios, not a venue liquidation simulator.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict, replace
import math
from typing import Mapping

import numpy as np
import pandas as pd


def empirical_cvar(losses, alpha=.95) -> float:
    """Exact mean of the worst (1-alpha) empirical probability mass.

    Fractional boundary weight avoids changing tail size when N*(1-alpha)
    is not an integer. Inputs are losses (positive means bad).
    """
    values = np.asarray(losses, dtype=float)
    if not 0 <= alpha < 1 or values.ndim != 1 or not len(values) or not np.isfinite(values).all():
        raise ValueError("finite nonempty loss vector and alpha in [0,1) required")
    ordered = np.sort(values)[::-1]
    mass = len(values)*(1-alpha)
    whole = int(math.floor(mass+1e-12))
    fraction = max(0., mass-whole)
    total = float(ordered[:whole].sum())
    if fraction > 1e-12 and whole < len(ordered):
        total += fraction*ordered[whole]
    return total/mass


def risk_diagnostics(equity, returns=None, alpha=.95, initial_capital=None) -> dict:
    values = equity["equity"] if isinstance(equity, pd.DataFrame) else pd.Series(equity)
    array = np.asarray(values, dtype=float)
    if not len(array) or not np.isfinite(array).all() or (array <= 0).any():
        raise ValueError("positive finite equity observations required")
    if initial_capital is None and returns is not None and len(returns):
        first_return = float(np.asarray(returns)[0])
        if math.isfinite(first_return) and first_return > -1:
            initial_capital = array[0]/(1+first_return)
    if initial_capital is not None and (not math.isfinite(initial_capital) or initial_capital <= 0):
        raise ValueError("positive finite initial capital required")
    peaks = np.maximum.accumulate(np.r_[initial_capital, array])[1:] if initial_capital is not None else np.maximum.accumulate(array)
    drawdowns = 1-array/peaks
    durations, duration = [], 0
    for drawdown in drawdowns:
        duration = duration+1 if drawdown > 1e-12 else 0
        durations.append(duration)
    if returns is None:
        returns = pd.Series(array).pct_change(fill_method=None).dropna()
    return_array = np.asarray(returns, dtype=float)
    if not np.isfinite(return_array).all():
        raise ValueError("nonfinite return")
    return {"alpha": float(alpha), "observations": len(array),
        "cvar_loss": empirical_cvar(-return_array, alpha) if len(return_array) else None,
        "max_drawdown": float(drawdowns.max()), "cdar": empirical_cvar(drawdowns, alpha),
        "max_drawdown_duration_bars": int(max(durations)),
        "current_drawdown_duration_bars": int(durations[-1]),
        "drawdown_convention": "positive peak-to-trough fraction; duration in observed bars",
        "tail_estimator": "empirical_fractional_probability_mass; not a future tail guarantee"}


@dataclass(frozen=True)
class StressSpec:
    name: str = "joint_liquidity_margin_shock"
    common_price_shock: float = -.30
    correlation: float = 1.
    spread_bps: float = 50.
    depth_fraction: float = .10
    liquidation_participation: float = .10
    maintenance_margin_rate: float = .05
    margin_multiplier: float = 3.
    commission_rate: float = .001
    impact_coefficient: float = .10
    collateral_haircut: float = .05

    def __post_init__(self):
        if not -1 < self.common_price_shock < 1 or not 0 <= self.correlation <= 1:
            raise ValueError("invalid common shock/correlation")
        if not 0 < self.depth_fraction <= 1 or not 0 < self.liquidation_participation <= 1:
            raise ValueError("invalid depth/participation")
        if not 0 <= self.collateral_haircut < 1:
            raise ValueError("invalid haircut")
        if any(not math.isfinite(x) or x < 0 for x in
               (self.spread_bps, self.maintenance_margin_rate, self.margin_multiplier,
                self.commission_rate, self.impact_coefficient)):
            raise ValueError("invalid scenario cost or margin")


def joint_stress_scenario(positions: Mapping[str, float], prices: Mapping[str, float], cash: float,
                          *, depth_notional: Mapping[str, float], spec: StressSpec | dict | None = None,
                          idiosyncratic_shocks: Mapping[str, float] | None = None) -> dict:
    """Price shock -> capacity-limited exit -> cash/residual equity bridge.

    correlation is a deterministic loading toward a shared shock, not an
    estimated Pearson correlation. Margin is a hypothetical requirement check;
    the spot account is never represented as having a venue margin loan.
    """
    spec = StressSpec(**spec) if isinstance(spec, dict) else spec or StressSpec()
    if not math.isfinite(cash) or cash < 0:
        raise ValueError("nonnegative finite spot cash required")
    rows, cash_after = [], float(cash)
    initial_equity = float(cash)
    price_pnl = fees = slippage = residual_value = gross_after_shock = 0.
    for symbol, quantity in sorted(positions.items()):
        price, depth = float(prices[symbol]), float(depth_notional[symbol])
        if not np.isfinite([quantity, price, depth]).all() or quantity < 0 or price <= 0 or depth < 0:
            raise ValueError("valid long-only quantity, price and quote depth required")
        idio = float((idiosyncratic_shocks or {}).get(symbol, spec.common_price_shock))
        shock = spec.correlation*spec.common_price_shock+(1-spec.correlation)*idio
        if not math.isfinite(shock) or shock <= -1:
            raise ValueError("price shock must leave a positive price")
        stressed_price = price*(1+shock)
        stressed_depth = depth*spec.depth_fraction
        exit_qty = min(quantity, stressed_depth*spec.liquidation_participation/stressed_price)
        participation = exit_qty*stressed_price/stressed_depth if stressed_depth else 0.
        slip_rate = spec.spread_bps/20000+spec.impact_coefficient*participation**1.5
        if slip_rate >= 1:
            raise ValueError("stress slippage would imply nonpositive fill price")
        fill_price = stressed_price*(1-slip_rate)
        fee = exit_qty*fill_price*spec.commission_rate
        proceeds = exit_qty*fill_price-fee
        remaining = quantity-exit_qty
        initial_equity += quantity*price
        price_pnl += quantity*(stressed_price-price)
        gross_after_shock += quantity*stressed_price
        cash_after += proceeds
        residual_value += remaining*stressed_price
        fees += fee
        slippage += exit_qty*(stressed_price-fill_price)
        rows.append({"symbol": symbol, "quantity_before": quantity, "price_before": price,
            "shock": shock, "stressed_price": stressed_price, "stressed_depth_notional": stressed_depth,
            "exit_quantity": exit_qty, "fill_price": fill_price, "fee": fee,
            "cash_proceeds": proceeds, "remaining_quantity": remaining,
            "residual_value": remaining*stressed_price, "capacity_constrained": remaining > 1e-12})
    final_equity = cash_after+residual_value
    margin_before_exit = gross_after_shock*spec.maintenance_margin_rate*spec.margin_multiplier
    margin_after_exit = residual_value*spec.maintenance_margin_rate*spec.margin_multiplier
    usable_collateral = final_equity*(1-spec.collateral_haircut)
    expected = initial_equity+price_pnl-fees-slippage
    return {"spec": asdict(spec), "positions": rows, "initial_equity": initial_equity,
        "price_pnl": price_pnl, "commission": fees, "slippage": slippage,
        "cash_after": cash_after, "residual_inventory_value": residual_value,
        "final_equity": final_equity, "equity_bridge_residual": final_equity-expected,
        "accounting_ok": abs(final_equity-expected) <= max(1e-8, initial_equity*1e-10),
        "hypothetical_margin_before_exit": margin_before_exit,
        "hypothetical_margin_after_exit": margin_after_exit,
        "haircut_collateral": usable_collateral,
        "hypothetical_margin_shortfall": max(0., margin_after_exit-usable_collateral),
        "scope": "hypothetical long-only spot stress; not a venue liquidation/funding engine",
        "correlation_semantics": "deterministic common-shock loading, not a calibrated stochastic correlation"}


def liquidation_stress_path(positions, prices, cash, *, depth_notional, spec=None,
                            max_steps=10, holding_cost_bps_per_step=0.):
    """Apply one shock, then finite capacity-limited liquidation intervals.

    Depth is an explicit scenario assumption replenished once each interval.
    No observed future liquidity, future recovery or guaranteed fill is assumed.
    Paid carrying cost is charged to remaining inventory after each interval.
    """
    if type(max_steps) is not int or max_steps < 1:
        raise ValueError("positive integer liquidation horizon required")
    if not math.isfinite(holding_cost_bps_per_step) or holding_cost_bps_per_step < 0:
        raise ValueError("finite nonnegative scenario carry required")
    spec = StressSpec(**spec) if isinstance(spec, dict) else spec or StressSpec()
    remaining, marks = dict(positions), dict(prices)
    initial = float(cash) + sum(remaining[s] * marks[s] for s in remaining)
    steps, cumulative_fees, cumulative_slip, cumulative_carry, price_pnl = [], 0., 0., 0., 0.
    for step in range(max_steps):
        result = joint_stress_scenario(remaining, marks, cash, depth_notional=depth_notional,
            spec=spec if step == 0 else replace(spec, common_price_shock=0.))
        remaining = {row["symbol"]: row["remaining_quantity"] for row in result["positions"]}
        marks = {row["symbol"]: row["stressed_price"] for row in result["positions"]}
        carry_due = result["residual_inventory_value"] * holding_cost_bps_per_step / 10000
        # A long-only cash account cannot invent borrowing to pay a scenario
        # charge. Insolvency ends this path with an explicit unpaid obligation.
        paid = min(result["cash_after"], carry_due)
        unpaid = carry_due - paid
        cash = result["cash_after"] - paid
        cumulative_fees += result["commission"]
        cumulative_slip += result["slippage"]
        cumulative_carry += carry_due
        price_pnl += result["price_pnl"]
        equity = cash + result["residual_inventory_value"] - unpaid
        residual = equity - (initial + price_pnl - cumulative_fees - cumulative_slip - cumulative_carry)
        row = {"step": step + 1, "cash": cash, "equity": equity,
            "residual_inventory_value": result["residual_inventory_value"],
            "remaining_quantities": dict(remaining), "commission": result["commission"],
            "slippage": result["slippage"], "carrying_cost": carry_due, "unpaid_carrying_cost": unpaid,
            "price_pnl": result["price_pnl"], "equity_bridge_residual": residual,
            "accounting_ok": result["accounting_ok"] and abs(residual) <= max(1e-8, abs(initial) * 1e-10),
            "hypothetical_margin_shortfall": max(0., result["hypothetical_margin_after_exit"] - equity * (1 - spec.collateral_haircut))}
        steps.append(row)
        if unpaid > 0 or all(q <= 1e-12 for q in remaining.values()):
            break
    terminal = steps[-1]
    return {"steps": steps, "initial_equity": initial, "final_equity": terminal["equity"],
        "accounting_ok": all(row["accounting_ok"] for row in steps),
        "fully_liquidated": all(q <= 1e-12 for q in remaining.values()),
        "status": "unpaid_scenario_carry" if terminal["unpaid_carrying_cost"] else "liquidated" if all(q <= 1e-12 for q in remaining.values()) else "residual_inventory_at_horizon",
        "spec": asdict(spec), "max_steps": max_steps,
        "holding_cost_bps_per_step": holding_cost_bps_per_step,
        "depth_semantics": "hypothetical fixed pre-shock quote depth replenished each scenario interval; not measured market depth",
        "shock_semantics": "one initial price shock; subsequent marks fixed; finite liquidation horizon",
        "scope": "long-only cash stress with explicit hypothetical margin and carry; not a venue liquidation engine"}


def stress_inventory_path(equity, frames, *, depth_notional, spec=None,
                          max_liquidation_steps=10, holding_cost_bps_per_step=0.):
    """Stress every observed account inventory, including transient positions.

    ``depth_notional`` is a fixed declared scenario, not historical L2 evidence.
    Each row uses only the contemporaneous position, cash and close mark. The
    worst proportional loss snapshot is then liquidated over a finite horizon.
    """
    if equity.empty or equity.index.has_duplicates or not equity.index.is_monotonic_increasing:
        raise ValueError("ordered unique nonempty account path required")
    spec = StressSpec(**spec) if isinstance(spec, dict) else spec or StressSpec()
    rows, worst = [], None
    for at, account in equity.iterrows():
        positions = {s: float(account["qty_" + s]) for s in frames}
        prices = {s: float(frame.loc[at, "close"]) for s, frame in frames.items()}
        result = joint_stress_scenario(positions, prices, float(account.cash),
            depth_notional=depth_notional, spec=spec)
        if not math.isclose(float(account.equity), result["initial_equity"], rel_tol=1e-9, abs_tol=1e-7):
            raise ValueError("account inventory and contemporaneous marks do not reconcile")
        loss_fraction = 1 - result["final_equity"] / result["initial_equity"] if result["initial_equity"] else 0.
        rows.append({"timestamp": at, "initial_equity": result["initial_equity"],
            "stressed_equity": result["final_equity"], "loss_fraction": loss_fraction,
            "cash_after": result["cash_after"], "residual_inventory_value": result["residual_inventory_value"],
            "commission": result["commission"], "slippage": result["slippage"],
            "hypothetical_margin_shortfall": result["hypothetical_margin_shortfall"],
            "equity_bridge_residual": result["equity_bridge_residual"], "accounting_ok": result["accounting_ok"]})
        if worst is None or loss_fraction > worst["loss_fraction"]:
            worst = {"timestamp": str(at), "loss_fraction": loss_fraction, "snapshot": result,
                "positions": positions, "prices": prices, "cash": float(account.cash)}
    path = pd.DataFrame(rows).set_index("timestamp")
    liquidation = liquidation_stress_path(worst["positions"], worst["prices"], worst["cash"],
        depth_notional=depth_notional, spec=spec, max_steps=max_liquidation_steps,
        holding_cost_bps_per_step=holding_cost_bps_per_step)
    summary = {"scope": "all observed account timestamps plus finite liquidation from worst proportional shock loss",
        "observations": len(path), "start": str(path.index[0]), "end": str(path.index[-1]),
        "worst_timestamp": worst["timestamp"], "worst_loss_fraction": worst["loss_fraction"],
        "accounting_ok": bool(path.accounting_ok.all()) and liquidation["accounting_ok"],
        "max_equity_bridge_residual": float(path.equity_bridge_residual.abs().max()),
        "worst_snapshot": worst["snapshot"], "liquidation_path": liquidation}
    return {"path": path, "summary": summary}
