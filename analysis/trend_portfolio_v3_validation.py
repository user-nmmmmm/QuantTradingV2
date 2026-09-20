"""Predeclared, retrospective V3 evidence; never selects or admits a strategy."""
from __future__ import annotations

import math
from collections import Counter
from typing import Mapping

import numpy as np

from analysis.trend_portfolio_validation import AcceptanceConfig, cohort_statistics, exit_day_cohorts


def gate(value, **evidence):
    return {"status": "insufficient" if value is None else "pass" if value else "fail", **evidence}


def health_reachability(settings, available_symbols):
    """Preflight attainable *input* universe, never silently lower the policy."""
    required = int(settings.get("strategy_health", {}).get("probation_min_distinct_symbols", 3))
    available = sorted(set(available_symbols))
    return {"status": "pass" if len(available) >= required else "unreachable",
            "required_distinct_symbols": required, "available_symbols": available,
            "reason": None if len(available) >= required else "recovery_requires_more_symbols_than_available",
            "scope": "input reachability only; actual recovery also needs profitable mature cohorts"}


def evaluate_primary(primary: Mapping, rows: list[dict], data_evidence: Mapping):
    """Each arm's primary account is one sample; replays do not add cohorts."""
    cohorts = exit_day_cohorts(primary.get("close_event_records", []))
    statistics = cohort_statistics(cohorts, AcceptanceConfig())
    peers = [r for r in rows if r.get("variant") == primary["variant"]
             and r.get("financing_mode") == primary["financing_mode"]]
    count = cohorts["cohort_count"]
    finance_complete = primary.get("financing_gross", 0) == 0
    lower = statistics["pf_confidence_interval"][0]
    gates = {
        "maximum_drawdown_15pct": gate(primary["max_drawdown_pct"] <= 15,
            observed=primary["max_drawdown_pct"], threshold=15),
        "positive_cost_net_return": gate(primary["return_pct"] > 0, observed=primary["return_pct"]),
        "accounting": gate(primary.get("accounting_check", {}).get("ok") is True),
        "minimum_cohorts": gate(count >= 30, observed=count, required=30),
        "cohort_financing_allocation": gate(True if finance_complete else None,
            reason=None if finance_complete else "interest is in account equity, not allocated to individual CloseEvent cohorts"),
        "pf_lower_bound": gate((count >= 30 and (statistics["lower_unbounded"] or lower is not None and lower > 1))
            if finance_complete else None, observed=lower, threshold=1),
        "remove_top_cohorts": gate((count >= 30 and all(
            row["remaining_net_closed_pnl"] >= 0 for row in statistics["concentration"].values()))
            if finance_complete else None, scenarios=statistics["concentration"]),
        "without_time_exit": gate(cohorts["net_closed_pnl_without_max_holding_period"] > 0
            if finance_complete else None, pnl=cohorts["net_closed_pnl_without_max_holding_period"]),
        "full_market_data": gate(True if data_evidence.get("full_market_verified") is True else None,
            evidence=data_evidence),
        "valuation_quality": gate(not primary.get("stale_valuation_count", 0)
            and primary.get("terminal_valuation", {}).get("status") == "ok"),
        "historical_trial_selection_bias": gate(None,
            reason="complete historical trial count unavailable; no final holdout is opened"),
    }
    benchmark = primary.get("benchmark_diagnostic", {})
    excess = benchmark.get("buyhold_risk_matched_return_pct")
    gates["risk_matched_benchmark"] = gate(
        primary["return_pct"] > excess if benchmark.get("risk_match_verified") and excess is not None else None,
        benchmark_id="BTC_ETH_equal_initial_50_50_cost_net_buy_hold", benchmark_return_pct=excess)
    windows = [r for r in peers if r["role"] == "rolling"]
    unique = {(r["start"], r["end"]) for r in windows}
    fraction = sum(r["return_pct"] > 0 for r in windows) / len(windows) if windows else None
    gates["profitable_half_year_windows"] = gate(
        fraction >= 2 / 3 if len(windows) == 13 and len(unique) == 13 else None,
        windows=len(windows), positive_fraction=fraction, required=2 / 3)
    recent = [r for r in peers if r["role"] == "recent"]
    gates["recent_positive_activity"] = gate(
        recent[0]["return_pct"] > 0 and recent[0]["fill_count"] > 0 if len(recent) == 1 else None)
    for multiple in (1.5, 2.):
        costs = [r for r in peers if r["role"] == "cost" and r["cost_multiplier"] == multiple]
        gates[f"cost_{multiple:g}x"] = gate(
            costs[0]["return_pct"] > 0 and costs[0]["max_drawdown_pct"] <= 15 if len(costs) == 1 else None,
            return_pct=costs[0]["return_pct"] if len(costs) == 1 else None)
    neighbors = [r for r in peers if r["role"] == "neighbor"]
    values = [r["return_pct"] for r in neighbors]
    gates["parameter_neighborhood"] = gate(
        primary["return_pct"] > 0 and sum(x > 0 for x in values) >= 3
        and float(np.median(values)) >= .5 * primary["return_pct"] if len(values) == 4 else None,
        returns_pct=values)
    states = [g["status"] for g in gates.values()]
    return {"variant": primary["variant"], "financing_mode": primary["financing_mode"],
        "status": "failed" if "fail" in states else "insufficient" if "insufficient" in states else "passed_research_only",
        "gates": gates, "cohorts": cohorts, "statistics": statistics,
        "unseen_oos": False, "live_admission": False}


def performance_summary(equity, initial_capital):
    values = np.asarray(equity, dtype=float)
    if not len(values) or not np.isfinite(values).all() or initial_capital <= 0:
        raise ValueError("finite nonempty equity and positive capital required")
    seeded = np.r_[initial_capital, values]
    high = np.maximum.accumulate(seeded)
    daily = np.diff(seeded) / seeded[:-1]
    vol = float(np.std(daily, ddof=1) * math.sqrt(365)) if len(daily) > 1 else 0.
    return {"initial_capital": initial_capital, "final_equity": float(values[-1]),
        "return_pct": float((values[-1] / initial_capital - 1) * 100),
        "max_drawdown_pct": float(np.max(1 - seeded / high) * 100),
        "annualized_volatility": vol, "daily_returns": daily.tolist()}


def execution_diagnostics(result):
    """Describe actual facts, keeping financing and execution costs separate."""
    audit = (result.get("portfolio_controller") or {}).get("audit", [])
    fills = result.get("trades", [])
    commission = sum(float(row.get("commission", 0.) or 0.) for row in fills)
    slippage = sum(abs(float(row.get("slip", 0.) or 0.)) * abs(float(row.get("qty", 0.))) for row in fills)
    ordinary = sum(abs(float(row["qty"]) * float(row["fill_price"])) for row in fills
                   if row.get("exit_reason") == "v3_rebalance")
    forced = sum(abs(float(row["qty"]) * float(row["fill_price"])) for row in fills
                 if row.get("exit_reason") != "v3_rebalance")
    held = [row["position_count"] for row in audit if "position_count" in row]
    leverage = [row["gross_weight"] for row in audit if "gross_weight" in row]
    selection_reasons, sizing_rules, incomplete_reasons = Counter(), Counter(), Counter()
    for day in audit:
        snapshot = day.get("weekly_snapshot") or {}
        for row in snapshot.get("selection_rows", {}).values():
            selection_reasons.update(row.get("reasons", []))
        sizing = snapshot.get("sizing") or {}
        for reasons in sizing.get("binding_constraints", {}).values():
            sizing_rules.update(reasons)
        unresolved = day.get("unfilled_reasons", day.get("uncompleted_targets", {}))
        if isinstance(unresolved, dict):
            for reasons in unresolved.values():
                incomplete_reasons.update(reasons if isinstance(reasons, list) else [str(reasons)])
        for detail in day.get("symbol_decisions", {}).values():
            incomplete_reasons.update(detail.get("reasons", []))
        if day.get("buy_scale", 1.) < 1:
            sizing_rules.update(["shared_funding_or_risk_budget"])
        if day.get("turnover_scale", 1.) < 1:
            sizing_rules.update(["weekly_turnover"])
    borrow = Counter(row.get("borrow_status", "unreported") for row in audit)
    return {"commission": commission, "modeled_price_slippage": slippage,
        "ordinary_filled_notional": ordinary, "forced_exit_filled_notional": forced,
        "maximum_position_count": max(held, default=0),
        "mean_position_count": float(np.mean(held)) if held else 0.,
        "maximum_gross_leverage": max(leverage, default=0.),
        "mean_gross_leverage": float(np.mean(leverage)) if leverage else 0.,
        "selection_rejection_counts": dict(selection_reasons), "limiting_rules": dict(sizing_rules),
        "unfilled_reasons": dict(incomplete_reasons), "financing_status_days": dict(borrow),
        "financing_verified_day_fraction": (sum(count for status, count in borrow.items()
            if status.startswith("verified")) / sum(borrow.values()) if borrow else 0.),
        "strategy_health": result.get("strategy_health", {}), "lifecycle": result.get("lifecycle", {}),
        "account_cost_contract": result.get("account_cost_contract", {})}
