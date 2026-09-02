"""The book behind the equity curve (BM3).

A return series cannot say whether a flat stretch was run in cash or at 2x
gross, and Sharpe reads identically either way - so ``core.metrics``'s
``calculate_exposure`` existed, tested, with no production caller.  The engine
now samples the non-flat book on every equity row and joins the exposure
columns onto the curve it returns, which puts them in ``equity.csv``, the
workbook's Equity sheet, the leverage panel of ``equity.png`` and an
``Exposure`` section of ``report.txt``.

These pin the three things that can go wrong: a row paired with the wrong
book, a gap presented as "flat" rather than "unknown", and a curve that
predates the columns being reported as zero exposure instead of unrecorded.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd
import pytest

from backtest.engine import BacktestEngine
from backtest.reporting import ReportGenerator
from backtest.reporting.risk_metrics import EXPOSURE_COLUMNS, summarize_exposure
from core.state import MarketState
from strategies.base import Strategy

SYMBOL = "BTC/USDT"
ENTRY_BAR = 30
EXIT_BAR = 40


class _HoldForTenBarsStrategy(Strategy):
    """Long from bar 30 to bar 40, flat on either side of it."""

    def __init__(self) -> None:
        super().__init__("HoldTen", set(MarketState))

    def should_enter(self, symbol, i, df, state, portfolio):
        if i == ENTRY_BAR:
            return {"action": "buy", "order_type": "market", "stop_loss": 90.0}
        return None

    def should_exit(self, symbol, i, df, state, portfolio):
        if i >= EXIT_BAR:
            return {"action": "sell", "reason": "signal", "order_type": "market"}
        return None


def _flat_prices(bars: int = 60, price: float = 100.0) -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=bars, freq="D", tz="UTC")
    return pd.DataFrame(
        {
            "open": price, "high": price + 1.0, "low": price - 1.0,
            "close": price, "volume": 1_000_000.0,
        },
        index=index,
    )


def _run_engine() -> dict:
    import backtest.engine as engine_module
    from router.router import Router

    def _router_factory(strategies, _configuration, log_path=None):
        return Router(
            strategies,
            regime_map={state.name: "HoldTen" for state in MarketState},
            log_path=log_path,
        )

    engine = BacktestEngine(
        initial_capital=10_000.0, slippage=0.0, warmup_period=ENTRY_BAR - 5,
    )
    original = engine_module.build_router
    engine_module.build_router = _router_factory
    try:
        return engine.run(
            {SYMBOL: _flat_prices()},
            strategies={"HoldTen": _HoldForTenBarsStrategy()},
            routing_log_enabled=False,
        )
    finally:
        engine_module.build_router = original


@pytest.fixture(scope="module")
def result() -> dict:
    return _run_engine()


class TestEngineRecordsExposurePerBar:
    def test_every_equity_row_carries_its_book(self, result):
        """A missing row would read as flat, which is a different claim."""
        curve = result["equity_curve"]

        for column in EXPOSURE_COLUMNS:
            assert column in curve.columns
            assert curve[column].isna().sum() == 0

    def test_flat_bars_are_zero_and_held_bars_are_not(self, result):
        curve = result["equity_curve"]
        invested = curve[curve["gross_exposure"] > 0]

        assert not invested.empty
        assert curve["gross_exposure"].iloc[0] == pytest.approx(0.0)
        assert curve["gross_exposure"].iloc[-1] == pytest.approx(0.0)
        assert (invested["priced_symbols"] == 1).all()

    def test_gross_matches_quantity_times_mark(self, result):
        curve = result["equity_curve"]
        trades = result["trades"]
        entry = next(trade for trade in trades if trade["side"] == "buy")
        held = curve[curve["gross_exposure"] > 0]

        # Flat 100 close throughout, so every held bar marks at the same price.
        assert held["gross_exposure"].iloc[0] == pytest.approx(
            entry["qty"] * 100.0
        )

    def test_leverage_is_exposure_over_that_rows_equity(self, result):
        curve = result["equity_curve"]
        held = curve[curve["gross_exposure"] > 0]

        assert (
            held["gross_exposure_pct_equity"]
            - held["gross_exposure"] / held["equity"]
        ).abs().max() == pytest.approx(0.0)

    def test_a_long_only_run_has_net_equal_to_gross(self, result):
        curve = result["equity_curve"]

        assert (curve["net_exposure"] - curve["gross_exposure"]).abs().max() == (
            pytest.approx(0.0)
        )


class TestSummary:
    def test_flat_periods_do_not_hide_the_leverage_when_invested(self, result):
        summary = summarize_exposure(result["equity_curve"])

        assert summary["status"] == "ok"
        assert 0.0 < summary["time_in_market_ratio"] < 1.0
        # The whole point of reporting both: diluted by flat time vs actual.
        assert (
            summary["mean_gross_leverage"]
            < summary["mean_gross_leverage_invested"]
        )
        assert summary["max_gross_leverage"] >= (
            summary["mean_gross_leverage_invested"]
        )
        assert summary["max_open_positions"] == 1

    def test_a_curve_without_the_columns_is_unrecorded_not_zero(self):
        curve = pd.DataFrame(
            {"equity": [100.0, 101.0], "cash": [100.0, 101.0]},
            index=pd.date_range("2024-01-01", periods=2),
        )

        summary = summarize_exposure(curve)

        assert summary["status"] == "not_recorded"
        assert "mean_gross_leverage" not in summary

    def test_short_exposure_is_negative_net_and_positive_gross(self):
        index = pd.date_range("2024-01-01", periods=3)
        curve = pd.DataFrame(
            {
                "equity": [1000.0, 1000.0, 1000.0],
                "cash": [1000.0, 1000.0, 1000.0],
                "gross_exposure": [0.0, 500.0, 500.0],
                "net_exposure": [0.0, -500.0, 500.0],
                "priced_symbols": [0, 1, 1],
                "gross_exposure_pct_equity": [0.0, 0.5, 0.5],
                "net_exposure_pct_equity": [0.0, -0.5, 0.5],
            },
            index=index,
        )

        summary = summarize_exposure(curve)

        assert summary["short_period_ratio"] == pytest.approx(1 / 3)
        assert summary["long_period_ratio"] == pytest.approx(1 / 3)
        assert summary["max_net_short_leverage"] == pytest.approx(-0.5)
        assert summary["max_net_long_leverage"] == pytest.approx(0.5)


class TestReportSurfaces:
    def test_exposure_reaches_extended_analytics_and_report_text(self, result):
        with tempfile.TemporaryDirectory() as directory:
            metrics = ReportGenerator(directory).generate(
                result["trades"], result["equity_curve"],
            )
            report = (Path(directory) / "report.txt").read_text(encoding="utf-8")

        exposure = metrics["ExtendedAnalytics"]["exposure"]
        assert exposure["status"] == "ok"
        assert "Exposure (敞口与杠杆)" in report
        assert "Time in market" in report

    def test_an_old_curve_says_so_instead_of_reporting_zeros(self):
        curve = pd.DataFrame(
            {"equity": [100.0, 101.0], "cash": [100.0, 101.0]},
            index=pd.date_range("2024-01-01", periods=2),
        )
        with tempfile.TemporaryDirectory() as directory:
            ReportGenerator(directory).generate([], curve)
            report = (Path(directory) / "report.txt").read_text(encoding="utf-8")

        assert "no exposure columns" in report


class TestWorkbookColumnsFollowNames:
    """The curve gained columns; the sheet's formats must not follow indices."""

    def test_number_formats_land_on_the_named_columns(self, result):
        openpyxl = pytest.importorskip("openpyxl")
        from backtest.reporting.render.workbook import write_workbook_report

        with tempfile.TemporaryDirectory() as directory:
            reporter = ReportGenerator(directory)
            metrics = reporter.generate(
                result["trades"], result["equity_curve"], metrics_only=True,
            )
            write_workbook_report(
                directory, metrics, result["equity_curve"], result["trades"], [],
            )
            book = openpyxl.load_workbook(
                Path(directory) / "backtest_report.xlsx"
            )

        sheet = book["Equity"]
        header = {cell.value: cell.column for cell in sheet[1]}
        assert "gross_exposure_pct_equity" in header

        def _format(name: str) -> str:
            return sheet.cell(row=2, column=header[name]).number_format

        assert _format("equity") == "#,##0.00"
        assert _format("gross_exposure") == "#,##0.00"
        assert _format("drawdown") == "0.00%"
        assert _format("gross_exposure_pct_equity") == "0.00%"
