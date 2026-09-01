"""Phase 4 routing, portfolio allocation, and research analytics."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from enum import Enum
from typing import Any, Iterable, Mapping, Optional, Sequence

import pandas as pd

from core.logger import get_logger
from core.risk.portfolio_governor import PortfolioRiskGovernor

logger = get_logger(__name__)


class TransitionAction(str, Enum):
    STOP_NEW_ENTRIES = "stop_new_entries"
    REDUCE = "reduce"
    FLATTEN = "flatten"


@dataclass(frozen=True)
class EntryCandidate:
    symbol: str
    strategy: Any
    bar_index: int
    frame: pd.DataFrame
    state: Any
    signal: Mapping[str, Any]
    score: float

    @property
    def strategy_name(self) -> str:
        return str(self.strategy.name)


@dataclass(frozen=True)
class AllocationDecision:
    symbol: str
    strategy: str
    score: float
    rank: int
    accepted: bool
    reason: str
    #: SR3-1: how the rank was actually decided. ``score`` means the scores
    #: separated the candidates; ``tie_break_alphabetical`` means they did not
    #: and the deterministic name order settled it - which is a diagnostic,
    #: never a silent Alpha ordering.
    ordering: str = "score"
    #: SR3-2: what the correlated-risk budget did to this candidate.
    risk_budget: Optional[Mapping[str, Any]] = None


class PortfolioSignalAllocator:
    """Rank all same-timestamp signals before any of them reserves risk.

    SR3-1 (STR-P1-03): every candidate used to carry ``score=0``, so when
    capital ran out the surviving order was the alphabetical tie-break -
    an undisclosed "BTC before ETH" allocation rule masquerading as ranking.
    Scores are now real (see ``core.candidate_scoring``), and a batch that
    still ties is reported as ``ordering=tie_break_alphabetical`` instead of
    passing silently.

    SR3-2: the ranked batch is then metered by a
    :class:`~core.risk.portfolio_governor.PortfolioRiskGovernor`, so one session's
    entries share a correlated-risk budget rather than each claiming the full
    per-trade risk independently.
    """

    def __init__(self, risk_governor: Optional[PortfolioRiskGovernor] = None) -> None:
        self.audit: list[AllocationDecision] = []
        self.risk_governor = risk_governor or PortfolioRiskGovernor()
        self.degenerate_batches = 0

    @staticmethod
    def rank(candidates: Iterable[EntryCandidate]) -> list[EntryCandidate]:
        return sorted(
            candidates,
            key=lambda item: (-float(item.score), item.strategy_name, item.symbol),
        )

    def allocate(self, candidates: Iterable[EntryCandidate], *, portfolio: Any,
                 broker: Any, risk_manager: Any,
                 current_prices: Mapping[str, float]) -> list[AllocationDecision]:
        ordered = self.rank(candidates)
        scores = {round(float(item.score), 12) for item in ordered}
        degenerate = len(ordered) > 1 and len(scores) == 1
        if degenerate:
            self.degenerate_batches += 1
            logger.warning(
                "Allocation ranking is degenerate: %d candidates share score "
                "%s, so the order is the deterministic name tie-break, not an "
                "alpha ranking (STR-P1-03): %s",
                len(ordered), next(iter(scores)),
                ", ".join(f"{item.strategy_name}:{item.symbol}" for item in ordered),
            )
        ordering = "tie_break_alphabetical" if degenerate else "score"
        session = None
        for item in ordered:
            try:
                session = item.frame.index[item.bar_index]
            except (AttributeError, IndexError, TypeError):
                session = item.bar_index
            break
        self.risk_governor.begin_session(session)
        decisions = []
        for rank, candidate in enumerate(ordered, start=1):
            result = candidate.strategy.submit_entry_candidate(
                candidate, portfolio=portfolio, broker=broker,
                risk_manager=risk_manager, current_prices=dict(current_prices),
                risk_governor=self.risk_governor,
            )
            accepted = bool(getattr(result, "accepted", False))
            budget = getattr(result, "risk_budget", None)
            decision = AllocationDecision(
                candidate.symbol, candidate.strategy_name, float(candidate.score),
                rank, accepted,
                "accepted" if accepted else "risk_or_execution_rejected",
                ordering=ordering,
                risk_budget=budget,
            )
            decisions.append(decision)
            self.audit.append(decision)
        return decisions


def state_duration_and_transition_matrix(states: Sequence[Any]) -> dict[str, Any]:
    names = [getattr(value, "name", str(value)) for value in states]
    durations: dict[str, list[int]] = defaultdict(list)
    transitions: Counter[tuple[str, str]] = Counter()
    if not names:
        return {"durations": {}, "transition_matrix": {}, "switches": 0}
    current, length = names[0], 1
    for previous, value in zip(names, names[1:]):
        if value == previous:
            length += 1
        else:
            durations[current].append(length)
            transitions[(previous, value)] += 1
            current, length = value, 1
    durations[current].append(length)
    matrix: dict[str, dict[str, int]] = defaultdict(dict)
    for (source, target), count in sorted(transitions.items()):
        matrix[source][target] = count
    return {"durations": dict(durations), "transition_matrix": dict(matrix),
            "switches": int(sum(transitions.values()))}


def joint_entry_exit_attribution(closed_trades: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    cells: dict[str, dict[str, dict[str, float | int]]] = defaultdict(dict)
    sample_size, total_pnl = 0, 0.0
    for trade in closed_trades:
        entry = str(trade.get("strategy") or "Unknown")
        controller = str(trade.get("exit_strategy") or "Unknown")
        cell = cells[entry].setdefault(controller, {"trades": 0, "net_pnl": 0.0})
        cell["trades"] = int(cell["trades"]) + 1
        pnl = float(trade.get("net_pnl") or 0.0)
        cell["net_pnl"] = float(cell["net_pnl"]) + pnl
        sample_size += 1
        total_pnl += pnl
    return {"sample_size": sample_size, "total_net_pnl": total_pnl,
            "matrix": {key: dict(value) for key, value in sorted(cells.items())}}


def holding_period_audit(closed_trades: Iterable[Mapping[str, Any]], *,
                         max_holding_days: Optional[float] = None) -> dict[str, Any]:
    records = []
    for trade in closed_trades:
        start = pd.to_datetime(trade.get("entry_time"), utc=True, errors="coerce")
        end = pd.to_datetime(trade.get("exit_time"), utc=True, errors="coerce")
        if pd.isna(start) or pd.isna(end):
            continue
        records.append({"symbol": trade.get("symbol"),
                        "days": float((end - start).total_seconds() / 86400.0)})
    values = pd.Series([item["days"] for item in records], dtype=float)
    if values.empty:
        return {"sample_size": 0, "max_holding_days": None, "timeouts": []}
    threshold = float(max_holding_days) if max_holding_days is not None else None
    return {"sample_size": len(records), "max_holding_days": float(values.max()),
            "p95_holding_days": float(values.quantile(0.95)),
            "median_holding_days": float(values.median()),
            "timeouts": [item for item in records
                         if threshold is not None and item["days"] >= threshold]}
