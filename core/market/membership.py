"""One decision-time membership parser shared by ML and factor consumers.

Intervals are [listed_at, delisted_at). A later-known exit cannot change a
past decision, and missing membership never becomes full-market coverage.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
import pandas as pd


def _time(value):
    if value is None or pd.isna(value):
        return None
    at = pd.to_datetime(value, utc=True, errors="coerce")
    return None if pd.isna(at) else at


def membership_rows(evidence):
    if isinstance(evidence, pd.DataFrame):
        if "symbol" not in evidence:
            raise ValueError("membership requires a symbol column")
        return evidence.to_dict("records")
    if evidence is None:
        return []
    if not isinstance(evidence, Mapping):
        raise ValueError("membership must be a DataFrame or symbol metadata mapping")
    rows = []
    for symbol, facts in evidence.items():
        facts = facts if isinstance(facts, (list, tuple)) else [facts]
        for interval in facts:
            if not isinstance(interval, Mapping):
                raise ValueError("membership interval must be a mapping")
            if "symbol" in interval and interval["symbol"] != symbol:
                raise ValueError("membership interval symbol contradicts its mapping key")
            rows.append({"symbol": symbol, **dict(interval)})
    return rows


def validate_interval_structure(evidence):
    """Reject ambiguous effective intervals before any research material is built."""
    rows = membership_rows(evidence)
    by_symbol: dict[str, list[tuple[Any, Any]]] = {}
    for row in rows:
        listed, available = _time(row.get("listed_at")), _time(row.get("available_at"))
        if listed is None or available is None:
            raise ValueError("membership facts require finite listed_at and available_at")
        end = _time(row.get("delisted_at"))
        if end is not None and _time(row.get("delisting_available_at")) is None:
            raise ValueError("delisted_at requires delisting_available_at")
        if end is not None and end <= listed:
            raise ValueError("delisting must follow listing")
        by_symbol.setdefault(row["symbol"], []).append((listed, end))
    for symbol, intervals in by_symbol.items():
        ordered = sorted(intervals, key=lambda interval: interval[0])
        for previous, current in zip(ordered, ordered[1:]):
            if previous[1] is None or current[0] < previous[1]:
                raise ValueError(f"overlapping listing intervals for {symbol}")
    return rows


def membership_as_of(
    evidence,
    symbol,
    as_of,
    *,
    require_sources=False,
    knowledge="local",
    require_observed=False,
    validate=True,
):
    """Select a known interval and known exit at the actual decision cutoff."""
    if knowledge not in {"local", "published"}:
        raise ValueError("unknown membership knowledge policy")
    cutoff = _time(as_of)
    if cutoff is None:
        raise ValueError("finite membership decision cutoff required")
    rows = validate_interval_structure(evidence) if validate else membership_rows(evidence)
    rows = [row for row in rows if row.get("symbol") == symbol]
    known, active, sources = [], [], []
    for row in rows:
        reasons = set(row.get("evidence_reasons", []))
        # A late exit notice cannot revoke a separately known listing before
        # that notice arrived. Keep the legacy audit warning, use the actual
        # knowledge boundary, and do not claim full historical coverage.
        if row.get("evidence_valid") is False and reasons != {
            "delisting_notice_available_after_effective_time"
        }:
            continue
        listed, available = _time(row.get("listed_at")), _time(row.get("available_at"))
        if listed is None or available is None:
            continue
        if require_sources and not (isinstance(row.get("source"), str) and row["source"].strip()):
            continue
        observed = _time(row.get("observed_at"))
        if knowledge == "local" and (
            (require_observed and observed is None) or (observed is not None and observed > cutoff)
        ):
            continue
        if available > cutoff:
            continue
        known.append(row)
        if listed > cutoff:
            continue
        end, exit_available = (
            _time(row.get("delisted_at")),
            _time(row.get("delisting_available_at")),
        )
        exit_observed = _time(row.get("delisting_observed_at"))
        exit_known = exit_available is not None and exit_available <= cutoff
        if knowledge == "local":
            if require_observed and exit_observed is None:
                exit_known = False
            elif exit_observed is not None:
                exit_known &= exit_observed <= cutoff
        if exit_known and end is not None and end <= cutoff:
            continue
        active.append(row)
        if row.get("source"):
            sources.append(row["source"])
    if len(active) > 1:
        raise ValueError("ambiguous active membership intervals")
    verified = bool(active) and require_sources
    return {
        "eligible": bool(active),
        "pit_evidence_verified": verified,
        "reason": ""
        if active
        else "outside_effective_listing_interval"
        if known
        else "membership_evidence_missing_invalid_or_not_yet_available",
        "basis": "source_verified_point_in_time_interval"
        if require_sources and known
        else "supplied_point_in_time_facts"
        if known
        else "membership_unknown",
        "sources": sources,
        "full_pit_coverage": False,
    }
