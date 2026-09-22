"""Report profiles avoid discarded rendering without changing report data."""

import json
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest

from backtest.reporting import ReportGenerator
from backtest.reporting.serialization import metrics_document


THROWAWAY_WRITERS = (
    "_save_report_text",
    "_plot_equity",
    "_plot_monthly_heatmap",
    "_plot_rolling_metrics",
    "_plot_pnl_distribution",
)
THROWAWAY_FILES = (
    "report.txt", "equity.csv", "trades.csv", "benchmark.csv",
    "equity.png", "monthly_returns_heatmap.png",
    "rolling_metrics.png", "pnl_distribution.png",
)


@pytest.fixture
def report_inputs():
    index = pd.date_range("2025-01-01", periods=180, freq="D", tz="UTC")
    returns = pd.Series([0.003, -0.002, 0.001, 0.004, -0.001] * 36, index=index)
    equity = pd.DataFrame({"equity": 10000 * (1 + returns).cumprod()}, index=index)
    benchmark = 10000 * (1 + returns * 0.55).cumprod()
    trades = [
        {"symbol": "BTC/USDT", "side": "buy", "qty": 1.0, "fill_price": 10000.0,
         "commission": 10.0, "slip": 5.0, "strategy_id": "TrendBreakout", "fill_time": index[0]},
        {"symbol": "BTC/USDT", "side": "sell", "qty": 1.0, "fill_price": 10300.0,
         "commission": 11.0, "slip": 5.0, "strategy_id": "TrendBreakout", "fill_time": index[3]},
    ]
    return trades, equity, benchmark


def mock_throwaway_writers(monkeypatch, report):
    writers = {name: Mock() for name in THROWAWAY_WRITERS}
    for name, writer in writers.items():
        monkeypatch.setattr(report, name, writer)
    return writers


def test_workbook_skips_discarded_writers_and_cleans_stale_files(
    tmp_path, monkeypatch, report_inputs,
):
    trades, equity, benchmark = report_inputs
    report = ReportGenerator(str(tmp_path))
    expected = report.generate(trades, equity, benchmark_curve=benchmark, metrics_only=True)
    for name in THROWAWAY_FILES:
        (tmp_path / name).write_text("stale output", encoding="utf-8")

    writers = mock_throwaway_writers(monkeypatch, report)
    written_csvs = []
    original_to_csv = pd.DataFrame.to_csv

    def record_csv(frame, target, *args, **kwargs):
        written_csvs.append(Path(target).name)
        return original_to_csv(frame, target, *args, **kwargs)

    monkeypatch.setattr(pd.DataFrame, "to_csv", record_csv)
    benchmark_csv = Mock(side_effect=AssertionError("Workbook must embed benchmark data directly"))
    monkeypatch.setattr(benchmark, "to_csv", benchmark_csv)
    actual = report.generate(trades, equity, benchmark_curve=benchmark, report_profile="workbook")

    for writer in writers.values():
        writer.assert_not_called()
    benchmark_csv.assert_not_called()
    assert written_csvs == ["closed_trades.csv"]
    assert metrics_document(actual) == metrics_document(expected)
    assert json.loads((tmp_path / "metrics.json").read_text(encoding="utf-8")) == metrics_document(expected)
    assert len(pd.read_csv(tmp_path / "closed_trades.csv")) == 1
    assert {path.name for path in tmp_path.iterdir()} == {
        "backtest_report.xlsx", "metrics.json", "closed_trades.csv",
        "reconciliation.json", "execution_quality.json", "invalid_closed_trades.json",
    }


@pytest.mark.parametrize("profile", ["compact", "full"])
def test_pdf_profiles_still_generate_text_charts_and_raw_csvs(
    profile, tmp_path, monkeypatch, report_inputs,
):
    trades, equity, benchmark = report_inputs
    report = ReportGenerator(str(tmp_path))
    writers = mock_throwaway_writers(monkeypatch, report)
    pdf_writer = Mock()
    workbook_writer = Mock()
    monkeypatch.setattr("backtest.reporting.render.pdf.write_pdf_report", pdf_writer)
    monkeypatch.setattr("backtest.reporting.render.workbook.write_workbook_report", workbook_writer)

    report.generate(trades, equity, benchmark_curve=benchmark, report_profile=profile)

    for writer in writers.values():
        writer.assert_called_once()
    pdf_writer.assert_called_once()
    workbook_writer.assert_not_called()
    assert {"equity.csv", "trades.csv", "benchmark.csv", "closed_trades.csv"}.issubset(
        {path.name for path in tmp_path.iterdir()}
    )


@pytest.mark.parametrize("profile", ["workbook", "compact", "full"])
def test_metrics_only_remains_free_of_output_for_every_profile(
    profile, tmp_path, monkeypatch, report_inputs,
):
    trades, equity, benchmark = report_inputs
    report = ReportGenerator(str(tmp_path))
    writers = mock_throwaway_writers(monkeypatch, report)
    pdf_writer = Mock()
    workbook_writer = Mock()
    monkeypatch.setattr("backtest.reporting.render.pdf.write_pdf_report", pdf_writer)
    monkeypatch.setattr("backtest.reporting.render.workbook.write_workbook_report", workbook_writer)

    metrics = report.generate(
        trades, equity, benchmark_curve=benchmark, report_profile=profile, metrics_only=True,
    )

    assert metrics["TotalTrades"] == 1
    for writer in writers.values():
        writer.assert_not_called()
    pdf_writer.assert_not_called()
    workbook_writer.assert_not_called()
    assert list(tmp_path.iterdir()) == []
