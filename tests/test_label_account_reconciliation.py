import pytest

from analysis.label_account_reconciliation import reconcile_candidate_account, reconcile_observation_account


def facts():
    labels = [{"candidate_id": cid, "direction": "long", "symbol": "BTC-USDT", "net_return_bps": 100}
              for cid in ("a", "b", "never")]
    bindings = [{"order_id": "order-" + cid, "candidate_id": cid} for cid in ("a", "b")]
    entries = [{"order_id": "order-" + cid, "symbol": "BTC-USDT", "side": "buy", "qty": qty,
                "fill_price": price, "commission": qty * price * .01, "fill_time": day}
               for cid, qty, price, day in (("a", 2, 100, "2020-01-01"), ("b", 1, 110, "2020-01-02"))]
    carry = [{"event_id": "carry", "symbol": "BTC-USDT", "timestamp": "2020-01-03", "amount": 6.}]
    return labels, bindings, entries, carry


def close(qty, details):
    return {"order_id": "exit", "symbol": "BTC-USDT", "side": "sell", "qty": qty,
        "fill_price": 120., "commission": 1.2 * qty, "fill_time": "2020-01-04",
        "lot_closes": [{"entry_order_id": "order-" + cid, "close_event_id": "close-" + cid,
            "lot_id": "lot-" + cid, "qty_closed": q, "entry_price": p,
            "entry_cost_share": fee, "entry_time": day}
            for cid, q, p, fee, day in details]}


def run(labels, bindings, trades, carry, equity):
    return reconcile_candidate_account(labels, bindings, trades, carry, initial_capital=1000.,
        final_equity=equity, final_marks={"BTC-USDT": 130.}, financing_complete=True,
        quote_currency="USDT")


def test_overlapping_candidates_partial_fifo_and_open_inventory_money_bridge():
    labels, bindings, entries, carry = facts()
    exit_ = close(1, [("a", 1, 100, 1, "2020-01-01")])
    result = run(labels, bindings, entries + [exit_], carry, 1059.7)
    assert result["status"] == "complete" and result["accounting"]["ok"]
    assert result["lot_matches"][0]["candidate_id"] == "a"
    assert result["lot_matches"][0]["net_pnl"] == pytest.approx(15.8)
    assert result["accounting"]["unrealized_after_paid_costs"] == pytest.approx(43.9)
    assert result["coverage"] == {"open_or_partially_closed": 2, "not_submitted": 1}
    assert all(row["actual_net_bps"] is None for row in result["candidates"])
    assert sum(row["cost_quote"] for row in result["financing_allocations"]) == 6


def test_one_exit_allocates_across_original_owners_and_costs_exactly_once():
    labels, bindings, entries, carry = facts()
    exit_ = close(3, [("a", 2, 100, 2, "2020-01-01"), ("b", 1, 110, 1.1, "2020-01-02")])
    result = run(labels, bindings, entries + [exit_], carry, 1037.3)
    assert result["accounting"]["ok"] and not result["open_inventory"]
    assert [m["net_pnl"] for m in result["lot_matches"]] == pytest.approx([31.6, 5.7])
    assert result["candidates"][0]["actual_net_bps"] == pytest.approx(1580.)
    assert result["accounting"]["fee_allocation_residual"] == pytest.approx(0)
    assert result["accounting"]["financing_allocation_residual"] == pytest.approx(0)


def test_ambiguous_financing_timing_keeps_charge_but_withholds_net_claim():
    labels, bindings, entries, carry = facts()
    carry[0]["timestamp"] = "2020-01-04"
    exit_ = close(3, [("a", 2, 100, 2, "2020-01-01"), ("b", 1, 110, 1.1, "2020-01-02")])
    result = run(labels, bindings, entries + [exit_], carry, 1037.3)
    assert result["accounting"]["ok"] and result["status"] == "incomplete"
    assert result["accounting"]["unallocated_financing"] == 6
    assert all(row["actual_net_bps"] is None for row in result["candidates"])


def test_unexplained_quantity_or_duplicate_close_fails():
    labels, bindings, entries, carry = facts()
    bad = close(2, [("a", 1, 100, 1, "2020-01-01")])
    with pytest.raises(ValueError, match="whole closing fill"):
        run(labels, bindings, entries + [bad], carry, 0)
    proper = close(1, [("a", 1, 100, 1, "2020-01-01")])
    with pytest.raises(ValueError, match="duplicate close"):
        run(labels, bindings, entries + [proper, proper], carry, 0)


def test_financing_in_another_currency_cannot_be_relabeled_into_account_quote():
    labels, bindings, entries, carry = facts()
    carry[0]["quote_currency"] = "BTC"
    with pytest.raises(ValueError, match="currency conversion"):
        run(labels, bindings, entries, carry, 1070.9)


def test_corrupt_partial_close_cannot_hide_in_offsetting_open_inventory():
    labels, bindings, entries, carry = facts()
    corrupted = close(1, [("a", 1, 150, 1, "2020-01-01")])
    with pytest.raises(ValueError, match="cost basis"):
        run(labels, bindings, entries + [corrupted], carry, 1059.7)


def test_ordinary_observation_without_five_day_labels_retains_account_facts():
    labels, bindings, entries, carry = facts()
    result = reconcile_observation_account({"candidates": labels, "decisions": bindings,
        "outcomes": [], "actual_financing": carry}, entries, initial_capital=1000.,
        final_equity=1070.9, final_marks={"BTC-USDT": 130.}, quote_currency="USDT")
    assert result["label_comparison"]["missing"] == 3
    assert result["accounting"]["ok"]
    assert all(row["label_error_bps"] is None for row in result["candidates"])
