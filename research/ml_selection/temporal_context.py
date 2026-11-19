"""Explicit ML selection time context and byte-verified source views.

Strict mode consumes the existing TemporalOHLCV revision store. A scheduled
close is never substituted for a local observation or source publication.
"""

from __future__ import annotations


import pandas as pd

from core.market.temporal_data import (
    TemporalDataError as TemporalDataError,
    TemporalDataPolicy as TemporalDataPolicy,
    TemporalOHLCV as TemporalOHLCV,
    strategy_decision_cutoff as strategy_decision_cutoff,
    temporal_policy as temporal_policy,
)

from signal_meta.selection_context import (
    SelectionTemporalContext as SelectionTemporalContext,
    utc as utc,
)

from signal_meta.signal_observation_types import fingerprint


def feature_snapshot_identity(row):
    """Bind serving features to their declared context and source-version digest."""
    import math
    from research.ml_selection.dataset import FEATURE_COLUMNS

    values = {}
    for name in FEATURE_COLUMNS:
        number = float(row[name])
        values[name] = number if math.isfinite(number) else None
    bar = row.get("bar_time")
    reason = row.get("exclusion_reason", "")
    reason = "" if reason is None or pd.isna(reason) else reason
    return fingerprint(
        {
            "symbol": row["symbol"],
            "as_of": utc(row["as_of"]).isoformat(),
            "bar_time": None if bar is None or pd.isna(bar) else utc(bar).isoformat(),
            "features": values,
            "eligible": bool(row.get("eligible", False)),
            "exclusion_reason": reason,
            "membership_basis": row.get("membership_basis"),
            "temporal_context_id": row["temporal_context_id"],
            "source_versions_sha256": row["source_versions_sha256"],
            "feature_source_versions_sha256": row.get("feature_source_versions_sha256"),
        }
    )


def validate_causal_prices(prices, sources, context):
    """Account features cannot consume realized marks absent from visible sources."""
    import math
    from core.market.data_versions import _record

    for symbol, price in prices.items():
        envelopes = sources.get(symbol)
        if not envelopes:
            raise ValueError("strict causal source close missing for account mark")
        active = []
        for envelope in envelopes.values():
            raw = envelope["record"]
            if fingerprint(raw) != envelope.get("record_sha256"):
                raise ValueError("strict account source byte identity mismatch")
            record = _record(raw)
            if (
                record["available_at"] is None
                or utc(record["available_at"]) > context.decision_cutoff
                or utc(record["event_time"]) + pd.Timedelta(days=1) > context.decision_cutoff
                or (
                    context.policy.knowledge == "local"
                    and utc(record["observed_at"]) > context.decision_cutoff
                )
                or (
                    context.policy.knowledge == "published"
                    and record["availability_evidence"]["kind"] != "source_publication"
                )
            ):
                raise ValueError("strict account source was unavailable at decision cutoff")
            if record.get("record_type") != "tombstone":
                active.append(record)
        if not active:
            raise ValueError("strict causal source close missing after withdrawal")
        latest = max(active, key=lambda record: utc(record["event_time"]))
        expected = float(latest["data"]["close"])
        if (
            isinstance(price, bool)
            or not math.isfinite(float(price))
            or float(price) <= 0
            or not math.isclose(float(price), expected, rel_tol=1e-12, abs_tol=1e-12)
        ):
            raise ValueError("account mark differs from the causal source close")
