"""Plan selector ablations and compare already completed paired accounts.

Generating a plan does not execute a model, backtest, download, or training job.
Economic comparison accepts explicit complete account ledgers; incomplete runs,
unfilled positions, absent calendars, and mismatched contracts remain unknown.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import math

import numpy as np
import pandas as pd

from core.reproducibility import canonical_json


CONTROL_FIELDS = ("input_hashes", "symbols", "evaluation_start", "evaluation_end_exclusive",
                  "timeframe", "account_mode", "initial_capital", "initial_state",
                  "execution", "exit", "risk", "allocator", "candidate_domain")


def plan_comparison(controls, *, frozen_candidate=None, readiness=None, seed=42):
    """Freeze a two-factor (selector operation x capital score) comparison plan.

    ``selector_kwargs`` is an executable ResearchSelector constructor contract.
    ``capital_score_source`` controls the score used for position sizing. Each
    arm must replay an independent original-engine account, never reuse fills
    from another arm. The off and native eligibility baselines have no score
    treatment, so redundant selector-score copies are intentionally omitted.
    """
    if not isinstance(controls, dict) or type(seed) is not int or seed < 0:
        raise ValueError("controls must be a mapping and seed a nonnegative integer")
    missing = [name for name in CONTROL_FIELDS if controls.get(name) is None]
    operations = [
        ("off", False, False, False, False, "native"),
        ("eligibility_only", True, False, False, False, "native"),
        ("ranking_only", True, True, False, False, "model"),
        ("return_gate_only", True, False, True, False, "native"),
        ("rl_gate_only", True, False, False, True, "native"),
        ("combined", True, True, True, True, "model"),
        ("momentum_ranking", True, True, False, False, "momentum"),
        ("random_ranking", True, True, False, False, "random"),
    ]
    arms = []
    for name, eligibility, ranking, return_gate, policy_gate, ranking_source in operations:
        weight_sources = ("native", "selector") if ranking else ("native",)
        for source in weight_sources:
            mode = ("policy" if policy_gate else "model" if return_gate or ranking_source == "model"
                    else ranking_source if ranking_source in {"momentum", "random"} else "qualified_native")
            flags = {"eligibility_filter": eligibility, "ranking": ranking,
                     "return_gate": return_gate, "policy_gate": policy_gate}
            arms.append({"arm_id": name + ("_selector_capital" if source == "selector" else ""),
                         "selector_enabled": name != "off", "ranking_source": ranking_source,
                         "selector_kwargs": ({"mode": mode, "selector_contract": flags,
                             "capital_score_source": "selector_score" if source == "selector" else "original_score"}
                             if name != "off" else None),
                         "seed": seed, "status": "planned_not_executed"})
    pending = list(missing)
    if frozen_candidate is None:
        pending.append("frozen_candidate_identity")
    if readiness is None:
        pending.append("verified_input_readiness")
    elif readiness.get("status") != "ready":
        pending.extend(readiness.get("pending_reasons", []))
        pending.extend(readiness.get("failed_reasons", []))
    payload = {"schema": "ml-selection-comparison-plan/v1", "controls": deepcopy(controls),
               "frozen_candidate": deepcopy(frozen_candidate), "arms": arms,
               "status": "pending_evidence" if pending else "registered_plan_only",
               "pending_requirements": sorted(set(pending)),
               "shared_qualification": "all enabled arms share identical causal history/liquidity/PIT eligibility; off is original strategy",
               "estimands": [
                   {"left": "off", "right": "eligibility_only", "effect": "qualification_filter"},
                   {"left": "eligibility_only", "right": "ranking_only", "effect": "ranking_with_native_position_sizing"},
                   {"left": "eligibility_only", "right": "return_gate_only", "effect": "expected_return_gate"},
                   {"left": "eligibility_only", "right": "rl_gate_only", "effect": "independent_policy_gate"},
                   {"left": "eligibility_only", "right": "combined", "effect": "gate_intersection_and_ranking"},
                   {"left": "ranking_only", "right": "ranking_only_selector_capital", "effect": "score_to_capital_weight"},
                   {"left": "combined", "right": "combined_selector_capital", "effect": "score_to_capital_weight_with_gates"}],
               "metrics": ["net_return", "max_drawdown", "gross_exposure", "fills", "orders",
                           "candidate_gate_approval_fill_funnel", "commissions", "turnover",
                           "terminal_inventory", "accounting", "end_to_end_timing", "stage_timing"],
               "execution_contract": {"engine": "original_backtest_engine_independent_account_per_arm",
                   "data": "identical_verified_bytes_and_roster", "exits": "original_strategy_exits_unchanged",
                   "costs_and_risk": "identical_engine_cost_risk_health_and_terminal_policy",
                   "cash": "legal_low_activity_result_not_automatic_success",
                   "failed_or_incomplete_accounts": "unknown_economics_never_zero_filled",
                   "final_holdout": "unopened_not_run_by_this_plan",
                   "thresholds": "frozen_candidate_values_no_test_window_tuning"},
               "training_performed": False, "backtest_performed": False, "formal_admission": False}
    payload["plan_id"] = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
    return payload


def _number(value, *, positive=False):
    if isinstance(value, bool):
        raise ValueError("boolean is not a numeric ledger value")
    number = float(value)
    if not math.isfinite(number) or positive and number <= 0:
        raise ValueError("ledger value must be finite" + (" and positive" if positive else ""))
    return number


def _account_metrics(ledger):
    issues = []
    if ledger.get("schema") != "ml-selection-account-ledger/v1":
        issues.append("unsupported_account_ledger_schema")
    if ledger.get("status") != "completed" or ledger.get("accounting_ok") is not True:
        issues.append("account_incomplete_or_accounting_unverified")
    if ledger.get("terminal_status") != "completed" or ledger.get("open_inventory") != []:
        issues.append("terminal_execution_or_inventory_unresolved")
    contract = ledger.get("contract", {})
    missing = [key for key in CONTROL_FIELDS if contract.get(key) is None]
    issues.extend("missing_control:" + name for name in missing)
    if issues:
        return None, None, issues
    try:
        capital = _number(contract["initial_capital"], positive=True)
        curve = pd.DataFrame(ledger.get("equity", []))
        if curve.empty or not {"timestamp", "equity"} <= set(curve):
            raise ValueError("equity_calendar_missing")
        times = pd.DatetimeIndex(pd.to_datetime(curve.timestamp, utc=True))
        if times.hasnans or times.has_duplicates or not times.is_monotonic_increasing:
            raise ValueError("equity_calendar_invalid")
        start = pd.to_datetime(contract["evaluation_start"], utc=True)
        end = pd.to_datetime(contract["evaluation_end_exclusive"], utc=True)
        if contract["timeframe"] != "1d" or start != start.normalize() or end != end.normalize() or end <= start:
            raise ValueError("comparison_requires_daily_utc_contract")
        expected = pd.date_range(start, end, freq="D", inclusive="left")
        if not times.equals(expected):
            raise ValueError("equity_calendar_not_complete_registered_period")
        equity = np.asarray([_number(value, positive=True) for value in curve.equity])
        if not isinstance(ledger.get("fills"), list):
            raise ValueError("authoritative_fill_ledger_missing")
        fills = ledger["fills"]
        for fill in fills:
            timestamp = pd.to_datetime(fill["timestamp"], utc=True)
            if pd.isna(timestamp) or not start <= timestamp < end:
                raise ValueError("fill_timestamp_outside_contract")
            _number(fill["qty"], positive=True)
            _number(fill["price"], positive=True)
        values = np.concatenate(([capital], equity))
        peak = np.maximum.accumulate(values)
        metrics = {"net_return": float(equity[-1] / capital - 1), "final_equity": float(equity[-1]),
                   "max_drawdown": float(np.max(1 - values / peak)), "fills": len(fills),
                   "activity_class": "no_actual_fills" if not fills else "actual_fills_observed",
                   "commission": (sum(_number(fill["commission"]) for fill in fills)
                       if all(fill.get("commission") is not None for fill in fills) else None),
                   "filled_notional": sum(_number(fill["qty"]) * _number(fill["price"]) for fill in fills),
                   "gross_exposure": (float(np.mean([_number(value) for value in curve.gross_exposure_pct_equity]))
                       if "gross_exposure_pct_equity" in curve and curve.gross_exposure_pct_equity.notna().all() else None),
                   "orders": len(ledger["orders"]) if isinstance(ledger.get("orders"), list) else None,
                   "end_to_end_seconds": (_number(ledger["timing"]["end_to_end_seconds"])
                       if isinstance(ledger.get("timing"), dict) and ledger["timing"].get("end_to_end_seconds") is not None else None)}
    except (TypeError, ValueError, KeyError, OverflowError) as exc:
        return None, None, [str(exc)]
    return metrics, times, []


def compare_account_ledgers(left, right):
    """Compare matched, already completed account receipts without replaying.

    Required receipt: schema/status/accounting_ok/terminal_status/open_inventory,
    full ``contract`` (CONTROL_FIELDS), daily ``equity`` records, and actual
    ``fills`` records (timestamp/qty/price; commission optional). Zero fills in a
    verified completed cash account are observed; missing/failed accounts have
    null economics. Selector flags and score treatments belong outside contract.
    """
    if not isinstance(left, dict) or not isinstance(right, dict):
        raise ValueError("account ledgers must be mappings")
    left_metrics, left_times, left_issues = _account_metrics(left)
    right_metrics, right_times, right_issues = _account_metrics(right)
    issues = [{"arm": "left", "reason": reason} for reason in left_issues]
    issues += [{"arm": "right", "reason": reason} for reason in right_issues]
    mismatch = [key for key in CONTROL_FIELDS
                if canonical_json(left.get("contract", {}).get(key)) != canonical_json(right.get("contract", {}).get(key))]
    issues.extend({"arm": "paired", "reason": "control_mismatch:" + key} for key in mismatch)
    if not issues and not left_times.equals(right_times):
        issues.append({"arm": "paired", "reason": "equity_calendars_differ"})
    matched = not issues
    differences = ({key: (right_metrics[key] - left_metrics[key]
                          if isinstance(right_metrics[key], (int, float)) and isinstance(left_metrics[key], (int, float)) else None)
                    for key in ("net_return", "final_equity", "max_drawdown", "fills", "commission",
                                "filled_notional", "gross_exposure", "orders", "end_to_end_seconds")}
                   if matched else None)
    return {"schema": "ml-selection-paired-account-comparison/v1", "status": "compared" if matched else "pending_valid_paired_accounts",
            "matched_contract": matched, "issues": issues, "left": left_metrics if matched else None,
            "right": right_metrics if matched else None, "right_minus_left": differences,
            "training_performed": False, "backtest_performed": False, "formal_admission": False,
            "interpretation": "Historical matched account differences do not establish independent forward profitability."}
