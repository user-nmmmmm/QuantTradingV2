import json
import tempfile
from pathlib import Path

import pandas as pd
import pytest

from analysis.phase5 import (
    AdmissionThresholds,
    ExperimentRegistry,
    HoldoutProtocol,
    HoldoutVault,
    concentration_stress,
    cross_market_validation,
    deflated_sharpe_ratio,
    evaluate_holdout_admission,
    factor_ablation,
    parameter_plateau,
    purged_cv_indices,
    walk_forward_splits,
)
from core.portfolio import Portfolio
from core.state import MarketState
from strategies.mean_reversion import RangeStrategy
from strategies.trend_breakout import TrendBreakoutStrategy
from strategies.volatility import VolatilityReversionStrategy


def _protocol():
    return HoldoutProtocol(
        "phase5-v1", "2020-01-01", "2020-01-04", "2020-01-05", "2020-01-07",
        "2020-01-08", "2020-01-10",
    )


def test_t5_1_frozen_holdout_is_disjoint_and_single_use(tmp_path):
    protocol = _protocol()
    frame = pd.Series(range(10), index=pd.date_range("2020-01-01", periods=10))
    parts = protocol.partition(frame)
    assert parts["train"].index.max() < parts["validation"].index.min()
    assert parts["validation"].index.max() < parts["holdout"].index.min()
    vault = HoldoutVault(tmp_path / "holdout.json")
    vault.freeze(protocol, data_hash="abc")
    with pytest.raises(PermissionError):
        vault.open_final(protocol_hash=protocol.fingerprint, purpose="parameter tuning")
    opened = vault.open_final(protocol_hash=protocol.fingerprint, purpose="final admission")
    assert opened["status"] == "opened_for_final_evaluation"
    with pytest.raises(RuntimeError):
        vault.open_final(protocol_hash=protocol.fingerprint, purpose="final admission rerun")


def test_t5_2_walk_forward_uses_only_past_and_respects_purge_embargo():
    windows = walk_forward_splits(
        40, train_size=12, validation_size=4, test_size=5, purge_size=2,
        embargo_size=3,
    )
    assert len(windows) >= 2
    for window in windows:
        assert window["train_end"] <= window["validation_start"]
        assert window["validation_end"] <= window["purge_start"]
        assert window["purge_end"] == window["test_start"]
        assert window["test_end"] <= window["embargo_end"]
    assert windows[1]["test_start"] >= windows[0]["embargo_end"]


def test_t5_3_purged_cv_removes_overlapping_events_and_embargoes_future_rows():
    starts = pd.date_range("2024-01-01", periods=12, freq="D")
    ends = starts + pd.to_timedelta([3] * 12, unit="D")
    folds = purged_cv_indices(starts, ends, n_splits=3, embargo_fraction=0.1)
    assert len(folds) == 3
    for fold in folds:
        test_start = starts[fold["test"]].min()
        test_end = ends[fold["test"]].max()
        assert set(fold["train"]).isdisjoint(fold["test"])
        assert all(not (starts[i] <= test_end and ends[i] >= test_start) for i in fold["train"])
        assert set(fold["embargoed"]).isdisjoint(fold["train"])


def test_t5_4_parameter_plateau_requires_immediate_neighbors():
    scores = {
        (10, 5): 0.92, (20, 5): 0.95, (30, 5): 0.91,
        (10, 10): 0.94, (20, 10): 1.00, (30, 10): 0.93,
        (10, 15): 0.90, (20, 15): 0.96, (30, 15): 0.91,
    }
    result = parameter_plateau(scores, tolerance=0.10)
    assert result["best_point"] == [20, 10]
    assert result["stable"] is True
    assert len(result["neighbor_points"]) == 4


def test_t5_5_factor_ablation_keeps_only_consistent_oos_gain_and_toggles_exist():
    result = factor_ablation(
        [1.0, 1.1, 0.9],
        {"OBV": [0.8, 0.9, 0.7], "RSI": [0.8, 1.2, 0.7]},
    )
    assert result["factors"]["OBV"]["decision"] == "retain"
    assert result["factors"]["RSI"]["decision"] == "remove_or_research"
    assert TrendBreakoutStrategy(use_obv=False).use_obv is False
    assert RangeStrategy(use_rsi=False).use_rsi is False
    assert VolatilityReversionStrategy(use_stochastic=False).use_stochastic is False


def test_t5_6_concentration_reports_top_1_3_5_10():
    result = concentration_stress([20, 10, 5, 2, -1, -2, -3] + [1] * 20)
    assert [row["removed_top_winners"] for row in result["scenarios"]] == [1, 3, 5, 10]
    assert result["baseline_net_pnl"] == 51


def test_t5_7_t5_10_holdout_admission_checks_all_phase5_gates():
    pnls = [2.0] * 50 + [-1.0] * 10
    trades = [
        {
            "net_pnl": pnl,
            "gross_pnl_theoretical": pnl + 0.2,
            "commission": 0.1,
            "slippage": 0.1,
            "strategy": "Stable",
        }
        for pnl in pnls
    ]
    index = pd.date_range("2025-01-01", periods=61)
    equity = pd.Series([100.0 + i for i in range(61)], index=index)
    benchmark = pd.Series([100.0 + i * 0.25 for i in range(61)], index=index)
    result = evaluate_holdout_admission(
        trades=trades, equity=equity, benchmark=benchmark,
        thresholds=AdmissionThresholds(maximum_drawdown=0.20),
    )
    assert result["decision"] == "admit"
    assert all(result["gates"].values())
    points = {
        (row["commission_multiplier"], row["slippage_multiplier"])
        for row in result["cost_stress"]["grid"]
    }
    assert {(1.0, 1.0), (1.5, 1.5), (2.0, 2.0), (3.0, 3.0)} <= points


def test_t5_8_cross_market_requires_exchange_timeframe_and_regime_coverage():
    scenarios = [
        {"exchange": "binance", "timeframe": "1d", "regime": "bull", "net_return": 0.1},
        {"exchange": "okx", "timeframe": "4h", "regime": "bear", "net_return": 0.02},
    ]
    assert cross_market_validation(scenarios)["stable_positive_edge"] is True


def test_t5_9_registry_is_idempotent_and_deflated_sharpe_counts_trials(tmp_path):
    registry = ExperimentRegistry(tmp_path / "experiments.jsonl")
    kwargs = {
        "hypothesis": "breakout stability",
        "parameters": {"entry": 20},
        "data_scope": {"partition": "validation"},
        "metrics": {"sharpe": 1.2},
    }
    first = registry.register(**kwargs)
    second = registry.register(**kwargs)
    assert first["experiment_id"] == second["experiment_id"]
    assert len(registry.records()) == 1
    result = deflated_sharpe_ratio([0.01, 0.02, -0.005, 0.015] * 20, trials=12)
    assert result["status"] == "ok"
    assert result["trials"] == 12
    assert 0 <= result["probability"] <= 1
