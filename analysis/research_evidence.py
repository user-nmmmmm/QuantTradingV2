"""Shared retrospective evidence: identities, all-attempt journals and diagnostics.

Nothing here certifies an unseen holdout, complete historic search or admission.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

import numpy as np
import pandas as pd

from core.reproducibility import canonical_json, sha256_bytes, sha256_file, sha256_frame


def source_identity(root=None):
    """对研究相关源码、配置和依赖锁计算内容哈希，不依赖 Git 跟踪状态。

    这是当前工作树的有限范围身份；不包含整个仓库，也不证明历史搜索完整。
    """
    root = Path(root or Path(__file__).resolve().parents[1])
    paths = set()
    for directory in ("analysis", "backtest", "composition", "config", "core", "router", "scripts", "strategies"):
        paths.update(p for p in (root / directory).rglob("*")
                     if p.is_file() and p.suffix in {".py", ".yaml", ".yml", ".json"})
    paths.update(root / name for name in ("main.py", "run_live.py", "requirements.lock.txt")
                 if (root / name).is_file())
    return {p.relative_to(root).as_posix(): sha256_file(p) for p in sorted(paths)}


class ResearchEvidenceRun:
    """先登记候选、输入和源码身份，再逐次记录调用方报告的执行状态。

    指定 ``output`` 时必须使用新目录，登记写入 registration.json，状态追加
    到 attempts.jsonl；不指定时只保存在内存。登记覆盖本次运行，不能补齐过去
    的搜索记录，也不会把已看过的数据变成独立留出集。
    """
    def __init__(self, *, candidates, parameters, data_map, output=None, source_hashes=None):
        self.output = Path(output) if output is not None else None
        self._verify_local_sources = source_hashes is None
        self.registration = {
            "schema": "research-evidence/v1", "registered_at": datetime.now(timezone.utc).isoformat(),
            "candidates": list(candidates), "parameters": parameters,
            "data_hashes": {name: sha256_frame(frame) if isinstance(frame, pd.DataFrame)
                            else sha256_bytes(canonical_json(frame).encode()) for name, frame in data_map.items()},
            "source_hashes": source_identity() if source_hashes is None else source_hashes,
            "historical_global_search_complete": False, "independent_holdout": False,
            "interpretation": "new local registration does not reconstruct earlier search history",
        }
        self.identity = sha256_bytes(canonical_json(self.registration).encode())
        self.events = []
        if self.output:
            self.output.mkdir(parents=True, exist_ok=False)
            (self.output / "registration.json").write_text(canonical_json(self.registration), encoding="utf-8")
        for candidate in candidates:
            self.record(candidate=candidate, phase="run", status="planned")

    def record(self, **event):
        row = {"sequence": len(self.events), "recorded_at": datetime.now(timezone.utc).isoformat(),
               "identity": self.identity, **event}
        self.events.append(row)
        if self.output:
            with (self.output / "attempts.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(canonical_json(row) + "\n")
        return row

    def snapshot(self):
        return {"registration": self.registration, "identity": self.identity,
                "events": self.events, "historical_global_search_complete": False}

    def verify_identity(self, data_map):
        """核对输入及本地源码是否变化，并把核对结果追加到本次日志。

        外部传入的 ``source_hashes`` 只作为身份记录，本方法不验证其来源；
        此时 ``source_unchanged=None``，不能按 True 解读。
        """
        current_data = {name: sha256_frame(frame) if isinstance(frame, pd.DataFrame)
                        else sha256_bytes(canonical_json(frame).encode()) for name, frame in data_map.items()}
        source_match = source_identity() == self.registration["source_hashes"] if self._verify_local_sources else None
        result = {"data_unchanged": current_data == self.registration["data_hashes"],
                  "source_unchanged": source_match,
                  "source_verification": "local_worktree" if self._verify_local_sources else "external_identity_not_verified"}
        result["identity_changed"] = not result["data_unchanged"] or source_match is False
        self.record(phase="identity", status="changed" if result["identity_changed"] else "checked", **result)
        return result


def sharpe_dependence_diagnostic(returns, *, periods_per_year=365.0, max_lag=None):
    """Bartlett/Newey-West long-run variance sensitivity; no profit probability."""
    values = np.asarray(returns, dtype=float)
    base = {"status": "insufficient", "admission_eligible": False,
            "periods_per_year": float(periods_per_year), "probability": None,
            "annualization": "iid sqrt(periods_per_year) reference and HAC long-run-variance sensitivity"}
    if not np.isfinite(periods_per_year) or periods_per_year <= 0:
        raise ValueError("positive finite periods_per_year required")
    if values.ndim != 1 or not np.isfinite(values).all():
        return {**base, "status": "invalid", "reason": "finite one-dimensional returns required"}
    if len(values) < 10 or np.std(values, ddof=1) == 0:
        return base
    lag = min(len(values) - 1, int(len(values) ** (1 / 3)) if max_lag is None else int(max_lag))
    if lag < 0:
        raise ValueError("max_lag must be nonnegative")
    centered = values - values.mean()
    variance = float(np.dot(centered, centered) / len(values))
    long_run = variance + 2 * sum((1 - k / (lag + 1)) * float(np.dot(centered[k:], centered[:-k]) / len(values))
                                  for k in range(1, lag + 1))
    return {**base, "status": "diagnostic", "observations": len(values), "max_lag": lag,
            "period_sharpe": float(values.mean() / values.std(ddof=1)),
            "iid_annualized_sharpe": float(values.mean() / values.std(ddof=1) * np.sqrt(periods_per_year)),
            "hac_annualized_sharpe": float(values.mean() / np.sqrt(long_run) * np.sqrt(periods_per_year)) if long_run > 0 else None,
            "long_run_variance": long_run, "period_variance": variance,
            "mean_standard_error_hac": float(np.sqrt(long_run / len(values))) if long_run > 0 else None,
            "interpretation": "stationarity and bandwidth assumptions; not an independent-observation certificate"}


def invalidate_panel_evidence(evidence, reason):
    """Invalidate every attached statistic when run identity/family checks fail."""
    evidence.update(status="invalid", reason=reason, probability=None)
    for row in evidence.get("dsr", {}).values():
        row.update(status="invalid", reason=reason, probability=None, diagnostic_probability=None)
    evidence["pbo"].update(status="invalid", reason=reason, probability=None, diagnostic_probability=None)
    for row in evidence["reality_check"].values():
        row.update(status="invalid", reason=reason, p_value=None, diagnostic_p_value=None)
    return evidence


def candidate_panel_evidence(series_by_candidate: Mapping, *, periods_per_year=365.0,
                             registered_before_results=False, benchmark_returns=None,
                             bootstrap_iterations=500, block_lengths=(5, 20, 60),
                             pbo_groups=8, seed=42):
    """在严格共同时间轴上计算 DSR、PBO 和联合块重采样诊断。

    候选必须逐期对齐，不取日期交集、不补零、不删除失败候选。默认基准为
    零收益现金。PBO 仅使用长度可整除 ``pbo_groups`` 的前缀，并公开被排除
    的尾部日期；DSR 和 Reality Check 保留完整时间轴。

    本次结果前登记不证明历史全局搜索完整，因此输出始终保留诊断口径，
    不产生交易准入结论。
    """
    from analysis.paper_validation import cscv_pbo, deflated_sharpe_evidence, white_reality_check
    if (isinstance(bootstrap_iterations, bool) or not isinstance(bootstrap_iterations, int)
            or bootstrap_iterations < 100):
        raise ValueError("bootstrap_iterations must be an integer >= 100")
    if isinstance(pbo_groups, bool) or not isinstance(pbo_groups, int) or pbo_groups < 2 or pbo_groups % 2:
        raise ValueError("pbo_groups must be a positive even integer >= 2")
    blocks = tuple(block_lengths)
    if (not blocks or len(set(blocks)) != len(blocks)
            or any(isinstance(b, bool) or not isinstance(b, int) or b < 1 for b in blocks)):
        raise ValueError("unique positive integer block lengths required")
    names = list(series_by_candidate)
    protocol = {"schema": "candidate-family-statistics/v2", "periods_per_year": periods_per_year,
                "pbo_groups": pbo_groups, "pbo_axis_policy": "leading_multiple_of_groups",
                "pbo_tail_policy": "exclude trailing remainder from PBO only; report dates and count",
                "reality_check": "joint circular block bootstrap; shared draws across every candidate",
                "block_lengths": list(blocks), "bootstrap_iterations": bootstrap_iterations, "seed": seed,
                "benchmark": "cash_zero_return" if benchmark_returns is None else "supplied_same_axis_benchmark",
                "family_complete": False, "registered_before_results": bool(registered_before_results)}
    base = {"status": "invalid", "candidate_ids": names, "probability": None,
            "historical_global_search_complete": False, "admission_eligible": False, "protocol": protocol,
            "pbo": {"status": "invalid", "probability": None, "diagnostic_probability": None,
                    "admission_eligible": False},
            "reality_check": {str(block): {"status": "invalid", "p_value": None,
                "diagnostic_p_value": None, "admission_eligible": False} for block in blocks}}
    if not names:
        return {**base, "reason": "empty candidate family", "dsr": {}}
    reference = series_by_candidate[names[0]]
    reference_index = reference.index if isinstance(reference, pd.Series) else None
    errors = []
    for name, series in series_by_candidate.items():
        try:
            valid = (isinstance(name, str) and bool(name.strip()) and isinstance(series, pd.Series)
                     and isinstance(series.index, pd.DatetimeIndex) and not series.empty
                     and series.index.equals(reference_index) and not series.index.has_duplicates
                     and series.index.is_monotonic_increasing and not series.index.hasnans
                     and np.isfinite(series.to_numpy(dtype=float)).all())
        except (TypeError, ValueError):
            valid = False
        if not valid:
            errors.append(name)
    if errors:
        return {**base, "reason": "incomplete, invalid or asynchronous family; no intersection or zero fill",
                "invalid_candidates": errors, "dsr": {name: {"status": "invalid", "probability": None} for name in names}}
    panel = pd.DataFrame(series_by_candidate)
    benchmark = pd.Series(0., index=panel.index, name="cash_zero_return") if benchmark_returns is None else benchmark_returns
    try:
        valid_benchmark = (isinstance(benchmark, pd.Series) and benchmark.index.equals(panel.index)
                           and np.isfinite(benchmark.to_numpy(dtype=float)).all())
    except (TypeError, ValueError):
        valid_benchmark = False
    if not valid_benchmark:
        return {**base, "reason": "benchmark must be finite and exactly match the full candidate axis",
                "dsr": {name: {"status": "invalid", "probability": None} for name in names}}
    used = len(panel) // pbo_groups * pbo_groups
    pbo = (cscv_pbo(panel.iloc[:used], n_groups=pbo_groups, family_complete=False) if used else
           {"status": "insufficient", "reason": "fewer observations than registered PBO groups",
            "probability": None, "diagnostic_probability": None, "admission_eligible": False,
            "candidate_ids": names})
    pbo["axis_protocol"] = {"policy": "leading_multiple_of_groups", "full_observations": len(panel),
                            "used_observations": used, "excluded_tail_count": len(panel) - used,
                            "used_start": panel.index[0].isoformat() if used else None,
                            "used_end": panel.index[used - 1].isoformat() if used else None,
                            "excluded_tail_timestamps": [t.isoformat() for t in panel.index[used:]]}
    return {**base, "status": "diagnostic", "observations": len(panel),
            "panel": {"timestamps": [t.isoformat() for t in panel.index],
                      "returns": {name: panel[name].tolist() for name in names}},
            "panel_sha256": sha256_frame(panel),
            "benchmark": {"kind": protocol["benchmark"], "name": benchmark.name,
                          "sha256": sha256_frame(benchmark.to_frame("benchmark")),
                          "returns": benchmark.tolist()},
            "pbo": pbo,
            "reality_check": {str(block): white_reality_check(panel, benchmark,
                family_complete=False, iterations=bootstrap_iterations, block_length=block,
                bootstrap="circular", seed=seed) for block in blocks},
            "dsr": {name: deflated_sharpe_evidence(panel, name, periods_per_year=periods_per_year,
                      historical_trials_complete=False, registered_before_results=registered_before_results) for name in names}}
