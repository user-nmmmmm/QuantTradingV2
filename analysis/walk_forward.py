"""以真实回测引擎执行滚动选参，返回窗口级证据与历史统计诊断。

每个可用窗口先为所有候选运行 [train_start, validation_end)，分别记录
train 和 validation 分数，只按 validation 选参；随后运行独立测试段。
这与 optimize 的单次训练/最终评价切分不同，但二者都属于历史研究。

候选必须由零参工厂创建，避免健康与冷却状态跨运行泄漏。每个候选使用
配置下限、声明历史和策略窗口共同确定的预热长度；前置历史不足的窗口
记录在 skipped_windows，不缩短预热。失败尝试也保留在研究登记中。

procedure 汇总各窗口获选者的测试收益，candidates 汇总每个候选的测试
收益供族级诊断。每窗重置为同一 initial_capital，持仓与资金不会跨窗
延续；拼接收益不代表一个连续实盘账户。窗口尾部遵循引擎的 terminal
policy：默认 mark_to_market 是无额外退出成本的估值转移，显式选择
forced_liquidation 或 valuation_only 时分别采用成本化退出或保留库存。

统计结果同时保留完整候选族、运行身份和依赖敏感性。历史上的时序选择
不能证明全局搜索完整或独立留出集通过，admission_eligible 始终为 False。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Sequence

import pandas as pd
import numpy as np

from analysis.research_validation import walk_forward_splits
from analysis.research_evidence import ResearchEvidenceRun, candidate_panel_evidence, invalidate_panel_evidence, sharpe_dependence_diagnostic
from backtest.engine import BacktestEngine
from core.logger import get_logger
from core.market_data import normalize_market_frame
from core.metrics import (
    benjamini_hochberg,
    bootstrap_return_distribution,
    calculate_equity_metrics,
    calculate_sharpe,
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
    bootstrap_block_length: int = 5
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
        if self.step is not None and self.step < self.test_size:
            raise ValueError("step must be at least test_size; overlapping test returns are forbidden")
        if self.bootstrap_block_length < 1:
            raise ValueError("bootstrap_block_length must be positive")


#: A candidate is a zero-argument factory returning a fresh strategy registry.
#: It must be a factory, not a registry: strategies carry health/cooldown state
#: across bars, and reusing one instance would let an earlier window's
#: lifecycle decide what a later window is allowed to trade.
CandidateFactory = Callable[[], Dict[str, Any]]


def candidate_warmup(build_strategies: CandidateFactory, minimum: int = 30) -> int:
    """Resolve declared lookbacks and conventional strategy window parameters."""
    required = max(minimum, int(getattr(build_strategies, "required_history_bars", 0)))
    for strategy in build_strategies().values():
        for name in ("required_history_bars", "entry_window", "exit_window", "window",
                     "lookback", "period", "rsi_period", "atr_period"):
            value = getattr(strategy, name, 0)
            value = value() if callable(value) else value
            if isinstance(value, (int, float)):
                required = max(required, int(value))
        stop_policy = getattr(strategy, "stop_policy", None)
        required = max(required, int(getattr(stop_policy, "atr_period", 0)))
    return required


def _concat_unique(parts: Sequence[pd.Series]) -> pd.Series:
    result = pd.concat(list(parts)) if parts else pd.Series(dtype=float)
    if result.index.has_duplicates or not result.index.is_monotonic_increasing:
        raise ValueError("out-of-sample returns must have unique increasing timestamps")
    return result


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
    start = pd.Timestamp(start).tz_convert("UTC").tz_localize(None) if pd.Timestamp(start).tzinfo else pd.Timestamp(start)
    end = pd.Timestamp(end).tz_convert("UTC").tz_localize(None) if pd.Timestamp(end).tzinfo else pd.Timestamp(end)
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


def _score(returns: pd.Series, metric: str, *, initial_time=None) -> Optional[float]:
    """The selection metric over one stretch of a window's return series."""
    if returns.empty:
        return None
    # Returns already contain the first period's P&L and costs. Reconstructing
    # equity without its opening capital drops that period a second time.
    if metric == "TotalReturn":
        value = (1.0 + returns).prod() - 1.0
    elif metric == "SharpeRatio":
        value = calculate_sharpe(returns, infer_periods_per_year(returns.index))["value"]
    else:
        # CAGR needs the actual opening equity timestamp. Infer it only for a
        # regular return clock; never invent a duration across irregular gaps.
        if initial_time is None:
            if infer_periods_per_year(returns.index) is None:
                return None
            initial_time = returns.index[0] - (returns.index[1] - returns.index[0])
        initial_time = pd.Timestamp(initial_time)
        if initial_time >= returns.index[0]:
            raise ValueError("opening equity time must precede the first return")
        equity = pd.concat([pd.Series([1.0], index=pd.DatetimeIndex([initial_time])),
                            (1.0 + returns).cumprod()])
        value = calculate_equity_metrics(equity.to_frame("equity")).get(metric)
    return None if value is None or not np.isfinite(float(value)) else float(value)


def _run_window(
    data_map: Mapping[str, pd.DataFrame],
    build_strategies: CandidateFactory,
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
    warmup_start: pd.Timestamp,
    config: WalkForwardConfig,
    warmup_period: Optional[int] = None,
) -> Dict[str, Any]:
    """One engine run whose routing begins exactly at ``start``."""
    start = pd.Timestamp(start).tz_convert("UTC").tz_localize(None) if pd.Timestamp(start).tzinfo else pd.Timestamp(start)
    end = pd.Timestamp(end).tz_convert("UTC").tz_localize(None) if pd.Timestamp(end).tzinfo else pd.Timestamp(end)
    actual_warmup = config.warmup_period if warmup_period is None else warmup_period
    window_data = _slice(data_map, warmup_start, end)
    for symbol, frame in window_data.items():
        if int((frame.index < start).sum()) < actual_warmup:
            raise ValueError(f"insufficient candidate warmup history for {symbol}: {actual_warmup} bars required")
    engine_kwargs = dict(config.engine_kwargs)
    # Windows return account returns and trade counts, neither of which uses
    # benchmarks. Keep an explicit caller override available for diagnostics.
    engine_kwargs.setdefault("calculate_benchmarks", False)
    engine = BacktestEngine(
        initial_capital=config.initial_capital,
        warmup_period=config.warmup_period if warmup_period is None else warmup_period,
        **engine_kwargs,
    )
    result = engine.run(
        window_data,
        strategies=build_strategies(),
        routing_log_enabled=False,
    )
    curve = result.get("equity_curve")
    if not isinstance(curve, pd.DataFrame) or curve.empty:
        return {"returns": pd.Series(dtype=float), "trades": 0}
    # The warmup prefix is real capital sitting flat; drop it so a window's
    # return series covers the window and nothing else.
    returns = _returns(curve)
    in_window = returns.loc[(returns.index >= start) & (returns.index <= end)]
    return {
        "returns": in_window,
        "trades": len(result.get("trades") or []),
    }


def run_walk_forward(
    data_map: Mapping[str, pd.DataFrame],
    candidates: Mapping[str, CandidateFactory],
    config: WalkForwardConfig,
    *, evidence_run: ResearchEvidenceRun | None = None,
) -> Dict[str, Any]:
    """Select per window on validation data, report on untouched test data."""
    if not candidates:
        raise ValueError("at least one candidate is required")
    if "__registered_walk_forward_selection_rule__" in candidates:
        raise ValueError("candidate name is reserved for the registered selection procedure")
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
    journal = evidence_run or ResearchEvidenceRun(candidates=names, parameters=asdict(config), data_map=data_map)
    failures = []
    warmups = {}
    for name in names:
        try:
            warmups[name] = candidate_warmup(candidates[name], config.warmup_period)
        except Exception as exc:
            warmups[name] = config.warmup_period
            failures.append(name)
            journal.record(candidate=name, phase="warmup", status="failed", error=repr(exc))
    required_warmup = max(warmups.values())
    windows: list[Dict[str, Any]] = []
    skipped: list[Dict[str, Any]] = []
    procedure_returns: list[pd.Series] = []
    pooled: Dict[str, list[pd.Series]] = {name: [] for name in names}

    def attempt(name, phase, window, **kwargs):
        journal.record(candidate=name, phase=phase, window=window, status="started",
                       start=str(kwargs["start"]), end=str(kwargs["end"]))
        try:
            result = _run_window(data_map, candidates[name], config=config,
                                 warmup_period=warmups[name], **kwargs)
            returns = result["returns"]
            if not isinstance(returns, pd.Series) or not np.isfinite(returns.to_numpy(dtype=float)).all():
                raise ValueError("candidate returned invalid/nonfinite observations")
            journal.record(candidate=name, phase=phase, window=window,
                           status="completed" if len(returns) else "abstained", rows=len(returns))
            return result
        except (KeyboardInterrupt, SystemExit) as exc:
            journal.record(candidate=name, phase=phase, window=window, status="interrupted", error=repr(exc))
            raise
        except Exception as exc:
            failures.append(name)
            journal.record(candidate=name, phase=phase, window=window, status="failed", error=repr(exc))
            return {"returns": pd.Series(dtype=float), "trades": 0, "failed": True}

    for index, split in enumerate(splits):
        selection_start = split["train_start"]
        if selection_start < required_warmup:
            # Shortening the warmup instead would make this window's
            # indicators differ from every other window's.
            skipped.append({
                "window": index, "reason": "insufficient_warmup_history",
                "required_bars": required_warmup,
                "available_bars": selection_start,
            })
            for name in names:
                journal.record(candidate=name, phase="window", window=index, status="abstained",
                               reason="insufficient_warmup_history")
            continue

        selection_scores: Dict[str, Dict[str, Any]] = {}
        for name in names:
            outcome = attempt(name, "selection", index,
                start=timeline[selection_start],
                end=timeline[split["validation_end"] - 1],
                warmup_start=timeline[selection_start - warmups[name]],
            )
            returns = outcome["returns"]
            validation_from = timeline[split["validation_start"]]
            selection_scores[name] = {
                "train_score": _score(
                    returns[returns.index < validation_from] if len(returns) else returns, config.selection_metric,
                    initial_time=timeline[selection_start - 1] if selection_start else None,
                ),
                "validation_score": _score(
                    returns[returns.index >= validation_from] if len(returns) else returns, config.selection_metric,
                    initial_time=timeline[split["validation_start"] - 1],
                ),
                "selection_trades": outcome["trades"],
                "failed": outcome.get("failed", False),
            }

        if any(row["failed"] for row in selection_scores.values()):
            skipped.append({"window": index, "reason": "candidate_failure_preserved_family_incomplete"})
            for name in names:
                journal.record(candidate=name, phase="test", window=index, status="abstained",
                               reason="selection_family_incomplete")
            continue

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
            for name in names:
                journal.record(candidate=name, phase="test", window=index, status="abstained",
                               reason="no_candidate_scored_on_validation")
            continue
        # Ties resolve to the first candidate in the caller's own order, which
        # is deterministic and does not depend on dict iteration luck.
        selected = max(scored, key=lambda item: (item[1], -names.index(item[0])))[0]
        journal.record(candidate=selected, phase="selection_decision", window=index, status="selected",
                       information_cutoff=str(timeline[split["validation_end"] - 1]),
                       test_start=str(timeline[split["test_start"]]), scores=selection_scores)
        train_best = max(
            ((name, selection_scores[name]["train_score"]) for name in names
             if selection_scores[name]["train_score"] is not None),
            key=lambda item: (item[1], -names.index(item[0])), default=(None, None),
        )[0]

        test_outcomes: Dict[str, Dict[str, Any]] = {}
        for name in names:
            outcome = attempt(name, "test", index,
                start=timeline[split["test_start"]],
                end=timeline[split["test_end"] - 1],
                # The selection window already required this much history and
                # sits entirely before the test window, so the prefix exists.
                warmup_start=timeline[split["test_start"] - warmups[name]],
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
            "candidate_warmup_bars": warmups,
            "test_scores": {
                name: _score(test_outcomes[name]["returns"], config.selection_metric,
                             initial_time=timeline[split["test_start"] - 1])
                for name in names
            },
            "test_return": _total_return(winner_returns),
            "test_trades": test_outcomes[selected]["trades"],
        })

    procedure = _concat_unique(procedure_returns)
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

    series = {name: _concat_unique(pooled[name]) for name in names}
    period_count = infer_periods_per_year(procedure.index) or 1.0
    family = candidate_panel_evidence(series, periods_per_year=period_count, registered_before_results=True)
    procedure_name = "__registered_walk_forward_selection_rule__"
    procedure_family = candidate_panel_evidence({**series, procedure_name: procedure}, periods_per_year=period_count,
                                               registered_before_results=True)
    procedure_dsr = procedure_family.get("dsr", {}).get(procedure_name,
        {"status": "insufficient", "probability": None})
    procedure_dsr["scope"] = "fixed candidate family plus the registered adaptive selection procedure; retrospective diagnostic"
    identity_check = journal.verify_identity(data_map)
    if failures or family["status"] == "invalid" or identity_check["identity_changed"]:
        correction.update(status="diagnostic_incomplete_family", reason="failed/incomplete candidates or changed identity",
                          diagnostic_rejected_count=correction.get("rejected_count", 0), rejected_count=0,
                          rejected=[False] * correction.get("sample_size", 0))
        for row in per_candidate.values():
            row["survives_fdr"] = False
        invalidate_panel_evidence(family, "candidate failures, incomplete panel or changed identity")
        family["failed_candidates"] = sorted(set(failures))
        procedure_dsr.update(status="invalid", probability=None, diagnostic_probability=None,
                             reason="candidate failures or incomplete common panel")
    for name in names:
        journal.record(candidate=name, phase="run", status="failed" if name in failures else
                       "completed" if len(series[name]) else "abstained", rows=len(series[name]))

    return {
        "selection_metric": config.selection_metric,
        "windows": windows,
        "skipped_windows": skipped,
        "candidates": per_candidate,
        "multiple_testing": correction,
        "candidate_family_evidence": family,
        "research_journal": journal.snapshot(),
        "identity_check": identity_check,
        "admission_eligible": False,
        "procedure": {
            "sample_size": int(len(procedure)),
            "total_return": _total_return(procedure),
            "mean_return": float(procedure.mean()) if len(procedure) else None,
            "bootstrap": bootstrap_return_distribution(
                procedure, n_samples=config.bootstrap_samples, seed=config.seed,
                block_length=config.bootstrap_block_length,
            ),
            "bootstrap_block_sensitivity": {str(block): bootstrap_return_distribution(procedure,
                n_samples=config.bootstrap_samples, seed=config.seed, block_length=block)
                for block in sorted({config.bootstrap_block_length, 5, 20, 60})},
            # Search inflation: the honest trial count is how many candidates
            # the selection step considered, not one.
            "deflated_sharpe": procedure_dsr,
            "sharpe_dependence": sharpe_dependence_diagnostic(procedure, periods_per_year=period_count),
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
    pooled = _concat_unique(parts)
    p_value = one_sided_bootstrap_p_value(
        pooled, n_samples=config.bootstrap_samples, seed=config.seed,
        block_length=config.bootstrap_block_length,
    )
    return {
        "name": name,
        "test_windows": len(parts),
        "sample_size": int(len(pooled)),
        "total_return": _total_return(pooled),
        "mean_return": float(pooled.mean()) if len(pooled) else None,
        "p_value": p_value["p_value"],
        "p_value_status": p_value["status"],
        "resampling": p_value.get("resampling"),
        "diagnostic_p_value": p_value.get("diagnostic_p_value"),
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
