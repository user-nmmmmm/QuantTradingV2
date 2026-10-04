"""Real execution stress and frozen next-round budget contract checks."""
from copy import deepcopy
import importlib

import pandas as pd
import pytest

from research.ml_selection.environment import FullEngineEnvironment
from research.ml_selection.execution_stress import opening_delay
from research.ml_selection.next_round import event_concentration, formal_budget_summary, pilot_status
from research.ml_selection.protocol import load_settings, validate_settings
from tests.test_ml_selection_environment import OneUnitHold, market, parameters


def episode(delay=0):
    return FullEngineEnvironment(market(), parameters=parameters(),
        engine_options={"initial_capital": 1000., "slippage": 0., "warmup_period": 0},
        strategies={"OneUnitHold": OneUnitHold()}, opening_delay_bars=delay).run_episode()


def test_delayed_entries_match_later_actual_open_and_exits_are_not_delayed():
    baseline, delayed = episode(), episode(1)
    initial = [row for row in baseline.result["trades"] if row["side"] == "buy"]
    stressed = [row for row in delayed.result["trades"] if row["side"] == "buy"]
    assert pd.Timestamp(stressed[0]["fill_time"]) - pd.Timestamp(initial[0]["fill_time"]) == pd.Timedelta(days=1)
    assert stressed[0]["fill_price"] == pytest.approx(101.)
    assert initial[0]["fill_price"] == pytest.approx(100.)
    assert delayed.summary["accounting_ok"] is True
    assert any(row["reason"] == "research_opening_delay" for row in delayed.result["execution_audit"])


def test_delay_scope_restores_original_broker_even_after_failure():
    module = importlib.import_module("backtest.engine")
    original = module.Broker
    with pytest.raises(RuntimeError):
        with opening_delay(2):
            assert module.Broker is not original
            raise RuntimeError("interrupt")
    assert module.Broker is original
    with opening_delay(0):
        assert module.Broker is original


def test_delayed_entry_keeps_original_ttl_and_releases_expired_order():
    settings = parameters()
    settings["execution"]["opening_order_ttl_bars"] = 1
    result = FullEngineEnvironment(market(), parameters=settings,
        engine_options={"initial_capital": 1000., "slippage": 0., "warmup_period": 0},
        strategies={"OneUnitHold": OneUnitHold()}, opening_delay_bars=3).run_episode()
    assert not any(row["side"] == "buy" for row in result.result["trades"])
    expiry = [row for row in result.result["execution_audit"] if row["reason"] == "opening_order_ttl"]
    assert expiry and expiry[0]["age_bars"] == 2
    assert pd.Timestamp(expiry[0]["timestamp"]) == pd.Timestamp("2024-01-03")


def test_event_groups_merge_transitively_overlapping_actual_positions():
    rows = pd.DataFrame([
        {"entry_time": "2024-01-01", "exit_time": "2024-01-03", "net_pnl": 10},
        {"entry_time": "2024-01-02", "exit_time": "2024-01-05", "net_pnl": -2},
        {"entry_time": "2024-01-04", "exit_time": "2024-01-06", "net_pnl": 5},
        {"entry_time": "2024-01-07", "exit_time": "2024-01-08", "net_pnl": -1},
    ])
    result = event_concentration(rows)
    assert result["event_count"] == 2
    assert result["events"][0]["positions"] == 3
    assert result["net_pnl"] == 12
    assert result["remaining_after_top_5"] == -1
    assert result["status"] == "insufficient"
    assert result["independent_events_proven"] is False


def test_missing_or_unclosed_events_are_not_invented_independent_samples():
    assert event_concentration(pd.DataFrame())["status"] == "insufficient"
    assert event_concentration(pd.DataFrame([{ "entry_time": "2024-01-02", "exit_time": "2024-01-01", "net_pnl": 100}]))["status"] == "insufficient"


def test_next_protocol_preregisters_two_rl_windows_and_unopened_future_sample():
    settings = load_settings("config/ml_selection_next.yaml")
    assert settings["rl"]["min_updates"] == 20
    assert settings["rl"]["seeds"] == [42, 43, 44]
    assert settings["next_research"]["rl_windows"] == [4]
    assert settings["next_research"]["experiment_budget"]["minimum_formal_updates"] == 120
    assert settings["next_research"]["final_sample"]["opened"] is False
    assert settings["evaluation_kind"] == "retrospective"


def test_formal_budget_requires_every_registered_seed_and_window(tmp_path):
    from research.ml_selection.protocol import save_json
    settings = load_settings("config/ml_selection_next.yaml")
    missing = tmp_path / "walk_forward/window_04/rl_seeds/44/rl_budget_receipt.json"
    for parent in [tmp_path, tmp_path / "walk_forward/window_04"]:
        for seed in [42, 43, 44]:
            path = parent / "rl_seeds" / str(seed) / "rl_budget_receipt.json"
            if path != missing:
                save_json(path, {"actual_updates": 20, "minimum_budget_met": True})
    partial = formal_budget_summary(tmp_path, settings)
    assert partial["actual_updates"] == 100
    assert len(partial["expected_rl_budget_cells"]) == 6
    assert partial["missing_rl_budget_cells"] == ["walk_forward/window_04/rl_seeds/44"]
    assert partial["all_minimum_budgets_met"] is False
    save_json(missing, {"actual_updates": 20, "minimum_budget_met": True})
    complete = formal_budget_summary(tmp_path, settings)
    assert complete["actual_updates"] == 120 and complete["all_minimum_budgets_met"]


@pytest.mark.parametrize("registered,completed,failed,status", [
    (9,9,0,"pilot_completed"), (9,8,1,"pilot_completed_with_failures"),
    (9,0,9,"pilot_failed"), (0,0,0,"pilot_failed"), (9,8,0,"pilot_completed_with_failures"),
])
def test_pilot_status_does_not_hide_missing_or_failed_cells(registered,completed,failed,status):
    assert pilot_status({"registered_cells":registered,"completed_cells":completed,"failed_cells":failed}) == status


@pytest.mark.parametrize("change", [
    {"rl": {"min_updates": 31}},
    {"rl": {"evaluation_thresholds": [.5, .5]}},
    {"rl": {"evaluation_threshold": 1.1}},
    {"stress": {"execution_scenarios": [{"name": "../escape"}]}},
    {"stress": {"execution_scenarios": [{"name": "delay", "opening_delay_bars": True}]}},
    {"next_research": {"rl_windows": [5]}},
])
def test_invalid_next_controls_fail_before_freeze(change):
    settings = deepcopy(load_settings("config/ml_selection_next.yaml"))
    for section, values in change.items():
        settings[section].update(values)
    with pytest.raises(ValueError):
        validate_settings(settings)
