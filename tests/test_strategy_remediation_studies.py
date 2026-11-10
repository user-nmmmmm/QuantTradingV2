"""Pure decision logic of the batch-2 strategy remediation studies."""

from __future__ import annotations

import pandas as pd

from scripts.run_time_exit_rebuy_study import rebuy_pairs, summarize, verdict


def _trade(time, side, reason, strategy, price, qty=1.0, cost=1.0, symbol="A/USDT"):
    return {"fill_time": time, "side": side, "exit_reason": reason, "strategy_id": strategy,
            "fill_price": price, "qty": qty, "symbol": symbol,
            "costs": str({"commission": cost / 2, "modeled_total": cost})}


def _trades(*rows):
    return pd.DataFrame(list(rows))


def test_rebuy_is_paired_within_k_days_and_measured():
    trades = _trades(
        _trade("2020-01-01", "sell", "MaxHoldingPeriod", "MaxHoldingPeriod", 100.0, 2.0, cost=2.0),
        _trade("2020-01-03", "buy", "signal", "TrendBreakout", 103.0, 2.0, cost=1.5),
    )
    (pair,) = rebuy_pairs(trades, 3)
    assert pair["days"] == 2
    assert pair["round_trip_cost"] == 3.5
    assert pair["price_gap"] == 6.0
    assert pair["impact"] == 9.5
    assert rebuy_pairs(trades, 1) == []


def test_only_first_buy_of_the_same_symbol_after_the_exit_counts():
    trades = _trades(
        _trade("2020-01-01", "buy", "signal", "TrendBreakout", 90.0),
        _trade("2020-01-05", "sell", "MaxHoldingPeriod", "MaxHoldingPeriod", 100.0),
        _trade("2020-01-06", "buy", "signal", "TrendBreakout", 101.0, symbol="B/USDT"),
        _trade("2020-01-07", "buy", "signal", "TrendBreakout", 99.0),
        _trade("2020-01-08", "buy", "signal", "TrendBreakout", 98.0),
    )
    (pair,) = rebuy_pairs(trades, 5)
    assert pair["rebuy_fill_time"].startswith("2020-01-07")
    assert pair["price_gap"] == -1.0


def test_verdict_needs_every_window_within_both_limits():
    quiet = summarize(_trades(_trade("2020-01-01", "buy", "signal", "TrendBreakout", 100.0)))
    assert quiet["time_exits"] == 0
    assert verdict({"train60": quiet, "validation20": quiet})["negligible"]
    costly = _trades(
        _trade("2020-01-01", "sell", "MaxHoldingPeriod", "MaxHoldingPeriod", 100.0, 10.0),
        _trade("2020-01-02", "buy", "signal", "TrendBreakout", 103.0, 10.0),
    )
    result = verdict({"train60": quiet, "validation20": summarize(costly)})
    assert not result["negligible"]
    # one re-buy, but 2 USDT of costs + 3 USDT x 10 of price gap > the 25 USDT limit
    assert result["checks"]["validation20"] == {"count_le_limit": True, "impact_le_limit": False}
    assert result["conclusion"] == "candidate_for_stage_C"
