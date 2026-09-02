"""Walk-forward parameter selection that actually re-runs the engine.

``core/metrics/validation.py`` and ``analysis/research_validation.py`` already
carry the statistics — window boundaries, purge/embargo gaps, deflated Sharpe,
FDR correction — but every one of them operates on a return series someone
else produced. Nothing bound them to a backtest, so the project's only
"out-of-sample" path (``analysis/optimize.py --oos``) ranked every candidate on
the **full** sample and only afterwards split the winner's equity curve. The
test segment had already been seen by the selection step, which is the exact
leak an OOS test exists to rule out.

This module closes that loop. For every walk-forward split it runs the real
:class:`~backtest.engine.BacktestEngine` twice per candidate:

1. once over ``[train_start, validation_end)`` — the selection sample. The
   resulting return series is cut at ``validation_start`` so the train and
   validation scores are reported separately and selection uses only the
   validation half, the part sitting immediately before the purge gap.
2. once over ``[test_start, test_end)`` — untouched by selection.

Two things come out of it. The **procedure** result stitches the per-window
winners' test returns into one series: that is what a trader running this
selection rule would have earned, and it is the number to quote. The
**per-candidate** results pool each candidate's test returns across every
window, which is what gives ``benjamini_hochberg`` the N hypotheses a search
over N candidates actually tested — ``optimize.py`` was passing it an empty
list, so the correction was a no-op while 16 combinations were being tried.

Conventions worth knowing before reading a result
-------------------------------------------------
*Warmup.* Each run is fed ``warmup_period`` bars of history before its window
starts and told to warm up for exactly that many, so routing begins on the
window's first bar rather than partway through it. Windows too close to the
start of the data to afford that prefix are skipped and reported in
``skipped_windows`` instead of being silently shortened.

*Capital.* Every window starts from the same ``initial_capital``. Compounding
across windows would make the aggregate a story about whichever window came
first; independent windows keep each period's return comparable and let the
pooled series be treated as a sample.

*Costs.* Windows are non-overlapping in time, so stitching their returns
implicitly assumes the book is flat at each boundary. The engine closes tail
positions at the end of every run (``EndOfBacktest``), so that assumption is
enforced rather than assumed, at the cost of one forced exit per window.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

import pandas as pd

from analysis.research_validation import deflated_sharpe_ratio, walk_forward_splits
from backtest.engine import BacktestEngine
from core.logger import get_logger
from core.market_data import normalize_market_frame
from core.metrics import (
    benjamini_hochberg,
    bootstrap_return_distribution,
    calculate_equity_metrics,
    infer_periods_per_year,
    one_sided_bootstrap_p_value,
)

logger = get_logger(__name__)

#: Metric keys from ``calculate_equity_metrics`` that may drive selection.
#: Restricted deliberately: selecting on a key that is ``None`` on short
#: windows (or one where larger is worse) silently randomises the choice.
SELECTION_METRICS = ("SharpeRatio", "TotalReturn", "CAGR")


@dataclass(frozen=True)
class WalkForwardConfig:
    """Window geometry and the statistics applied to the pooled result."""

    train_size: int
    validation_size: int
    test_size: int
    purge_size: int = 0
    embargo_size: int = 0
    step: Optional[int] = None
    expanding: bool = False
    warmup_period: int = 30
    initial_capital: float = 10_000.0
    selection_metric: str = "SharpeRatio"
    bootstrap_samples: int = 2000
    seed: int = 42
    fdr: float = 0.05
    engine_kwargs: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.selection_metric not in SELECTION_METRICS:
            raise ValueError(
                f"selection_metric must be one of {SELECTION_METRICS}"
            )
        if self.warmup_period < 0:
            raise ValueError("warmup_period cannot be negative")
        if self.initial_capital <= 0:
            raise ValueError("initial_capital must be positive")


#: A candidate is a zero-argument factory returning a fresh strategy registry.
#: It must be a factory, not a registry: strategies carry health/cooldown state
#: across bars, and reusing one instance would let an earlier window's
#: lifecycle decide what a later window is allowed to trade.
CandidateFactory = Callable[[], Dict[str, Any]]


def common_timeline(data_map: Mapping[str, pd.DataFrame]) -> pd.DatetimeIndex:
    """The union timeline window positions are counted against.

    Matches ``HistoricalMarketDataAdapter``'s ``alignment_mode="union"``, so a
    position here means the same bar the engine will iterate.
    """
    timeline = pd.DatetimeIndex([])
    for frame in data_map.values():
        prepared = normalize_market_frame(frame)
        if prepared.empty:
            continue
        timeline = timeline.union(prepared.index)
    return timeline.sort_values()


def _slice(
    data_map: Mapping[str, pd.DataFrame], start: pd.Timestamp, end: pd.Timestamp,
) -> Dict[str, pd.DataFrame]:
    """Every symbol's bars in ``[start, end]``, dropping symbols with none."""
    sliced: Dict[str, pd.DataFrame] = {}
    for symbol, frame in data_map.items():
        prepared = normalize_market_frame(frame)
        if prepared.empty:
            continue
        part = prepared.loc[(prepared.index >= start) & (prepared.index <= end)]
        if not part.empty:
            sliced[symbol] = part
    return sliced


def _returns(equity_curve: pd.DataFrame) -> pd.Series:
    if not isinstance(equity_curve, pd.DataFrame) or equity_curve.empty:
        return pd.Series(dtype=float)
    return equity_curve["equity"].pct_change(fill_method=None).dropna()


def _score(returns: pd.Series, metric: str) -> Optional[float]:
    """The selection metric over one stretch of a window's return series."""
    if returns.empty:
        return None
    equity = (1.0 + returns).cumprod()
    metrics = calculate_equity_metrics(equity.to_frame("equity"))
    value = metrics.get(metric)
    return None if value is None else float(value)


def _run_window(
    data_map: Mapping[str, pd.DataFrame],
    build_strategies: CandidateFactory,
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
    warmup_start: pd.Timestamp,
    config: WalkForwardConfig,
) -> Dict[str, Any]:
    """One engine run whose routing begins exactly at ``start``."""
    engine = BacktestEngine(
        initial_capital=config.initial_capital,
        warmup_period=config.warmup_period,
        **dict(config.engine_kwargs),
    )
    result = engine.run(
        _slice(data_map, warmup_start, end),
        strategies=build_strategies(),
        routing_log_enabled=False,
    )
    curve = result.get("equity_curve")
    if not isinstance(curve, pd.DataFrame) or curve.empty:
        return {"returns": pd.Series(dtype=float), "trades": 0}
    # The warmup prefix is real capital sitting flat; drop it so a window's
    # return series covers the window and nothing else.
    in_window = curve.loc[curve.index >= start]
    return {
        "returns": _returns(in_window),
        "trades": len(result.get("trades") or []),
    }


def run_walk_forward(
    data_map: Mapping[str, pd.DataFrame],
    candidates: Mapping[str, CandidateFactory],
    config: WalkForwardConfig,
) -> Dict[str, Any]:
    """Select per window on validation data, report on untouched test data."""
    if not candidates:
        raise ValueError("at least one candidate is required")
    timeline = common_timeline(data_map)
    if len(timeline) == 0:
        raise ValueError("data_map produced an empty timeline")

    splits = walk_forward_splits(
        len(timeline),
        train_size=config.train_size,
        validation_size=config.validation_size,
        test_size=config.test_size,
        purge_size=config.purge_size,
        embargo_size=config.embargo_size,
        step=config.step,
        expanding=config.expanding,
    )
    names = list(candidates)
    windows: list[Dict[str, Any]] = []
    skipped: list[Dict[str, Any]] = []
    procedure_returns: list[pd.Series] = []
    pooled: Dict[str, list[pd.Series]] = {name: [] for name in names}

    for index, split in enumerate(splits):
        selection_start = split["train_start"]
        if selection_start < config.warmup_period:
            # Shortening the warmup instead would make this window's
            # indicators differ from every other window's.
            skipped.append({
                "window": index, "reason": "insufficient_warmup_history",
                "required_bars": config.warmup_period,
                "available_bars": selection_start,
            })
            continue

        selection_scores: Dict[str, Dict[str, Any]] = {}
        for name in names:
            outcome = _run_window(
                data_map, candidates[name],
                start=timeline[selection_start],
                end=timeline[split["validation_end"] - 1],
                warmup_start=timeline[selection_start - config.warmup_period],
                config=config,
            )
            returns = outcome["returns"]
            validation_from = timeline[split["validation_start"]]
            selection_scores[name] = {
                "train_score": _score(
                    returns[returns.index < validation_from], config.selection_metric
                ),
                "validation_score": _score(
                    returns[returns.index >= validation_from], config.selection_metric
                ),
                "selection_trades": outcome["trades"],
            }

        ranked = [
            (name, selection_scores[name]["validation_score"]) for name in names
        ]
        scored = [(name, score) for name, score in ranked if score is not None]
        if not scored:
            # Every candidate was flat (or produced no variance) across the
            # validation half, so the selection rule has nothing to choose on.
            # Picking anyway would be picking by dict order.
            skipped.append({
                "window": index, "reason": "no_candidate_scored_on_validation",
                "validation_start": str(timeline[split["validation_start"]]),
            })
            continue
        # Ties resolve to the first candidate in the caller's own order, which
        # is deterministic and does not depend on dict iteration luck.
        selected = max(scored, key=lambda item: (item[1], -names.index(item[0])))[0]
        train_best = max(
            ((name, selection_scores[name]["train_score"]) for name in names
             if selection_scores[name]["train_score"] is not None),
            key=lambda item: (item[1], -names.index(item[0])), default=(None, None),
        )[0]

        test_outcomes: Dict[str, Dict[str, Any]] = {}
        for name in names:
            outcome = _run_window(
                data_map, candidates[name],
                start=timeline[split["test_start"]],
                end=timeline[split["test_end"] - 1],
                # The selection window already required this much history and
                # sits entirely before the test window, so the prefix exists.
                warmup_start=timeline[split["test_start"] - config.warmup_period],
                config=config,
            )
            test_outcomes[name] = outcome
            if not outcome["returns"].empty:
                pooled[name].append(outcome["returns"])

        winner_returns = test_outcomes[selected]["returns"]
        if not winner_returns.empty:
            procedure_returns.append(winner_returns)

        windows.append({
            "window": index,
            "train_start": str(timeline[split["train_start"]]),
            "validation_start": str(timeline[split["validation_start"]]),
            "test_start": str(timeline[split["test_start"]]),
            "test_end": str(timeline[split["test_end"] - 1]),
            "purged_bars": split["test_start"] - split["validation_end"],
            "selected": selected,
            # A selection that flips depending on which half you score on is
            # a selection the data does not support.
            "train_best": train_best,
            "selection_agrees": train_best == selected,
            "scores": selection_scores,
            "test_scores": {
                name: _score(test_outcomes[name]["returns"], config.selection_metric)
                for name in names
            },
            "test_return": _total_return(winner_returns),
            "test_trades": test_outcomes[selected]["trades"],
        })

    procedure = pd.concat(procedure_returns) if procedure_returns else pd.Series(
        dtype=float
    )
    per_candidate = {
        name: _candidate_summary(name, pooled[name], config) for name in names
    }
    ordered = [per_candidate[name]["p_value"] for name in names]
    testable = [value for value in ordered if value is not None]
    correction = (
        benjamini_hochberg(testable, fdr=config.fdr)
        if testable else {"status": "insufficient", "sample_size": 0,
                          "adjusted_p_values": [], "rejected": [],
                          "rejected_count": 0}
    )
    _attach_correction(per_candidate, names, correction)

    return {
        "selection_metric": config.selection_metric,
        "windows": windows,
        "skipped_windows": skipped,
        "candidates": per_candidate,
        "multiple_testing": correction,
        "procedure": {
            "sample_size": int(len(procedure)),
            "total_return": _total_return(procedure),
            "mean_return": float(procedure.mean()) if len(procedure) else None,
            "bootstrap": bootstrap_return_distribution(
                procedure, n_samples=config.bootstrap_samples, seed=config.seed
            ),
            # Search inflation: the honest trial count is how many candidates
            # the selection step considered, not one.
            "deflated_sharpe": deflated_sharpe_ratio(
                procedure,
                trials=len(names),
                periods_per_year=infer_periods_per_year(procedure.index) or 1.0,
            ),
            "selection_stability": _stability(windows),
        },
    }


def _total_return(returns: pd.Series) -> Optional[float]:
    if returns.empty:
        return None
    return float((1.0 + returns).prod() - 1.0)


def _candidate_summary(
    name: str, parts: Sequence[pd.Series], config: WalkForwardConfig,
) -> Dict[str, Any]:
    """One candidate's pooled out-of-sample record across every test window."""
    pooled = pd.concat(list(parts)) if parts else pd.Series(dtype=float)
    p_value = one_sided_bootstrap_p_value(
        pooled, n_samples=config.bootstrap_samples, seed=config.seed
    )
    return {
        "name": name,
        "test_windows": len(parts),
        "sample_size": int(len(pooled)),
        "total_return": _total_return(pooled),
        "mean_return": float(pooled.mean()) if len(pooled) else None,
        "p_value": p_value["p_value"],
        "p_value_status": p_value["status"],
    }


def _attach_correction(
    per_candidate: Dict[str, Dict[str, Any]],
    names: Sequence[str],
    correction: Mapping[str, Any],
) -> None:
    """Write each candidate's adjusted p-value back onto its own row.

    Only candidates that produced a p-value entered the correction, so the
    adjusted values are consumed in that same order; the rest are marked
    ``not_tested`` rather than being given a neighbour's number.
    """
    adjusted = list(correction.get("adjusted_p_values") or [])
    rejected = list(correction.get("rejected") or [])
    cursor = 0
    for name in names:
        row = per_candidate[name]
        if row["p_value"] is None or cursor >= len(adjusted):
            row["adjusted_p_value"] = None
            row["survives_fdr"] = None
            continue
        row["adjusted_p_value"] = adjusted[cursor]
        row["survives_fdr"] = bool(rejected[cursor]) if cursor < len(rejected) else None
        cursor += 1


def _stability(windows: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """How often the selection rule changed its mind, and whether halves agree."""
    if not windows:
        return {"status": "insufficient", "sample_size": 0}
    selections = [window["selected"] for window in windows]
    switches = sum(
        1 for previous, current in zip(selections, selections[1:])
        if previous != current
    )
    agreements = [bool(window["selection_agrees"]) for window in windows]
    return {
        "status": "ok",
        "sample_size": len(windows),
        "distinct_selections": len(set(selections)),
        "selection_switches": switches,
        "train_validation_agreement": sum(agreements) / len(agreements),
        "positive_windows": sum(
            1 for window in windows
            if (window["test_return"] or 0.0) > 0
        ),
    }


__all__ = [
    "CandidateFactory", "SELECTION_METRICS", "WalkForwardConfig",
    "common_timeline", "run_walk_forward",
]
