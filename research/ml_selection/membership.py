"""Decision-time membership evidence, including missing and delisted assets.

Observed price history and today's asset list never establish full historical
membership. Invalid or absent publication/source evidence fails closed by
default; an explicit downgrade retains research rows with a weaker basis.
"""
from __future__ import annotations

from collections.abc import Mapping

import pandas as pd


def _stamp(value):
    if value is None or pd.isna(value):
        return None
    stamp = pd.to_datetime(value, utc=True, errors="coerce")
    return None if pd.isna(stamp) else stamp


def _source(value):
    return isinstance(value, str) and bool(value.strip())


def validate_membership_evidence(evidence) -> tuple[pd.DataFrame, dict]:
    """Validate each interval without inventing facts or exchange coverage."""
    if isinstance(evidence, Mapping):
        rows = [{"symbol": symbol, **dict(item)} for symbol, item in evidence.items()]
        frame = pd.DataFrame(rows)
    elif evidence is None:
        frame = pd.DataFrame(columns=["symbol"])
    elif isinstance(evidence, pd.DataFrame):
        frame = evidence.copy()
    else:
        raise ValueError("membership evidence must be a DataFrame or mapping")
    if "symbol" not in frame:
        raise ValueError("membership evidence requires symbol")
    output = []
    for row in frame.to_dict("records"):
        reasons = []
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not symbol.strip():
            reasons.append("missing_symbol")
        listed, available = _stamp(row.get("listed_at")), _stamp(row.get("available_at"))
        if listed is None:
            reasons.append("missing_or_invalid_listing_effective_time")
        if available is None:
            reasons.append("missing_or_invalid_listing_available_time")
        if not _source(row.get("source")):
            reasons.append("missing_listing_source")
        delisted = _stamp(row.get("delisted_at"))
        supplied_delisting = row.get("delisted_at") is not None and pd.notna(row.get("delisted_at"))
        delisting_available = _stamp(row.get("delisting_available_at"))
        if supplied_delisting:
            if delisted is None:
                reasons.append("invalid_delisting_effective_time")
            if delisting_available is None:
                reasons.append("missing_or_invalid_delisting_available_time")
            elif delisted is not None and delisting_available > delisted:
                reasons.append("delisting_notice_available_after_effective_time")
            if not _source(row.get("delisting_source", row.get("source"))):
                reasons.append("missing_delisting_source")
            if listed is not None and delisted is not None and delisted <= listed:
                reasons.append("delisting_not_after_listing")
        output.append({**row, "listed_at": listed, "available_at": available,
                       "delisted_at": delisted, "delisting_available_at": delisting_available,
                       "evidence_valid": not reasons, "evidence_reasons": reasons})
    result = pd.DataFrame(output, columns=None if output else ["symbol", "evidence_valid", "evidence_reasons"])
    # Multiple intervals require unambiguous, non-overlapping effective facts.
    for symbol, group in result.groupby("symbol", sort=False):
        valid = group.loc[group.evidence_valid].sort_values("listed_at")
        for previous, current in zip(valid.to_dict("records"), valid.to_dict("records")[1:]):
            if pd.isna(previous["delisted_at"]) or current["listed_at"] < previous["delisted_at"]:
                mask = result.symbol == symbol
                result.loc[mask, "evidence_valid"] = False
                for index in result.index[mask]:
                    result.at[index, "evidence_reasons"] = [*result.at[index, "evidence_reasons"], "overlapping_listing_intervals"]
    invalid = result.loc[~result.evidence_valid] if len(result) else result
    return result, {"evidence_rows": len(result), "valid_evidence_rows": int(result.evidence_valid.sum()),
                    "symbols": int(result.symbol.nunique()),
                    "delisted_symbols": int(result.loc[result.get("delisted_at", pd.Series(index=result.index, dtype=object)).notna(), "symbol"].nunique()),
                    "invalid_evidence": invalid.to_dict("records"),
                    "coverage_claim": "provided_sources_only_not_complete_exchange_history"}


def audit_membership(evidence, decisions: pd.DataFrame, *, missing_policy="exclude") -> tuple[pd.DataFrame, dict]:
    """Annotate decision rows with eligibility and the actual evidence basis."""
    if missing_policy not in {"exclude", "downgrade"}:
        raise ValueError("missing_policy must be exclude or downgrade")
    if not {"symbol", "as_of"} <= set(decisions):
        raise ValueError("membership decisions require symbol and as_of")
    facts, report = validate_membership_evidence(evidence)
    times = pd.to_datetime(decisions.as_of, utc=True, errors="coerce")
    if times.isna().any():
        raise ValueError("membership decisions require valid as_of")
    rows = []
    for row, as_of in zip(decisions.to_dict("records"), times):
        supplied = facts.loc[facts.symbol == row["symbol"]]
        valid = supplied.loc[supplied.evidence_valid]
        known = valid.loc[valid.available_at <= as_of] if len(valid) else valid
        active = known.loc[(known.listed_at <= as_of) &
                           (known.delisted_at.isna() | (known.delisted_at > as_of))] if len(known) else known
        insufficient = supplied.empty or valid.empty or known.empty
        if len(active):
            basis, eligible, reason = "source_verified_point_in_time_interval", True, ""
        elif insufficient:
            basis = "observed_history_only_downgraded" if missing_policy == "downgrade" else "membership_unknown"
            eligible, reason = missing_policy == "downgrade", "membership_evidence_missing_invalid_or_not_yet_available"
        else:
            basis, eligible, reason = "source_verified_point_in_time_interval", False, "outside_effective_listing_interval"
        rows.append({**row, "membership_eligible": eligible, "membership_basis": basis,
                     "membership_reason": reason, "pit_evidence_verified": bool(len(active)),
                     "membership_sources": active.source.tolist() if len(active) else []})
    annotated = pd.DataFrame(rows, columns=None if rows else [*decisions.columns, "membership_eligible", "membership_basis", "membership_reason", "pit_evidence_verified", "membership_sources"])
    return annotated, {**report, "missing_policy": missing_policy, "decision_rows": len(rows),
                       "eligible_decisions": sum(row["membership_eligible"] for row in rows),
                       "verified_decisions": sum(row["pit_evidence_verified"] for row in rows),
                       "downgraded_decisions": sum(row["membership_basis"] == "observed_history_only_downgraded" for row in rows),
                       "full_pit_coverage": False}
