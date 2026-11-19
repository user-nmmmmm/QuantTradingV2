"""Shared retrospective evidence: identities, all-attempt journals and diagnostics.

Nothing here certifies an unseen holdout, complete historic search or admission.
"""

from __future__ import annotations

from copy import deepcopy
from core.foundation.reproducibility import sha256_file as sha256_file
from core.foundation.source_bytes import (
    _IGNORED_SOURCE_DIRECTORIES as _IGNORED_SOURCE_DIRECTORIES,
    _SOURCE_DIRECTORIES as _SOURCE_DIRECTORIES,
    _SOURCE_SUFFIXES as _SOURCE_SUFFIXES,
    _capture_sources as _capture_sources,
    _source_files as _source_files,
    _source_path as _source_path,
    _source_walk_error as _source_walk_error,
    source_identity as source_identity,
)
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

import numpy as np
import pandas as pd

from core.foundation.reproducibility import canonical_json, sha256_bytes, sha256_frame
from core.foundation.data_identity import (
    EXACT_DATA_IDENTITY,
    LEGACY_DATA_IDENTITY,
    data_hashes,
    sha256_frame_exact,
)


class ResearchEvidenceRun:
    """先登记候选、输入和源码身份，再逐次记录调用方报告的执行状态。

    指定 ``output`` 时必须使用新目录，登记写入 registration.json，状态追加
    到 attempts.jsonl；不指定时只保存在内存。登记覆盖本次运行，不能补齐过去
    的搜索记录，也不会把已看过的数据变成独立留出集。
    """

    def __init__(
        self,
        *,
        candidates,
        parameters,
        data_map,
        output=None,
        source_hashes=None,
        source_root=None,
        source_paths=None,
        data_identity_version=LEGACY_DATA_IDENTITY,
    ):
        self._data_identity_version = data_identity_version
        initial_data_hashes = data_hashes(data_map, version=data_identity_version)
        self.output = Path(output) if output is not None else None
        self._source_root = Path(source_root or Path(__file__).resolve().parents[1]).resolve()
        if isinstance(source_paths, (str, bytes, Path)):
            raise ValueError("source paths must be a sequence, not a single path")
        self._source_paths = (
            None
            if source_paths is None
            else tuple(
                _source_path(self._source_root, p).relative_to(self._source_root).as_posix()
                for p in source_paths
            )
        )
        if source_hashes is not None and source_paths is not None and source_root is None:
            raise ValueError("source_root is required to verify an external manifest")
        self._verify_local_sources = source_hashes is None or source_root is not None
        if source_hashes is not None and source_root is not None and self._source_paths is None:
            self._source_paths = tuple(source_hashes)
        local_sources = (
            source_identity(self._source_root, paths=self._source_paths)
            if self._verify_local_sources
            else None
        )
        if (
            source_hashes is not None
            and self._verify_local_sources
            and local_sources != source_hashes
        ):
            raise ValueError("source manifest does not match local bytes or declared closure")
        scope = {
            "policy": (
                "external_manifest/v1"
                if not self._verify_local_sources
                else "explicit_paths/v1"
                if self._source_paths is not None
                else "default_research_closure/v2"
            ),
            "paths": None if self._source_paths is None else list(self._source_paths),
            "verification": (
                "local_worktree" if self._verify_local_sources else "external_identity_not_verified"
            ),
        }
        self.registration = {
            "schema": "research-evidence/v1",
            "registered_at": datetime.now(timezone.utc).isoformat(),
            "candidates": list(candidates),
            "parameters": deepcopy(parameters),
            "data_hashes": initial_data_hashes,
            "data_identity_version": data_identity_version,
            "source_hashes": local_sources if source_hashes is None else deepcopy(source_hashes),
            "source_scope": scope,
            "historical_global_search_complete": False,
            "independent_holdout": False,
            "interpretation": "new local registration does not reconstruct earlier search history",
        }
        self.identity = sha256_bytes(canonical_json(self.registration).encode())
        self.events = []
        if self.output:
            self.output.mkdir(parents=True, exist_ok=False)
            (self.output / "registration.json").write_text(
                canonical_json(self.registration), encoding="utf-8"
            )
        for candidate in self.registration["candidates"]:
            self.record(candidate=candidate, phase="run", status="planned")

    def record(self, **event):
        row = {
            "sequence": len(self.events),
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "identity": self.identity,
            **event,
        }
        self.events.append(row)
        if self.output:
            with (self.output / "attempts.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(canonical_json(row) + "\n")
        return row

    def snapshot(self):
        return {
            "registration": self.registration,
            "identity": self.identity,
            "events": self.events,
            "historical_global_search_complete": False,
        }

    def verify_identity(self, data_map):
        """Verify inputs and the declared local byte closure, then journal it.

        Legacy externally supplied hashes without an explicit ``source_root``
        remain unverified (``source_unchanged=None``). Directory scopes detect
        added/deleted files; exact file manifests only certify their own scope.
        """
        version = self._data_identity_version
        data_error = None
        try:
            current_data = data_hashes(data_map, version=version)
        except (TypeError, ValueError) as exc:
            current_data = None
            data_error = f"{type(exc).__name__}: {exc}"
        policy_match = version == self.registration.get(
            "data_identity_version", LEGACY_DATA_IDENTITY
        )
        source_match = None
        source_error = None
        source_changes = {"added": [], "deleted": [], "modified": []}
        if self._verify_local_sources:
            try:
                current_sources = _capture_sources(
                    self._source_root, self._source_paths, allow_missing=True
                )
                original_sources = self.registration["source_hashes"]
                source_changes = {
                    "added": sorted(current_sources.keys() - original_sources.keys()),
                    "deleted": sorted(original_sources.keys() - current_sources.keys()),
                    "modified": sorted(
                        p
                        for p in current_sources.keys() & original_sources.keys()
                        if current_sources[p] != original_sources[p]
                    ),
                }
                source_match = current_sources == original_sources
            except (OSError, ValueError) as exc:
                source_match = False
                source_error = f"{type(exc).__name__}: {exc}"
        result = {
            "data_unchanged": policy_match and current_data == self.registration["data_hashes"],
            "data_identity_version": version,
            "data_identity_policy_unchanged": policy_match,
            "data_error": data_error,
            "source_unchanged": source_match,
            "source_changes": source_changes,
            "source_error": source_error,
            "source_verification": "local_worktree"
            if self._verify_local_sources
            else "external_identity_not_verified",
        }
        result["identity_changed"] = not result["data_unchanged"] or source_match is False
        self.record(
            phase="identity",
            status="changed" if result["identity_changed"] else "checked",
            **result,
        )
        return result


def sharpe_dependence_diagnostic(returns, *, periods_per_year=365.0, max_lag=None):
    """Bartlett/Newey-West long-run variance sensitivity; no profit probability."""
    values = np.asarray(returns, dtype=float)
    base = {
        "status": "insufficient",
        "admission_eligible": False,
        "periods_per_year": float(periods_per_year),
        "probability": None,
        "annualization": "iid sqrt(periods_per_year) reference and HAC long-run-variance sensitivity",
    }
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
    long_run = variance + 2 * sum(
        (1 - k / (lag + 1)) * float(np.dot(centered[k:], centered[:-k]) / len(values))
        for k in range(1, lag + 1)
    )
    return {
        **base,
        "status": "diagnostic",
        "observations": len(values),
        "max_lag": lag,
        "period_sharpe": float(values.mean() / values.std(ddof=1)),
        "iid_annualized_sharpe": float(
            values.mean() / values.std(ddof=1) * np.sqrt(periods_per_year)
        ),
        "hac_annualized_sharpe": float(
            values.mean() / np.sqrt(long_run) * np.sqrt(periods_per_year)
        )
        if long_run > 0
        else None,
        "long_run_variance": long_run,
        "period_variance": variance,
        "mean_standard_error_hac": float(np.sqrt(long_run / len(values))) if long_run > 0 else None,
        "interpretation": "stationarity and bandwidth assumptions; not an independent-observation certificate",
    }


def invalidate_panel_evidence(evidence, reason):
    """Invalidate every attached statistic when run identity/family checks fail."""
    evidence.update(status="invalid", reason=reason, probability=None)
    for row in evidence.get("dsr", {}).values():
        row.update(status="invalid", reason=reason, probability=None, diagnostic_probability=None)
    evidence["pbo"].update(
        status="invalid", reason=reason, probability=None, diagnostic_probability=None
    )
    for row in evidence["reality_check"].values():
        row.update(status="invalid", reason=reason, p_value=None, diagnostic_p_value=None)
    return evidence


def _validate_panel_settings(bootstrap_iterations, pbo_groups, block_lengths, bootstrap_method):
    if (
        isinstance(bootstrap_iterations, bool)
        or not isinstance(bootstrap_iterations, int)
        or bootstrap_iterations < 100
    ):
        raise ValueError("bootstrap_iterations must be an integer >= 100")
    if (
        isinstance(pbo_groups, bool)
        or not isinstance(pbo_groups, int)
        or pbo_groups < 2
        or pbo_groups % 2
    ):
        raise ValueError("pbo_groups must be a positive even integer >= 2")
    blocks = tuple(block_lengths)
    if (
        not blocks
        or len(set(blocks)) != len(blocks)
        or any(isinstance(b, bool) or not isinstance(b, int) or b < 1 for b in blocks)
    ):
        raise ValueError("unique positive integer block lengths required")
    if bootstrap_method not in {"stationary", "circular"}:
        raise ValueError("supported bootstrap_method required")
    return blocks


def _candidate_panel_context(
    series_by_candidate,
    periods_per_year,
    pbo_groups,
    blocks,
    bootstrap_iterations,
    seed,
    benchmark_returns,
    local_family_complete,
    registered_before_results,
    bootstrap_method,
):
    complete = local_family_complete is True
    names = list(series_by_candidate)
    protocol = {
        "schema": "candidate-family-statistics/v2",
        "periods_per_year": periods_per_year,
        "pbo_groups": pbo_groups,
        "pbo_axis_policy": "leading_multiple_of_groups",
        "pbo_tail_policy": "exclude trailing remainder from PBO only; report dates and count",
        "reality_check": f"joint {bootstrap_method} block bootstrap; shared draws across every candidate",
        "block_lengths": list(blocks),
        "bootstrap_iterations": bootstrap_iterations,
        "seed": seed,
        "benchmark": "cash_zero_return"
        if benchmark_returns is None
        else "supplied_same_axis_benchmark",
        "family_complete": complete,
        "registered_before_results": bool(registered_before_results),
    }
    base = {
        "status": "invalid",
        "candidate_ids": names,
        "probability": None,
        "historical_global_search_complete": False,
        "admission_eligible": False,
        "protocol": protocol,
        "pbo": {
            "status": "invalid",
            "probability": None,
            "diagnostic_probability": None,
            "admission_eligible": False,
        },
        "reality_check": {
            str(block): {
                "status": "invalid",
                "p_value": None,
                "diagnostic_p_value": None,
                "admission_eligible": False,
            }
            for block in blocks
        },
    }
    return names, protocol, base


def _candidate_panel_errors(series_by_candidate, names):
    reference = series_by_candidate[names[0]]
    reference_index = reference.index if isinstance(reference, pd.Series) else None
    errors = []
    for name, series in series_by_candidate.items():
        try:
            valid = (
                isinstance(name, str)
                and bool(name.strip())
                and isinstance(series, pd.Series)
                and isinstance(series.index, pd.DatetimeIndex)
                and not series.empty
                and series.index.equals(reference_index)
                and not series.index.has_duplicates
                and series.index.is_monotonic_increasing
                and not series.index.hasnans
                and np.isfinite(series.to_numpy(dtype=float)).all()
            )
        except (TypeError, ValueError):
            valid = False
        if not valid:
            errors.append(name)
    return errors


def _candidate_panel_statistics(
    panel, benchmark, base, protocol, bootstrap_method, registered_before_results
):
    from analysis.paper_validation import cscv_pbo, deflated_sharpe_evidence, white_reality_check

    names = base["candidate_ids"]
    pbo_groups = protocol["pbo_groups"]
    complete = protocol["family_complete"]
    blocks = protocol["block_lengths"]
    bootstrap_iterations = protocol["bootstrap_iterations"]
    seed = protocol["seed"]
    periods_per_year = protocol["periods_per_year"]
    used = len(panel) // pbo_groups * pbo_groups
    pbo = (
        cscv_pbo(panel.iloc[:used], n_groups=pbo_groups, family_complete=complete)
        if used
        else {
            "status": "insufficient",
            "reason": "fewer observations than registered PBO groups",
            "probability": None,
            "diagnostic_probability": None,
            "admission_eligible": False,
            "candidate_ids": names,
        }
    )
    pbo["axis_protocol"] = {
        "policy": "leading_multiple_of_groups",
        "full_observations": len(panel),
        "used_observations": used,
        "excluded_tail_count": len(panel) - used,
        "used_start": panel.index[0].isoformat() if used else None,
        "used_end": panel.index[used - 1].isoformat() if used else None,
        "excluded_tail_timestamps": [t.isoformat() for t in panel.index[used:]],
    }
    return {
        **base,
        "status": "diagnostic",
        "observations": len(panel),
        "panel": {
            "timestamps": [t.isoformat() for t in panel.index],
            "returns": {name: panel[name].tolist() for name in names},
        },
        "panel_sha256": sha256_frame(panel),
        "benchmark": {
            "kind": protocol["benchmark"],
            "name": benchmark.name,
            "sha256": sha256_frame(benchmark.to_frame("benchmark")),
            "returns": benchmark.tolist(),
        },
        "pbo": pbo,
        "reality_check": {
            str(block): white_reality_check(
                panel,
                benchmark,
                family_complete=complete,
                iterations=bootstrap_iterations,
                block_length=block,
                bootstrap=bootstrap_method,
                seed=seed,
            )
            for block in blocks
        },
        "dsr": {
            name: deflated_sharpe_evidence(
                panel,
                name,
                periods_per_year=periods_per_year,
                historical_trials_complete=False,
                registered_before_results=registered_before_results,
            )
            for name in names
        },
    }


def candidate_panel_evidence(
    series_by_candidate: Mapping,
    *,
    periods_per_year=365.0,
    registered_before_results=False,
    benchmark_returns=None,
    bootstrap_iterations=500,
    block_lengths=(5, 20, 60),
    pbo_groups=8,
    seed=42,
    bootstrap_method="circular",
    local_family_complete=False,
):
    """在严格共同时间轴上计算 DSR、PBO 和联合块重采样诊断。

    候选必须逐期对齐，不取日期交集、不补零、不删除失败候选。默认基准为
    零收益现金。PBO 仅使用长度可整除 ``pbo_groups`` 的前缀，并公开被排除
    的尾部日期；DSR 和 Reality Check 保留完整时间轴。

    本次结果前登记不证明历史全局搜索完整，因此输出始终保留诊断口径，
    不产生交易准入结论。
    """
    blocks = _validate_panel_settings(
        bootstrap_iterations, pbo_groups, block_lengths, bootstrap_method
    )
    names, protocol, base = _candidate_panel_context(
        series_by_candidate,
        periods_per_year,
        pbo_groups,
        blocks,
        bootstrap_iterations,
        seed,
        benchmark_returns,
        local_family_complete,
        registered_before_results,
        bootstrap_method,
    )
    if not names:
        return {**base, "reason": "empty candidate family", "dsr": {}}
    errors = _candidate_panel_errors(series_by_candidate, names)
    if errors:
        return {
            **base,
            "reason": "incomplete, invalid or asynchronous family; no intersection or zero fill",
            "invalid_candidates": errors,
            "dsr": {name: {"status": "invalid", "probability": None} for name in names},
        }
    panel = pd.DataFrame(series_by_candidate)
    benchmark = (
        pd.Series(0.0, index=panel.index, name="cash_zero_return")
        if benchmark_returns is None
        else benchmark_returns
    )
    try:
        valid_benchmark = (
            isinstance(benchmark, pd.Series)
            and benchmark.index.equals(panel.index)
            and np.isfinite(benchmark.to_numpy(dtype=float)).all()
        )
    except (TypeError, ValueError):
        valid_benchmark = False
    if not valid_benchmark:
        return {
            **base,
            "reason": "benchmark must be finite and exactly match the full candidate axis",
            "dsr": {name: {"status": "invalid", "probability": None} for name in names},
        }
    return _candidate_panel_statistics(
        panel, benchmark, base, protocol, bootstrap_method, registered_before_results
    )


_ACCOUNT_BASIS_FIELDS = (
    "return_basis",
    "capital_policy_id",
    "cost_policy_id",
    "risk_policy_id",
    "timeframe",
    "currency",
)


def panel_result_binding(returns, accounting):
    """Bind supplied account returns and their declared basis to one completion.

    The account trace digest is provenance supplied by the caller. This binding
    does not certify real venue execution or the correctness of the account path.
    """
    import json
    import re

    if (
        not isinstance(returns, pd.Series)
        or not isinstance(returns.index, pd.DatetimeIndex)
        or returns.empty
        or returns.index.hasnans
        or not returns.index.is_unique
        or not returns.index.is_monotonic_increasing
    ):
        raise ValueError("finite chronological unique account return Series required")
    try:
        if not np.isfinite(returns.to_numpy(dtype=float)).all():
            raise ValueError("nonfinite returns")
        value = json.loads(json.dumps(dict(accounting), allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("finite returns and accounting JSON required") from exc
    for key in _ACCOUNT_BASIS_FIELDS:
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError(f"accounting.{key} is required")
    if value["return_basis"] != "net_account_equity_returns":
        raise ValueError("only net account equity returns have portfolio-return evidence")
    if not isinstance(value.get("account_trace_sha256"), str) or not re.fullmatch(
        r"[0-9a-f]{64}", value["account_trace_sha256"]
    ):
        raise ValueError("account trace SHA-256 is required")
    return {
        "returns_sha256": sha256_frame_exact(returns.to_frame("return")),
        "accounting_sha256": sha256_bytes(canonical_json(value).encode()),
    }


def _registered_panel_inputs(scope, returns_by_arm, accounting_by_arm):
    candidates = scope.protocol["candidate_ids"]
    benchmarks = scope.protocol["benchmark_ids"]
    arms = list(dict.fromkeys(candidates + benchmarks))
    if not isinstance(returns_by_arm, Mapping) or set(returns_by_arm) != set(arms):
        raise ValueError(
            "exact declared candidate and benchmark return set required; no dropping or filling"
        )
    if not isinstance(accounting_by_arm, Mapping) or set(accounting_by_arm) != set(arms):
        raise ValueError("exact accounting basis for every arm required")
    latest = {
        event["candidate"]: event
        for event in scope.run.events
        if event.get("phase") == "run" and event.get("status") == "completed"
    }
    baseline_basis = {
        field: accounting_by_arm[arms[0]].get(field) for field in _ACCOUNT_BASIS_FIELDS
    }
    reference_index = returns_by_arm[arms[0]].index
    for arm in arms:
        binding = panel_result_binding(returns_by_arm[arm], accounting_by_arm[arm])
        if any(latest[arm].get(key) != value for key, value in binding.items()):
            raise ValueError(f"completed attempt result binding changed or absent: {arm}")
        if not returns_by_arm[arm].index.equals(reference_index):
            raise ValueError(
                "asynchronous arm indices; no intersection or implicit timezone alignment"
            )
        if {
            field: accounting_by_arm[arm].get(field) for field in _ACCOUNT_BASIS_FIELDS
        } != baseline_basis:
            raise ValueError(
                "capital, cost, risk, currency, timeframe and holding-return basis must match"
            )
        if (
            accounting_by_arm[arm]["cost_policy_id"] != scope.protocol["cost_policy_id"]
            or accounting_by_arm[arm]["risk_policy_id"] != scope.protocol["risk_policy_id"]
        ):
            raise ValueError("account cost/risk policy differs from the preregistration")
    statistics = scope.protocol.get("statistics")
    if not isinstance(statistics, dict) or set(statistics) != {
        "pbo_groups",
        "periods_per_year",
    }:
        raise ValueError("preregister statistics.pbo_groups and statistics.periods_per_year")
    resampling = scope.protocol["resampling"]
    if resampling["method"] not in {"stationary", "circular"}:
        raise ValueError("registered bootstrap method is not implemented; no silent substitution")
    return (
        candidates,
        benchmarks,
        arms,
        latest,
        baseline_basis,
        reference_index,
        statistics,
        resampling,
    )


def _registered_panel_result(
    result,
    scope,
    returns_by_arm,
    accounting_by_arm,
    candidates,
    arms,
    baseline_basis,
    reference_index,
    evidence,
    after,
):
    for benchmark, value in evidence.items():
        value["exact_panel_sha256"] = sha256_frame_exact(
            pd.DataFrame({arm: returns_by_arm[arm] for arm in candidates})
        )
        value["exact_benchmark_sha256"] = sha256_frame_exact(
            returns_by_arm[benchmark].to_frame("return")
        )
        value["legacy_panel_sha256_policy"] = (
            "legacy CSV identity retained for old report compatibility; use exact_* with the registered v2 binding"
        )
    result.update(
        status="diagnostic",
        statistics=evidence,
        registered_basis=baseline_basis,
        data_identity_version=scope.run.registration.get(
            "data_identity_version", LEGACY_DATA_IDENTITY
        ),
        result_bindings={
            arm: panel_result_binding(returns_by_arm[arm], accounting_by_arm[arm]) for arm in arms
        },
        observations=len(reference_index),
        unique_time_observations=len(reference_index),
        results_binding_verification="bound to latest completed attempts; account trace provenance remains caller supplied",
        local_scope="declared current family only",
        final_scope_audit=after,
        reason="local PBO/White evidence only; historical global search remains incomplete",
    )


def _registered_panel_readiness(scope, result):
    from analysis.search_scope import SearchScope

    if not isinstance(scope, SearchScope):
        result["errors"].append("a live byte-verified SearchScope is required")
        return None
    audit = scope.audit()
    result["search_scope_audit"] = audit
    if audit["status"] != "ok":
        result["errors"].extend(audit["errors"])
        return None
    result["registered_candidate_ids"] = list(scope.protocol["candidate_ids"])
    result["registered_benchmark_ids"] = list(scope.protocol["benchmark_ids"])
    result["local_family_complete"] = audit["local_family_complete"]
    result["results_complete"] = audit["results_complete"]
    if scope.protocol["resampling"].get("method") == "not_applicable":
        result.update(
            status="not_evaluated",
            reason="registered engineering fixture has no return-inference design",
        )
        return None
    if (
        scope.run.registration.get("data_identity_version", LEGACY_DATA_IDENTITY)
        != EXACT_DATA_IDENTITY
    ):
        result["errors"].append(
            "registered inference requires exact/v2 data identity; legacy reports remain separate"
        )
        return None
    if not audit["results_complete"]:
        result["errors"].append(
            "all declared arms must complete; failed, interrupted or missing arms cannot be removed"
        )
        return None
    return audit


def registered_candidate_panel_evidence(scope, returns_by_arm, *, accounting_by_arm):
    """Compute only the exact completed, byte-bound local registered family.

    Every benchmark is evaluated separately on the unchanged common axis. The
    current local journal can attest local PBO/White completeness; DSR's global
    historical search requirement remains false. No missing arm is synthesized.
    """
    result = {
        "schema": "registered-candidate-family-statistics/v1",
        "status": "invalid",
        "local_family_complete": False,
        "results_complete": False,
        "historical_global_search_complete": False,
        "admission_eligible": False,
        "research": "not_evaluated",
        "statistics": None,
        "errors": [],
    }
    audit = _registered_panel_readiness(scope, result)
    if audit is None:
        return result
    try:
        (
            candidates,
            benchmarks,
            arms,
            latest,
            baseline_basis,
            reference_index,
            statistics,
            resampling,
        ) = _registered_panel_inputs(scope, returns_by_arm, accounting_by_arm)
        evidence = {
            benchmark: candidate_panel_evidence(
                {arm: returns_by_arm[arm] for arm in candidates},
                periods_per_year=statistics["periods_per_year"],
                registered_before_results=True,
                benchmark_returns=returns_by_arm[benchmark],
                bootstrap_iterations=resampling["iterations"],
                block_lengths=tuple(resampling["block_lengths"]),
                pbo_groups=statistics["pbo_groups"],
                seed=resampling["seed"],
                bootstrap_method=resampling["method"],
                local_family_complete=True,
            )
            for benchmark in benchmarks
        }
        # Computation and re-verification are a single evidence boundary. Do not
        # let a long bootstrap hide changes to source, data, results or journal.
        after = scope.audit()
        if after["status"] != "ok" or after.get("identity") != audit.get("identity"):
            raise ValueError("run identity changed during statistics")
        for arm in arms:
            binding = panel_result_binding(returns_by_arm[arm], accounting_by_arm[arm])
            if any(latest[arm].get(key) != value for key, value in binding.items()):
                raise ValueError("result bytes changed during statistics")
        invalid = [name for name, value in evidence.items() if value["status"] == "invalid"]
        if invalid:
            raise ValueError(f"invalid candidate panels for benchmarks: {invalid}")
        _registered_panel_result(
            result,
            scope,
            returns_by_arm,
            accounting_by_arm,
            candidates,
            arms,
            baseline_basis,
            reference_index,
            evidence,
            after,
        )
    except (TypeError, ValueError, KeyError, AttributeError) as exc:
        result["errors"].append(str(exc))
    return result
