"""Financing is account cash cost, not invented per-cohort attribution."""
import hashlib
import json

import pytest

from scripts.strategy_review_cost_evidence import (
    cost_adjusted_gates, cost_adjusted_status, financing_evidence,
)
from scripts.strategy_review_report_helpers import verified_runs


HEADER = "timestamp,symbol,kind,rate,notional,amount,source\n"


def save(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def ledger(path, amounts):
    path.write_text(HEADER + "".join(
        f"2024-01-01,__QUOTE__,{'funding' if amount < 0 else 'quote_borrow'},0.08,100,{amount},configured_default\n"
        for amount in amounts), encoding="utf-8")


@pytest.mark.parametrize("content", ["", "\n", HEADER])
def test_existing_empty_financing_ledger_is_verified_zero(tmp_path, content):
    (tmp_path / "financing_ledger.csv").write_text(content, encoding="utf-8")
    result = financing_evidence(tmp_path)
    assert result["financing_net_expense"] == 0
    assert result["financing_row_count"] == 0
    assert result["financing_ledger_nonempty"] is False
    assert result["financing_ledger_status"] == "verified_zero"
    assert result["cohort_admission_status"] == "cost_complete"


def test_nonzero_borrow_expense_keeps_account_fact_but_disallows_cost_complete_cohort_claim(tmp_path):
    ledger(tmp_path / "financing_ledger.csv", [0.22229197176750623, 0.05169414673880706])
    result = financing_evidence(tmp_path)
    assert result["financing_net_expense"] == pytest.approx(0.2739861185063133)
    assert result["financing_ledger_nonempty"]
    assert result["financing_kinds"] == ["quote_borrow"]
    assert result["cohort_admission_status"] == "insufficient"
    assert result["financing_allocated_to_cohorts"] is False
    assert "unallocated" in result["cost_scope"]


@pytest.mark.parametrize("amounts", [[-1.0], [1.0, -1.0]])
def test_funding_credits_and_zero_net_carry_do_not_make_cohort_allocation_known(tmp_path, amounts):
    ledger(tmp_path / "financing_ledger.csv", amounts)
    result = financing_evidence(tmp_path)
    assert result["financing_nonzero_row_count"] == len(amounts)
    assert result["financing_net_expense"] == sum(amounts)
    assert result["financing_gross_credit"] == 1
    assert result["cohort_admission_status"] == "insufficient"
    assert result["financing_attribution_status"] == "unallocated_signed_carry"


def test_missing_ledger_is_unknown_instead_of_zero(tmp_path):
    result = financing_evidence(tmp_path)
    assert result["financing_ledger_status"] == "unknown"
    assert result["financing_net_expense"] is None
    assert result["financing_ledger_nonempty"] is None
    assert result["cohort_admission_status"] == "insufficient"


@pytest.mark.parametrize("content", [
    "amount\n1\n", HEADER + "not,a,complete,row\n",
    HEADER + "2024-01-01,__QUOTE__,quote_borrow,0.08,100,nan,configured_default\n",
    HEADER + "2024-01-01,__QUOTE__,quote_borrow,0.08,100,nonsense,configured_default\n",
    HEADER + "invalid-time,__QUOTE__,quote_borrow,0.08,100,0,configured_default\n",
])
def test_malformed_ledger_is_unknown_instead_of_zero(tmp_path, content):
    (tmp_path / "financing_ledger.csv").write_text(content, encoding="utf-8")
    result = financing_evidence(tmp_path)
    assert result["financing_ledger_status"] == "unknown"
    assert result["financing_net_expense"] is None
    assert result["cohort_admission_status"] == "insufficient"


def closed_account(path, *, final=10009.0, open_position=False, forced=False):
    save(path / "summary.json", dict(name="fixture", initial_capital=10000, final_equity=final, forced_exit=forced))
    save(path / "accounting_check.json", dict(ok=True))
    save(path / "close_events.json", [dict(realized_pnl=10.0, exit_reason="EndOfBacktest" if open_position or forced else "signal")])
    (path / "trades.csv").write_text("symbol,side,qty,exit_reason\nBTC/USDT,buy,1,signal\n" +
        ("" if open_position else "BTC/USDT,sell,1,EndOfBacktest\n" if forced else "BTC/USDT,sell,1,signal\n"), encoding="utf-8")
    (path / "equity_engine.csv").write_text(
        f"timestamp,equity,gross_exposure\n2024-01-01,{final},{100 if open_position else 0}\n", encoding="utf-8")


@pytest.mark.parametrize("forced", [False, True])
def test_flat_account_can_reconcile_total_net_closed_pnl_without_redistributing_groups(tmp_path, forced):
    ledger(tmp_path / "financing_ledger.csv", [1.0])
    closed_account(tmp_path, forced=forced)
    result = financing_evidence(tmp_path)
    assert result["close_event_pnl_ex_financing"] == 10
    assert result["account_net_pnl_all_costs"] == 9
    assert result["all_positions_fully_closed"]
    assert result["net_closed_after_financing"] == 9
    assert result["closed_net_reconciliation_status"] == "verified_flat_account"
    assert result["cohort_admission_status"] == "insufficient"
    assert not result["financing_allocated_to_cohorts"]


def test_open_or_valuation_transferred_book_cannot_claim_definitive_net_closed_pnl(tmp_path):
    ledger(tmp_path / "financing_ledger.csv", [1.0])
    closed_account(tmp_path, open_position=True)
    result = financing_evidence(tmp_path)
    assert result["close_event_pnl_ex_financing"] == 10
    assert result["actual_close_event_pnl_ex_financing"] == 0
    assert result["valuation_close_event_pnl"] == 10
    assert result["all_positions_fully_closed"] is False
    assert result["net_closed_after_financing"] is None


def test_flat_claim_requires_accounting_identity_match(tmp_path):
    ledger(tmp_path / "financing_ledger.csv", [1.0])
    closed_account(tmp_path, final=10008.0)
    result = financing_evidence(tmp_path)
    assert result["closed_net_reconciliation_status"] == "mismatch"
    assert result["net_closed_after_financing"] is None


def test_only_unsupported_positive_cost_claims_are_masked_and_raw_failures_stay_visible():
    gates = dict(cohort_support="pass", profit_factor="pass", positive_net_return="pass",
                 drawdown="fail", remove_top5_top10="fail")
    costs = dict(cohort_admission_status="insufficient")
    result = cost_adjusted_gates(gates, costs)
    assert result["profit_factor"] == "insufficient"
    assert result["drawdown"] == result["remove_top5_top10"] == "fail"
    assert result["positive_net_return"] == "pass"
    assert gates["profit_factor"] == "pass"
    assert cost_adjusted_status("pass", costs) == "insufficient"
    assert cost_adjusted_status("fail", costs) == "fail"


def test_verified_run_report_preserves_raw_artifacts_and_joins_corrected_cost_scope(tmp_path):
    protocol = dict(baseline_specs=[], validation_specs=[dict(name="fixture")], arms=[], rolling_windows=[])
    save(tmp_path / "review_protocol.json", protocol)
    folder = tmp_path / "validation_results/runs/fixture"
    folder.mkdir(parents=True)
    closed_account(folder)
    ledger(folder / "financing_ledger.csv", [1.0])
    gates = {key: "pass" for key in ("cohort_support", "profit_factor", "positive_net_return", "drawdown", "remove_top5_top10")}
    save(folder / "review_research_gates.json", gates)
    save(folder / "review_cohort_evidence.json", dict(cohort_count=35, sample_status="sufficient", scenarios={"0":dict(profit_factor=2, pf_95pct_ci=[1.1,3])}))
    artifacts = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in folder.iterdir()}
    save(folder / "review_identity.json", dict(sha256="fixture-identity", artifacts=artifacts,
        identity={"protocol_sha256":hashlib.sha256((tmp_path / "review_protocol.json").read_bytes()).hexdigest()}))
    rows, _ = verified_runs(tmp_path, protocol)
    row = rows.iloc[0]
    assert row.statistical_gate_status_raw == "pass"
    assert row.statistical_gate_status == "insufficient"
    assert row.research_gates_true_cost["profit_factor"] == "insufficient"
    assert row.financing_net_expense == 1
    assert row.net_closed_after_financing == 9
    assert all(hashlib.sha256((folder/name).read_bytes()).hexdigest() == expected for name,expected in artifacts.items())
