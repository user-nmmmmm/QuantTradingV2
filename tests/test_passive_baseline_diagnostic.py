"""Passive-baseline diagnostic: split, holdout refusal, and what the passive accounts actually do.

Synthetic markets only; nothing here is performance evidence.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from scripts import run_passive_baseline_diagnostic as study


def flat_market(symbols=("BTC-USDT",), length: int = 40, start: str = "2023-01-01") -> dict[str, pd.DataFrame]:
    index = pd.date_range(start, periods=length, freq="D")
    return {symbol: pd.DataFrame({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
                                  "volume": 5_000_000.0}, index=index) for symbol in symbols}


def trending_market(length: int = 120) -> dict[str, pd.DataFrame]:
    index = pd.date_range("2023-01-01", periods=length, freq="D")
    frames = {}
    for number, symbol in enumerate(("BTC-USDT", "AAA-USDT", "BBB-USDT")):
        close = 100 * np.exp(np.cumsum(0.003 + 0.01 * np.sin(np.arange(length) / 9 + number)))
        frames[symbol] = pd.DataFrame({"open": close * 0.999, "high": close * 1.01, "low": close * 0.99,
                                       "close": close, "volume": 5_000_000.0}, index=index)
    return frames


def test_split_is_60_20_20_with_a_purge_and_embargo_gap():
    segments = study.research_segments()
    assert segments["train60"][0] == study.DEFAULT_START and segments["final20"][1] == study.DEFAULT_END
    gap = pd.Timedelta(days=study.PURGE_BARS + study.EMBARGO_BARS + 1)
    assert pd.Timestamp(segments["validation20"][0]) - pd.Timestamp(segments["train60"][1]) == gap
    assert pd.Timestamp(segments["final20"][0]) - pd.Timestamp(segments["validation20"][1]) == gap
    days = len(pd.date_range(study.DEFAULT_START, study.DEFAULT_END, freq="D"))
    train_days = len(pd.date_range(*segments["train60"], freq="D"))
    assert train_days == int(days * 0.6)


def test_the_holdout_is_not_evaluated_and_cannot_be_requested():
    assert set(study.evaluated_segments()) == {"train60", "validation20"}
    holdout = study.research_segments()["final20"]
    with pytest.raises(ValueError, match="final20"):
        study.run_segment("final20", holdout, universe=None)
    beyond = (study.evaluated_segments()["validation20"][0], holdout[0])
    with pytest.raises(ValueError, match="final20"):
        study.run_segment("validation20", beyond, universe=None)


def test_ratio_follows_the_r1_zero_drawdown_convention():
    assert study.return_drawdown_ratio(10.0, 5.0) == 2.0
    assert study.return_drawdown_ratio(0.0, 0.0) == 0.0
    assert study.return_drawdown_ratio(5.0, 0.0) is None


def test_a_passive_account_cannot_be_levered():
    with pytest.raises(ValueError):
        study.PassivePolicy("levered", gross=1.5)


def test_buy_and_hold_fills_once_at_the_next_open_and_loses_only_its_costs():
    frames = flat_market()
    policy = study.PassivePolicy("BTC buy-and-hold", symbols=(study.BTC,))
    run = study.simulate_passive(policy, frames)
    assert len(run["trades"]) == 1
    fill = run["trades"][0]
    assert fill["side"] == "buy" and pd.Timestamp(fill["fill_time"]).tz_localize(None) == frames[study.BTC].index[1]
    assert fill["fill_price"] > 100.0  # spread, slippage and range slippage are paid on top of the open
    assert study.cash_reconciles(run)
    row = study.summarize_passive(policy, run)
    assert row["accounting_ok"] and row["rejected_orders"] == 0 and row["costs"] > 0
    # On a flat market the only loss is what the single fill cost.
    assert -0.5 < row["return_pct"] < 0
    assert run["equity_curve"]["equity"].iloc[0] == study.INITIAL_CAPITAL
    assert row["avg_gross_exposure"] > 0.95


def test_monthly_equal_weight_adds_a_listing_and_sells_a_symbol_on_its_last_bar():
    frames = flat_market(("AAA-USDT", "BBB-USDT", "CCC-USDT"), length=100)
    frames["BBB-USDT"] = frames["BBB-USDT"].iloc[20:]   # lists on 2023-01-21
    frames["CCC-USDT"] = frames["CCC-USDT"].iloc[:70]   # last bar 2023-03-11
    run = study.simulate_passive(study.PassivePolicy("equal weight", monthly=True), frames)
    trades = pd.DataFrame(run["trades"])
    trades["day"] = pd.to_datetime(trades["fill_time"]).dt.tz_localize(None)
    first_b = trades[trades.symbol == "BBB-USDT"].day.min()
    assert first_b == pd.Timestamp("2023-02-01")  # joins at the first month boundary after listing
    c_sells = trades[(trades.symbol == "CCC-USDT") & (trades.side == "sell")]
    assert c_sells.day.max() == frames["CCC-USDT"].index[-1]
    assert set(run["open_positions"]) == {"AAA-USDT", "BBB-USDT"}
    values = {symbol: qty * 100.0 for symbol, qty in run["open_positions"].items()}
    assert values["AAA-USDT"] == pytest.approx(values["BBB-USDT"], rel=0.02)
    assert study.cash_reconciles(run)
    assert not [r for r in run["execution_audit"] if r.get("outcome") == "rejected"]


def test_constant_mix_holds_the_requested_share_of_equity():
    policy = study.PassivePolicy("BTC at 20%", gross=0.2, symbols=(study.BTC,), monthly=True)
    row = study.summarize_passive(policy, study.simulate_passive(policy, flat_market(length=70)))
    assert row["avg_gross_exposure"] == pytest.approx(0.2, abs=0.01)


def test_curve_metrics_use_one_value_per_day():
    index = pd.DatetimeIndex(["2023-01-01", "2023-01-02", "2023-01-03", "2023-01-03 00:00:00.000001"])
    metrics = study.curve_metrics(pd.Series([10000.0, 10100.0, 9900.0, 10200.0], index=index), 10000.0)
    assert metrics["return_pct"] == pytest.approx(2.0)
    assert metrics["max_drawdown_pct"] == pytest.approx(0.0)  # the day closes at 10200, above the 10100 peak
    assert metrics["sharpe"] is not None


@pytest.mark.slow
def test_a_segment_puts_the_engine_run_beside_the_passive_accounts(tmp_path, monkeypatch):
    frames = trending_market()
    monkeypatch.setattr(study, "_load_universe_data", lambda start, end, data_dir: (frames, sorted(frames)))
    window = study.evaluated_segments()["train60"]
    result = study.run_segment("train60", window, universe=None, output_dir=tmp_path)
    names = [row["object"] for row in result["rows"]]
    assert names[:4] == ["S1 TrendBreakout (default)", "cash", "BTC buy-and-hold", "equal weight, monthly rebalance"]
    assert all(row["accounting_ok"] for row in result["rows"])
    assert result["best_r1_passive"]["object"] in names[1:4]
    assert isinstance(result["strategy_ratio_above_best_passive"], bool)
    assert "final20" not in study.format_table("train60", result)
