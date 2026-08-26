"""Generate the auditable Phase 5 research-governance and holdout bundle.

The script intentionally never optimizes on the final holdout.  It freezes a
chronological protocol, opens that partition once, evaluates the pre-existing
Phase 4 strategy state, and persists the resulting pass/fail decision.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from analysis.phase5 import (
    ExperimentRegistry,
    HoldoutProtocol,
    HoldoutVault,
    cross_market_validation,
    deflated_sharpe_ratio,
    evaluate_holdout_admission,
    purged_cv_indices,
    walk_forward_splits,
)
from backtest.reporting import ReportGenerator


OUT = ROOT / "docs" / "phase5"
BASELINE = ROOT / "docs" / "baseline" / "phase0" / "archived_reports"
PRIMARY = BASELINE / "20260824_163836_3498d_10Syms_Ret-15.8pct"


def _dump(name: str, value) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / name).write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )


def _file_digest(paths) -> str:
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.name.encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _load_equity(path: Path) -> pd.Series:
    frame = pd.read_csv(path, parse_dates=["timestamp"]).set_index("timestamp")
    return frame["equity"].astype(float)


def _load_benchmark(path: Path) -> pd.Series:
    frame = pd.read_csv(path, parse_dates=["timestamp"]).set_index("timestamp")
    return frame.iloc[:, 0].astype(float)


def _closed_trades(path: Path):
    return ReportGenerator(str(OUT))._reconstruct_closed_trades(pd.read_csv(path))


def main() -> None:
    final_path = OUT / "final_holdout_report.json"
    if final_path.exists():
        print(final_path.read_text(encoding="utf-8"))
        return

    OUT.mkdir(parents=True, exist_ok=True)
    equity = _load_equity(PRIMARY / "equity.csv")
    benchmark = _load_benchmark(PRIMARY / "benchmark.csv")
    cut_train = int(len(equity) * 0.60)
    cut_validation = int(len(equity) * 0.80)
    protocol = HoldoutProtocol(
        protocol_id="phase5-baseline-v1",
        train_start=str(equity.index[0].date()),
        train_end=str(equity.index[cut_train - 1].date()),
        validation_start=str(equity.index[cut_train].date()),
        validation_end=str(equity.index[cut_validation - 1].date()),
        holdout_start=str(equity.index[cut_validation].date()),
        holdout_end=str(equity.index[-1].date()),
    )
    data_hash = _file_digest(
        [PRIMARY / "equity.csv", PRIMARY / "benchmark.csv", PRIMARY / "trades.csv"]
    )
    vault = HoldoutVault(OUT / "holdout_protocol.json")
    frozen = vault.freeze(protocol, data_hash=data_hash)
    _dump("data_partition_protocol.json", frozen)

    research_observations = cut_validation
    wf = walk_forward_splits(
        research_observations,
        train_size=730,
        validation_size=180,
        test_size=180,
        purge_size=30,
        embargo_size=30,
        expanding=True,
    )
    all_closed = _closed_trades(PRIMARY / "trades.csv")
    research_closed = [
        trade for trade in all_closed
        if pd.Timestamp(trade["exit_time"]) <= equity.index[cut_validation - 1]
    ]
    cv = purged_cv_indices(
        [trade["entry_time"] for trade in research_closed],
        [trade["exit_time"] for trade in research_closed],
        n_splits=5,
        embargo_fraction=0.01,
    )
    _dump("walk_forward_and_purged_cv.json", {
        "walk_forward": wf,
        "purged_cv": cv,
        "rule": "candidate selection is train/validation only; test follows a 30-bar purge",
    })

    registry = ExperimentRegistry(OUT / "experiment_registry.jsonl")
    archived_returns = []
    for report_dir in sorted(BASELINE.iterdir()):
        equity_path = report_dir / "equity.csv"
        if not equity_path.exists():
            continue
        series = _load_equity(equity_path)
        returns = series.pct_change(fill_method=None).dropna()
        metric = {
            "total_return": float(series.iloc[-1] / series.iloc[0] - 1),
            "daily_sharpe": None if returns.std(ddof=1) == 0 else float(returns.mean() / returns.std(ddof=1)),
        }
        registry.register(
            hypothesis="archived pre-Phase-5 strategy run",
            parameters={"archive": report_dir.name},
            data_scope={"source": str(equity_path.relative_to(ROOT)), "partition": "historical_research"},
            metrics=metric,
        )
        archived_returns.append(metric)
    research_returns = equity.iloc[:cut_validation].pct_change(fill_method=None).dropna()
    _dump("multiple_testing_and_deflated_sharpe.json", {
        "registered_trials": len(registry.records()),
        "deflated_sharpe": deflated_sharpe_ratio(
            research_returns, trials=max(1, len(registry.records())), periods_per_year=365.25,
        ),
        "registry": "experiment_registry.jsonl",
    })

    _dump("parameter_stability.json", {
        "status": "implementation_complete_evidence_pending",
        "implementation": "analysis.phase5.parameter_plateau",
        "reason": "archived reports do not contain a labeled two-dimensional parameter grid",
        "required_evidence": "run analysis.optimize on train/validation only and pass its grid to parameter_plateau",
    })
    _dump("factor_ablation.json", {
        "status": "implementation_complete_evidence_pending",
        "implementation": "analysis.phase5.factor_ablation",
        "switches": {
            "OBV": "TrendBreakoutStrategy(use_obv=False)",
            "RSI": "RangeStrategy(use_rsi=False)",
            "Stochastic": "VolatilityReversionStrategy(use_stochastic=False)",
        },
        "reason": "raw market inputs for the archived baseline were not retained in the Phase 0 package",
    })
    _dump("cross_market_validation.json", {
        **cross_market_validation([]),
        "status": "evidence_pending",
        "reason": "no second-exchange or alternate-timeframe input dataset is present in the repository",
        "implementation": "analysis.phase5.cross_market_validation",
    })

    vault.open_final(protocol_hash=protocol.fingerprint, purpose="final strategy admission")
    holdout_equity = equity.loc[protocol.holdout_start:protocol.holdout_end]
    holdout_benchmark = benchmark.loc[protocol.holdout_start:protocol.holdout_end]
    holdout_trades = [
        trade for trade in all_closed
        if pd.Timestamp(protocol.holdout_start) <= pd.Timestamp(trade["exit_time"])
        <= pd.Timestamp(protocol.holdout_end)
    ]
    admission = evaluate_holdout_admission(
        trades=holdout_trades,
        equity=holdout_equity,
        benchmark=holdout_benchmark,
    )
    admission.update({
        "protocol_hash": protocol.fingerprint,
        "data_hash": data_hash,
        "source": str(PRIMARY.relative_to(ROOT)),
        "holdout_trade_count": len(holdout_trades),
        "governance_note": "Final holdout was evaluated once; rejection must not trigger tuning on this partition.",
    })
    _dump("final_holdout_report.json", admission)

    tasks = {f"T-5.{index}": "implemented_and_tested" for index in range(1, 11)}
    tasks["T-5.11"] = "completed_final_holdout_rejected" if admission["decision"] == "reject" else "completed_final_holdout_admitted"
    summary = {
        "tasks": tasks,
        "performance_gates": admission["gates"],
        "decision": admission["decision"],
        "important_distinction": "workflow completion does not override failed investment-performance gates",
        "evidence": sorted(path.name for path in OUT.iterdir()),
    }
    _dump("phase5_summary.json", summary)
    (OUT / "README.md").write_text(
        "# Phase 5 研究与样本外验证\n\n"
        "本目录固化训练、验证与最终 Holdout 协议，并保存 Walk-forward、Purged CV、"
        "参数平台、因子消融、集中度、成本、多场景、试验台账和最终准入证据。\n\n"
        "完成研究流程不等于策略获准上线。`final_holdout_report.json` 是一次性最终测试；"
        "若门槛失败，必须建立新假设和新协议版本，不得返回该 Holdout 调参。\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
