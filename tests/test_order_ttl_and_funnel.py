"""Opening-order TTL (B-01) and the signal funnel wired to a real event log.

**TTL.** A GTC limit entry whose price is never touched used to work forever.
Nothing about that was visible: the order held its risk reservation (which
``core/risk/reservation.py`` releases only on a terminal status, and a working
order has none) and ``has_active_open_order`` kept the strategy from entering
that symbol again. One untouched order therefore removed a symbol from the run
permanently while consuming portfolio risk capacity - the roadmap's B-01,
carried as P0.

**Funnel.** ``calculate_signal_funnel`` read every payload as a mapping, but
the pipeline publishes dataclasses, so it raised ``AttributeError`` on any real
event log. It had no production caller, so nothing had ever found out.
"""
from __future__ import annotations

from decimal import Decimal

import pandas as pd
import pytest

from backtest.reporting import ReportGenerator
from core.broker import Broker
from core.broker.matching import PROTECTIVE_EXIT_REASON
from core.broker.types import BacktestOrderStatus
from core.metrics import calculate_signal_funnel
from core.portfolio import Portfolio

SYMBOL = "BTC/USDT"


def _bar(day: int, price: float = 100.0, volume: float = 1_000_000.0) -> pd.Series:
    return pd.Series(
        {
            "open": price, "high": price + 1.0, "low": price - 1.0,
            "close": price, "volume": volume,
        },
        name=pd.Timestamp(f"2024-01-{day:02d}"),
    )


def _broker(ttl: int = 3, **overrides) -> Broker:
    return Broker(
        Portfolio(initial_capital=1_000_000.0),
        commission_rate=0.0, commission_rate_maker=0.0, slippage=0.0,
        opening_order_ttl_bars=ttl, **overrides,
    )


def _drain(broker: Broker, days, price: float = 100.0) -> None:
    for day in days:
        broker.process_orders({SYMBOL: _bar(day, price)})


class TestUnreachableEntryExpires:
    def test_a_limit_that_never_prices_is_retired_after_the_ttl(self):
        broker = _broker(ttl=3)
        order = broker.submit_order(
            SYMBOL, "buy", 1.0, price=50.0, order_type="limit",
            timestamp=pd.Timestamp("2024-01-01"),
        )

        _drain(broker, range(2, 6))  # four bars, none of which touch 50

        assert order.status is BacktestOrderStatus.EXPIRED
        assert order.filled_qty == 0.0

    def test_it_survives_right_up_to_the_ttl(self):
        """Off-by-one here would cancel live orders a bar early."""
        broker = _broker(ttl=3)
        order = broker.submit_order(
            SYMBOL, "buy", 1.0, price=50.0, order_type="limit",
            timestamp=pd.Timestamp("2024-01-01"),
        )

        _drain(broker, range(2, 5))  # exactly ttl bars

        assert order.status is not BacktestOrderStatus.EXPIRED
        assert order in broker.active_orders

    def test_expiry_releases_the_risk_reservation(self):
        """The leak that locked capacity, not just the order object."""
        broker = _broker(ttl=2)
        broker.submit_order(
            SYMBOL, "buy", 4.0, price=50.0, order_type="limit",
            timestamp=pd.Timestamp("2024-01-01"),
        )
        _drain(broker, [2])
        assert broker.pending_open_notional() != {}

        _drain(broker, range(3, 6))

        assert broker.pending_open_notional() == {}

    def test_expiry_unblocks_the_symbol_for_new_entries(self):
        """``has_active_open_order`` is what made this a permanent lock."""
        broker = _broker(ttl=2)
        broker.submit_order(
            SYMBOL, "buy", 1.0, price=50.0, order_type="limit",
            timestamp=pd.Timestamp("2024-01-01"),
        )
        _drain(broker, [2])
        assert broker.has_active_open_order(SYMBOL) is True

        _drain(broker, range(3, 6))

        assert broker.has_active_open_order(SYMBOL) is False

    def test_the_expiry_is_audited_with_its_reason(self):
        broker = _broker(ttl=2)
        broker.submit_order(
            SYMBOL, "buy", 1.0, price=50.0, order_type="limit",
            timestamp=pd.Timestamp("2024-01-01"),
        )

        _drain(broker, range(2, 7))

        rows = [
            row for row in broker.execution_audit if row["outcome"] == "expired"
        ]
        assert len(rows) == 1
        assert rows[0]["reason"] == "opening_order_ttl"
        assert rows[0]["ttl_bars"] == 2


class TestPartialFillsDoNotResetTheClock:
    def test_a_partially_filled_entry_expires_at_its_total_age_limit(self):
        """Even repeated tiny fills cannot renew an opening order forever."""
        # 0.1% of 1e6 volume = 1000 units a bar, against a 20k order.
        broker = _broker(ttl=2, max_participation_rate=0.001)
        order = broker.submit_order(
            SYMBOL, "buy", 20_000.0, price=100.0, order_type="market",
            timestamp=pd.Timestamp("2024-01-01"),
        )

        _drain(broker, range(2, 5))  # third matchable bar is beyond the TTL

        assert order.status is BacktestOrderStatus.EXPIRED
        assert 0 < order.filled_qty < 20_000.0
        assert order.age_bars == 3

    def test_multiple_matching_passes_on_one_bar_age_once(self):
        broker = _broker(ttl=1, max_participation_rate=0.001)
        order = broker.submit_order(
            SYMBOL, "buy", 20_000.0, price=100.0, order_type="market",
            timestamp=pd.Timestamp("2024-01-01"),
        )

        bar = {SYMBOL: _bar(2)}
        broker.process_orders(bar)
        broker.process_orders(bar)

        assert order.age_bars == 1
        assert order.status is not BacktestOrderStatus.EXPIRED


class TestWhatTheRuleMustNotTouch:
    def test_exits_are_exempt(self):
        """Expiring an exit would leave a real position unmanaged."""
        broker = _broker(ttl=1)
        broker.submit_order(
            SYMBOL, "buy", 2.0, price=100.0, order_type="market",
            timestamp=pd.Timestamp("2024-01-01"),
        )
        _drain(broker, [2])
        exit_order = broker.submit_order(
            SYMBOL, "sell", 2.0, price=500.0, order_type="limit",
            timestamp=pd.Timestamp("2024-01-02"),
        )

        _drain(broker, range(3, 9))  # 500 is never reached

        assert exit_order.status is not BacktestOrderStatus.EXPIRED

    def test_a_resident_protective_stop_rests_indefinitely(self):
        broker = _broker(ttl=1)
        broker.submit_order(
            SYMBOL, "buy", 2.0, price=100.0, order_type="market",
            timestamp=pd.Timestamp("2024-01-01"),
        )
        _drain(broker, [2])
        stop = broker.submit_order(
            SYMBOL, "sell", 2.0, price=10.0, order_type="stop",
            timestamp=pd.Timestamp("2024-01-02"),
            exit_reason=PROTECTIVE_EXIT_REASON,
        )

        _drain(broker, range(3, 12))

        assert stop.status is not BacktestOrderStatus.EXPIRED

    def test_ttl_zero_restores_the_old_unbounded_behaviour(self):
        broker = _broker(ttl=0)
        order = broker.submit_order(
            SYMBOL, "buy", 1.0, price=50.0, order_type="limit",
            timestamp=pd.Timestamp("2024-01-01"),
        )

        _drain(broker, range(2, 20))

        assert order.status is not BacktestOrderStatus.EXPIRED

    def test_a_negative_ttl_is_refused(self):
        with pytest.raises(ValueError, match="opening_order_ttl_bars"):
            _broker(ttl=-1)


class TestSignalFunnelReadsRealEvents:
    """Payloads are dataclasses in the pipeline and dicts once serialised."""

    def test_dataclass_payloads_no_longer_raise(self):
        from core.domain import RiskDecision
        from core.events import FillEvent, OrderEvent

        class _Envelope:
            def __init__(self, correlation_id, event_type, payload):
                self.correlation_id = correlation_id
                self.event_type = event_type
                self.payload = payload

        events = [
            _Envelope("chain-1", "risk_decision", RiskDecision(
                decision_id="d1", account="a", symbol=SYMBOL, action="buy",
                requested_qty=1, approved_qty=1, reference_price=100,
                approved=True, reason="ok", intent_id="i1",
            )),
            _Envelope("chain-1", "order", OrderEvent(
                client_order_id="i1", status=BacktestOrderStatus.FILLED,
                requested_qty=1.0, filled_qty=1.0, remaining_qty=0.0,
            )),
            _Envelope("chain-1", "fill", FillEvent(
                fill_id="f1", client_order_id="i1", symbol=SYMBOL, side="buy",
                qty=Decimal("1"), price=Decimal("100"),
            )),
        ]

        funnel = calculate_signal_funnel(events)

        assert funnel["total_correlation_chains"] == 1
        assert funnel["stages"]["risk_evaluated"]["count"] == 1
        assert funnel["stages"]["risk_approved"]["count"] == 1
        assert funnel["stages"]["order_accepted"]["count"] == 1
        assert funnel["stages"]["filled"]["count"] == 1

    def test_mapping_payloads_still_work(self):
        class _Envelope:
            def __init__(self, correlation_id, event_type, payload):
                self.correlation_id = correlation_id
                self.event_type = event_type
                self.payload = payload

        events = [
            _Envelope("c", "risk_decision", {"approved": False}),
            _Envelope("c", "order_intent", {}),
        ]

        funnel = calculate_signal_funnel(events)

        assert funnel["stages"]["risk_evaluated"]["count"] == 1
        assert funnel["stages"]["risk_approved"]["count"] == 0
        assert funnel["stages"]["order_created"]["count"] == 1

    def test_an_engine_run_produces_a_populated_funnel(self):
        import tempfile

        from backtest.engine import BacktestEngine
        from tests.engine_baseline_harness import (
            DEFAULT_WARMUP_PERIOD,
            build_synthetic_data_map,
        )

        result = BacktestEngine(
            initial_capital=10_000.0, warmup_period=DEFAULT_WARMUP_PERIOD,
        ).run(build_synthetic_data_map(), routing_log_enabled=False)

        with tempfile.TemporaryDirectory() as directory:
            metrics = ReportGenerator(directory).generate(
                result["trades"], result["equity_curve"],
                metrics_only=True, event_log=result["event_log"],
            )

        funnel = metrics["ExtendedAnalytics"]["signal_funnel"]
        assert funnel["total_correlation_chains"] > 0
        assert funnel["stages"]["filled"]["count"] > 0
        # Exits carry no risk_decision (only opening intents reserve capacity),
        # so order_created legitimately exceeds risk_evaluated here.
        assert (
            funnel["stages"]["order_created"]["count"]
            >= funnel["stages"]["risk_evaluated"]["count"]
        )

    def test_it_is_absent_rather_than_zero_without_an_event_log(self):
        import tempfile

        curve = pd.DataFrame(
            {"equity": [100.0, 101.0], "cash": [100.0, 101.0]},
            index=pd.date_range("2024-01-01", periods=2),
        )
        with tempfile.TemporaryDirectory() as directory:
            metrics = ReportGenerator(directory).generate(
                [], curve, metrics_only=True,
            )

        assert "signal_funnel" not in metrics["ExtendedAnalytics"]
