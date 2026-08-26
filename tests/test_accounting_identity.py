"""Phase 1 (T-1.8): accounting-identity reconciliation (Gate G2).

equity(t) must equal initial_capital + cumulative_realized_pnl(t) +
unrealized_pnl(t) at every bar and cumulatively at the end of a run.
"""
import pandas as pd
import pytest

from backtest.engine import BacktestEngine
from core.accounting_check import AccountingReconciler
from core.lots import CloseEvent
from core.portfolio import Portfolio
from tests.engine_baseline_harness import DEFAULT_WARMUP_PERIOD, build_synthetic_data_map


class TestAccountingIdentityRealRun:
    def test_full_backtest_run_has_zero_accounting_discrepancies(self):
        data_map = build_synthetic_data_map(bars=120)
        engine = BacktestEngine(
            initial_capital=10_000.0, slippage=0.0005, warmup_period=DEFAULT_WARMUP_PERIOD,
        )
        result = engine.run(data_map, routing_log_enabled=False)

        check = result["accounting_check"]
        assert check["checks_performed"] > 0
        assert check["ok"] is True, check["discrepancies"]
        assert check["max_abs_difference"] < 1e-4

    def test_no_symbol_data_reports_a_clean_zero_check_result(self):
        # An empty dict short-circuits before any result dict is built; a
        # symbol whose frame normalizes to nothing hits the empty_result path
        # this accounting_check default belongs to.
        engine = BacktestEngine(initial_capital=10_000.0)
        result = engine.run({"BTC/USDT": pd.DataFrame()})
        assert result["accounting_check"] == {
            "ok": True,
            "checks_performed": 0,
            "max_abs_difference": 0.0,
            "discrepancy_count": 0,
            "discrepancies": [],
        }


class TestAccountingReconcilerCatchesCorruption:
    def test_matching_equity_and_pnl_passes(self):
        reconciler = AccountingReconciler(initial_capital=10_000.0)
        portfolio = Portfolio(initial_capital=10_000.0)
        portfolio.update_position("BTC/USDT", 1.0, 100.0, strategy_id="S", order_id="o1")
        close_event = CloseEvent(
            close_event_id="LOT-1:1", position_id="POS-1", lot_id="LOT-1",
            symbol="BTC/USDT", opening_strategy_id="S", exit_reason="signal",
            qty=1.0, exit_price=110.0, theoretical_exit_price=110.0,
            realized_pnl=10.0, timestamp=pd.Timestamp("2024-01-02"),
            is_position_fully_closed=True,
        )
        # After the close: no open lots, equity should be initial + realized.
        portfolio.update_position("BTC/USDT", -1.0, 110.0, strategy_id="S", order_id="o2")
        reconciler.check_bar(
            0, pd.Timestamp("2024-01-02"), equity=10_010.0, portfolio=portfolio,
            current_prices={"BTC/USDT": 110.0}, close_events=[close_event],
        )
        result = reconciler.result()
        assert result.ok is True
        assert result.discrepancies == []

    def test_corrupted_equity_is_flagged_as_a_discrepancy(self):
        reconciler = AccountingReconciler(initial_capital=10_000.0)
        portfolio = Portfolio(initial_capital=10_000.0)
        portfolio.update_position("BTC/USDT", 1.0, 100.0, strategy_id="S", order_id="o1")
        close_event = CloseEvent(
            close_event_id="LOT-1:1", position_id="POS-1", lot_id="LOT-1",
            symbol="BTC/USDT", opening_strategy_id="S", exit_reason="signal",
            qty=1.0, exit_price=110.0, theoretical_exit_price=110.0,
            realized_pnl=10.0, timestamp=pd.Timestamp("2024-01-02"),
            is_position_fully_closed=True,
        )
        portfolio.update_position("BTC/USDT", -1.0, 110.0, strategy_id="S", order_id="o2")
        # A corrupted/misreported equity (off by 500) must be caught.
        reconciler.check_bar(
            0, pd.Timestamp("2024-01-02"), equity=10_510.0, portfolio=portfolio,
            current_prices={"BTC/USDT": 110.0}, close_events=[close_event],
        )
        result = reconciler.result()
        assert result.ok is False
        assert len(result.discrepancies) == 1
        assert result.discrepancies[0].difference == pytest.approx(500.0)

    def test_unrealized_pnl_included_for_still_open_lots(self):
        reconciler = AccountingReconciler(initial_capital=10_000.0)
        portfolio = Portfolio(initial_capital=10_000.0)
        portfolio.update_position("BTC/USDT", 1.0, 100.0, strategy_id="S", order_id="o1")
        # No closes yet; mark price moved to 105 -> unrealized +5.
        reconciler.check_bar(
            0, pd.Timestamp("2024-01-02"), equity=10_005.0, portfolio=portfolio,
            current_prices={"BTC/USDT": 105.0}, close_events=[],
        )
        result = reconciler.result()
        assert result.ok is True
