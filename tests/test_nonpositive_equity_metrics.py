"""Report statistics and renderers must not turn insolvent capital into valid percentage returns."""
from __future__ import annotations

import json

import pandas as pd
import pytest
from matplotlib.axes import Axes
from openpyxl import load_workbook

from backtest.reporting import ReportGenerator
from backtest.reporting.risk_metrics import calculate_active_risk_metrics, calculate_portfolio_risk_metrics
from core.metrics import calculate_benchmark_comparison, calculate_drawdown, calculate_drawdown_events, calculate_equity_metrics, monthly_returns
from core.metrics.performance import monthly_return_records


def _series(values):
    return pd.Series(values, index=pd.date_range("2026-01-01", periods=len(values), name="timestamp"), dtype=float)


def test_headline_sharpe_does_not_silently_drop_an_undefined_interval():
    result = calculate_equity_metrics(_series([100, 120, 0, -10]).to_frame("equity"))
    assert result["EndEquity"] == -10 and result["TotalReturn"] == pytest.approx(-1.1)
    assert result["MaxDrawdownPct"] == pytest.approx(-13 / 12)
    assert result["SharpeRatio"] is None and result["SharpeStatus"] == "undefined"
    assert result["SharpeSamples"] == 3
    assert result["MetricsFormulaVersion"] == "2.1"


@pytest.mark.parametrize("values", [[100, 120, 0, -10], [100, 120, -10, 30]])
@pytest.mark.parametrize("consumer", ["portfolio", "active"])
def test_risk_statistics_reject_zero_and_negative_denominators_without_changing_sample(values, consumer):
    equity = _series(values)
    if consumer == "portfolio":
        result = calculate_portfolio_risk_metrics(equity)
    else:
        result = calculate_active_risk_metrics(equity, _series([100, 110, 105, 115]))
    assert result["status"] == "undefined"
    assert result["sample_size"] == 3
    assert "positive" in result["reason"]
    json.dumps(result, allow_nan=False)


def test_a_nonpositive_peak_cannot_report_a_positive_drawdown_percentage():
    equity = _series([-10, -20, -15])
    summary = calculate_drawdown(equity)
    assert summary["status"] == "undefined" and summary["max_pct"] is None
    assert calculate_drawdown_events(equity)[0]["depth_pct"] is None


@pytest.mark.parametrize("start", [0, -10])
def test_headline_return_requires_a_positive_starting_denominator(start):
    result = calculate_equity_metrics(_series([start, 100, 120]).to_frame("equity"))
    assert result["TotalReturn"] is None and result["CAGR"] is None


def test_a_zero_terminal_value_still_has_defined_prior_positive_period_returns():
    equity = _series([100, 120, 0])
    risk = calculate_portfolio_risk_metrics(equity)
    assert risk["status"] == "ok" and risk["sample_size"] == 2
    assert calculate_equity_metrics(equity.to_frame("equity"))["CAGR"] == -1


@pytest.mark.parametrize("start", [0, -10])
@pytest.mark.parametrize("side", ["strategy", "benchmark"])
def test_benchmark_total_returns_require_positive_denominators(start, side):
    strategy, benchmark = _series([100, 110, 120]), _series([100, 120, 130])
    if side == "strategy":
        strategy.iloc[0] = start
    else:
        benchmark.iloc[0] = start
    result = calculate_benchmark_comparison(strategy, benchmark)
    assert result["status"] == "undefined"
    assert result[f"{side}_return"] is None
    assert result["excess_return"] is None and result["correlation"] is None


def test_monthly_records_retain_undefined_months_without_fabricating_percentage_gains():
    equity = pd.Series([100, 0, -10, 25, 30], index=pd.to_datetime([
        "2026-01-31", "2026-02-28", "2026-03-31", "2026-04-30", "2026-05-31"]), dtype=float)
    metrics = calculate_equity_metrics(equity.to_frame("equity"))
    records = metrics["MonthlyReturns"]
    assert records[1]["return"] == -1
    assert [row["return"] for row in records[2:4]] == [None, None]
    assert [row["status"] for row in records[2:4]] == ["undefined", "undefined"]
    assert metrics["AvgMonthlyReturn"] is None and metrics["MonthlyReturnStatus"] == "undefined"
    assert metrics["MonthlyReturnSamples"] == 4
    defined = monthly_returns(equity)
    assert list(defined.index.strftime("%Y-%m")) == ["2026-02", "2026-05"]
    assert list(defined) == pytest.approx([-1, .2])


@pytest.mark.parametrize("values, total", [([100, 120, 0, -10], -1.1), ([100, 120, -10, 30], -.7)])
def test_benchmark_keeps_total_loss_but_not_a_correlation_over_undefined_returns(values, total):
    result = calculate_benchmark_comparison(_series(values), _series([100, 110, 120, 130]))
    assert result["strategy_return"] == pytest.approx(total)
    assert result["excess_return"] == pytest.approx(total - .3)
    assert result["correlation"] is None


def test_missing_months_after_zero_are_not_counted_as_observed_return_intervals():
    equity = pd.Series([100., 0., 25.], index=pd.to_datetime(["2026-01-31", "2026-02-28", "2026-05-31"]))
    result = calculate_equity_metrics(equity.to_frame("equity"))
    missing = result["MonthlyReturns"][2:4]
    assert all(row["observations"] == 0 and row["status"] == "insufficient_data" for row in missing)
    assert result["MonthlyReturnSamples"] == 1


def test_monthly_records_distinguish_missing_endpoints_from_an_unrepresentable_return():
    equity = pd.Series([1e-308, 1e308], index=pd.to_datetime(["2026-01-31", "2026-02-28"]))
    first, second = monthly_return_records(equity)
    assert first["status"] == "insufficient_data"
    assert first["reason"] == "missing current or preceding month endpoint"
    assert second["return"] is None and second["status"] == "undefined"
    assert second["reason"] == "unrepresentable monthly return"
    json.dumps([first, second], allow_nan=False)


@pytest.mark.filterwarnings("error::RuntimeWarning")
def test_return_bars_show_only_defined_intervals_and_do_not_fill_them_with_zero(tmp_path, monkeypatch):
    captured = []
    original = Axes.bar

    def capture(axes, x, height, *args, **kwargs):
        captured.append(list(height))
        return original(axes, x, height, *args, **kwargs)

    monkeypatch.setattr(Axes, "bar", capture)
    ReportGenerator(str(tmp_path))._plot_equity(_series([100, 120, 0, -10]).to_frame("equity"))
    assert captured == [pytest.approx([.2, -1])]
    assert (tmp_path / "equity.png").is_file()


def test_workbook_keeps_raw_nonpositive_equity_and_leaves_undefined_returns_blank(tmp_path):
    ReportGenerator(str(tmp_path)).generate([], _series([100, 120, 0, -10, 25]).to_frame("equity"),
                                             report_profile="workbook")
    workbook = load_workbook(tmp_path / "backtest_report.xlsx", data_only=True)
    try:
        rows = list(workbook["Equity"].values)
        columns = {name: index for index, name in enumerate(rows[0])}
        assert [row[columns["equity"]] for row in rows[1:]] == [100, 120, 0, -10, 25]
        returns = [row[columns["period_return"]] for row in rows[1:]]
        assert returns[0] is None and returns[3:] == [None, None]
        assert returns[1:3] == pytest.approx([.2, -1])
    finally:
        workbook.close()
