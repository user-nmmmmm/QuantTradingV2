"""Causal targets, true grouped ranking, equal-budget curves and PIT evidence."""
from copy import deepcopy
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from research.ml_selection.label_experiments import (
    ExitLabelContract, exit_label_from_episode, frozen_label_contracts, run_exit_label_probe,
)
from research.ml_selection.learning import run_learning_curve
from research.ml_selection.membership import audit_membership, validate_membership_evidence
from research.ml_selection.models import (
    LambdaRankModel, RidgeModel, FeatureScaler, calibration_metrics, fit_model, load_model, ranking_relevance,
)


def grouped_samples():
    times = pd.date_range("2024-01-01", periods=6, tz="UTC").repeat(4)
    x = np.tile([-2., -1., 1., 2.], 6)
    train = pd.DataFrame({"as_of": times, "label_available_at": times + pd.Timedelta(days=1),
                          "x": x, "label_net_return": x / 100.})
    validation = train.iloc[:8].copy()
    validation.as_of += pd.Timedelta(days=10)
    validation.label_available_at += pd.Timedelta(days=10)
    return train.iloc[[4, 0, 20, 8, 1, 5, 9, 21, 2, 6, 10, 22, 3, 7, 11, 23, 12, 13, 14, 15, 16, 17, 18, 19]], validation


def rank_params():
    return {"num_boost_round": 10, "early_stopping_rounds": 3,
            "min_data_in_leaf": 1, "min_data_in_bin": 1, "relevance_bins": 5, "gate_alpha": 0.}


def test_integer_relevance_is_within_group_and_preserves_ties():
    assert ranking_relevance([.1, .1, .3, .9, -.5, .5], [4, 2], 5).tolist() == [0, 0, 2, 4, 0, 4]
    assert ranking_relevance([100.], [1], 5).tolist() == [0]
    with pytest.raises(ValueError, match="cover"):
        ranking_relevance([1, 2], [3], 5)
    with pytest.raises(ValueError, match="relevance_bins"):
        ranking_relevance([1, 2], [2], 31)


def test_true_ranker_groups_queries_and_serializes_independent_return_gate(tmp_path):
    pytest.importorskip("lightgbm")
    train, valid = grouped_samples()
    model = fit_model(train, valid, features=["x"], kind="lambdarank", params=rank_params())
    assert isinstance(model, LambdaRankModel)
    assert model.metadata["params"]["objective"] == "lambdarank"
    assert model.metadata["params"]["metric"] == "ndcg"
    assert model.metadata["train_group_sizes"] == [4] * 6
    assert model.metadata["validation_group_sizes"] == [4, 4]
    assert model.metadata["score_semantics"] == "within_as_of_ranking_score"
    assert model.predict_net_return(valid) == pytest.approx(valid.label_net_return)
    target = tmp_path / "ranker.json"
    model.save(target)
    restored = load_model(target)
    assert restored.model_id == model.model_id
    assert restored.gate.model_id == model.gate.model_id
    assert restored.predict(valid) == pytest.approx(model.predict(valid))
    assert restored.predict_net_return(valid) == pytest.approx(model.predict_net_return(valid))


def test_ranker_validation_returns_never_fit_or_calibrate_gate():
    pytest.importorskip("lightgbm")
    train, valid = grouped_samples()
    before = fit_model(train, valid, features=["x"], kind="lambdarank", params=rank_params())
    after = fit_model(train, valid.assign(x=valid.x * 1000., label_net_return=-valid.label_net_return * 1000.),
                      features=["x"], kind="lambdarank", params=rank_params())
    assert before.gate.model_id == after.gate.model_id
    assert before.gate.scaler.mean == pytest.approx(after.gate.scaler.mean)
    assert before.predict_net_return(valid) == pytest.approx(after.predict_net_return(valid))


def test_ranker_drops_incomplete_query_before_preprocessing_and_requires_competition():
    pytest.importorskip("lightgbm")
    train, valid = grouped_samples()
    date = train.as_of.min()
    train.loc[train.as_of == date, "x"] = 1e9
    train.loc[train.index[train.as_of == date][0], "label_net_return"] = np.nan
    model = fit_model(train, valid, features=["x"], kind="lambdarank", params=rank_params())
    assert model.metadata["train_rows"] == 20
    assert model.metadata["train_dropped_invalid_labels"] == 4
    assert model.scaler.mean[0] == pytest.approx(0.)
    singles = train.groupby("as_of").head(1).dropna(subset=["label_net_return"])
    with pytest.raises(ValueError, match="competing"):
        fit_model(singles, valid, features=["x"], kind="lambdarank", params=rank_params())


def test_legacy_ridge_artifact_identity_is_preserved(tmp_path):
    scaler = FeatureScaler.fit(np.array([[1.], [2.]]), ["x"])
    model = RidgeModel(scaler, [1.], 0., {"legacy": True})
    path = tmp_path / "legacy.json"
    model.save(path)
    assert load_model(path).model_id == model.model_id


def test_return_calibration_excludes_entire_pending_or_unknown_cohort():
    frame = pd.DataFrame({"as_of": ["2024-01-01"] * 4 + ["2024-01-02"] * 2,
                          "label_available_at": ["2024-01-03"] * 5 + ["2024-02-01"],
                          "label_net_return": [.1, .2, .3, .4, 1., "future_unreadable"]})
    metrics = calibration_metrics(frame, [.11, .21, .31, .41, 1., 2.], mature_as_of="2024-01-04", quantiles=2)
    assert metrics["evaluated_rows"] == 4
    assert metrics["evaluated_cohorts"] == 1
    assert metrics["bias"] == pytest.approx(.01)
    assert metrics["rmse"] == pytest.approx(.01)
    assert metrics["groups"][0]["mean_net_return"] == pytest.approx(.15)
    assert metrics["fitted_on_evaluation"] is False


def test_learning_curve_same_evaluation_budget_and_training_only_maturity():
    dates = pd.date_range("2020-01-01", "2024-01-01", freq="MS", tz="UTC")
    train = pd.DataFrame({"as_of": dates, "label_available_at": dates + pd.Timedelta(days=20),
                          "x": np.arange(len(dates)), "label_net_return": np.arange(len(dates)) / 100.})
    train.loc[train.index[-1], ["x", "label_net_return"]] = [1e9, 1e9]
    train.loc[train.index[-1], "label_available_at"] = pd.Timestamp("2025-01-01", tz="UTC")
    validation = pd.DataFrame({"as_of": ["2024-01-02", "2024-01-02"],
                               "label_available_at": ["2024-01-22", "2024-01-22"],
                               "x": [3., 4.], "label_net_return": [.03, .04]})
    observed = []

    def portfolio(model, training, evaluation, month):
        observed.append((month, evaluation.copy(), model.scaler.mean[0], len(training)))
        return {"status": "original_engine_replayed", "accounting_ok": True}

    result = run_learning_curve(train, validation, features=["x"], params={"alpha": 0},
                                mature_as_of="2024-01-23", portfolio_evaluator=portfolio)
    windows = result["windows"]
    assert [row["training_months"] for row in windows] == [12, 24, 36]
    assert len({row["parameter_budget_id"] for row in windows}) == 1
    assert [row["rows"] for row in windows] == [11, 23, 35]
    assert all(row["dropped_pending_or_unknown_rows"] == 1 for row in windows)
    assert all(row["metadata"]["train_latest_label_available_at"] < "2024-01-02" for row in windows)
    assert all(row["dependency"]["blocks_are_not_claimed_independent"] for row in windows)
    assert all(mean < 100 for _, _, mean, _ in observed)
    for _, frame, _, _ in observed:
        pd.testing.assert_frame_equal(frame, observed[0][1])
    assert result["independent_final_sample"] is False


def test_membership_requires_source_and_respects_publication_and_delisting():
    evidence = {"GOOD": {"listed_at": "2024-01-01", "available_at": "2024-01-02", "source": "exchange:listing",
                          "delisted_at": "2024-02-01", "delisting_available_at": "2024-01-20", "delisting_source": "exchange:delisting"},
                "NO_SOURCE": {"listed_at": "2024-01-01", "available_at": "2024-01-01"}}
    decisions = pd.DataFrame({"symbol": ["GOOD", "GOOD", "GOOD", "NO_SOURCE", "UNKNOWN"],
                              "as_of": ["2024-01-01", "2024-01-03", "2024-02-01", "2024-01-03", "2024-01-03"]})
    rows, report = audit_membership(evidence, decisions)
    assert rows.membership_eligible.tolist() == [False, True, False, False, False]
    assert report["verified_decisions"] == 1
    assert report["delisted_symbols"] == 1
    assert report["full_pit_coverage"] is False
    downgraded, report = audit_membership(evidence, decisions, missing_policy="downgrade")
    assert downgraded.membership_eligible.tolist() == [True, True, False, True, True]
    assert report["downgraded_decisions"] == 3
    assert not downgraded.iloc[-1].pit_evidence_verified


def test_learning_curve_pending_peer_and_boundary_label_remove_entire_decision_group():
    training = pd.DataFrame({"as_of": ["2023-02-01", "2023-03-01", "2023-03-01", "2023-04-01"],
        "label_available_at": ["2023-02-21", "2023-03-21", "2024-02-01", "2024-01-01"],
        "x": [1., 2., 1e9, 1e9], "label_net_return": [.1, -.5, 1000., 1000.]})
    validation = pd.DataFrame({"as_of": ["2024-01-01", "2024-01-01"],
        "label_available_at": ["2024-01-21", "2024-01-21"], "x": [1., 2.], "label_net_return": [.1, .2]})
    result = run_learning_curve(training, validation, features=["x"], months=(12,), mature_as_of="2024-02-01")
    row = result["windows"][0]
    assert row["rows"] == 1
    assert row["dropped_pending_or_unknown_rows"] == 3
    assert row["metadata"]["train_rows"] == 1
    assert row["metadata"]["train_max_as_of"] == "2023-02-01T00:00:00+00:00"


def test_membership_late_notice_and_overlapping_intervals_cannot_claim_pit():
    evidence = pd.DataFrame([
        {"symbol": "LATE", "listed_at": "2024-01-01", "available_at": "2024-01-01", "source": "listing",
         "delisted_at": "2024-02-01", "delisting_available_at": "2024-02-02"},
        {"symbol": "OVERLAP", "listed_at": "2024-01-01", "available_at": "2024-01-01", "source": "listing"},
        {"symbol": "OVERLAP", "listed_at": "2024-01-02", "available_at": "2024-01-02", "source": "listing"},
    ])
    rows, _ = validate_membership_evidence(evidence)
    assert not rows.evidence_valid.any()
    assert "delisting_notice_available_after_effective_time" in rows.iloc[0].evidence_reasons
    assert "overlapping_listing_intervals" in rows.iloc[1].evidence_reasons


def episode(trades, **summary):
    return SimpleNamespace(result={"trades": trades}, summary={"terminated_by_risk": False, "accounting_ok": True,
                           "pending_orders": [], "unresolved_positions": {}, **summary})


def candidate():
    return {"symbol": "X", "as_of": "2024-01-01", "strategy": "Original", "signal": {"action": "buy", "stop_loss": 90.}}


def fill(side, qty, price, day, reason="signal"):
    return {"symbol": "X", "side": side, "qty": qty, "fill_price": price, "fill_time": day,
            "commission": 1., "exit_reason": reason}


@pytest.mark.parametrize("trades,extra,status", [
    ([], {}, "unfilled_or_ineligible_unknown_target"),
    ([fill("buy", 1., 100., "2024-01-02")], {}, "unclosed_or_pending_unknown_target"),
    ([fill("buy", 1., 100., "2024-01-02"), fill("sell", .5, 120., "2024-01-03")], {}, "unclosed_or_pending_unknown_target"),
    ([fill("buy", 1., 100., "2024-01-02"), fill("sell", 1., 120., "2024-01-03")], {"terminated_by_risk": True}, "risk_terminated_unknown_target"),
    ([fill("buy", 1., 100., "2024-01-02"), fill("sell", 1., 120., "2024-01-03", "EndOfBacktest")], {}, "unclosed_or_pending_unknown_target"),
])
def test_unfilled_unclosed_risk_or_synthetic_exit_targets_are_unknown(trades, extra, status):
    row = exit_label_from_episode(episode(trades, **extra), candidate=candidate(), contract=ExitLabelContract(), intervention_id="id")
    assert row["outcome_status"] == status
    assert np.isnan(row["label_net_return"])
    assert np.isnan(row["portfolio_marginal_net_return"])
    assert pd.isna(row["label_available_at"])


def test_real_exit_target_uses_actual_cash_flows_and_fees_once():
    trades = [fill("buy", 1., 100., "2024-01-02"), fill("sell", 1., 120., "2024-01-03")]
    row = exit_label_from_episode(episode(trades), candidate=candidate(), contract=ExitLabelContract(), intervention_id="id")
    assert row["label_net_return"] == pytest.approx(.18)
    assert row["actual_execution_net_return"] == pytest.approx(.18)
    assert row["label_available_at"] == pd.Timestamp("2024-01-04", tz="UTC")
    assert np.isnan(row["portfolio_marginal_net_return"])
    contracts = frozen_label_contracts()
    assert contracts["actual_execution"]["unfilled_is_zero_return"] is False
    assert contracts["original_exit"]["account_state"] == "fresh_cash_no_positions_or_orders"


def test_probe_runs_original_exit_and_does_not_force_missing_signal():
    from config.config import config
    from core.state import MarketState
    from strategies.base import Strategy

    class Original(Strategy):
        def __init__(self):
            super().__init__("Original", set(MarketState))

        def should_enter(self, symbol, i, df, state, portfolio):
            if i == 0:
                return {"action": "buy", "order_type": "market", "stop_loss": 90.}

        def should_exit(self, symbol, i, df, state, portfolio):
            if i == 2:
                return {"action": "sell", "order_type": "market", "reason": "original_test_exit"}

        def initial_entry_quantity(self, **kwargs):
            return 1.

    settings = deepcopy(config._config)
    settings["account"]["mode"] = "spot"
    settings["execution"]["fee_schedule"]["market_type"] = "spot"
    settings["router"]["cooldown_bars"] = 0
    settings["strategy_health"]["enabled"] = False
    settings["portfolio_risk"]["enabled"] = False
    settings["drawdown_budget"]["enabled"] = False
    settings["portfolio_targets"] = {"enabled": False}
    settings["risk"]["max_leverage"] = 1.
    frames = {"X": pd.DataFrame({"open": [100., 100., 101., 103., 104.], "high": [102., 103., 105., 106., 107.],
                                 "low": [99.] * 5, "close": [100., 101., 103., 104., 105.], "volume": [1e6] * 5},
                                index=pd.date_range("2024-01-01", periods=5))}
    signal = {"action": "buy", "order_type": "market", "stop_loss": 90.}
    frames["Y"] = frames["X"].copy()
    frames["Z"] = frames["X"].copy()
    rows, summary = run_exit_label_probe(frames, [{"symbol": "X", "as_of": "2024-01-02", "strategy": "Original", "signal": signal},
                                                {"symbol": "X", "as_of": "2024-01-03", "strategy": "Original", "signal": signal}],
        strategies={"Original": Original()}, contract=ExitLabelContract(initial_capital=1000., nominal_notional=100., risk_budget_fraction=.1),
        parameters=settings, engine_options={"warmup_period": 0, "slippage": 0.})
    assert rows.iloc[0].outcome_status == "closed_actual_filled_exit"
    assert rows.iloc[0].label_exit_reason == "original_test_exit"
    assert rows.iloc[0].signal_audit["original_signal_reproduced"] is True
    assert np.isfinite(rows.iloc[0].label_net_return)
    assert rows.iloc[1].outcome_status == "signal_not_reproduced_unknown_target"
    assert np.isnan(rows.iloc[1].label_net_return)
    assert summary["known_targets"] == 1
    assert summary["registered_symbol_count"] == 3
    assert rows.iloc[0].registered_symbols == ["X", "Y", "Z"]
    # The other registered symbols retain market/history coverage but cannot
    # enter the isolated intervention's account.
    assert rows.iloc[0].episode_summary["actual_fill_count"] == rows.iloc[0].actual_fill_count
    assert summary["production_protections"] == "preserved"


def test_real_trend_breakout_probe_preserves_three_symbol_health_registration():
    from config.config import config
    from core.strategy_health import StrategyHealthPolicy
    from strategies.trend_breakout import TrendBreakoutStrategy

    settings = deepcopy(config._config)
    settings["account"]["mode"] = "spot"
    settings["execution"]["fee_schedule"]["market_type"] = "spot"
    assert settings["strategy_health"]["enabled"] is True
    assert settings["strategy_health"]["probation_min_distinct_symbols"] == 3
    strategy = TrendBreakoutStrategy()
    strategy.configure_health_policy(StrategyHealthPolicy.from_mapping(settings["strategy_health"]))
    index = pd.date_range("2024-01-01", periods=30)
    flat = pd.DataFrame({"open": 100., "high": 101., "low": 99., "close": 100., "volume": 1e6}, index=index)
    frames = {symbol: flat.copy() for symbol in ("A-USDT", "B-USDT", "C-USDT")}
    candidate = {"symbol": "A-USDT", "as_of": "2024-01-22", "strategy": "TrendBreakout",
                 "signal": {"action": "buy", "order_type": "market", "stop_loss": 90.}}
    rows, report = run_exit_label_probe(frames, [candidate], strategies={"TrendBreakout": strategy},
        contract=ExitLabelContract(horizon_bars=5), parameters=settings,
        engine_options={"warmup_period": 0})
    row = rows.iloc[0]
    registration = row.health_registration
    assert registration["policy"]["enabled"] is True
    assert registration["policy"]["probation_min_distinct_symbols"] == 3
    assert registration["eligible_symbols"] == ["A-USDT", "B-USDT", "C-USDT"]
    assert row.registered_symbol_count == 3
    assert row.observation_end == pd.Timestamp("2024-01-27", tz="UTC")
    assert row.outcome_status == "unfilled_or_ineligible_unknown_target"
    assert np.isnan(row.label_net_return)
    assert report["production_protections"] == "preserved"


def test_score_difference_is_explicit_signal_mismatch_and_not_execution_zero_target():
    original = {**candidate(), "signal": {"action": "buy", "stop_loss": 90., "score": 1.}}
    audit = {"original_signal_evaluated": True, "original_signal_reproduced": False,
             "original_signal": {"action": "buy", "stop_loss": 90., "score": 1. + 1e-12}}
    row = exit_label_from_episode(episode([]), candidate=original, contract=ExitLabelContract(),
                                  intervention_id="id", signal_audit=audit)
    assert row["outcome_status"] == "signal_not_reproduced_unknown_target"
    assert np.isnan(row["label_net_return"])
    from research.ml_selection.label_experiments import _same_signal
    assert _same_signal(audit["original_signal"], original["signal"]) is False
