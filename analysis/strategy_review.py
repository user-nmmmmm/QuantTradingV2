"""Common event-cohort evidence and a date-gated, single-use forward protocol."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from core.strategy_health import classify_exit_controller


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def cohort_evidence(events, *, mark_to_market=True):
    grouped = {}
    seen = set()
    for event in events:
        row = asdict(event) if is_dataclass(event) else dict(event)
        event_id = row["close_event_id"]
        if event_id in seen:
            raise ValueError("Duplicate authoritative close event")
        seen.add(event_id)
        if mark_to_market and row.get("exit_reason") == "EndOfBacktest":
            continue
        timestamp = pd.Timestamp(row["timestamp"])
        timestamp = timestamp.tz_localize("UTC") if timestamp.tzinfo is None else timestamp.tz_convert("UTC")
        controller = classify_exit_controller(row["exit_reason"])
        action = "" if controller == "strategy" else (row.get("risk_action_id") or "")
        key = (str(timestamp.date()), row["opening_strategy_id"], controller, action)
        pnl = float(row["realized_pnl"])
        if not math.isfinite(pnl):
            raise ValueError("Nonfinite close PnL")
        grouped[key] = grouped.get(key, 0.0) + pnl
    values = np.array([grouped[key] for key in sorted(grouped)], dtype=float)
    enough = len(values) >= 30
    scenarios = {}
    for remove_top in (0, 5, 10):
        retained = values.copy()
        positive = np.flatnonzero(retained > 0)
        removed = positive[np.argsort(retained[positive])[-remove_top:]] if remove_top else np.array([], dtype=int)
        retained[removed] = 0
        gain = float(retained[retained > 0].sum())
        loss = float(-retained[retained < 0].sum())
        lower = upper = None
        lower_unbounded = upper_unbounded = False
        if len(values):
            rng = np.random.default_rng(42)
            starts = rng.integers(0, len(values), size=(2000, math.ceil(len(values) / 5)))
            indices = ((starts[..., None] + np.arange(5)) % len(values)).reshape(2000, -1)[:, :len(values)]
            samples = retained[indices]
            gains = np.maximum(samples, 0).sum(axis=1)
            losses = -np.minimum(samples, 0).sum(axis=1)
            ratios = np.divide(gains, losses, out=np.full(2000, np.inf), where=losses > 0)
            # Zero/zero is no evidence, not an infinitely profitable resample.
            ratios[(gains == 0) & (losses == 0)] = 0
            ordered = np.sort(ratios)
            lo, hi = ordered[math.floor(.025 * 1999)], ordered[math.ceil(.975 * 1999)]
            lower_unbounded, upper_unbounded = bool(np.isinf(lo)), bool(np.isinf(hi))
            lower, upper = (None if lower_unbounded else float(lo)), (None if upper_unbounded else float(hi))
        scenarios[str(remove_top)] = dict(removed_count=len(removed), remaining_net_closed_pnl=float(retained.sum()),
            profit_factor=gain / loss if loss > 0 else None,
            profit_factor_unbounded=loss == 0 and gain > 0,
            pf_95pct_ci=[lower, upper], lower_unbounded=lower_unbounded, upper_unbounded=upper_unbounded)
    return dict(schema="cohort_evidence/v2", cohort_key_version=2, cohort_count=len(values),
        sample_status="sufficient" if enough else "insufficient", block_length=5, seed=42, iterations=2000,
        tail_mark_events_excluded=mark_to_market, scenarios=scenarios,
        method="circular chronological cohort block bootstrap; zero-loss resamples retained",
        groups=[dict(session=k[0], strategy=k[1], controller=k[2], action=k[3], pnl=grouped[k]) for k in sorted(grouped)])


def evaluate_gates(summary, evidence):
    sufficient = evidence["cohort_count"] >= 30
    point = evidence["scenarios"]["0"]
    def assessed(ok):
        return "insufficient" if not sufficient else "pass" if ok else "fail"
    pf_ok = point["profit_factor_unbounded"] or (point["profit_factor"] is not None and point["profit_factor"] > 1.15)
    lower_ok = point["lower_unbounded"] or (point["pf_95pct_ci"][0] is not None and point["pf_95pct_ci"][0] > 1)
    return dict(cohort_support="pass" if sufficient else "insufficient",
                profit_factor=assessed(pf_ok and lower_ok),
                positive_net_return=assessed(float(summary["return_pct"]) > 0),
                drawdown="pass" if float(summary["max_drawdown_pct"]) <= 20 else "fail",
                remove_top5_top10=assessed(all(evidence["scenarios"][str(n)]["remaining_net_closed_pnl"] > 0 for n in (5, 10))))


def freeze_prospective(path, *, code_hash, config_hash, registry_hash, frozen_at=None):
    path = Path(path)
    now = pd.Timestamp(frozen_at or datetime.now(timezone.utc))
    if now.tzinfo is None:
        raise ValueError("Freeze time must include timezone")
    now = now.tz_convert("UTC")
    boundary = now.normalize() + pd.Timedelta(days=1)
    start = boundary + pd.Timedelta(days=30)
    end = start + pd.Timedelta(days=180)
    policy = dict(schema="strategy_review_forward/v1", registered_at=now.isoformat(),
        code_hash=code_hash, config_hash=config_hash, experiment_registry_hash=registry_hash,
        candidate="repaired_official_defaults_not_matrix_winner", observation_boundary=boundary.isoformat(),
        embargo_days=30, test_start=start.isoformat(), test_end_exclusive=end.isoformat(),
        mature_after=(end + pd.Timedelta(days=20)).isoformat(), minimum_cohorts=30,
        status="pending_unseen_evidence", opened_at=None, data_access_log=[], admission="paused_revalidation")
    policy["protocol_hash"] = hashlib.sha256(_json(policy).encode()).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(_json(policy) + "\n")
    return policy


def open_prospective(path, *, code_hash, config_hash, registry_hash, data_hash, labels_complete, now=None):
    """Claim final evaluation once; never makes a live admission decision."""
    path = Path(path)
    record = json.loads(path.read_text(encoding="utf-8"))
    validate_prospective(record)
    point = pd.Timestamp(now or datetime.now(timezone.utc))
    if point.tzinfo is None or point < pd.Timestamp(record["mature_after"]):
        raise PermissionError("Forward window and labels are not mature")
    if (code_hash, config_hash, registry_hash) != (record["code_hash"], record["config_hash"], record["experiment_registry_hash"]):
        raise PermissionError("Frozen identity mismatch")
    if labels_complete is not True or not isinstance(data_hash, str) or not data_hash.strip():
        raise PermissionError("Complete mature data evidence required")
    if record["status"] != "pending_unseen_evidence":
        raise PermissionError("Final evaluation already opened")
    # Exclusive receipt is the durable claim, including a crash before rewriting JSON.
    receipt = path.with_suffix(path.suffix + ".opened")
    with receipt.open("x", encoding="utf-8") as stream:
        stream.write(_json(dict(opened_at=point.isoformat(), data_hash=data_hash, protocol_hash=record["protocol_hash"])) + "\n")
    record.update(status="opened_for_final_evaluation", opened_at=point.isoformat(), data_hash=data_hash)
    record["data_access_log"].append(dict(purpose="final_evaluation", timestamp=point.isoformat(), data_hash=data_hash))
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(_json(record) + "\n", encoding="utf-8")
    temporary.replace(path)
    return record


def validate_prospective(record):
    """Verify the original registration before trusting its identity or dates.

    Opening adds only the documented mutable receipt fields.  Reconstructing
    the initial registration keeps old v1 protocols readable without accepting
    edits to their observation window, support or candidate identity.
    """
    if record.get("schema") != "strategy_review_forward/v1":
        raise ValueError("Unsupported prospective protocol schema")
    if record.get("status") == "pending_unseen_evidence":
        if record.get("opened_at") is not None or record.get("data_access_log") != [] or "data_hash" in record:
            raise ValueError("Pending protocol contains prior sample access evidence")
    elif record.get("status") == "opened_for_final_evaluation":
        log = record.get("data_access_log")
        if (not record.get("opened_at") or not record.get("data_hash")
                or not isinstance(log, list) or len(log) != 1
                or log[0] != dict(purpose="final_evaluation", timestamp=record["opened_at"],
                                 data_hash=record["data_hash"])):
            raise ValueError("Opened protocol access evidence is incomplete")
    else:
        raise ValueError("Unknown prospective protocol access status")
    original = dict(record)
    expected = original.pop("protocol_hash", None)
    original.pop("data_hash", None)
    original.update(status="pending_unseen_evidence", opened_at=None, data_access_log=[])
    actual = hashlib.sha256(_json(original).encode()).hexdigest()
    if expected != actual:
        raise ValueError("Prospective protocol registration hash mismatch")
    dates = [pd.Timestamp(record[key]) for key in (
        "registered_at", "observation_boundary", "test_start", "test_end_exclusive", "mature_after"
    )]
    if any(pd.isna(t) or t.tzinfo is None for t in dates) or not all(
        left < right for left, right in zip(dates, dates[1:])
    ):
        raise ValueError("Invalid prospective protocol time boundaries")
    if record["status"] == "opened_for_final_evaluation":
        opened = pd.Timestamp(record["opened_at"])
        if pd.isna(opened) or opened.tzinfo is None or opened < dates[-1]:
            raise ValueError("Prospective sample was opened before maturity")
    return record
