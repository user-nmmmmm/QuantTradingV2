"""Infeasible real terminal liquidity remains a failed research stress result."""
from types import SimpleNamespace
import json

import pytest

from research.ml_selection import pipeline
from research.ml_selection.environment import FullEngineEnvironment
from tests.test_ml_selection_environment import OneUnitHold, PassThrough, market, parameters
from tests.test_ml_selection_pipeline import ScoreModel, settings


LIQUIDITY_ERROR = "End-window exit cannot fill within actual liquidity"


def failing_real_environment():
    frames = market()
    frames["TEST-USDT"].iloc[-1, frames["TEST-USDT"].columns.get_loc("volume")] = .25
    configured = parameters()
    configured["execution"]["max_participation_rate"] = 1.
    return FullEngineEnvironment(frames, parameters=configured,
        engine_options={"initial_capital": 1000., "slippage": 0., "warmup_period": 0,
                        "terminal_policy": "forced_liquidation"},
        strategies={"OneUnitHold": OneUnitHold()})


def test_real_incomplete_terminal_exit_preserves_partial_fills_and_positions(tmp_path):
    env = failing_real_environment()
    result = pipeline._stress_episode(tmp_path, "low_participation", env, PassThrough())
    assert result["status"] == "failed_end_window_liquidity"
    assert result["execution_completed"] is False
    assert result["partial_evidence_status"] == "available_original_engine_state"
    for metric in ("net_return", "reward_sum", "max_drawdown", "accounting_ok", "terminated_by_risk"):
        assert result[metric] is None
    target = tmp_path / result["artifact_directory"]
    partial = json.loads((target / "partial_evidence.json").read_text(encoding="utf-8"))
    assert partial["terminal_policy"] == "forced_liquidation"
    assert partial["terminal_contract_satisfied"] is False
    assert partial["positions_at_failure"]["TEST-USDT"]["qty"] == .75
    assert partial["open_lots_at_failure"]["TEST-USDT"][0]["qty_open"] == .75
    assert [(row["side"], row["qty"]) for row in partial["fills"]] == [("buy", 1.), ("sell", .25)]
    assert partial["fills"][-1]["exit_reason"] == "EndOfBacktest"
    assert partial["last_market_time"].startswith("2024-01-05")
    assert partial["recorded_fill_count"] == 2
    assert (target / "trades.csv").is_file()
    # The original environment still refuses completion; reporting never force-fills it.
    assert env.active_engine.event_processor.portfolio.get_position("TEST-USDT")["qty"] == .75


def setup_evaluation(tmp_path, monkeypatch, failure_scope, *, error=LIQUIDITY_ERROR):
    configured = settings()
    configured["next_research"] = {}
    configured["gates"] = {"require_positive_after_removing_top_5": False}
    configured["stress"] = {"cost_multipliers": [1.5, 2.],
        "execution_scenarios": [{"name": "low_participation"}, {"name": "limited_cash"}]}
    parent = ScoreModel()
    frozen = {"protocol_id": "synthetic-protocol", "settings": configured,
              "data_evidence": {"symbols": ["A/USDT"]}}
    pipeline.save_json(tmp_path / "candidate.json", {"selected_candidate": "ridge", "model_id": parent.model_id})
    def summary(value=.03):
        return {"net_return": value, "reward_sum": value, "max_drawdown": .02,
                "accounting_ok": True, "terminated_by_risk": False, "actual_fill_count": 2}
    monkeypatch.setattr(pipeline, "matrix", lambda *args, **kwargs:
                        {"native": summary(.01), "ridge": summary(.03)})
    monkeypatch.setattr(pipeline, "selection", lambda *args, **kwargs: SimpleNamespace(audit=[]))
    monkeypatch.setattr(pipeline, "persist_episode", lambda *args, **kwargs: summary())
    monkeypatch.setattr(pipeline, "concentration_check", lambda *args, **kwargs: {"status": "insufficient"})
    calls = []
    def environment(*args, multiplier=1., execution_scenario=None, **kwargs):
        name = execution_scenario["name"] if execution_scenario else str(multiplier)
        calls.append(name)
        failed = name == ("low_participation" if failure_scope == "execution" else "1.5")
        def run(selector):
            if failed:
                raise ValueError(error)
            return SimpleNamespace()
        return SimpleNamespace(run_episode=run)
    monkeypatch.setattr(pipeline, "make_environment", environment)
    return frozen, {"ridge": parent, "primary": parent}, calls


@pytest.mark.parametrize("failure_scope", ["execution", "cost"])
def test_expected_stress_failure_is_retained_and_later_scenarios_run(tmp_path, monkeypatch, failure_scope):
    frozen, models, calls = setup_evaluation(tmp_path, monkeypatch, failure_scope)
    result = pipeline.evaluate(tmp_path, frozen, {}, None, models)
    assert calls == ["1.5", "2.0", "low_participation", "limited_cash"]
    execution = json.loads((tmp_path / "execution_stress.json").read_text(encoding="utf-8"))
    cost = json.loads((tmp_path / "cost_stress.json").read_text(encoding="utf-8"))
    assert execution["limited_cash"]["execution_completed"] is True
    failed_name = "low_participation" if failure_scope == "execution" else "1.5"
    failed = execution[failed_name] if failure_scope == "execution" else cost[failed_name]
    assert failed["status"] == "failed_end_window_liquidity"
    assert failed["partial_evidence_status"] == "unavailable"
    assert failed["net_return"] is None and failed["accounting_ok"] is None
    assert result["checks"][f"{failure_scope}_stress_completed"] is False
    assert result["retrospective_checks_passed"] is False
    assert result["formal_admission"] is False
    assert result["status"] == "retrospective_research_only"
    assert result["separate_verdicts"]["accounting"]["all_stress"] is False
    assert result["separate_verdicts"]["accounting"]["unknown_stress_accounting"] == [failed_name]
    if failure_scope == "execution":
        assert result["separate_verdicts"]["risk"]["stress_risk_terminations"][failed_name] is None
    else:
        assert result["checks"]["cost_1_5_positive"] is None
        assert result["separate_verdicts"]["return"]["cost_stress_positive"][failed_name] is None


def test_unknown_stress_value_error_still_aborts(tmp_path, monkeypatch):
    frozen, models, calls = setup_evaluation(tmp_path, monkeypatch, "execution", error="malformed matching book")
    with pytest.raises(ValueError, match="malformed matching book"):
        pipeline.evaluate(tmp_path, frozen, {}, None, models)
    assert "limited_cash" not in calls


def test_known_error_in_ordinary_matrix_is_not_silently_a_stress_result(tmp_path, monkeypatch):
    frozen, models, _ = setup_evaluation(tmp_path, monkeypatch, "execution")
    def matrix(*args, **kwargs):
        raise ValueError(LIQUIDITY_ERROR)
    monkeypatch.setattr(pipeline, "matrix", matrix)
    with pytest.raises(ValueError, match="End-window exit cannot fill"):
        pipeline.evaluate(tmp_path, frozen, {}, None, models)
