"""Registered family statistics must honor real journals, identities and units."""

from copy import deepcopy
from datetime import datetime, timezone
from itertools import combinations
import json
import math

import numpy as np
import pandas as pd
import pytest

from analysis.paper_validation import cpcv_splits, cscv_pbo, fit_cpcv_event_paths
from analysis.research_evidence import (
    panel_result_binding,
    registered_candidate_panel_evidence,
    source_identity,
)
from analysis.search_scope import SearchScope, data_manifest
from core.foundation.reproducibility import canonical_json, sha256_bytes, sha256_file


def digest(value):
    return sha256_bytes(canonical_json(value).encode())


@pytest.fixture
def family(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "config.json").write_text('{"scope":"fixed"}', encoding="utf-8")
    paths = ["config.json"]
    data = {"synthetic": {"seed": 91, "observations": 80}}
    protocol = {
        "schema": "paper-research-protocol/v1",
        "study_id": "registered-statistics-fixture",
        "registered_at": datetime.now(timezone.utc).isoformat(),
        "paper_refs": ["L01", "L02", "L03"],
        "source_manifest_sha256": digest(source_identity(root, paths=paths)),
        "config_sha256": sha256_file(root / "config.json"),
        "data_manifest_sha256": digest(data_manifest(data)),
        "evaluation_kind": "retrospective",
        "information_policy": {"mode": "retrospective", "knowledge": "local", "unknown": "exclude"},
        "candidate_ids": ["a", "b"],
        "benchmark_ids": ["cash"],
        "search_scope": "current_local_family",
        "historical_global_search_complete": False,
        "primary_metric": "fixed_local_excess",
        "economic_or_precision_target": {"rationale": "independent protocol fixture"},
        "training_selection_rule": "no selection or fitting",
        "evaluation_windows": [
            {"id": "fixed", "role": "development", "start": "2020-01-01", "end": "2021-01-01"}
        ],
        "cost_policy_id": "fixed-net-cost/v1",
        "risk_policy_id": "fixed-risk/v1",
        "resampling": {
            "method": "stationary",
            "block_lengths": [5, 10],
            "iterations": 100,
            "seed": 17,
        },
        "statistics": {"pbo_groups": 4, "periods_per_year": 365},
        "attempt_budget": 4,
        "resource_budget": {"wall_time_seconds": 60, "memory_mb": 512, "max_workers": 1},
        "stop_rules": ["retain every arm and stop on drift"],
        "final_access_policy_id": None,
    }
    scope = SearchScope.register(
        protocol=protocol,
        data_map=data,
        output=tmp_path / "run",
        source_root=root,
        source_paths=paths,
    )
    rng = np.random.default_rng(91)
    index = pd.date_range("2020-01-01", periods=80, tz="UTC")
    returns = {
        "a": pd.Series(rng.normal(0.001, 0.01, 80), index=index),
        "b": pd.Series(rng.normal(0, 0.01, 80), index=index),
        "cash": pd.Series(0.0, index=index),
    }
    bases = {
        arm: {
            "return_basis": "net_account_equity_returns",
            "capital_policy_id": "fixed-initial-capital/v1",
            "cost_policy_id": "fixed-net-cost/v1",
            "risk_policy_id": "fixed-risk/v1",
            "timeframe": "1d",
            "currency": "USDT",
            "account_trace_sha256": digest({"arm": arm, "synthetic_fixture": True}),
        }
        for arm in returns
    }
    return scope, returns, bases, root, data


def complete(scope, returns, bases, arm):
    scope.record(candidate=arm, status="started")
    scope.record(
        candidate=arm, status="completed", **panel_result_binding(returns[arm], bases[arm])
    )


def finish_family(family):
    scope, returns, bases, _, _ = family
    for arm in returns:
        complete(scope, returns, bases, arm)
    return scope, returns, bases


def test_complete_journal_enables_only_local_pbo_white_not_global_dsr(family):
    scope, returns, bases = finish_family(family)
    result = registered_candidate_panel_evidence(scope, returns, accounting_by_arm=bases)
    assert result["status"] == "diagnostic"
    assert result["local_family_complete"] and result["results_complete"]
    assert not result["historical_global_search_complete"] and not result["admission_eligible"]
    statistic = result["statistics"]["cash"]
    assert statistic["pbo"]["probability"] is not None
    assert statistic["reality_check"]["5"]["p_value"] is not None
    assert statistic["reality_check"]["5"]["bootstrap"] == "stationary"
    assert all(
        value["probability"] is None and not value["search_accounting_complete"]
        for value in statistic["dsr"].values()
    )
    assert result["unique_time_observations"] == 80
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("missing", ["a", "b", "cash"])
def test_missing_arm_is_never_removed_or_replaced_by_zero(family, missing):
    scope, returns, bases = finish_family(family)
    del returns[missing]
    result = registered_candidate_panel_evidence(scope, returns, accounting_by_arm=bases)
    assert result["status"] == "invalid" and result["statistics"] is None
    assert result["registered_candidate_ids"] == ["a", "b"]


def test_unfinished_failed_arm_cannot_be_excluded_even_if_return_series_is_supplied(family):
    scope, returns, bases, _, _ = family
    complete(scope, returns, bases, "a")
    complete(scope, returns, bases, "cash")
    scope.record(candidate="b", status="started")
    scope.record(
        candidate="b", status="failed", reason="engine did not produce a valid return path"
    )
    result = registered_candidate_panel_evidence(scope, returns, accounting_by_arm=bases)
    assert result["status"] == "invalid" and result["statistics"] is None
    assert result["local_family_complete"] and not result["results_complete"]


def test_system_failure_retry_preserves_history_and_exact_final_result(family):
    scope, returns, bases, root, data = family
    scope.record(candidate="a", status="started")
    scope.record(candidate="a", status="failed", reason="recoverable process failure")
    resumed = SearchScope.resume(output=scope.output, data_map=data, source_root=root)
    for arm in returns:
        complete(resumed, returns, bases, arm)
    result = registered_candidate_panel_evidence(resumed, returns, accounting_by_arm=bases)
    assert result["status"] == "diagnostic"
    assert result["search_scope_audit"]["attempts_used"] == 4
    assert len(result["search_scope_audit"]["failed_or_interrupted_attempts"]) == 1


@pytest.mark.parametrize("change", ["returns", "accounting", "source", "data", "unbound"])
def test_changed_bytes_or_unbound_completed_results_fail_closed(family, change):
    scope, returns, bases = finish_family(family)
    if change == "returns":
        returns["a"].iloc[0] += 0.1
    elif change == "accounting":
        bases["a"]["account_trace_sha256"] = "a" * 64
    elif change == "source":
        (family[3] / "config.json").write_text("{}", encoding="utf-8")
    elif change == "data":
        family[4]["synthetic"]["observations"] = 90
    else:
        scope.run.events[-1].pop("returns_sha256")
    result = registered_candidate_panel_evidence(scope, returns, accounting_by_arm=bases)
    assert result["status"] == "invalid" and result["statistics"] is None


@pytest.mark.parametrize(
    "field",
    [
        "capital_policy_id",
        "cost_policy_id",
        "risk_policy_id",
        "timeframe",
        "currency",
        "return_basis",
    ],
)
def test_cross_arm_accounting_mismatch_is_not_comparable_even_when_byte_bound(family, field):
    scope, returns, bases, _, _ = family
    if field == "return_basis":
        bases["b"][field] = "proxy_event_scores"
        with pytest.raises(ValueError, match="net account"):
            panel_result_binding(returns["b"], bases["b"])
        return
    bases["b"][field] = "different"
    for arm in returns:
        complete(scope, returns, bases, arm)
    result = registered_candidate_panel_evidence(scope, returns, accounting_by_arm=bases)
    assert result["status"] == "invalid"


def test_asynchronous_axis_is_not_intersected_even_when_both_series_are_valid(family):
    scope, returns, bases, _, _ = family
    returns["b"].index = returns["b"].index + pd.Timedelta(days=1)
    for arm in returns:
        complete(scope, returns, bases, arm)
    result = registered_candidate_panel_evidence(scope, returns, accounting_by_arm=bases)
    assert result["status"] == "invalid" and "asynchronous" in result["errors"][0]


def test_zero_variance_candidate_remains_in_statistical_family(family):
    scope, returns, bases, _, _ = family
    returns["b"][:] = 0
    for arm in returns:
        complete(scope, returns, bases, arm)
    result = registered_candidate_panel_evidence(scope, returns, accounting_by_arm=bases)
    assert result["status"] == "diagnostic"
    stats = result["statistics"]["cash"]
    assert stats["candidate_ids"] == ["a", "b"]
    assert stats["dsr"]["a"]["status"] == "insufficient"
    assert stats["pbo"]["status"] == "insufficient"


def test_duplicate_candidate_returns_never_multiply_time_observations(family):
    scope, returns, bases, _, _ = family
    returns["b"] = returns["a"].copy()
    for arm in returns:
        complete(scope, returns, bases, arm)
    result = registered_candidate_panel_evidence(scope, returns, accounting_by_arm=bases)
    assert result["observations"] == result["unique_time_observations"] == 80
    assert result["statistics"]["cash"]["dsr"]["a"]["observed_trial_count"] == 2


def test_independent_mean_score_cscv_enumerates_every_combination():
    # Independent rank count, without invoking the implementation's rank helper.
    values = np.array(
        [
            [4, 0, -2],
            [2, -1, 1],
            [-4, 2, 1],
            [-2, 4, -1],
            [1, -3, 2],
            [0, -1, 4],
            [-1, 2, -2],
            [2, 1, 0],
        ],
        dtype=float,
    )
    frame = pd.DataFrame(
        values, index=pd.date_range("2000-01-01", periods=8), columns=["a", "b", "c"]
    )
    result = cscv_pbo(frame, n_groups=4, score="mean", family_complete=True)
    expected = []
    for groups in combinations(range(4), 2):
        train = [i for g in groups for i in (2 * g, 2 * g + 1)]
        test = [i for g in range(4) if g not in groups for i in (2 * g, 2 * g + 1)]
        selected = max(range(3), key=lambda j: sum(values[i, j] for i in train))
        scores = [sum(values[i, j] for i in test) for j in range(3)]
        rank = (
            1
            + sum(s < scores[selected] for s in scores)
            + (sum(s == scores[selected] for s in scores) - 1) / 2
        )
        expected.append(rank / 4 <= 0.5)
    assert result["computed_splits"] == math.comb(4, 2)
    assert result["probability"] == sum(expected) / len(expected)


class MeanEstimator:
    def fit(self, features, labels):
        self.value = float(labels.mean())
        self.train_positions = list(features.index)
        return self

    def predict(self, features):
        return np.repeat(self.value, len(features))


def cpcv_fixture():
    index = pd.date_range("2020-01-01", periods=24, tz="UTC")
    plan = cpcv_splits(index, index, n_groups=4, n_test_groups=2, embargo_fraction=0)
    features = pd.DataFrame({"x": np.arange(24, dtype=float)}, index=index)
    labels = pd.Series(np.arange(24, dtype=float), index=index)
    return plan, features, labels


def test_cpcv_adapter_really_fits_each_fold_on_its_own_permissible_rows():
    plan, features, labels = cpcv_fixture()
    result = fit_cpcv_event_paths(plan, features, labels, MeanEstimator)
    assert result["status"] == "ok" and result["fit_count"] == 6
    assert result["independent_time_observations"] == 24 and result["path_count"] == 3
    lookup = {fold["split_id"]: float(labels.iloc[fold["train"]].mean()) for fold in plan["splits"]}
    for path in result["paths"]:
        assert len(path["observations"]) == 24
        for row in path["observations"]:
            assert row["prediction"] == lookup[row["split_id"]]
    assert any(v != labels.mean() for v in lookup.values())


def test_cpcv_reusing_one_fitted_model_and_tampering_plan_are_rejected():
    plan, features, labels = cpcv_fixture()
    model = MeanEstimator()
    assert fit_cpcv_event_paths(plan, features, labels, lambda: model)["status"] == "invalid"
    plan["splits"][0]["train"].append(plan["splits"][0]["test"][0])
    assert fit_cpcv_event_paths(plan, features, labels, MeanEstimator)["status"] == "invalid"


def test_cpcv_feature_axis_cannot_be_reordered_after_plan_construction():
    plan, features, labels = cpcv_fixture()
    assert (
        fit_cpcv_event_paths(plan, features.iloc[::-1], labels.iloc[::-1], MeanEstimator)["status"]
        == "invalid"
    )


def reregister(family, *, changes):
    old, _, _, root, data = family
    protocol = deepcopy(old.protocol)
    protocol.update(changes)
    return SearchScope.register(
        protocol=protocol,
        data_map=data,
        output=root.parent / "alternative-run",
        source_root=root,
        source_paths=["config.json"],
    )


def test_engineering_protocol_is_not_forced_to_invent_returns_or_probabilities(family):
    scope = reregister(
        family,
        changes={
            "resampling": {"method": "not_applicable", "reason": "fixed account arithmetic only"}
        },
    )
    result = registered_candidate_panel_evidence(scope, None, accounting_by_arm=None)
    assert result["status"] == "not_evaluated" and result["statistics"] is None
    assert not result["admission_eligible"]


def test_statistical_design_must_exist_before_result_access(family):
    scope = reregister(family, changes={"statistics": None})
    _, returns, bases, _, _ = family
    for arm in returns:
        complete(scope, returns, bases, arm)
    result = registered_candidate_panel_evidence(scope, returns, accounting_by_arm=bases)
    assert result["status"] == "invalid" and "preregister statistics" in result["errors"][0]


def test_identity_drift_during_bootstrap_invalidates_all_statistics(family, monkeypatch):
    import analysis.research_evidence as module

    scope, returns, bases = finish_family(family)
    original = module.candidate_panel_evidence

    def mutate(*args, **kwargs):
        result = original(*args, **kwargs)
        returns["a"].iloc[0] += 0.2
        return result

    monkeypatch.setattr(module, "candidate_panel_evidence", mutate)
    result = module.registered_candidate_panel_evidence(scope, returns, accounting_by_arm=bases)
    assert result["status"] == "invalid" and result["statistics"] is None
    assert "bytes changed during" in result["errors"][0]


def test_legacy_registration_cannot_be_promoted_to_exact_registered_inference(family):
    scope = reregister(family, changes={"data_identity_version": "legacy/v1"})
    _, returns, bases, _, _ = family
    for arm in returns:
        complete(scope, returns, bases, arm)
    result = registered_candidate_panel_evidence(scope, returns, accounting_by_arm=bases)
    assert result["status"] == "invalid" and "exact/v2" in result["errors"][0]
