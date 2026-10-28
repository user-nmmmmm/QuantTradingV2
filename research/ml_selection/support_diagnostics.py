"""Support and participation diagnostics; no new model or admission thresholds."""
from __future__ import annotations
from collections import Counter, defaultdict
import math

import numpy as np
import pandas as pd
from core.foundation.reproducibility import canonical_json


def _rows(value):
    return value.to_dict("records") if isinstance(value, pd.DataFrame) else [dict(row) for row in value]


def _stamp(row):
    value = row.get("bar_time", row.get("timestamp", row.get("as_of")))
    if value is None:
        return None
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        return None
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _event(row):
    explicit = row.get("market_event_id", row.get("event_id", row.get("signal_id")))
    if explicit is not None and not pd.isna(explicit) and str(explicit):
        return str(explicit)
    stamp = _stamp(row)
    return "|".join((stamp.isoformat() if stamp is not None else "unknown_time", str(row.get("book", "unknown_book")),
                     str(row.get("symbol", "unknown_symbol")), str(row.get("strategy", "unknown_strategy"))))



def _opportunity_identity(row):
    stamp = _stamp(row)
    return canonical_json({"timestamp": stamp.isoformat() if stamp is not None else None,
        **{key: row.get(key) for key in ("book", "symbol", "strategy", "eligible", "information_cutoff",
                                       "data_version", "source_version", "features")}})


def _selected(row):
    value = row.get("selected")
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, str) and value.lower() in {"true", "false"}:
        return value.lower() == "true"
    return None


def _stage(row):
    reason = str(row.get("reason", row.get("selection_reason", "unknown"))).lower()
    filled = row.get("filled_qty", 0)
    if filled is not None and isinstance(filled, (int, float)) and math.isfinite(filled) and filled > 0:
        return "filled"
    if row.get("raw_setup") is False or reason in {"no_signal", "no_setup", "no_candidate"}:
        return "no_signal"
    if row.get("eligible") is False or reason in {"ineligible", "data_not_ready", "stale_data", "membership_missing"}:
        return "ineligible"
    if "health" in reason or reason in {"paused", "strategy_paused"}:
        return "health_paused"
    if any(token in reason for token in ("risk", "breaker", "drawdown")):
        return "risk_rejected"
    if any(token in reason for token in ("cash", "budget", "capital", "competition")):
        return "capital_competition"
    if "model" in reason or "policy" in reason or row.get("selected") is False:
        return "model_rejected"
    if row.get("order_id") or reason in {"order_accepted", "unfilled", "order_rejected", "no_position"}:
        return "unfilled"
    return "unknown_unvisited_gate"


def _kish(weights):
    return sum(weights) ** 2 / sum(x*x for x in weights) if weights and sum(x*x for x in weights) > 0 else 0.0


def selection_support_diagnostics(decisions, *, minimum_dates=None, minimum_events=None):
    """Collapse repeated seeds/books only by identified economic opportunity.

    Kish concentration measures are not a certificate of temporal independence.
    Thresholds, when supplied, are existing policy inputs rather than new defaults.
    """
    rows = _rows(decisions)
    grouped, strata = defaultdict(list), defaultdict(list)
    for row in rows:
        weight = row.get("support_weight", 1.0)
        if (isinstance(weight, bool) or not isinstance(weight, (int, float))
                or not math.isfinite(weight) or weight < 0):
            raise ValueError("support_weight requires a finite nonnegative value")
        grouped[_event(row)].append(row)
        stamp = _stamp(row)
        day = stamp.normalize().isoformat() if stamp is not None else "unknown_date"
        key = (str(row.get("book", "unknown_book")), day, str(row.get("state", row.get("regime", "unknown_state"))))
        strata[key].append(row)
    dates, event_weights = defaultdict(float), []
    for values in grouped.values():
        if len({_opportunity_identity(row) for row in values}) > 1:
            raise ValueError("same event ID carries different opportunity date/book/asset/eligibility")
        weight = float(np.mean([row.get("support_weight", 1.0) for row in values]))
        event_weights.append(weight)
        known = {_stamp(row).normalize().isoformat() for row in values if _stamp(row) is not None}
        if len(known) > 1:
            raise ValueError("one economic event cannot represent multiple market dates")
        if known:
            dates[next(iter(known))] += weight
    gates = Counter(_stage(row) for row in rows)
    chain = Counter()
    limited_events = set()
    for row in rows:
        facts = row.get("gate_facts", [])
        if isinstance(facts, str):
            try:
                import json
                facts = json.loads(facts)
            except (TypeError, ValueError):
                facts = []
        visited = [_stage({"reason": fact.get("reason", "unknown")}) for fact in facts if isinstance(fact, dict)] if isinstance(facts, list) else []
        if not visited:
            visited = [_stage(row)]
        chain.update(visited)
        if "capital_competition" in visited:
            limited_events.add(_event(row))
    known_actions = sum(_selected(row) is not None for row in rows)
    support_ok = bool(rows)
    for required, observed in ((minimum_dates, len(dates)), (minimum_events, len(grouped))):
        if required is not None:
            if type(required) is not int or required <= 0:
                raise ValueError("existing support limits must be positive integers")
            support_ok &= observed >= required
    return {"schema": "ml-selection-support/v1", "status": "diagnostic" if support_ok else "insufficient",
            "rows": len(rows), "unique_market_events": len(grouped), "independent_date_count": len(dates),
            "date_ids": sorted(dates), "event_ids": sorted(grouped),
            "repeated_event_rows": len(rows)-len(grouped), "kish_event_concentration_ess": _kish(event_weights),
            "kish_date_concentration_ess": _kish(list(dates.values())),
            "stage_counts": {name: gates.get(name, 0) for name in ("no_signal", "ineligible", "model_rejected",
                "capital_competition", "health_paused", "risk_rejected", "unfilled", "filled", "unknown_unvisited_gate")},
            "observed_gate_chain_counts": dict(sorted(chain.items())),
            "observed_capital_limited_events": len(limited_events),
            "selected_rows": sum(_selected(row) is True for row in rows) if known_actions else None,
            "selection_action_known_rows": known_actions,
            "participation_rate": sum(_selected(row) is True for row in rows)/known_actions if known_actions else None,
            "participation_rate_basis": "known recorded selection actions only; missing actions stay unknown",
            "strata": [{"book": key[0], "date": key[1], "state": key[2], "rows": len(values),
                        "unique_market_events": len({_event(row) for row in values}),
                        "selected_rows": sum(_selected(row) is True for row in values),
                        "stage_counts": dict(Counter(_stage(row) for row in values))}
                       for key, values in sorted(strata.items())],
            "existing_support_policy": {"minimum_dates": minimum_dates, "minimum_events": minimum_events,
                                        "P1_P2_admission_thresholds_changed": False},
            "economic_value": None, "formal_admission": False,
            "limitations": ["Seed repetitions never create new market dates or economic events.",
                "Kish concentration ESS is bounded by events/dates; it is not autocorrelation-adjusted statistical ESS.",
                "Unknown/unvisited original gates stay unknown; rank changes without actual capital competition do not show allocation value.",
                "Selection action probability, predicted net return and calibrated profit probability remain different quantities."]}


def participation_matched_baseline(opportunities, reference_decisions, *, seed=42):
    """Select the same per-day/book count on the same eligible opportunity set.

    This only constructs a registered comparison arm. Its economic account
    result requires a separate identical-account original-engine evaluation.
    """
    rows, reference = _rows(opportunities), _rows(reference_decisions)
    ids, reference_ids = [_event(row) for row in rows], [_event(row) for row in reference]
    if len(ids) != len(set(ids)) or len(reference_ids) != len(set(reference_ids)) or set(ids) != set(reference_ids):
        raise ValueError("matched-participation arms require the same unique opportunities")
    reference_by_id = {_event(row): row for row in reference}
    if any(_opportunity_identity(row) != _opportunity_identity(reference_by_id[_event(row)]) for row in rows):
        raise ValueError("same event ID has different opportunity date/book/asset/eligibility")
    if type(seed) is not int:
        raise ValueError("registered seed must be an integer")
    def group(row):
        stamp = _stamp(row)
        if stamp is None:
            raise ValueError("matched participation needs actual market dates")
        return (stamp.normalize().isoformat(), str(row.get("book", "unknown_book")))
    batches, desired = defaultdict(list), Counter()
    for row in rows:
        batches[group(row)].append(row)
    for row in reference:
        if _selected(row) is None:
            raise ValueError("reference requires explicit known selected actions")
        if _selected(row):
            desired[group(row)] += 1
    rng, selected = np.random.default_rng(seed), set()
    for key, values in sorted(batches.items()):
        eligible = sorted((_event(row) for row in values if row.get("eligible") is True))
        if desired[key] > len(eligible):
            raise ValueError("reference participation exceeds eligible comparison opportunities")
        selected.update(rng.choice(eligible, size=desired[key], replace=False).tolist())
    result = [{**row, "selected": _event(row) in selected, "comparison_arm": "matched_participation",
             "comparison_seed": seed, "counterfactual_account_evaluated": False} for row in rows]
    actual = Counter(group(row) for row in result if row["selected"])
    if actual != desired:
        raise ValueError("matched participation lost a reference date/book group")
    return result


def compare_selection_support(arms, *, required_arms=("rule", "ridge", "frozen_rl", "matched_participation")):
    """Report available fixed arms; missing arms/data are insufficient, not zero."""
    arm_rows = {name: _rows(rows) for name, rows in arms.items()}
    reports = {name: selection_support_diagnostics(rows) for name, rows in arm_rows.items()}
    identities = [{_event(row): _opportunity_identity(row) for row in rows} for rows in arm_rows.values()]
    identity_match = bool(identities) and all(identity == identities[0] for identity in identities)
    missing = sorted(set(required_arms)-set(reports))
    sets = [set(report["event_ids"]) for report in reports.values()]
    same = identity_match and bool(sets) and all(ids == sets[0] for ids in sets)
    unknown = sorted(name for name, report in reports.items() if report["selection_action_known_rows"] != report["rows"])
    adequate = all(report["status"] == "diagnostic" for report in reports.values())
    return {"schema": "ml-selection-support-comparison/v1",
            "status": "diagnostic" if same and not missing and not unknown and adequate else "insufficient",
            "missing_arms": missing, "unknown_action_arms": unknown, "same_opportunity_universe": same, "arms": reports,
            "independent_date_count": len({day for report in reports.values() for day in report["date_ids"]}),
            "economic_result": None, "ranking_allocation_value": None,
            "reason": "Support/participation comparison only; evaluate identical cash, holdings, risk, execution and exits in the original engine before any economic claim.",
            "formal_admission": False}
