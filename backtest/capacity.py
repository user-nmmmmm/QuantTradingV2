"""Capital-capacity curve runner and explanation report (Phase 3 / T-3.11)."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, Mapping, Optional

import pandas as pd

from backtest.engine import BacktestEngine


@dataclass(frozen=True)
class CapacityPoint:
    initial_capital: float
    final_equity: float
    total_return: float
    fill_count: int
    unique_order_count: int
    rejected_order_count: int
    partial_fill_count: int
    rejection_rate: float
    average_participation_rate: float
    total_commission: float
    total_slippage_cost: float
    total_impact_cost: float
    total_financing_cost: float
    trade_path_signature: str
    rejection_reasons: Dict[str, int]
    explanation: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _trade_path_signature(trades: list[Mapping[str, Any]]) -> str:
    # Human-auditable deterministic signature rather than an opaque hash.
    counts = Counter((str(t.get("symbol")), str(t.get("side"))) for t in trades)
    return "|".join(
        f"{symbol}:{side}:{count}"
        for (symbol, side), count in sorted(counts.items())
    ) or "no_fills"


def _point_from_result(initial_capital: float, result: Mapping[str, Any]) -> CapacityPoint:
    trades = list(result.get("trades") or [])
    audit = list(result.get("execution_audit") or [])
    equity_curve = result.get("equity_curve")
    if isinstance(equity_curve, pd.DataFrame) and not equity_curve.empty:
        final_equity = float(equity_curve["equity"].iloc[-1])
    else:
        final_equity = float(initial_capital)
    rejected = [row for row in audit if row.get("outcome") == "rejected"]
    partial = [row for row in audit if row.get("outcome") == "partial_fill"]
    order_ids = {str(row.get("order_id")) for row in audit if row.get("order_id")}
    rejection_reasons = Counter(str(row.get("reason", "unknown")) for row in rejected)
    participations = [
        float(t.get("participation_rate", 0.0))
        for t in trades
        if t.get("participation_rate") is not None
    ]
    commission = sum(float(t.get("commission", 0.0)) for t in trades)
    slippage_cost = sum(
        float((t.get("costs") or {}).get("slippage", 0.0)) for t in trades
    )
    impact_cost = sum(
        float((t.get("costs") or {}).get("impact", 0.0)) for t in trades
    )
    financing = sum(
        float(row.get("amount", 0.0))
        for row in (result.get("financing_ledger") or [])
    )
    unique_orders = len(order_ids)
    rejection_rate = len(rejected) / unique_orders if unique_orders else 0.0
    reasons = dict(sorted(rejection_reasons.items()))
    explanation_parts = []
    if partial:
        explanation_parts.append(
            f"{len(partial)} partial fills were caused by the configured participation cap"
        )
    if reasons:
        explanation_parts.append(
            "rejections were explicitly attributed to "
            + ", ".join(f"{key}={value}" for key, value in reasons.items())
        )
    if impact_cost > 0:
        explanation_parts.append(
            f"non-linear market impact contributed {impact_cost:.2f} of cost"
        )
    if not explanation_parts:
        explanation_parts.append("no capacity constraint changed the executed path")
    return CapacityPoint(
        initial_capital=float(initial_capital),
        final_equity=final_equity,
        total_return=(final_equity / initial_capital - 1.0) if initial_capital else 0.0,
        fill_count=len(trades),
        unique_order_count=unique_orders,
        rejected_order_count=len(rejected),
        partial_fill_count=len(partial),
        rejection_rate=rejection_rate,
        average_participation_rate=(
            sum(participations) / len(participations) if participations else 0.0
        ),
        total_commission=commission,
        total_slippage_cost=slippage_cost,
        total_impact_cost=impact_cost,
        total_financing_cost=financing,
        trade_path_signature=_trade_path_signature(trades),
        rejection_reasons=reasons,
        explanation="; ".join(explanation_parts),
    )


def run_capacity_curve(
    data_map: Mapping[str, pd.DataFrame],
    *,
    capital_levels: Iterable[float] = (10_000, 100_000, 1_000_000, 10_000_000),
    engine_kwargs: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Run identical inputs at each capital level and explain every path change."""
    levels = [float(level) for level in capital_levels]
    if not levels or any(level <= 0 for level in levels):
        raise ValueError("capital_levels must contain positive values")
    if levels != sorted(set(levels)):
        raise ValueError("capital_levels must be unique and ascending")
    kwargs = dict(engine_kwargs or {})
    points = []
    for level in levels:
        engine = BacktestEngine(initial_capital=level, **kwargs)
        result = engine.run(dict(data_map), routing_log_enabled=False)
        points.append(_point_from_result(level, result))

    base_signature = points[0].trade_path_signature
    path_changes = []
    for point in points[1:]:
        if point.trade_path_signature != base_signature:
            path_changes.append({
                "initial_capital": point.initial_capital,
                "baseline_signature": base_signature,
                "observed_signature": point.trade_path_signature,
                "explanation": point.explanation,
                "explained": bool(
                    point.partial_fill_count
                    or point.rejected_order_count
                    or point.total_impact_cost > 0
                ),
            })
    return_inflections = []
    for previous, point in zip(points, points[1:]):
        delta = point.total_return - previous.total_return
        if abs(delta) < 0.001:
            continue
        return_inflections.append({
            "from_capital": previous.initial_capital,
            "to_capital": point.initial_capital,
            "return_change": delta,
            "fill_count_change": point.fill_count - previous.fill_count,
            "partial_fill_count_change": (
                point.partial_fill_count - previous.partial_fill_count
            ),
            "rejection_rate_change": (
                point.rejection_rate - previous.rejection_rate
            ),
            "impact_cost_change": (
                point.total_impact_cost - previous.total_impact_cost
            ),
            "explanation": point.explanation,
        })
    return {
        "capital_levels": levels,
        "points": [point.to_dict() for point in points],
        "path_changes": path_changes,
        "return_inflections": return_inflections,
        "all_path_changes_explained": all(
            row["explained"] for row in path_changes
        ),
    }
