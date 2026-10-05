"""Selector execution contracts using mocks and frozen inference; no fitting."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pandas as pd
import pytest

from backtest.engine import BacktestEngine
from config.config import config
from core.allocation import EntryCandidate, PortfolioSignalAllocator
from core.runtime import EventProcessor, MarketDataSlice, validate_selector_execution_path
from research.ml_selection.dataset import FEATURE_COLUMNS
from research.ml_selection.selector import (
    ACCOUNT_FEATURES, POLICY_PROBABILITY_SEMANTICS, ResearchSelector,
)


class FixedScoreModel:
    """Predictions supplied as test inputs, without a model fit."""
    kind = "ridge"

    def __init__(self):
        self.inputs = []

    def predict(self, frame):
        self.inputs.append(frame.copy())
        return frame.return_1d.to_numpy(dtype=float)


class FixedPolicy:
    features = (*FEATURE_COLUMNS, *ACCOUNT_FEATURES)

    def __init__(self, metadata=None):
        self.metadata = {"evaluation_threshold": .51, **(metadata or {})}

    def act(self, frame, deterministic=True, threshold=.5):
        probabilities = frame.return_5d.to_numpy(dtype=float)
        return probabilities >= threshold, probabilities


def decision_fixture():
    stamp = pd.Timestamp("2026-09-01", tz="UTC")
    symbols = ("GOOD/USDT", "RETURN_REJECT/USDT", "POLICY_REJECT/USDT", "BOTH_REJECT/USDT")
    rows = []
    candidates = []
    for symbol, expected, probability in zip(symbols, (.04, -.04, .04, -.04), (.9, .9, .1, .1)):
        rows.append({**dict.fromkeys(FEATURE_COLUMNS, 0.), "symbol": symbol,
                     "as_of": stamp + pd.Timedelta(days=1), "eligible": True,
                     "exclusion_reason": "", "return_1d": expected, "return_5d": probability,
                     "label_net_return": 12345.})
        frame = pd.DataFrame({"close": [100.]}, index=[stamp])
        candidates.append(EntryCandidate(symbol, SimpleNamespace(name="OriginalStrategy"),
                          0, frame, "trend", {"action": "buy", "requested_qty": 10.}, 1.5))
    portfolio = SimpleNamespace(cash=10000., positions={}, get_total_value=lambda _: 10000.)
    event = MarketDataSlice(stamp, {s: pd.Series({"close": 100.}) for s in symbols},
                            {}, timeframe="1d")
    context = {"event": event, "portfolio": portfolio,
               "broker": SimpleNamespace(pending_orders=[], active_orders=[]),
               "risk_manager": SimpleNamespace(), "current_prices": dict.fromkeys(symbols, 100.)}
    return pd.DataFrame(rows), candidates, context


def test_legacy_policy_only_retains_negative_parent_prediction_and_original_signal():
    dataset, candidates, context = decision_fixture()
    model = FixedScoreModel()
    selector = ResearchSelector(dataset, mode="policy", model=model, policy=FixedPolicy())
    selected = selector.select(candidates, **context)
    assert [c.symbol for c in selected] == [candidates[0].symbol, candidates[1].symbol]
    assert selected[1].signal is candidates[1].signal
    assert selected[1].strategy is candidates[1].strategy
    assert selected[1].frame is candidates[1].frame
    negative = selector.audit[1]
    assert negative["gate_contract"] == "policy_only"
    assert negative["gate_contract_source"] == "legacy_default"
    assert negative["return_gate_passed"] is False
    assert negative["return_gate_applied"] is False
    assert negative["policy_gate_applied"] is True
    assert negative["policy_action"] is True and negative["effective_gate_passed"] is True
    assert negative["selection_probability_semantics"] == POLICY_PROBABILITY_SEMANTICS
    assert negative["effective_rejection_reasons"] == []
    assert not any(c.startswith("label_") for c in selector.table)
    assert not any(c.startswith("label_") for c in model.inputs[0])
    assert [row["reason"] for row in selector.audit] == ["selected", "selected", "policy_gate", "policy_gate"]


def test_policy_and_return_records_each_effective_rejection_and_exact_funnel():
    dataset, candidates, context = decision_fixture()
    selector = ResearchSelector(dataset, mode="policy", model=FixedScoreModel(), policy=FixedPolicy(),
                                policy_gate_mode="policy_and_return")
    assert [c.symbol for c in selector.select(candidates, **context)] == [candidates[0].symbol]
    assert [r["reason"] for r in selector.audit] == [
        "selected", "return_gate", "policy_gate", "policy_and_return_gate"]
    assert selector.audit[-1]["effective_rejection_reasons"] == ["return_gate", "policy_gate"]
    assert all(row["return_gate_applied"] and row["policy_gate_applied"] for row in selector.audit)
    summary = selector.summary()
    assert summary["candidate_count"] == summary["data_qualified_count"] == summary["scored_count"] == 4
    assert summary["selected_count"] == 1 and summary["gate_rejected_count"] == 3
    assert summary["data_rejected_count"] == 0
    assert summary["selected_count_semantics"] == "selector_acceptance_not_order_or_fill"


def test_future_policy_metadata_contract_is_used_and_explicit_override_is_audited():
    dataset, candidates, context = decision_fixture()
    policy = FixedPolicy({"policy_gate_mode": "policy_and_return"})
    inherited = ResearchSelector(dataset, mode="policy", model=FixedScoreModel(), policy=policy)
    assert len(inherited.select(candidates, **context)) == 1
    assert inherited.summary()["gate_contract_source"] == "policy_metadata"
    overridden = ResearchSelector(dataset, mode="policy", model=FixedScoreModel(), policy=policy,
                                  policy_gate_mode="policy_only")
    assert len(overridden.select(candidates, **context)) == 2
    assert overridden.summary()["gate_contract_source"] == "explicit_override"


def test_stochastic_trajectory_keeps_sampled_action_when_return_gate_blocks_execution():
    dataset, candidates, context = decision_fixture()
    policy = FixedPolicy()
    policy.act = Mock(return_value=(np.ones(4, dtype=bool), np.full(4, .9)))
    selector = ResearchSelector(dataset, mode="policy", model=FixedScoreModel(), policy=policy,
                                policy_gate_mode="policy_and_return", deterministic=False)
    assert len(selector.select(candidates, **context)) == 2
    assert selector.trajectory[1]["action"] is True
    assert selector.trajectory[1]["effective_action"] is False
    assert selector.audit[1]["policy_action"] is True
    assert selector.audit[1]["effective_gate_passed"] is False


def test_missing_and_ineligible_candidates_are_separate_from_scored_gate_rejections():
    dataset, candidates, context = decision_fixture()
    dataset = dataset.iloc[:3].copy()
    dataset.loc[1, ["eligible", "exclusion_reason"]] = [False, "insufficient_liquidity"]
    selector = ResearchSelector(dataset, mode="policy", model=FixedScoreModel(), policy=FixedPolicy())
    selector.select(candidates, **context)
    summary = selector.summary()
    assert (summary["candidate_count"], summary["data_qualified_count"], summary["scored_count"],
            summary["selected_count"], summary["data_rejected_count"], summary["gate_rejected_count"]) == (4, 2, 2, 1, 2, 1)
    rejected = [r for r in selector.audit if not r["data_qualified"]]
    assert all(r["policy_gate_applied"] is False and r["return_gate_applied"] is False for r in rejected)
    assert all(r["policy_action"] is None and r["return_gate_passed"] is None for r in rejected)
    assert set(r["reason"] for r in rejected) == {"insufficient_liquidity", "missing_causal_snapshot"}


@pytest.mark.parametrize("mode", ["model", "momentum", "random", "qualified_native"])
def test_non_policy_modes_keep_their_gates_and_do_not_describe_profit_probabilities(mode):
    dataset, candidates, context = decision_fixture()
    selector = ResearchSelector(dataset, mode=mode, model=FixedScoreModel())
    selected = selector.select(candidates, **context)
    assert len(selected) == (2 if mode == "model" else 4)
    assert selector.summary()["selection_probability_semantics"] is None
    assert all(r["policy_gate_applied"] is False for r in selector.audit)
    assert all(r["return_gate_applied"] is (mode == "model") for r in selector.audit)


@pytest.mark.parametrize("options", [
    {"policy_gate_mode": "unknown"},
    {"mode": "model", "policy_gate_mode": "policy_and_return"},
    {"policy": FixedPolicy({"policy_gate_mode": "unknown"})},
])
def test_invalid_gate_contract_fails_before_inference(options):
    dataset, _, _ = decision_fixture()
    defaults = {"mode": "policy", "model": FixedScoreModel(), "policy": FixedPolicy()}
    with pytest.raises(ValueError, match="policy_gate_mode|requires policy selection"):
        ResearchSelector(dataset, **{**defaults, **options})


@pytest.mark.parametrize("actions", [np.full(4, np.nan), np.full(4, .2), np.ones(3)])
def test_non_binary_or_malformed_policy_actions_fail_closed(actions):
    dataset, candidates, context = decision_fixture()
    policy = FixedPolicy()
    policy.act = Mock(return_value=(actions, np.full(4, .9)))
    selector = ResearchSelector(dataset, mode="policy", model=FixedScoreModel(), policy=policy)
    with pytest.raises(ValueError, match="malformed policy"):
        selector.select(candidates, **context)
    assert selector.audit == []


def test_shared_runtime_refuses_selector_and_target_controller_before_accessing_dependencies():
    with pytest.raises(ValueError, match="coin selection.*portfolio target controller"):
        EventProcessor(portfolio=object(), execution=object(), risk_manager=object(),
            state_machine=object(), router=object(), allocator=object(),
            candidate_selector=object(), portfolio_controller=object())


@pytest.mark.parametrize("from_config", [False, True])
def test_engine_refuses_both_explicit_and_actual_v3_configured_target_paths(monkeypatch, from_config):
    from scripts.run_trend_portfolio_v3 import effective_config
    from tests.test_v3_engine_integration import synthetic_market, spec
    _, metadata = synthetic_market(length=225, count=3)
    settings = effective_config(deepcopy(config._config), spec(financing="verified_only"))
    settings["portfolio_targets"] = {"enabled": from_config, "metadata": metadata}
    monkeypatch.setattr(config, "_config", settings)
    with pytest.raises(ValueError, match="coin selection.*portfolio target controller"):
        BacktestEngine(candidate_selector=Mock(), portfolio_controller=None if from_config else object())


def test_engine_rechecks_configuration_when_target_execution_is_enabled_after_construction(monkeypatch):
    settings = deepcopy(config._config)
    settings["portfolio_targets"] = {"enabled": False}
    monkeypatch.setattr(config, "_config", settings)
    engine = BacktestEngine(candidate_selector=Mock(), timeframe="1d")
    settings["portfolio_targets"]["enabled"] = True
    with pytest.raises(ValueError, match="coin selection.*portfolio target controller"):
        engine.run({})


def test_target_execution_still_runs_when_selector_is_disabled(monkeypatch):
    from scripts.run_trend_portfolio_v3 import effective_config
    from tests.test_v3_engine_integration import synthetic_market, spec
    frames, metadata = synthetic_market(length=225, count=3)
    settings = effective_config(deepcopy(config._config), spec(financing="verified_only"))
    settings["account"]["mode"] = "spot"
    settings["execution"]["fee_schedule"]["market_type"] = "spot"
    settings["portfolio_targets"] = {"enabled": True, "metadata": metadata,
        "cost_aware": {"enabled": True, "rebalance": "partial", "max_turnover_weight": .15}}
    monkeypatch.setattr(config, "_config", settings)
    engine = BacktestEngine(initial_capital=10000., warmup_period=0, timeframe="1d",
        trading_start="2023-07-19", terminal_policy="valuation_only", calculate_benchmarks=False)
    result = engine.run(frames, routing_log_enabled=False)
    assert engine.portfolio_controller is not None
    assert result["accounting_check"]["ok"] and len(result["trades"]) == 18


def test_execution_guard_allows_existing_selector_and_controller_paths_individually():
    validate_selector_execution_path(selector_enabled=False, portfolio_targets_enabled=True)
    validate_selector_execution_path(selector_enabled=False, portfolio_controller=object())
    validate_selector_execution_path(selector_enabled=True)


def test_frozen_weights_keep_the_original_policy_only_decisions_without_any_fit():
    pytest.importorskip("lightgbm")
    from backtest.coin_selector import read_bundle
    from research.ml_selection.models import load_model
    path, package = read_bundle()
    model = load_model(path.parent / package["files"]["model"]["path"])
    policy = load_model(path.parent / package["files"]["policy"]["path"])
    dataset, candidates, context = decision_fixture()
    selector = ResearchSelector(dataset, mode="policy", model=model, policy=policy,
                                initial_capital=10000., **package["selection"])
    selected = selector.select(candidates, **context)
    served = pd.DataFrame([{**{c: row[c] for c in FEATURE_COLUMNS},
        "cash_fraction": 1., "gross_exposure_fraction": 0., "portfolio_drawdown": 0.,
        "held_count_fraction": 0., "pending_count_fraction": 0.} for _, row in dataset.iterrows()])
    expected, probabilities = policy.act(served, deterministic=True, threshold=.51)
    assert [c.symbol for c in selected] == [c.symbol for c, admit in zip(candidates, expected) if admit]
    np.testing.assert_array_equal([r["selection_probability"] for r in selector.audit], probabilities)
    assert selector.gate_contract == "policy_only"
    assert model.model_id == package["candidate"]["parent_model_id"]
    assert policy.model_id == package["candidate"]["model_id"]


@pytest.mark.parametrize("flags, expected, model_calls, policy_calls", [
    ((False, False, False, False), (0, 1, 2, 3), 0, 0),
    ((True, False, False, False), (0, 2, 3), 0, 0),
    ((False, True, False, False), (0, 1, 2, 3), 1, 0),
    ((False, False, True, False), (0, 2), 1, 0),
    ((False, False, False, True), (0, 1), 0, 1),
    ((True, True, True, True), (0,), 1, 1),
])
def test_registered_ablation_arms_are_executable_without_fit(flags, expected, model_calls, policy_calls):
    dataset, candidates, context = decision_fixture()
    dataset.loc[1, ["eligible", "exclusion_reason"]] = [False, "insufficient_liquidity"]
    contract = dict(zip(("eligibility_filter", "ranking", "return_gate", "policy_gate"), flags))
    model, policy = FixedScoreModel(), FixedPolicy()
    policy.act = Mock(wraps=policy.act)
    selector = ResearchSelector(dataset, mode="policy", model=model, policy=policy,
                                selector_contract=contract, capital_score_source="original_score")
    selected = selector.select(candidates, **context)
    assert [c.symbol for c in selected] == [candidates[i].symbol for i in expected]
    assert len(model.inputs) == model_calls and policy.act.call_count == policy_calls
    assert all(c.score == candidates[i].score for c, i in zip(selected, expected))
    assert all(r["selector_contract"] == contract for r in selector.audit)
    assert selector.summary()["capital_score_source"] == "original_score"
    assert selector.summary()["scored_count"] == (len(model.inputs[0]) if model_calls
                                                  else 4 if policy_calls else 0)


def test_original_capital_score_preserves_strategy_score_but_ml_rank_changes_allocator_order():
    dataset, candidates, context = decision_fixture()
    candidates = [replace(c, score=float(score)) for c, score in zip(candidates, (.5, 5., 1., 4.))]
    contract = {"eligibility_filter": True, "ranking": True, "return_gate": False, "policy_gate": False}
    selector = ResearchSelector(dataset, mode="policy", model=FixedScoreModel(), selector_contract=contract,
                                capital_score_source="original_score")
    selected = selector.select(candidates, **context)
    assert [c.score for c in selected] == [.5, 5., 1., 4.]
    assert [c.symbol for c in PortfolioSignalAllocator.rank(selected)] == [
        candidates[0].symbol, candidates[2].symbol, candidates[3].symbol, candidates[1].symbol]
    assert PortfolioSignalAllocator.rank(selected) != PortfolioSignalAllocator.rank(candidates)
    assert all(row["allocation_score"] == c.score for row, c in zip(selector.audit, candidates))


def test_capital_score_ablation_holds_explicit_ranking_fixed_and_keeps_legacy_default():
    dataset, candidates, context = decision_fixture()
    contract = {"eligibility_filter": True, "ranking": True, "return_gate": False, "policy_gate": False}
    treatments = []
    for source in ("original_score", "selector_score"):
        selector = ResearchSelector(dataset, mode="model", model=FixedScoreModel(),
                                    selector_contract=contract, capital_score_source=source)
        treatments.append(selector.select(candidates, **context))
    assert [c.selection_rank_score for c in treatments[0]] == [c.selection_rank_score for c in treatments[1]]
    assert all(c.selection_rank_score is not None for c in treatments[1])
    assert [c.score for c in treatments[0]] == [c.score for c in candidates]
    assert [c.score for c in treatments[1]] != [c.score for c in candidates]
    legacy = ResearchSelector(dataset, mode="policy", model=FixedScoreModel(), policy=FixedPolicy())
    selected = legacy.select(candidates, **context)
    assert selected and all(c.selection_rank_score is None for c in selected)


def test_disabled_ablation_can_pass_original_candidates_without_snapshots_or_models():
    dataset, candidates, context = decision_fixture()
    contract = dict.fromkeys(("eligibility_filter", "ranking", "return_gate", "policy_gate"), False)
    selector = ResearchSelector(dataset.iloc[:0], mode="policy", selector_contract=contract)
    selected = selector.select(candidates, **context)
    assert all(a is b for a, b in zip(selected, candidates))
    assert selector.summary()["scored_count"] == 0
    assert selector.summary()["gate_contract"] == "passthrough"
    assert selector.summary()["data_qualified_count"] == selector.summary()["data_rejected_count"] == 0
    assert selector.summary()["data_qualification_not_evaluated_count"] == 4
    assert selector.summary()["gate_rejected_count"] == 0
    assert selector.summary()["selected_without_data_qualification_count"] == 4


def test_disabling_eligibility_does_not_admit_unavailable_causal_model_inputs():
    dataset, candidates, context = decision_fixture()
    dataset.loc[0, "return_1d"] = np.nan
    contract = {"eligibility_filter": False, "ranking": True, "return_gate": False, "policy_gate": False}
    selector = ResearchSelector(dataset, mode="policy", model=FixedScoreModel(), selector_contract=contract)
    assert len(selector.select(candidates, **context)) == 3
    assert selector.audit[0]["reason"] == "feature_unavailable"
    assert selector.audit[0]["effective_rejection_reasons"] == ["feature_unavailable"]


@pytest.mark.parametrize("contract", [
    {}, {"ranking": True}, dict.fromkeys(("eligibility_filter", "ranking", "return_gate", "policy_gate"), 1),
    {"eligibility_filter": True, "ranking": True, "return_gate": False, "policy_gate": False, "extra": False},
])
def test_ablation_requires_exact_boolean_contract(contract):
    dataset, _, _ = decision_fixture()
    with pytest.raises(ValueError, match="selector_contract requires four boolean"):
        ResearchSelector(dataset, selector_contract=contract)
