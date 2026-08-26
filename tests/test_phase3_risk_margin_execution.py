"""Phase 3 acceptance suite: drawdown, accounts, carry, liquidation and capacity."""

from __future__ import annotations

import pandas as pd
import pytest

from backtest.capacity import run_capacity_curve
from core.accounts import AccountMode
from core.broker import BacktestOrderStatus, Broker
from core.portfolio import Portfolio
from core.risk import BreakerAction, RiskManager
from tests.engine_baseline_harness import build_synthetic_data_map


def _bar(timestamp: str, **overrides):
    values = {
        "open": 100.0,
        "high": 102.0,
        "low": 98.0,
        "close": 100.0,
        "volume": 1000.0,
    }
    values.update(overrides)
    return pd.Series(values, name=pd.Timestamp(timestamp))


class TestHighWaterDrawdownProtection:
    def test_tiered_actions_are_high_water_based_sticky_and_manual(self):
        risk = RiskManager(
            daily_loss_limit=0.50,
            portfolio_drawdown_reduce=0.10,
            portfolio_drawdown_block=0.15,
            portfolio_drawdown_liquidate=0.20,
            portfolio_drawdown_lock=0.25,
        )
        assert risk.check_circuit_breaker(100.0, 100.0) is False
        assert risk.check_circuit_breaker(89.0, 100.0) is False
        assert risk.breaker_action is BreakerAction.REDUCE
        assert risk.risk_multiplier == pytest.approx(0.5)

        assert risk.check_circuit_breaker(84.0, 100.0) is True
        assert risk.breaker_action is BreakerAction.BLOCK_NEW
        risk.reset_daily_breaker()
        assert risk.check_circuit_breaker(99.0, 99.0) is True
        assert risk.breaker_action is BreakerAction.BLOCK_NEW

        risk.check_circuit_breaker(74.0, 99.0)
        assert risk.breaker_action is BreakerAction.LOCKED
        risk.manual_resume(approved_by="risk-owner", current_equity=74.0, rebase_high_water=True)
        assert risk.breaker_action is BreakerAction.NORMAL
        assert risk.circuit_breaker_triggered is False
        assert risk.breaker_audit[-1]["approved_by"] == "risk-owner"

    def test_daily_loss_limit_resets_without_resetting_high_water_state(self):
        risk = RiskManager(
            daily_loss_limit=0.05,
            portfolio_drawdown_reduce=0.50,
            portfolio_drawdown_block=0.60,
            portfolio_drawdown_liquidate=0.70,
            portfolio_drawdown_lock=0.80,
        )
        risk.check_circuit_breaker(100.0, 100.0)
        assert risk.check_circuit_breaker(94.0, 100.0) is True
        assert risk.breaker_action is BreakerAction.NORMAL
        risk.reset_daily_breaker()
        assert risk.circuit_breaker_triggered is False
        assert risk.high_water_equity == pytest.approx(100.0)


class TestAccountModesAndMargin:
    def test_spot_forbids_short_while_margin_keeps_collateral_separate(self):
        spot = Portfolio(1000.0, account_mode=AccountMode.SPOT)
        spot_broker = Broker(spot, commission_rate=0.0)
        order = spot_broker.submit_order(
            "BTC", "short", 1.0, timestamp=pd.Timestamp("2024-01-01")
        )
        assert spot_broker.process_orders({"BTC": _bar("2024-01-02")}) == []
        assert order.status is BacktestOrderStatus.REJECTED

        margin = Portfolio(
            1000.0,
            account_mode=AccountMode.SPOT_MARGIN,
            initial_margin_rate=0.20,
            maintenance_margin_rate=0.10,
        )
        margin.update_position("BTC", 2.0, 100.0, fee=1.0)
        assert margin.cash == pytest.approx(999.0)
        assert margin.get_equity({"BTC": 110.0}) == pytest.approx(1019.0)
        snapshot = margin.margin_snapshot({"BTC": 110.0})
        assert snapshot.initial_margin == pytest.approx(44.0)
        assert snapshot.available_margin == pytest.approx(975.0)

    def test_initial_and_maintenance_margin_are_enforced(self):
        portfolio = Portfolio(
            1000.0,
            account_mode=AccountMode.PERPETUAL,
            initial_margin_rate=0.50,
            maintenance_margin_rate=0.20,
        )
        broker = Broker(portfolio, commission_rate=0.0)
        order = broker.submit_order(
            "BTC", "buy", 30.0, timestamp=pd.Timestamp("2024-01-01")
        )
        assert broker.process_orders({"BTC": _bar("2024-01-02")}) == []
        assert order.status is BacktestOrderStatus.REJECTED

        portfolio.update_position("BTC", 20.0, 100.0)
        snapshot = portfolio.margin_snapshot({"BTC": 1.0})
        assert snapshot.liquidation_required is True
        assert snapshot.equity == pytest.approx(-980.0)
        assert snapshot.maintenance_margin == pytest.approx(4.0)

    def test_mark_price_liquidation_uses_canonical_close_path(self):
        portfolio = Portfolio(
            1000.0,
            account_mode=AccountMode.PERPETUAL,
            initial_margin_rate=0.50,
            maintenance_margin_rate=0.20,
        )
        portfolio.update_position(
            "BTC", 20.0, 100.0, strategy_id="Leveraged", order_id="entry"
        )
        broker = Broker(
            portfolio,
            commission_rate=0.0,
            volatility_slippage_factor=0.0,
            max_participation_rate=0.05,
        )
        bar = _bar("2024-01-02", mark_price=1.0, open=1.0, high=1.0, low=1.0, close=1.0)
        assert portfolio.margin_snapshot({"BTC": 1.0}).liquidation_required is True
        trades = broker.force_liquidate(
            {"BTC": bar}, timestamp=bar.name, reason="MarginLiquidation"
        )
        assert len(trades) == 1
        assert trades[0]["theoretical_price"] == pytest.approx(1.0)
        assert trades[0]["exit_reason"] == "MarginLiquidation"
        assert portfolio.positions == {}
        assert broker.close_events[-1].exit_reason == "MarginLiquidation"


class TestFinancingAndBorrow:
    def test_historical_funding_is_signed_and_auditable(self):
        portfolio = Portfolio(
            1000.0,
            account_mode=AccountMode.PERPETUAL,
            initial_margin_rate=0.20,
            maintenance_margin_rate=0.10,
        )
        portfolio.update_position("BTC", 1.0, 100.0)
        broker = Broker(portfolio, funding_rate_required=True)
        entries = broker.accrue_carry({
            "BTC": _bar("2024-01-01T08:00:00Z", mark_price=100.0, funding_rate=0.01)
        })
        assert entries[0]["kind"] == "funding"
        assert entries[0]["source"] == "historical_bar"
        assert entries[0]["amount"] == pytest.approx(1.0)
        assert portfolio.cash == pytest.approx(999.0)

    def test_missing_required_funding_rate_fails_closed(self):
        portfolio = Portfolio(
            1000.0,
            account_mode=AccountMode.PERPETUAL,
            initial_margin_rate=0.20,
            maintenance_margin_rate=0.10,
        )
        portfolio.update_position("BTC", 1.0, 100.0)
        with pytest.raises(ValueError, match="missing funding_rate"):
            Broker(portfolio, funding_rate_required=True).accrue_carry({
                "BTC": _bar("2024-01-01T08:00:00Z")
            })

    def test_margin_short_borrow_limit_and_cost_are_audited(self):
        portfolio = Portfolio(
            1000.0,
            account_mode=AccountMode.SPOT_MARGIN,
            initial_margin_rate=0.20,
            maintenance_margin_rate=0.10,
        )
        broker = Broker(
            portfolio,
            commission_rate=0.0,
            default_borrow_limit_qty=2.0,
            default_borrow_rate_annual=0.365,
        )
        rejected = broker.submit_order(
            "BTC", "short", 3.0, timestamp=pd.Timestamp("2024-01-01")
        )
        broker.process_orders({"BTC": _bar("2024-01-02")})
        assert rejected.status is BacktestOrderStatus.REJECTED
        assert broker.execution_audit[-1]["reason"] == "borrow_limit"

        portfolio.update_position("BTC", -1.0, 100.0)
        broker.accrue_carry({"BTC": _bar("2024-01-02")})
        entries = broker.accrue_carry({"BTC": _bar("2024-01-03")})
        assert entries[0]["kind"] == "borrow"
        assert entries[0]["amount"] == pytest.approx(0.1)


class TestDynamicExecutionAndCapacity:
    def test_spread_volatility_impact_and_partial_fills_are_visible(self):
        portfolio = Portfolio(10_000.0)
        broker = Broker(
            portfolio,
            commission_rate=0.0,
            slippage=0.0,
            spread_bps=10.0,
            volatility_slippage_factor=0.10,
            use_impact_cost=True,
            impact_coefficient=0.10,
            impact_exponent=1.5,
            max_participation_rate=0.05,
        )
        order = broker.submit_order(
            "BTC", "buy", 10.0, timestamp=pd.Timestamp("2024-01-01")
        )
        first = broker.process_orders({
            "BTC": _bar("2024-01-02", volume=100.0, high=105.0, low=95.0)
        })
        assert first[0]["qty"] == pytest.approx(5.0)
        assert first[0]["fill_price"] > 100.0
        assert first[0]["spread_slippage_rate"] == pytest.approx(0.0005)
        assert first[0]["volatility_slippage_rate"] == pytest.approx(0.01)
        assert first[0]["impact_slippage_rate"] > 0
        assert order.status is BacktestOrderStatus.PARTIALLY_FILLED
        second = broker.process_orders({"BTC": _bar("2024-01-03", volume=100.0)})
        assert second[0]["qty"] == pytest.approx(5.0)
        assert order.status is BacktestOrderStatus.FILLED
        assert any(row["reason"] == "participation_limit" for row in broker.execution_audit)

    def test_four_level_capacity_curve_is_explanation_complete(self):
        report = run_capacity_curve(
            build_synthetic_data_map(bars=90),
            capital_levels=(10_000, 100_000, 1_000_000, 10_000_000),
            engine_kwargs={"warmup_period": 30, "account_mode": "spot_margin"},
        )
        assert report["capital_levels"] == [10_000.0, 100_000.0, 1_000_000.0, 10_000_000.0]
        assert len(report["points"]) == 4
        assert report["all_path_changes_explained"] is True
        assert all(row["explanation"] for row in report["return_inflections"])
        for point in report["points"]:
            assert point["explanation"]
