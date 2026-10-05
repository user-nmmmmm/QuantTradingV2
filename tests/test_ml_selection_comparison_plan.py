"""Use completed fixture receipts; no account replay or fitting is performed."""
from copy import deepcopy
import json

import pytest
import pandas as pd
from types import SimpleNamespace

from research.ml_selection.comparison import compare_account_ledgers, plan_comparison


def contract():
    return {"input_hashes": {"A_USDT.csv": "f" * 64}, "symbols": ["A/USDT"],
            "evaluation_start": "2026-01-01", "evaluation_end_exclusive": "2026-01-04",
            "timeframe": "1d", "account_mode": "spot", "initial_capital": 100.,
            "initial_state": {"cash": 100., "positions": {}, "reservations": []},
            "execution": {"commission": .001, "slippage_bps": 5, "terminal_policy": "forced_liquidation"},
            "exit": {"strategy": "original"}, "risk": {"risk_per_trade": .01},
            "allocator": {"name": "original"}, "candidate_domain": "original_strategy_health_hook"}


def ledger():
    return {"schema": "ml-selection-account-ledger/v1", "status": "completed", "accounting_ok": True,
            "terminal_status": "completed", "open_inventory": [], "contract": contract(),
            "equity": [{"timestamp": "2026-01-01", "equity": 110., "gross_exposure_pct_equity": 50.},
                       {"timestamp": "2026-01-02", "equity": 90., "gross_exposure_pct_equity": 50.},
                       {"timestamp": "2026-01-03", "equity": 120., "gross_exposure_pct_equity": 0.}],
            "fills": [{"timestamp": "2026-01-01", "qty": 1., "price": 100., "commission": .1}],
            "orders": [{"id": "order-1"}], "timing": {"end_to_end_seconds": 12.}}


def test_plan_is_complete_factor_split_and_default_never_runs():
    controls = contract()
    report = plan_comparison(controls, frozen_candidate={"id": "frozen"})
    assert len(report["arms"]) == 12
    arms = {row["arm_id"]: row for row in report["arms"]}
    by_name = {name: row["selector_kwargs"] for name, row in arms.items()}
    assert not arms["off"]["selector_enabled"] and by_name["off"] is None
    assert by_name["rl_gate_only"]["selector_contract"]["policy_gate"] and not by_name["rl_gate_only"]["selector_contract"]["return_gate"]
    assert by_name["ranking_only"]["capital_score_source"] == "original_score"
    assert by_name["ranking_only_selector_capital"]["capital_score_source"] == "selector_score"
    assert not report["training_performed"] and not report["backtest_performed"]
    assert not report["formal_admission"]
    assert controls == contract()
    assert report["status"] == "pending_evidence"
    assert report["execution_contract"]["final_holdout"].startswith("unopened")


def test_plan_missing_controls_are_explicit_unknowns_and_identity_stable():
    first = plan_comparison({})
    assert "initial_capital" in first["pending_requirements"]
    assert "frozen_candidate_identity" in first["pending_requirements"]
    assert first["plan_id"] == plan_comparison({})["plan_id"]


def test_every_planned_arm_maps_to_selector_constructor_without_fit_or_predict():
    from research.ml_selection.dataset import FEATURE_COLUMNS
    from research.ml_selection.selector import ResearchSelector
    frame = pd.DataFrame([{**{name: 0. for name in FEATURE_COLUMNS}, "symbol": "A/USDT",
                           "as_of": "2026-01-01", "eligible": True, "exclusion_reason": ""}])
    model = SimpleNamespace(metadata={}, predict=lambda *_: pytest.fail("planning must not score models"))
    policy = SimpleNamespace(metadata={}, features=FEATURE_COLUMNS,
                             act=lambda *_: pytest.fail("planning must not execute policy"))
    for arm in plan_comparison(contract())["arms"]:
        if not arm["selector_enabled"]:
            assert arm["selector_kwargs"] is None
            continue
        selector = ResearchSelector(frame, model=model, policy=policy, **arm["selector_kwargs"])
        assert selector.selector_contract == arm["selector_kwargs"]["selector_contract"]


def test_paired_economics_come_from_matching_complete_curves_and_actual_fills():
    left, right = ledger(), ledger()
    right["equity"][-1]["equity"] = 115.
    report = compare_account_ledgers(left, right)
    assert report["status"] == "compared"
    assert report["right_minus_left"]["net_return"] == pytest.approx(-.05)
    assert report["left"]["max_drawdown"] == pytest.approx(1 - 90 / 110)
    assert report["left"]["fills"] == 1
    assert report["left"]["commission"] == .1
    assert report["left"]["gross_exposure"] == pytest.approx(100 / 3)


@pytest.mark.parametrize("change", ["failed", "inventory", "account", "cost", "calendar", "missing_fills", "accounting"])
def test_invalid_or_unmatched_accounts_never_zero_fill_economic_results(change):
    left, right = ledger(), ledger()
    if change == "failed":
        right["status"] = "failed"
    elif change == "inventory":
        right["open_inventory"] = [{"symbol": "A/USDT", "qty": 1}]
    elif change == "account":
        right["contract"]["account_mode"] = "spot_margin"
    elif change == "cost":
        right["contract"]["execution"]["commission"] = .01
    elif change == "calendar":
        right["equity"].pop(1)
    elif change == "missing_fills":
        del right["fills"]
    elif change == "accounting":
        right["accounting_ok"] = False
    report = compare_account_ledgers(left, right)
    assert report["status"] == "pending_valid_paired_accounts"
    assert report["right"] is None and report["left"] is None
    assert report["right_minus_left"] is None and report["issues"]


def test_verified_cash_result_differs_from_missing_account_evidence():
    cash = ledger()
    for row in cash["equity"]:
        row["equity"] = 100.
        row["gross_exposure_pct_equity"] = 0.
    cash["fills"] = []
    report = compare_account_ledgers(cash, deepcopy(cash))
    assert report["left"]["net_return"] == 0
    assert report["left"]["activity_class"] == "no_actual_fills"
    assert not report["formal_admission"]


def test_missing_optional_metrics_remain_null():
    source = ledger()
    del source["fills"][0]["commission"]
    del source["timing"]
    del source["orders"]
    for row in source["equity"]:
        del row["gross_exposure_pct_equity"]
    report = compare_account_ledgers(source, deepcopy(source))
    assert report["status"] == "compared"
    assert report["left"]["commission"] is None
    assert report["left"]["gross_exposure"] is None
    assert report["right_minus_left"]["end_to_end_seconds"] is None


def test_plan_cli_only_reads_existing_receipts(tmp_path, capsys):
    from scripts.plan_selector_comparison import main
    # Missing portable models/registrations are pending; planning still works.
    controls = tmp_path / "controls.json"
    controls.write_text(json.dumps(contract()))
    left, right = tmp_path / "left.json", tmp_path / "right.json"
    left.write_text(json.dumps(ledger()))
    right.write_text(json.dumps(ledger()))
    output = tmp_path / "plan.json"
    args = ["--settings", str(tmp_path / "missing.yaml"), "--bundle", str(tmp_path / "missing.json"),
            "--controls", str(controls), "--left-ledger", str(left), "--right-ledger", str(right), "--output", str(output)]
    assert main(args) == 0
    report = json.loads(output.read_text())
    assert report["existing_account_comparison"]["status"] == "compared"
    assert not report["training_performed"] and not report["backtest_performed"]
    with pytest.raises(SystemExit):
        main(args)
    capsys.readouterr()
