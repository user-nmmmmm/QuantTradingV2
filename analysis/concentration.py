"""Research-side concentration evidence: does the result survive losing its best trades?

A trend-following result can be positive overall while a handful of trades, one
calendar year or one symbol carries all of it. This module reports the numbers a
pre-registered rule needs to judge that (R5 of the backtest research programme):
what is left after removing the N most profitable trades, the best calendar year
and the best symbol.

It reports numbers and their statuses only. Thresholds and verdicts belong to the
frozen protocol that calls it, and nothing here feeds an admission decision.

Conventions follow ``core.metrics``: pure functions, no input mutation, ``None``
with a status instead of NaN or infinity, and an explicit ``sample_size``.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from core.analytics.diagnostics import calculate_pnl_concentration
from core.metrics.trade_quality import calculate_profit_factor

CONCENTRATION_SCHEMA = "concentration-evidence/v1"


def _profit_factor(values: np.ndarray, minimum_samples: int) -> Dict[str, Any]:
    result = calculate_profit_factor(values.tolist(), minimum_samples=minimum_samples)
    return {"value": result["value"], "status": result["status"], "sample_size": result["sample_size"]}


def _excluding_top_trades(values: np.ndarray, top_n: Sequence[int], minimum_samples: int) -> Dict[str, Any]:
    """Net PnL and profit factor of what is left without the N most profitable trades."""
    ordered = np.sort(values)[::-1]
    total = float(values.sum())
    out: Dict[str, Any] = {}
    for count in top_n:
        count = int(count)
        if count <= 0:
            raise ValueError("top_n entries must be positive")
        if count >= len(ordered):
            out[str(count)] = {"status": "insufficient", "removed": count, "net_pnl_excluding": None,
                               "share_of_total": None, "profit_factor_excluding": None,
                               "reason": "not more trades than the number removed"}
            continue
        removed, rest = ordered[:count], ordered[count:]
        out[str(count)] = {
            "status": "ok",
            "removed": count,
            "removed_net_pnl": float(removed.sum()),
            "net_pnl_excluding": float(rest.sum()),
            # Against a zero or negative total a share would flip sign and mean nothing.
            "share_of_total": float(removed.sum() / total) if total > 0 else None,
            "profit_factor_excluding": _profit_factor(rest, minimum_samples),
        }
    return out


def _excluding_best_group(records: List[Mapping[str, Any]], key: str, label: str) -> Dict[str, Any]:
    """Leave out the group (calendar year or symbol) with the largest net PnL."""
    groups: Dict[Any, float] = {}
    for record in records:
        group = record.get(key)
        if group is None:
            return {"status": "insufficient", "reason": f"a trade has no {label}"}
        groups[group] = groups.get(group, 0.0) + float(record["net_pnl"])
    if len(groups) < 2:
        return {"status": "insufficient", "reason": f"fewer than two {label}s", "groups": len(groups)}
    total = sum(groups.values())
    gross_profit = sum(value for value in groups.values() if value > 0)
    best = max(sorted(groups, key=str), key=lambda group: groups[group])
    return {
        "status": "ok",
        "groups": len(groups),
        "best": best,
        "best_net_pnl": groups[best],
        "net_pnl_excluding_best": total - groups[best],
        # Share of the profitable groups' total: the rule "no single symbol above X of the profit".
        "best_share_of_gross_profit": (groups[best] / gross_profit) if gross_profit > 0 and groups[best] > 0 else None,
        "by_group": {str(group): groups[group] for group in sorted(groups, key=str)},
    }


def _exit_year(value: Any) -> Optional[int]:
    if value is None:
        return None
    stamp = pd.Timestamp(value)
    return None if pd.isna(stamp) else int(stamp.year)


def concentration_evidence(
    trades: Iterable[Mapping[str, Any]],
    *,
    top_n: Sequence[int] = (5, 10),
    minimum_samples: int = 30,
) -> Dict[str, Any]:
    """Concentration numbers for closed round-trip trades.

    Each record needs ``net_pnl``. ``exit_time`` enables the leave-one-year section and
    ``symbol`` the leave-one-symbol section; when any trade lacks the field that section is
    ``insufficient`` and the others are unaffected. Records without ``net_pnl`` are ignored
    and counted in ``skipped``.

    ``status`` is ``ok`` only with at least ``minimum_samples`` trades; below that the
    numbers are still reported, as a profit factor is, and must not be read as evidence.
    """
    if isinstance(minimum_samples, bool) or minimum_samples < 1:
        raise ValueError("minimum_samples must be a positive integer")
    supplied = list(trades)
    records = [record for record in supplied if record.get("net_pnl") is not None]
    values = np.asarray([float(record["net_pnl"]) for record in records], dtype=float)
    base: Dict[str, Any] = {"schema": CONCENTRATION_SCHEMA, "sample_size": int(len(values)),
                            "skipped": len(supplied) - len(records), "minimum_samples": int(minimum_samples)}
    if not np.isfinite(values).all():
        return {**base, "status": "invalid_input", "reason": "net_pnl must be finite"}
    if len(values) == 0:
        return {**base, "status": "insufficient", "total_net_pnl": None, "profit_factor": None,
                "profit_hhi": None, "excluding_top_trades": {}, "excluding_best_year": {"status": "insufficient"},
                "excluding_best_symbol": {"status": "insufficient"}}
    by_year = [{"net_pnl": record["net_pnl"], "year": _exit_year(record.get("exit_time"))} for record in records]
    return {
        **base,
        "status": "ok" if len(values) >= minimum_samples else "insufficient",
        "total_net_pnl": float(values.sum()),
        "profit_factor": _profit_factor(values, minimum_samples),
        "profit_hhi": calculate_pnl_concentration(records, top_n=())["profit_hhi"],
        "excluding_top_trades": _excluding_top_trades(values, top_n, minimum_samples),
        "excluding_best_year": _excluding_best_group(by_year, "year", "exit year"),
        "excluding_best_symbol": _excluding_best_group(records, "symbol", "symbol"),
    }
