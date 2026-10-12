"""Mandatory event logging and second-source market-data audit utilities."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence

import pandas as pd

from core.events import EventCodec, EventEnvelope


def write_event_log(
    events: Iterable[EventEnvelope], path: str | Path
) -> Dict[str, Any]:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    materialized = tuple(events)
    with target.open("w", encoding="utf-8", newline="") as handle:
        for event in materialized:
            handle.write(EventCodec.encode(event))
            handle.write("\n")
    counts = Counter(event.event_type for event in materialized)
    return {
        "path": target.name,
        "events": len(materialized),
        "event_type_counts": dict(sorted(counts.items())),
    }


def validate_audit_coverage(
    *,
    event_summary: Mapping[str, Any],
    routing_log_path: Optional[str | Path],
    routing_required: bool,
    trade_count: int,
    close_count: int,
) -> Dict[str, Any]:
    counts = dict(event_summary.get("event_type_counts") or {})
    missing = []
    if trade_count:
        for event_type in ("signal", "risk_decision", "order_intent", "order", "fill"):
            if not counts.get(event_type):
                missing.append(event_type)
    if close_count and not counts.get("close"):
        missing.append("close")
    routing_path = Path(routing_log_path) if routing_log_path else None
    routing_ok = bool(
        not routing_required
        or (routing_path is not None and routing_path.exists() and routing_path.stat().st_size > 0)
    )
    if not routing_ok:
        missing.append("routing")
    return {
        "status": "ok" if not missing else "failed",
        "missing_event_types": missing,
        "routing_log_present": routing_ok,
        "trade_count": int(trade_count),
        "close_count": int(close_count),
    }


def _bar(frame: pd.DataFrame, timestamp: Any) -> Optional[pd.Series]:
    if frame is None or frame.empty:
        return None
    point = pd.Timestamp(timestamp)
    if point in frame.index:
        value = frame.loc[point]
        return value.iloc[-1] if isinstance(value, pd.DataFrame) else value
    return None


def cross_verify_top_trades(
    closed_trades: Sequence[Mapping[str, Any]],
    primary_data: Mapping[str, pd.DataFrame],
    secondary_data: Optional[Mapping[str, pd.DataFrame]],
    *,
    top_n: int = 20,
    tolerance_bps: float = 10.0,
) -> Dict[str, Any]:
    """Verify entry/exit OHLC for the largest winners and losers.

    No network is performed here.  A caller must supply an independently
    sourced data map; absence is reported explicitly instead of silently
    treating the primary feed as its own verifier.
    """

    if top_n <= 0:
        raise ValueError("top_n must be positive")
    if tolerance_bps < 0:
        raise ValueError("tolerance_bps cannot be negative")
    if not secondary_data:
        return {
            "status": "unverified",
            "reason": "secondary_data_not_supplied",
            "requested_top_winners": top_n,
            "requested_top_losers": top_n,
            "checked": 0,
            "passed": 0,
            "failed": 0,
            "records": [],
        }

    ordered = sorted(closed_trades, key=lambda item: float(item.get("net_pnl", 0.0)))
    selected = ordered[:top_n] + list(reversed(ordered[-top_n:]))
    unique = []
    seen = set()
    for trade in selected:
        key = (
            trade.get("lot_id"), trade.get("symbol"),
            str(trade.get("entry_time")), str(trade.get("exit_time")),
        )
        if key not in seen:
            unique.append(trade)
            seen.add(key)

    records = []
    limit = tolerance_bps / 10000.0
    for trade in unique:
        symbol = str(trade.get("symbol"))
        comparisons = []
        for label in ("entry_time", "exit_time"):
            timestamp = trade.get(label)
            primary = _bar(primary_data.get(symbol), timestamp)
            secondary = _bar(secondary_data.get(symbol), timestamp)
            if primary is None or secondary is None:
                comparisons.append({
                    "point": label, "timestamp": timestamp, "status": "missing",
                })
                continue
            deviations = {}
            for column in ("open", "high", "low", "close"):
                left = float(primary[column])
                right = float(secondary[column])
                deviations[column] = None if right == 0 else abs(left / right - 1.0)
            passed = all(value is not None and value <= limit for value in deviations.values())
            comparisons.append({
                "point": label,
                "timestamp": timestamp,
                "status": "passed" if passed else "failed",
                "deviation_bps": {
                    key: None if value is None else value * 10000.0
                    for key, value in deviations.items()
                },
            })
        passed = bool(comparisons) and all(item["status"] == "passed" for item in comparisons)
        records.append({
            "symbol": symbol,
            "lot_id": trade.get("lot_id"),
            "net_pnl": trade.get("net_pnl"),
            "status": "passed" if passed else "failed",
            "comparisons": comparisons,
        })

    passed_count = sum(item["status"] == "passed" for item in records)
    return {
        "status": "passed" if records and passed_count == len(records) else "failed",
        "tolerance_bps": tolerance_bps,
        "requested_top_winners": top_n,
        "requested_top_losers": top_n,
        "checked": len(records),
        "passed": passed_count,
        "failed": len(records) - passed_count,
        "records": records,
    }


def write_json_report(path: str | Path, value: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n",
        encoding="utf-8",
        newline="",
    )


__all__ = [
    "cross_verify_top_trades",
    "validate_audit_coverage",
    "write_event_log",
    "write_json_report",
]
