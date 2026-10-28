"""Materialize only visible immutable label revisions at a training boundary."""
from __future__ import annotations

import math
import numpy as np
import pandas as pd

from signal_meta.signal_label_versions import OutcomeRevisionBook


def materialize_training_labels(dataset, *, cutoff, revision_book):
    """Choose latest visible revision before testing eligibility; never fall back.

    Caller-supplied candidate_id/horizon_bars are mandatory: mapping a proxy
    candle label to an engine signal without its identity would mix studies.
    ``eligible`` remains the decision opportunity flag so chronological_split
    can withhold an entire date cohort when a peer's outcome is unavailable.
    """
    if not isinstance(revision_book, OutcomeRevisionBook) or not revision_book.versioned:
        raise ValueError("a validated versioned OutcomeRevisionBook is required")
    if not {"candidate_id", "horizon_bars"} <= set(dataset):
        raise ValueError("versioned training rows require candidate_id and horizon_bars")
    rows = dataset.copy()
    selected = revision_book.as_of(cutoff)
    for key in ("label_net_return", "label_mae"):
        rows[key] = np.nan
    rows["label_available_at"] = pd.Series(pd.NaT, index=rows.index, dtype="datetime64[ns, UTC]")
    rows["label_revision_id"], rows["label_cost_policy_sha256"] = None, None
    rows["label_training_eligible"] = False
    rows["label_eligibility_reason"] = "no_visible_label_revision"
    rows["label_basis"] = "versioned_independent_signal_diagnostic_not_portfolio"
    for index, entry in rows.iterrows():
        identity = revision_book.candidate_context(entry["candidate_id"])
        if "symbol" in entry and entry["symbol"] != identity["symbol"]:
            raise ValueError("training candidate symbol differs from label identity")
        if "as_of" in entry and pd.to_datetime(entry["as_of"], utc=True) != pd.to_datetime(identity["as_of"], utc=True):
            raise ValueError("training decision cutoff differs from label candidate")
        horizon = entry["horizon_bars"]
        if isinstance(horizon, (bool, np.bool_)) or not isinstance(horizon, (int, np.integer)):
            raise ValueError("label horizon must be an integer")
        label = selected.get((entry["candidate_id"], int(horizon)))
        if label is None:
            continue
        rows.at[index, "label_revision_id"] = label["revision_id"]
        rows.at[index, "label_cost_policy_sha256"] = label.get("cost_policy_sha256")
        rows.at[index, "label_available_at"] = pd.to_datetime(label["available_at"], utc=True)
        value = label.get("net_return_bps")
        qualified = (revision_book.eligible(label) and not label.get("ambiguous", False)
                     and isinstance(value, (int, float)) and not isinstance(value, bool)
                     and math.isfinite(value))
        rows.at[index, "label_eligibility_reason"] = "" if qualified else str(label.get("status", "ineligible_revision"))
        rows.at[index, "label_training_eligible"] = qualified
        if qualified:
            rows.at[index, "label_net_return"] = float(value) / 10000
            if label.get("mae_bps") is not None:
                rows.at[index, "label_mae"] = float(label["mae_bps"]) / 10000
    rows.attrs["label_materialization"] = {"schema": "training-label-materialization/v1",
        "cutoff": pd.to_datetime(cutoff, utc=True).isoformat(),
        "selection": "latest strictly before cutoff, then eligibility", **revision_book.audit()}
    return rows
