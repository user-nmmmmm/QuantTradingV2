"""Concentration evidence is checked against hand-computed numbers."""
from __future__ import annotations

import copy

import pytest

from analysis.concentration import CONCENTRATION_SCHEMA, concentration_evidence


def trade(pnl, symbol="AAA", exit_time="2024-03-01"):
    return {"net_pnl": pnl, "symbol": symbol, "exit_time": exit_time}


# Wins 100, 60, 40, 10 = 210; losses 30, 20, 10 = 60; net 150.
HAND = [trade(100, "AAA", "2023-05-01"), trade(60, "BBB", "2023-09-01"), trade(40, "AAA", "2024-02-01"),
        trade(10, "CCC", "2024-06-01"), trade(-30, "BBB", "2024-07-01"), trade(-20, "CCC", "2025-01-10"),
        trade(-10, "AAA", "2025-02-10")]


def test_hand_computed_totals_and_top_trade_removal():
    report = concentration_evidence(HAND, top_n=(1, 2), minimum_samples=5)
    assert report["schema"] == CONCENTRATION_SCHEMA and report["status"] == "ok"
    assert report["sample_size"] == 7 and report["skipped"] == 0
    assert report["total_net_pnl"] == pytest.approx(150.0)
    assert report["profit_factor"]["value"] == pytest.approx(210 / 60)

    top1 = report["excluding_top_trades"]["1"]
    assert top1["removed_net_pnl"] == pytest.approx(100.0)
    assert top1["net_pnl_excluding"] == pytest.approx(50.0)
    assert top1["share_of_total"] == pytest.approx(100 / 150)
    assert top1["profit_factor_excluding"]["value"] == pytest.approx(110 / 60)

    top2 = report["excluding_top_trades"]["2"]
    assert top2["net_pnl_excluding"] == pytest.approx(-10.0)        # the edge is gone without two trades
    assert top2["share_of_total"] == pytest.approx(160 / 150)       # above 100%
    assert top2["profit_factor_excluding"]["value"] == pytest.approx(50 / 60)


def test_profit_hhi_matches_the_definition():
    report = concentration_evidence(HAND, minimum_samples=5)
    expected = sum((win / 210) ** 2 for win in (100, 60, 40, 10))
    assert report["profit_hhi"] == pytest.approx(expected)


def test_leave_one_year_and_leave_one_symbol():
    report = concentration_evidence(HAND, minimum_samples=5)
    # By exit year: 2023 = 160, 2024 = 20, 2025 = -30.
    year = report["excluding_best_year"]
    assert year["status"] == "ok" and year["best"] == 2023 and year["groups"] == 3
    assert year["net_pnl_excluding_best"] == pytest.approx(-10.0)
    assert year["by_group"] == {"2023": pytest.approx(160.0), "2024": pytest.approx(20.0), "2025": pytest.approx(-30.0)}
    # By symbol: AAA = 130, BBB = 30, CCC = -10; profitable symbols sum to 160.
    symbol = report["excluding_best_symbol"]
    assert symbol["best"] == "AAA" and symbol["net_pnl_excluding_best"] == pytest.approx(20.0)
    assert symbol["best_share_of_gross_profit"] == pytest.approx(130 / 160)


def test_small_samples_are_reported_but_marked_insufficient():
    report = concentration_evidence(HAND)                             # default minimum is 30 trades
    assert report["status"] == "insufficient" and report["total_net_pnl"] == pytest.approx(150.0)
    assert report["profit_factor"]["status"] == "insufficient"
    # Removing five leaves the two largest losses: no gross profit, so the factor is zero.
    assert report["excluding_top_trades"]["5"]["net_pnl_excluding"] == pytest.approx(-50.0)
    assert report["excluding_top_trades"]["5"]["profit_factor_excluding"] == {
        "value": 0.0, "status": "insufficient", "sample_size": 2}
    assert report["excluding_top_trades"]["10"]["status"] == "insufficient"
    assert report["excluding_top_trades"]["10"]["net_pnl_excluding"] is None


def test_enough_trades_make_every_part_ok():
    trades = [trade(5 if i % 3 else -4, f"S{i % 4}", f"202{3 + i % 3}-01-15") for i in range(60)]
    report = concentration_evidence(trades)
    assert report["status"] == "ok" and report["profit_factor"]["status"] == "ok"
    assert report["excluding_top_trades"]["10"]["status"] == "ok"
    assert report["excluding_top_trades"]["10"]["profit_factor_excluding"]["status"] == "ok"
    # Forty wins of 5 and twenty losses of 4: removing ten wins leaves 150 against 80.
    assert report["excluding_top_trades"]["10"]["net_pnl_excluding"] == pytest.approx(70.0)
    assert report["excluding_top_trades"]["10"]["profit_factor_excluding"]["value"] == pytest.approx(150 / 80)


def test_a_losing_total_has_no_share_and_ties_pick_a_stable_best_group():
    losing = [trade(10, "BBB"), trade(10, "AAA"), trade(-50, "CCC")]
    report = concentration_evidence(losing, top_n=(1,), minimum_samples=1)
    assert report["total_net_pnl"] == pytest.approx(-30.0)
    assert report["excluding_top_trades"]["1"]["share_of_total"] is None
    assert report["excluding_best_symbol"]["best"] == "AAA"          # AAA and BBB tie; the order is by name
    assert report["excluding_best_year"]["status"] == "insufficient"  # a single exit year


def test_missing_optional_fields_only_disable_their_own_section():
    trades = [{"net_pnl": 10, "symbol": "AAA"}, {"net_pnl": -5, "symbol": "BBB"}, {"net_pnl": 7, "symbol": "AAA"}]
    report = concentration_evidence(trades, top_n=(1,), minimum_samples=1)
    assert report["excluding_best_year"] == {"status": "insufficient", "reason": "a trade has no exit year"}
    assert report["excluding_best_symbol"]["status"] == "ok"
    assert report["excluding_top_trades"]["1"]["net_pnl_excluding"] == pytest.approx(2.0)


def test_records_without_net_pnl_are_skipped_and_counted():
    report = concentration_evidence([trade(10), {"symbol": "AAA"}, {"net_pnl": None}], top_n=(1,), minimum_samples=1)
    assert report["sample_size"] == 1 and report["skipped"] == 2
    assert report["excluding_top_trades"]["1"]["status"] == "insufficient"


def test_empty_and_invalid_input_have_explicit_statuses():
    empty = concentration_evidence([])
    assert empty["status"] == "insufficient" and empty["sample_size"] == 0 and empty["total_net_pnl"] is None
    assert concentration_evidence([trade(float("nan"))])["status"] == "invalid_input"
    assert concentration_evidence([trade(float("inf"))])["status"] == "invalid_input"


@pytest.mark.parametrize("kwargs", [{"top_n": (0,)}, {"top_n": (-1,)}, {"minimum_samples": 0}, {"minimum_samples": True}])
def test_bad_arguments_raise(kwargs):
    with pytest.raises(ValueError):
        concentration_evidence(HAND, **kwargs)


def test_the_input_is_not_mutated():
    before = copy.deepcopy(HAND)
    concentration_evidence(HAND, minimum_samples=5)
    assert HAND == before
