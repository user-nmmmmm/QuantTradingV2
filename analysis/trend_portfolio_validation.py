"""Conservative, descriptive admission evidence for TrendPortfolioV2 research.

Each primary run is evaluated separately. Replayed windows, venues, symbols and
partial lot fills must never inflate the number of independent observations.
Exit-day groups are conservative *proxies* for independent trend events; the
chronological block bootstrap additionally preserves short serial dependence.
This module neither changes strategy settings nor authorizes live trading.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
import math
from statistics import NormalDist
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class AcceptanceConfig:
    minimum_cohorts: int = 30
    preferred_cohorts: int = 50
    positive_rolling_fraction: float = 2 / 3
    bootstrap_iterations: int = 2000
    bootstrap_block_length: int = 5
    bootstrap_seed: int = 42
    confidence: float = .95
    concentration_removals: tuple[int, ...] = (1, 3, 5, 10)
    # These are explicit research policy thresholds, not fitted return targets.
    double_cost_min_return_pct: float = -5.0
    double_cost_max_drawdown_pct: float = 20.0
    double_cost_max_return_drop_pct: float = 10.0
    neighbor_min_positive_fraction: float = 2 / 3
    neighbor_min_median_ratio: float = .5
    recent_days: int = 365
    dsr_min_probability: float = .95

    def __post_init__(self):
        if self.minimum_cohorts < 1 or self.preferred_cohorts < self.minimum_cohorts:
            raise ValueError("cohort thresholds must be positive and ordered")
        if self.bootstrap_iterations < 100 or self.bootstrap_block_length < 1:
            raise ValueError("bootstrap needs at least 100 iterations and a positive block length")
        if not 0 < self.confidence < 1 or self.recent_days < 1:
            raise ValueError("invalid confidence or recent window")
        if any(not 0 < value <= 1 for value in (
                self.positive_rolling_fraction, self.neighbor_min_positive_fraction,
                self.neighbor_min_median_ratio, self.dsr_min_probability)):
            raise ValueError("fraction thresholds must be in (0, 1]")


def _number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _utc(value):
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        raise ValueError("missing timestamp")
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _gate(ok, **evidence):
    return {"status": "unavailable" if ok is None else "pass" if ok else "fail", **evidence}


def exit_day_cohorts(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Collapse authoritative execution-cost-net closes across assets by UTC day.

    Terminal valuation/forced closing events are excluded from cohort evidence.
    Duplicate IDs and missing facts fail closed rather than silently disappearing.
    Engine realized_pnl includes execution costs, but excludes the separately
    posted financing ledger. Admission gates separately verify this limitation.
    """
    grouped, seen, invalid = {}, set(), []
    excluded_terminal_pnl = time_exit_pnl = 0.0
    time_exit_count = terminal_count = 0
    for index, event in enumerate(events):
        row = asdict(event) if is_dataclass(event) else dict(event)
        try:
            event_id = row["close_event_id"]
            if not event_id or event_id in seen:
                raise ValueError("missing or duplicate authoritative close_event_id")
            seen.add(event_id)
            stamp = _utc(row["timestamp"])
            pnl = _number(row["realized_pnl"])
            if pnl is None:
                raise ValueError("nonfinite realized_pnl")
            reason = str(row["exit_reason"])
            if not reason:
                raise ValueError("missing exit_reason")
        except (KeyError, TypeError, ValueError) as exc:
            invalid.append({"index": index, "reason": str(exc)})
            continue
        if reason == "EndOfBacktest":
            excluded_terminal_pnl += pnl
            terminal_count += 1
            continue
        day = stamp.date().isoformat()
        group = grouped.setdefault(day, {"session": day, "net_pnl": 0.0,
                                         "time_exit_pnl": 0.0, "close_event_count": 0})
        group["net_pnl"] += pnl
        group["close_event_count"] += 1
        if reason == "MaxHoldingPeriod":
            group["time_exit_pnl"] += pnl
            time_exit_pnl += pnl
            time_exit_count += 1
    groups = [grouped[day] for day in sorted(grouped)]
    net_pnl = sum(group["net_pnl"] for group in groups)
    return {"schema": "exit_day_cohorts/v1", "status": "invalid" if invalid else "ok",
            "cohort_key": "UTC exit day across all assets and exit controllers within one run",
            "independence": "exit_day_proxy_not_proof_of_independent_trends",
            "pnl_basis": "execution_cost_net; financing is not allocated by CloseEvent",
            "cohort_count": len(groups), "groups": groups, "invalid_events": invalid,
            "net_closed_pnl": net_pnl, "max_holding_period_pnl": time_exit_pnl,
            "max_holding_period_close_count": time_exit_count,
            "net_closed_pnl_without_max_holding_period": net_pnl - time_exit_pnl,
            "excluded_terminal_pnl": excluded_terminal_pnl,
            "excluded_terminal_close_count": terminal_count}


def cohort_statistics(cohorts, config: AcceptanceConfig = AcceptanceConfig()):
    """Circular moving-block PF interval and concentration stress, without NaN/inf."""
    values = np.asarray([row["net_pnl"] for row in cohorts["groups"]], dtype=float)
    gains, losses = float(np.maximum(values, 0).sum()), float(-np.minimum(values, 0).sum())
    lower = upper = None
    lower_unbounded = upper_unbounded = False
    if len(values):
        rng = np.random.default_rng(config.bootstrap_seed)
        length = config.bootstrap_block_length
        starts = rng.integers(0, len(values), size=(config.bootstrap_iterations,
                                                   math.ceil(len(values) / length)))
        indices = ((starts[..., None] + np.arange(length)) % len(values)).reshape(
            config.bootstrap_iterations, -1)[:, :len(values)]
        samples = values[indices]
        sample_gains = np.maximum(samples, 0).sum(axis=1)
        sample_losses = -np.minimum(samples, 0).sum(axis=1)
        ratios = np.divide(sample_gains, sample_losses,
                           out=np.full(config.bootstrap_iterations, np.inf), where=sample_losses > 0)
        ratios[(sample_gains == 0) & (sample_losses == 0)] = 0
        ordered = np.sort(ratios)
        tail = (1 - config.confidence) / 2
        lo = ordered[math.floor(tail * (len(ordered) - 1))]
        hi = ordered[math.ceil((1 - tail) * (len(ordered) - 1))]
        lower_unbounded, upper_unbounded = bool(np.isinf(lo)), bool(np.isinf(hi))
        lower, upper = (None if lower_unbounded else float(lo)), (None if upper_unbounded else float(hi))
    scenarios = {}
    for count in config.concentration_removals:
        retained = values.copy()
        positive = np.flatnonzero(retained > 0)
        removed = positive[np.argsort(retained[positive])[-count:]] if count else []
        retained[removed] = 0
        scenarios[str(count)] = {"removed_count": len(removed),
                                 "remaining_net_closed_pnl": float(retained.sum())}
    return {"profit_factor": gains / losses if losses > 0 else None,
            "profit_factor_unbounded": gains > 0 and losses == 0,
            "pf_confidence_interval": [lower, upper], "lower_unbounded": lower_unbounded,
            "upper_unbounded": upper_unbounded, "confidence": config.confidence,
            "bootstrap_iterations": config.bootstrap_iterations,
            "block_length": config.bootstrap_block_length, "seed": config.bootstrap_seed,
            "method": "circular chronological exit-day block bootstrap; zero-loss draws retained",
            "concentration": scenarios}


def selection_bias_evidence(returns, metadata, config: AcceptanceConfig = AcceptanceConfig()):
    """DSR with measured cross-trial Sharpe dispersion, in per-observation units.

    A current-suite-only diagnostic cannot certify the unknown prior search.
    Annualized Sharpes must be converted by the caller before submission.
    """
    meta = dict(metadata or {})
    values = np.asarray(list(returns) if returns is not None else [], dtype=float)
    raw_sharpes = meta.get("trial_sharpes", [])
    sharpes = np.asarray(raw_sharpes, dtype=float)
    total_trials = meta.get("total_trials")
    complete = (meta.get("historical_trials_complete") is True
                and meta.get("registered_before_results") is True
                and isinstance(total_trials, int) and not isinstance(total_trials, bool)
                and total_trials >= 1 and len(sharpes) == total_trials)
    result = {"status": "unavailable", "probability": None,
              "historical_trials_complete": meta.get("historical_trials_complete") is True,
              "registered_before_results": meta.get("registered_before_results") is True,
              "total_trials": total_trials, "observed_trial_sharpes": int(len(sharpes)),
              "current_run_count": meta.get("current_run_count"),
              "formula": "DSR using per-observation Sharpe and measured cross-trial Sharpe dispersion",
              "reason": "complete preregistered trial accounting and cross-trial Sharpe dispersion required"}
    if (len(values) < 3 or not np.isfinite(values).all() or not np.isfinite(sharpes).all()
            or len(sharpes) < 1 or values.std(ddof=1) == 0):
        return result
    count = total_trials if complete else len(sharpes)
    if count > 1 and len(sharpes) < 2:
        return result
    std = values.std(ddof=1)
    observed = float(values.mean() / std)
    expected = 0.0
    if count > 1:
        normal, gamma = NormalDist(), .5772156649015329
        expected = float(sharpes.std(ddof=1)) * (
            (1 - gamma) * normal.inv_cdf(1 - 1 / count)
            + gamma * normal.inv_cdf(1 - 1 / (count * math.e)))
    centered, variance = values - values.mean(), float(values.var())
    skew = float(np.mean(centered ** 3) / variance ** 1.5)
    kurtosis = float(np.mean(centered ** 4) / variance ** 2)
    sampling_variance = 1 - skew * observed + (kurtosis - 1) * observed ** 2 / 4
    if sampling_variance <= 0:
        result["reason"] = "nonpositive Sharpe sampling variance"
        return result
    probability = float(NormalDist().cdf((observed - expected) * math.sqrt(len(values) - 1)
                                        / math.sqrt(sampling_variance)))
    result.update(observed_period_sharpe=observed, expected_max_period_sharpe=expected,
                  sample_size=len(values), diagnostic_probability=probability,
                  diagnostic_scope="complete_registered_search" if complete else "observed_trials_only")
    if complete:
        result.update(status="pass" if probability >= config.dsr_min_probability else "fail",
                      probability=probability, reason=None)
    return result


def _matching(primary, rows, role, *, venue=True, same_period=True, same_parameters=True):
    def matches(row):
        if row.get("role") != role:
            return False
        keys = ["variant", "timeframe"]
        if venue:
            keys.append("venue")
        if same_period:
            keys.extend(("start", "end"))
        if same_parameters:
            keys.extend(("stability", "cooldown"))
        if primary.get("symbols") is not None:
            if sorted(row.get("symbols", [])) != sorted(primary["symbols"]):
                return False
        return all(str(row.get(key)) == str(primary.get(key)) for key in keys)
    return [row for row in rows if matches(row)]


def _period_activity(primary, cohorts, boundary):
    events = [row for row in cohorts["groups"] if _utc(row["session"]) >= boundary]
    pnl = sum(row["net_pnl"] for row in events)
    fills = primary.get("trades")
    if fills is None:
        return _gate(None, net_closed_pnl=pnl, reason="entry fill records unavailable")
    entries = 0
    invalid = 0
    for fill in fills:
        if str(fill.get("side", "")).lower() != "buy":
            continue
        try:
            if _utc(fill.get("fill_time", fill.get("timestamp"))) >= boundary:
                entries += 1
        except (TypeError, ValueError):
            invalid += 1
    return _gate(None if invalid else entries > 0 and pnl > 0, start=boundary.isoformat(),
                 entry_fill_count=entries, net_closed_pnl=pnl, invalid_entry_timestamps=invalid,
                 contribution_basis="net realized closes in period, excluding terminal valuation")


def _financing_completeness(row):
    total, gross = _number(row.get("financing_total")), _number(row.get("financing_gross"))
    valid = total is not None and gross is not None and gross >= 0 and gross + 1e-10 >= abs(total)
    complete = valid and gross == 0
    return _gate(True if complete else None, financing_total=total, financing_gross=gross,
                 declared_cohort_allocation=row.get("cohort_financing_allocated", False),
                 reason=None if complete else "canonical CloseEvent PnL has no authoritative financing allocation",
                 equity_net_includes_financing=True,
                 rule="zero financing movements; allocation flags alone do not adjust cohort PnL")


def _evaluate_primary(primary, rows, config, trial_metadata):
    events = primary.get("close_event_records")
    cohorts = exit_day_cohorts(events or [])
    stats = cohort_statistics(cohorts, config)
    count, gates = cohorts["cohort_count"], {}
    valid = events is not None and cohorts["status"] == "ok"
    identity_ok = all(primary.get(key) is not None for key in (
        "variant", "venue", "timeframe", "start", "end"))
    gates["run_identity"] = _gate(identity_ok)
    try:
        start, end = _utc(primary["start"]), _utc(primary["end"])
        real_events = [asdict(event) if is_dataclass(event) else event for event in (events or [])]
        # The shared engine values terminal holdings at last_bar + one microsecond.
        # Those marks are already excluded from every cohort/performance gate.
        chronology_ok = start <= end and all(start <= _utc(event["timestamp"]) <= end
            for event in real_events if event.get("exit_reason") != "EndOfBacktest")
    except (KeyError, TypeError, ValueError):
        chronology_ok = False
    gates["event_period_alignment"] = _gate(chronology_ok)
    valid = valid and chronology_ok
    gates["authoritative_close_events"] = _gate(valid, invalid_events=cohorts["invalid_events"])
    gates["cohort_financing_completeness"] = _financing_completeness(primary)
    financing_complete = gates["cohort_financing_completeness"]["status"] == "pass"
    gates["minimum_exit_cohorts"] = _gate(valid and count >= config.minimum_cohorts,
                                         observed=count, required=config.minimum_cohorts,
                                         preferred=config.preferred_cohorts)
    net = _number(primary.get("return_pct"))
    benchmark = _number(primary.get("buyhold_risk_matched_return_pct"))
    benchmark_verified = (primary.get("benchmark_diagnostic") or {}).get("risk_match_verified")
    gates["positive_net_return"] = _gate(None if net is None else net > 0, return_pct=net)
    gates["risk_matched_buyhold_excess"] = _gate(
        None if net is None or benchmark is None or benchmark_verified is False else net > benchmark,
        strategy_return_pct=net, benchmark_return_pct=benchmark,
        risk_match_verified=benchmark_verified,
        excess_return_pct=None if net is None or benchmark is None else net - benchmark)
    lower = stats["pf_confidence_interval"][0]
    lower_ok = stats["lower_unbounded"] or lower is not None and lower > 1
    gates["cohort_pf_lower_bound"] = _gate(None if not financing_complete else
                                           valid and count >= config.minimum_cohorts and lower_ok,
                                           lower=lower, lower_unbounded=stats["lower_unbounded"],
                                           required_strictly_greater_than=1)
    gates["remove_top_cohorts"] = _gate(
        None if not financing_complete else valid and count >= config.minimum_cohorts and all(
            row["remaining_net_closed_pnl"] >= 0 for row in stats["concentration"].values()),
        scenarios=stats["concentration"])
    gates["profit_without_time_exits"] = _gate(
        None if not financing_complete else valid and cohorts["net_closed_pnl_without_max_holding_period"] > 0,
        net_closed_pnl=cohorts["net_closed_pnl"],
        max_holding_period_pnl=cohorts["max_holding_period_pnl"],
        net_closed_pnl_without_time_exits=cohorts["net_closed_pnl_without_max_holding_period"],
        limitation="attribution subtraction; not a counterfactual replay without the safety valve")
    rolling = _matching(primary, rows, "rolling", same_period=False)
    rolling_values = [_number(row.get("return_pct")) for row in rolling]
    unique_rolling = len({(row.get("start"), row.get("end")) for row in rolling}) == len(rolling)
    fraction = sum(value > 0 for value in rolling_values if value is not None) / len(rolling) if rolling else None
    gates["rolling_windows"] = _gate(
        None if fraction is None or None in rolling_values or not unique_rolling else fraction >= config.positive_rolling_fraction,
        windows=len(rolling), positive_fraction=fraction, required_fraction=config.positive_rolling_fraction,
        dependence_note="overlapping windows are descriptive, not extra independent cohorts")
    costs = _matching(primary, rows, "cost")
    for multiplier in (1.5, 2.0):
        matching = [row for row in costs if _number(row.get("cost_multiplier")) == multiplier]
        if len(matching) != 1:
            gates[f"cost_{multiplier:g}x"] = _gate(None, reason="exactly one matched cost replay required")
            continue
        candidate = matching[0]
        value, drawdown = _number(candidate.get("return_pct")), _number(candidate.get("max_drawdown_pct"))
        ok = None
        if multiplier == 1.5 and value is not None:
            cost_cohorts = exit_day_cohorts(candidate.get("close_event_records", []))
            cost_stats = cohort_statistics(cost_cohorts, config)
            cost_lower = cost_stats["pf_confidence_interval"][0]
            ok = (value > 0 and cost_cohorts["status"] == "ok"
                  and cost_cohorts["cohort_count"] >= config.minimum_cohorts
                  and (cost_stats["lower_unbounded"] or cost_lower is not None and cost_lower > 1))
            if _financing_completeness(candidate)["status"] != "pass":
                ok = None
        elif multiplier == 2 and value is not None and drawdown is not None and net is not None:
            ok = (value >= config.double_cost_min_return_pct
                  and drawdown <= config.double_cost_max_drawdown_pct
                  and net - value <= config.double_cost_max_return_drop_pct)
        gates[f"cost_{multiplier:g}x"] = _gate(ok, return_pct=value, max_drawdown_pct=drawdown,
            policy="positive net and cohort PF lower CI > 1" if multiplier == 1.5 else
            "predeclared minimum return, maximum drawdown and maximum return deterioration")
    neighbors = _matching(primary, rows, "neighbor", same_parameters=False)
    registered_stability, registered_cooldown = (2, 3, 5), (0, 2)
    center = (primary.get("stability"), primary.get("cooldown"))
    neighbor_pairs = [(row.get("stability"), row.get("cooldown")) for row in neighbors]
    valid_neighbors = (len(set(neighbor_pairs)) == len(neighbor_pairs)
                       and center[0] in registered_stability and center[1] in registered_cooldown)
    if valid_neighbors:
        valid_neighbors = all(pair[0] in registered_stability and pair[1] in registered_cooldown
                              and pair != center for pair in neighbor_pairs)
    all_neighbor_count = len(neighbors)
    if valid_neighbors:
        # The registered grid includes diagonals. Keep their trial count but assess
        # a local platform using the directly adjacent grid points only.
        neighbors = [row for row in neighbors if
            abs(registered_stability.index(row["stability"]) - registered_stability.index(center[0]))
            + abs(registered_cooldown.index(row["cooldown"]) - registered_cooldown.index(center[1])) == 1]
    neighbor_values = [_number(row.get("return_pct")) for row in neighbors]
    ratio = median = None
    neighbor_ok = None
    if neighbors and valid_neighbors and None not in neighbor_values and net is not None:
        ratio = sum(value > 0 for value in neighbor_values) / len(neighbors)
        median = float(np.median(neighbor_values))
        neighbor_ok = (len(neighbors) >= 2 and net > 0 and ratio >= config.neighbor_min_positive_fraction
                       and median >= net * config.neighbor_min_median_ratio)
    gates["neighbor_plateau"] = _gate(neighbor_ok, tested_neighbors=len(neighbors),
                                      registered_grid_observations=all_neighbor_count,
                                      assessed_points=[[row.get("stability"), row.get("cooldown")]
                                                       for row in neighbors] if valid_neighbors else [],
                                      positive_fraction=ratio, median_return_pct=median,
                                      rule="at least two preregistered neighbors; positive fraction and median retention")
    peers = (_matching(primary, rows, "venue", venue=False)
             + _matching(primary, rows, "primary", venue=False))
    peer_values = {str(row.get("venue")).lower(): _number(row.get("return_pct")) for row in peers}
    signs_ok = None
    if "binance" in peer_values and "okx" in peer_values and None not in peer_values.values():
        # Agreement on two negative returns does not establish a viable candidate.
        signs_ok = peer_values["binance"] > 0 and peer_values["okx"] > 0
    gates["venue_direction"] = _gate(signs_ok, matched_returns_pct=peer_values,
                                     rule="Binance and OKX both positive on matched period and assets")
    gates["post_2022_activity"] = _period_activity(primary, cohorts, _utc("2022-01-01"))
    try:
        recent_start = _utc(primary.get("recent_start")) if primary.get("recent_start") else (
            _utc(primary["end"]) - pd.Timedelta(days=config.recent_days))
        gates["recent_activity"] = _period_activity(primary, cohorts, recent_start)
        recent_runs = [row for row in _matching(primary, rows, "recent", same_period=False)
                       if _utc(row["start"]) == recent_start and _utc(row["end"]) == _utc(primary["end"])]
        recent_return = _number(recent_runs[0].get("return_pct")) if len(recent_runs) == 1 else None
        gates["recent_window_return"] = _gate(
            None if recent_return is None else recent_return > 0,
            return_pct=recent_return, matched_replay_count=len(recent_runs),
            start=recent_start.isoformat(), end=_utc(primary["end"]).isoformat(),
            rule="positive net return in a separately started recent-window replay")
    except (KeyError, TypeError, ValueError):
        gates["recent_activity"] = _gate(None, reason="valid end or recent_start required")
        gates["recent_window_return"] = _gate(None, reason="valid matched recent replay required")
    if not financing_complete:
        for name in ("post_2022_activity", "recent_activity"):
            gates[name].update(status="unavailable", reason="closed contribution excludes unallocated financing")
    dsr = selection_bias_evidence(primary.get("daily_returns", []), trial_metadata, config)
    gates["selection_bias"] = dsr
    statuses = [gate["status"] for gate in gates.values()]
    status = "failed" if "fail" in statuses else "incomplete" if "unavailable" in statuses else "passed"
    return {"run_id": primary.get("run_id"), "variant": primary.get("variant"),
            "venue": primary.get("venue"), "timeframe": primary.get("timeframe"),
            "start": primary.get("start"), "end": primary.get("end"),
            "research_status": status, "gates": gates, "cohorts": cohorts, "statistics": stats}


def evaluate_trend_portfolio(results: Sequence[Mapping[str, Any]], *,
                             config: AcceptanceConfig | None = None,
                             trial_metadata: Mapping[str, Any] | None = None,
                             fresh_evidence: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Evaluate an already preregistered suite; missing evidence never passes.

    Required per-run identity: role, variant, venue, timeframe, start, end.
    Metrics: return_pct, max_drawdown_pct, buyhold_risk_matched_return_pct;
    evidence: close_event_records, trades, daily_returns, accounting_check,
    financing_total and financing_gross (zero when the ledger has no entries).
    Cost/neighbor/venue runs must match the primary's period and asset universe.
    The caller must freeze AcceptanceConfig before running or viewing results.
    """
    config = config or AcceptanceConfig()
    rows = [dict(row) for row in results]
    primaries = [row for row in rows if row.get("role") == "primary"]
    assessed = [_evaluate_primary(row, rows, config, trial_metadata) for row in primaries]
    engineering = "completed" if rows and all(
        (row.get("accounting_check") or {}).get("ok") is True and not row.get("error")
        for row in rows) else "unverified_or_failed"
    research = "failed" if any(row["research_status"] == "failed" for row in assessed) else (
        "incomplete" if not assessed or any(row["research_status"] == "incomplete" for row in assessed)
        else "passed_descriptive_gates")
    fresh = dict(fresh_evidence or {})
    unseen_ok = (fresh.get("kind") in ("forward", "sealed_holdout")
                 and fresh.get("registered_before_evaluation") is True
                 and fresh.get("used_for_selection") is False
                 and fresh.get("completed") is True and fresh.get("validation_verified") is True
                 and all(fresh.get(key) for key in ("protocol_hash", "data_hash", "code_hash")))
    return {"schema": "trend_portfolio_acceptance/v1", "engineering_status": engineering,
            "research_status": research, "live_admission": False,
            "fresh_evidence_status": "verified" if unseen_ok else "unavailable",
            "admission_status": "eligible_for_independent_review" if (
                unseen_ok and engineering == "completed" and research == "passed_descriptive_gates")
                else "not_admitted",
            "thresholds": asdict(config), "primary_runs": assessed,
            "trial_metadata": dict(trial_metadata or {}),
            "limitations": ["Previously inspected 2020-2026 data are research/validation, not untouched holdout.",
                "Exit-day cohorts and their block bootstrap do not prove statistical independence.",
                "Venues and overlapping rolling windows are never pooled to inflate cohort support.",
                "Daily and four-hour evidence must each pass separately.",
                "Historical search attempts must be known before DSR can support admission.",
                "Equity includes financing; cohort profitability is unavailable when financing has no authoritative allocation.",
                "This report cannot authorize trading; fresh frozen evidence requires independent review."]}
