"""SYS-03 hand-calculated outcome, time and benchmark migration boundaries."""
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from backtest.reporting import ReportGenerator
from core.benchmarks import (btc_eth_first_open_buy_hold, dynamic_equal_weight_rebalanced,
                             fixed_equal_weight_buy_hold)
from core.metrics import calculate_benchmark_comparison, calculate_equity_metrics, monthly_returns
from core.metrics import calculate_trade_quality
from core.metrics import calculate_signal_funnel
from core.metrics.execution import calculate_execution_quality
from backtest.reporting.risk_metrics import summarize_exposure


def event(kind, order, seconds=0, **fields):
    return SimpleNamespace(event_type=kind, account_id="paper", occurred_at=pd.Timestamp("2026-01-01T00:00:00Z") + pd.Timedelta(seconds=seconds),
                           payload={"client_order_id": order, **fields})


def execution_fixture():
    rows = [event("order_intent", str(i), requested_qty=10, reference_price=100, action="buy" if i != 2 else "sell")
            for i in range(1, 6)]
    rows += [event("fill", "1", 60, fill_id="f1", qty=5, price=101),
             event("order", "1", 60, status="partially_filled", filled_qty=5),
             event("fill", "1", 120, fill_id="f2", qty=5, price=103),
             event("order", "1", 120, status="filled", filled_qty=10),
             event("fill", "2", 30, fill_id="f3", qty=2, price=99),
             event("order", "2", 90, status="canceled", filled_qty=2),
             event("order", "3", 10, status="rejected"),
             event("order", "4", 80, status="expired"),
             event("order", "5", 10, status="accepted")]
    return rows


def test_execution_facts_deduplicate_and_reconcile_hand_calculated_rates():
    rows = execution_fixture()
    result = calculate_execution_quality(rows * 100)
    assert result == calculate_execution_quality(rows)
    assert result["status"] == "ok"
    assert result["sample_size"] == 5
    for key in ("full_fill_rate", "partial_fill_rate", "rejection_rate", "cancel_rate", "expiry_rate"):
        assert result[key]["value"] == pytest.approx(.2)
    assert result["first_fill_seconds"]["value"] == 45
    assert result["completion_seconds"]["value"] == 120
    assert result["implementation_shortfall_bps"]["value"] == pytest.approx(22 / 1200 * 10000)


@pytest.mark.parametrize("extra", [event("fill", "1", 60, fill_id="f1", qty=6, price=101),
                                  event("order", "2", 100, status="filled", filled_qty=10),
                                  event("fill", "5", -1, fill_id="late", qty=1, price=100)])
def test_execution_bad_facts_never_return_valid_headlines(extra):
    result = calculate_execution_quality(execution_fixture() + [extra])
    assert result["status"] == "invalid_input"
    assert result["full_fill_rate"]["value"] is None


def test_terminal_status_requires_actual_fill_facts_and_same_clock_replay_is_monotone():
    missing = [event("order", "missing", status="filled", requested_qty=10)]
    assert calculate_execution_quality(missing)["status"] == "invalid_input"
    rows = [event("order_intent", "1", requested_qty=1, action="buy", reference_price=100),
            event("fill", "1", 10, fill_id="x", qty=1, price=100),
            event("order", "1", 10, status="filled", filled_qty=1),
            event("order", "1", 10, status="partially_filled", filled_qty=.5)]
    result = calculate_execution_quality(rows)
    assert result["orders"][0]["final_status"] == "filled"
    assert result == calculate_execution_quality(rows + rows[2:])


def test_execution_missing_reference_and_missing_stream_are_not_zero():
    rows = [event("order", "old", status="filled", requested_qty=1, filled_qty=1),
            event("fill", "old", fill_id="legacy", qty=1, price=100)]
    result = calculate_execution_quality(rows)
    assert result["full_fill_rate"]["value"] == 1
    assert result["implementation_shortfall_bps"]["status"] == "not_modeled"
    assert result["first_fill_seconds"]["value"] is None
    assert calculate_execution_quality(None)["status"] == "not_modeled"
    assert calculate_execution_quality([])["status"] == "insufficient_data"


def test_trade_distribution_streaks_and_wilson_interval_hand_calculated():
    rows = [{"net_pnl": value, "entry_price": 100, "qty": 2} for value in [20, 10, -15, -5, 0, 40]]
    result = calculate_trade_quality(rows)["distribution"]
    assert result["currency_expectancy"] == pytest.approx(50 / 6)
    assert result["payoff_ratio"] == pytest.approx((70 / 3) / 10)
    assert result["streaks"]["wins"]["max_count"] == 2
    assert result["streaks"]["wins"]["net_pnl"] == 30
    assert result["streaks"]["losses"]["net_pnl"] == -20
    assert result["win_rate_interval"]["lower"] < .5 < result["win_rate_interval"]["upper"]
    assert result["expectancy_per_entry_notional"]["value"] == pytest.approx(50 / 6 / 200)


def test_signal_funnel_joins_actual_signal_identity_not_transport_group():
    signal = SimpleNamespace(event_type="signal", event_id="signal-fact", correlation_id="transport-envelope",
                             payload={"action": "buy"})
    risk = SimpleNamespace(event_type="risk_decision", correlation_id="signal-fact", payload={"approved": True})
    result = calculate_signal_funnel([signal, risk])
    assert result["raw_entry_signal_chains"] == 1
    assert result["total_correlation_chains"] == 1
    assert result["unclassified_chains"] == 0


def test_irregular_annualization_requires_explicit_scale_and_does_not_mutate():
    curve = pd.DataFrame({"equity": [100., 102., 99., 104.]}, index=pd.to_datetime([
        "2026-01-01", "2026-01-02", "2026-01-04", "2026-01-05"]))
    original = curve.copy(deep=True)
    inferred = calculate_equity_metrics(curve)
    assert inferred["SharpeRatio"] is None
    assert inferred["AnnualizationReason"]
    explicit = calculate_equity_metrics(curve, periods_per_year=365.25)
    assert np.isfinite(explicit["SharpeRatio"])
    assert explicit["AnnualizationPolicy"] == "explicit"
    pd.testing.assert_frame_equal(curve, original)
    for invalid in (curve.iloc[::-1], pd.concat([curve, curve.iloc[:1]]), curve.assign(equity=[100, np.nan, 99, 104])):
        with pytest.raises(ValueError):
            calculate_equity_metrics(invalid)


def test_monthly_records_keep_absent_and_partial_months_without_bridging():
    curve = pd.DataFrame({"equity": [100., 110., 130.]}, index=pd.to_datetime([
        "2026-01-15", "2026-01-31", "2026-03-31"]))
    rows = calculate_equity_metrics(curve)["MonthlyReturns"]
    assert [r["month"] for r in rows] == ["2026-01", "2026-02", "2026-03"]
    assert rows[1]["observations"] == 0
    assert all(r["return"] is None for r in rows)
    assert monthly_returns(curve.equity).empty


def test_exposure_uses_elapsed_time_across_missing_bars():
    frame = pd.DataFrame({"gross_exposure": [100, 0, 0], "net_exposure": [100, 0, 0],
                          "priced_symbols": [1, 0, 0], "gross_exposure_pct_equity": [1, 0, 0],
                          "net_exposure_pct_equity": [1, 0, 0]}, index=pd.to_datetime([
                              "2026-01-01", "2026-01-04", "2026-01-05"]))
    result = summarize_exposure(frame)["elapsed_time_weighted"]
    assert result["time_in_market_ratio"] == .75
    assert result["invested_days"] == 3
    assert result["flat_days"] == 1


def test_benchmarks_keep_distinct_policies_and_reproduce_frozen_first_open():
    dates = pd.date_range("2026-01-01", periods=3, tz="UTC")
    data = {"BTC/USDT": pd.DataFrame({"open": [100, 110, 120], "close": [110, 120, 130]}, index=dates),
            "ETH/USDT": pd.DataFrame({"open": [50, 60], "close": [55, 70]}, index=dates[1:])}
    frozen = btc_eth_first_open_buy_hold(data, 10000, start=dates[0], end=dates[-1])
    assert frozen.equity.tolist() == [10500, 11500, 13500]
    assert frozen.weights.iloc[0]["ETH/USDT"] == 0
    fixed = fixed_equal_weight_buy_hold(data, 10000)
    delayed = fixed_equal_weight_buy_hold(data, 10000, start_idx=1)
    assert delayed.weights.iloc[0].eq(0.0).all()
    dynamic = dynamic_equal_weight_rebalanced(data, 10000, cost_bps=5)
    assert len({x.metadata["benchmark_id"] for x in [frozen, fixed, dynamic]}) == 3
    comparison = calculate_benchmark_comparison(frozen.equity, frozen.equity)
    assert comparison["benchmark_id"] == frozen.metadata["benchmark_id"]
    assert comparison["excess_return"] == 0
    with pytest.raises(ValueError, match="source missing"):
        btc_eth_first_open_buy_hold({"BTC/USDT": data["BTC/USDT"]}, 10000, start=dates[0], end=dates[-1])


def test_rebalanced_benchmark_charges_actual_drift_turnover():
    index = pd.date_range("2026-01-01", periods=2, tz="UTC")
    result = dynamic_equal_weight_rebalanced({
        "A": pd.DataFrame({"close": [100, 110]}, index=index),
        "B": pd.DataFrame({"close": [100, 90]}, index=index)}, 1000, cost_bps=10)
    assert result.turnover.tolist() == pytest.approx([1.0, .05])
    assert result.costs.tolist() == pytest.approx([1.0, .04995])
    assert result.equity.iloc[-1] == pytest.approx(998.95005)


def test_standard_report_persists_closed_facts_reconciliation_execution(tmp_path, monkeypatch):
    for name in ("_plot_equity", "_plot_monthly_heatmap", "_plot_rolling_metrics", "_plot_pnl_distribution"):
        monkeypatch.setattr(ReportGenerator, name, lambda *a, **k: None)
    monkeypatch.setattr("backtest.reporting.render.pdf.write_pdf_report", lambda *a, **k: None)
    curve = pd.DataFrame({"equity": [100., 102., 99.], "cash": [100., 102., 99.]},
                         index=pd.date_range("2026-01-01", periods=3, tz="UTC"))
    ReportGenerator(str(tmp_path)).generate([], curve, event_log=execution_fixture())
    assert pd.read_csv(tmp_path / "closed_trades.csv").empty
    reconciliation = json.loads((tmp_path / "reconciliation.json").read_text())["metrics"]
    assert reconciliation["status"] == "pass"
    assert reconciliation["account_cash_bridge"]["status"] == "not_modeled"
    execution = json.loads((tmp_path / "execution_quality.json").read_text())["metrics"]
    assert execution["sample_size"] == 5


def test_legacy_missing_fee_survives_export_and_invalidates_trade_headlines(tmp_path, monkeypatch):
    for name in ("_plot_equity", "_plot_monthly_heatmap", "_plot_rolling_metrics", "_plot_pnl_distribution"):
        monkeypatch.setattr(ReportGenerator, name, lambda *a, **k: None)
    monkeypatch.setattr("backtest.reporting.render.pdf.write_pdf_report", lambda *a, **k: None)
    times = pd.date_range("2026-01-01", periods=2, tz="UTC")
    trades = [{"symbol": "BTC/USDT", "side": "buy", "qty": 1, "fill_price": 100,
               "strategy_id": "S", "fill_time": times[0]},
              {"symbol": "BTC/USDT", "side": "sell", "qty": 1, "fill_price": 110,
               "commission": .11, "strategy_id": "S", "fill_time": times[1]}]
    metrics = ReportGenerator(str(tmp_path)).generate(trades,
        pd.DataFrame({"equity": [1000, 1010], "cash": [900, 1010]}, index=times))
    assert metrics["TradeInputIntegrity"]["status"] == "invalid_input"
    assert metrics["NetPnL"] is None and metrics["ProfitFactor"] is None
    invalid = json.loads((tmp_path / "invalid_closed_trades.json").read_text())["metrics"]
    assert len(invalid["records"]) == 1
    assert len(invalid["records"][0]["raw_rows"]) == 2
    assert pd.read_csv(tmp_path / "closed_trades.csv").iloc[0]["status"] == "invalid_input"
    assert json.loads((tmp_path / "reconciliation.json").read_text())["metrics"]["status"] == "invalid_input"
