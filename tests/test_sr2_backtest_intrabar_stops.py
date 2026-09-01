"""STR-P1-01: the backtest's protective stop is a resident venue stop.

Before this, a backtest found the breach at the bar's close and exited with a
market order at the *next* bar's open, while live carried a real reduce-only
stop-market order. These tests pin the equivalence: the level rests at the
broker, it fills inside the bar it is breached, its price follows the
pre-registered conservative OHLC path, and it is never the second close of a
position something else already closed.

See ``backtest/protective_stops.py`` and §8 of
``docs/protective_stop_contract.md``.
"""
from __future__ import annotations

import pandas as pd
import pytest

from backtest.protective_stops import CONSERVATIVE_BAR_PATH, ResidentStopSimulator
from core.broker import Broker
from core.broker.matching import is_protective_stop
from core.portfolio import Portfolio
from core.runtime import MarketDataSlice

SYMBOL = "BTC/USDT"


def _bar(ts, o, h, l, c, v=1_000_000.0):
    return pd.Series(
        {"open": o, "high": h, "low": l, "close": c, "volume": v},
        name=pd.Timestamp(ts),
    )


def _event(ts, bar):
    return MarketDataSlice(
        timestamp=pd.Timestamp(ts), bars={SYMBOL: bar}, histories={},
    )


class _FakeStrategy:
    """Only the surface the simulator reads: ``context[symbol]['stop_loss']``."""

    def __init__(self, stop=None):
        self.name = "Fake"
        self.context = {SYMBOL: {"stop_loss": stop}} if stop else {}

    def set_stop(self, stop):
        self.context.setdefault(SYMBOL, {})["stop_loss"] = stop


def _harness(stop=95.0, *, enabled=True):
    portfolio = Portfolio(initial_capital=10_000.0)
    broker = Broker(portfolio, commission_rate=0.0, slippage=0.0)
    strategy = _FakeStrategy(stop)
    sim = ResidentStopSimulator(
        broker, {"Fake": strategy}, enabled=enabled,
    )
    return portfolio, broker, strategy, sim


def _open_long(broker, *, ts, entry_bar, qty=1.0, price=100.0, stop=95.0):
    """Queue an entry on ``ts`` and fill it at ``entry_bar``'s open."""
    broker.submit_order(
        SYMBOL, "buy", qty, price=price, order_type="market",
        timestamp=pd.Timestamp(ts), strategy_id="Fake", stop_loss=stop,
    )
    return broker.process_orders({SYMBOL: entry_bar})


class TestResidentStopIsArmedFromTheFill:
    def test_entry_bar_is_protected_within_its_own_bar(self):
        """Live arms the stop on the fill, so the entry bar cannot be naked."""
        portfolio, broker, _, sim = _harness(stop=95.0)
        entry = _bar("2024-01-02", 100.0, 101.0, 94.0, 96.0)
        _open_long(broker, ts="2024-01-01", entry_bar=entry)
        assert portfolio.get_position(SYMBOL)["qty"] == pytest.approx(1.0)

        trades = sim.step(_event("2024-01-02", entry), bar_index=1)

        assert len(trades) == 1
        assert trades[0]["side"] == "sell"
        # Filled at the level, on the entry bar itself - not at the next open.
        assert trades[0]["fill_price"] == pytest.approx(95.0)
        assert portfolio.get_position(SYMBOL)["qty"] == pytest.approx(0.0)

    def test_no_stop_before_the_entry_fills(self):
        _, broker, _, sim = _harness(stop=95.0)
        broker.submit_order(
            SYMBOL, "buy", 1.0, price=100.0, order_type="market",
            timestamp=pd.Timestamp("2024-01-01"), strategy_id="Fake",
            stop_loss=95.0,
        )
        # Entry still working: nothing is filled, so nothing may be protected.
        sim.step(_event("2024-01-01", _bar("2024-01-01", 100, 101, 90, 96)))
        assert not [
            o for o in broker.pending_orders + broker.active_orders
            if is_protective_stop(o)
        ]


class TestConservativeIntrabarPath:
    def test_breach_fills_at_the_level_not_the_next_open(self):
        portfolio, broker, _, sim = _harness(stop=95.0)
        calm = _bar("2024-01-02", 100.0, 102.0, 99.0, 101.0)
        _open_long(broker, ts="2024-01-01", entry_bar=calm)
        sim.step(_event("2024-01-02", calm), bar_index=1)
        assert portfolio.get_position(SYMBOL)["qty"] == pytest.approx(1.0)

        breach = _bar("2024-01-03", 100.0, 100.5, 93.0, 99.0)
        trades = sim.step(_event("2024-01-03", breach), bar_index=2)

        assert len(trades) == 1
        # min(open, stop): the bar traded through 95 on its way to 93.
        assert trades[0]["fill_price"] == pytest.approx(95.0)
        # The legacy path would have exited on the *next* bar at 99+; the whole
        # point of STR-P1-01 is that this closes inside the breach bar.
        assert pd.Timestamp(trades[0]["fill_time"]) == pd.Timestamp("2024-01-03")

    def test_gap_through_the_level_fills_at_the_gapped_open(self):
        """A stop cannot fill at a price the bar never traded."""
        _, broker, _, sim = _harness(stop=95.0)
        calm = _bar("2024-01-02", 100.0, 102.0, 99.0, 101.0)
        _open_long(broker, ts="2024-01-01", entry_bar=calm)
        sim.step(_event("2024-01-02", calm), bar_index=1)

        gap = _bar("2024-01-03", 88.0, 90.0, 85.0, 89.0)
        trades = sim.step(_event("2024-01-03", gap), bar_index=2)

        assert trades[0]["fill_price"] == pytest.approx(88.0)

    def test_untouched_level_does_not_fire(self):
        portfolio, broker, _, sim = _harness(stop=95.0)
        calm = _bar("2024-01-02", 100.0, 102.0, 99.0, 101.0)
        _open_long(broker, ts="2024-01-01", entry_bar=calm)
        sim.step(_event("2024-01-02", calm), bar_index=1)

        held = _bar("2024-01-03", 101.0, 105.0, 95.5, 104.0)
        assert sim.step(_event("2024-01-03", held), bar_index=2) == []
        assert portfolio.get_position(SYMBOL)["qty"] == pytest.approx(1.0)

    def test_adverse_extreme_is_walked_before_the_favourable_one(self):
        """A bar that could have hit both stop and profit resolves as a stop."""
        _, broker, _, sim = _harness(stop=95.0)
        calm = _bar("2024-01-02", 100.0, 102.0, 99.0, 101.0)
        _open_long(broker, ts="2024-01-01", entry_bar=calm)
        sim.step(_event("2024-01-02", calm), bar_index=1)

        both = _bar("2024-01-03", 100.0, 130.0, 90.0, 128.0)
        trades = sim.step(_event("2024-01-03", both), bar_index=2)

        assert len(trades) == 1
        assert trades[0]["fill_price"] == pytest.approx(95.0)


class TestRatchet:
    def test_raising_the_level_cancel_replaces_upward(self):
        _, broker, strategy, sim = _harness(stop=95.0)
        calm = _bar("2024-01-02", 100.0, 102.0, 99.0, 101.0)
        _open_long(broker, ts="2024-01-01", entry_bar=calm)
        sim.step(_event("2024-01-02", calm), bar_index=1)

        strategy.set_stop(98.0)
        quiet = _bar("2024-01-03", 101.0, 105.0, 100.0, 104.0)
        sim.step(_event("2024-01-03", quiet), bar_index=2)

        resting = [
            o for o in broker.pending_orders + broker.active_orders
            if is_protective_stop(o)
        ]
        assert len(resting) == 1
        assert resting[0].price == pytest.approx(98.0)

    def test_loosening_the_level_is_ignored(self):
        _, broker, strategy, sim = _harness(stop=95.0)
        calm = _bar("2024-01-02", 100.0, 102.0, 99.0, 101.0)
        _open_long(broker, ts="2024-01-01", entry_bar=calm)
        sim.step(_event("2024-01-02", calm), bar_index=1)

        strategy.set_stop(80.0)
        quiet = _bar("2024-01-03", 101.0, 105.0, 100.0, 104.0)
        sim.step(_event("2024-01-03", quiet), bar_index=2)

        resting = [
            o for o in broker.pending_orders + broker.active_orders
            if is_protective_stop(o)
        ]
        assert len(resting) == 1
        assert resting[0].price == pytest.approx(95.0)


class TestExactlyOneAuthoritativeClose:
    def test_strategy_exit_at_the_open_cancels_the_resident_stop(self):
        """The market exit fills first on the path; the stop must not re-sell."""
        portfolio, broker, _, sim = _harness(stop=95.0)
        calm = _bar("2024-01-02", 100.0, 102.0, 99.0, 101.0)
        _open_long(broker, ts="2024-01-01", entry_bar=calm)
        sim.step(_event("2024-01-02", calm), bar_index=1)

        broker.submit_order(
            SYMBOL, "sell", 1.0, price=101.0, order_type="market",
            timestamp=pd.Timestamp("2024-01-02"), strategy_id="Fake",
            exit_reason="Breakout Exit",
        )
        # Next bar: the queued exit fills at the open, and the same bar's low
        # would also have breached the stop.
        nxt = _bar("2024-01-03", 100.0, 100.5, 90.0, 92.0)
        broker.process_orders({SYMBOL: nxt})
        assert portfolio.get_position(SYMBOL)["qty"] == pytest.approx(0.0)

        assert sim.step(_event("2024-01-03", nxt), bar_index=2) == []
        assert portfolio.get_position(SYMBOL)["qty"] == pytest.approx(0.0)
        assert not [
            o for o in broker.pending_orders + broker.active_orders
            if is_protective_stop(o)
        ]

    def test_forced_liquidation_cancels_the_resident_stop(self):
        portfolio, broker, _, sim = _harness(stop=95.0)
        calm = _bar("2024-01-02", 100.0, 102.0, 99.0, 101.0)
        _open_long(broker, ts="2024-01-01", entry_bar=calm)
        sim.step(_event("2024-01-02", calm), bar_index=1)

        breach = _bar("2024-01-03", 100.0, 100.5, 90.0, 92.0)
        broker.force_liquidate(
            {SYMBOL: breach}, timestamp=pd.Timestamp("2024-01-03"),
            reason="DailyLossLimit", risk_action_id="daily-1",
        )

        assert portfolio.get_position(SYMBOL)["qty"] == pytest.approx(0.0)
        assert not [
            o for o in broker.pending_orders + broker.active_orders
            if is_protective_stop(o)
        ]

    def test_partial_position_reduce_re_arms_at_the_new_quantity(self):
        portfolio, broker, _, sim = _harness(stop=95.0)
        calm = _bar("2024-01-02", 100.0, 102.0, 99.0, 101.0)
        _open_long(broker, ts="2024-01-01", entry_bar=calm, qty=2.0)
        sim.step(_event("2024-01-02", calm), bar_index=1)

        reduce_bar = _bar("2024-01-03", 101.0, 103.0, 100.0, 102.0)
        broker.force_liquidate(
            {SYMBOL: reduce_bar}, timestamp=pd.Timestamp("2024-01-03"),
            reason="DrawdownReduce", remaining_fraction=0.5,
        )
        assert portfolio.get_position(SYMBOL)["qty"] == pytest.approx(1.0)

        nxt = _bar("2024-01-04", 102.0, 104.0, 101.0, 103.0)
        sim.step(_event("2024-01-04", nxt), bar_index=3)
        resting = [
            o for o in broker.pending_orders + broker.active_orders
            if is_protective_stop(o)
        ]
        assert len(resting) == 1
        assert resting[0].qty == pytest.approx(1.0)


class TestAuditAndSwitch:
    def test_audit_records_the_path_and_the_level_that_fired(self):
        _, broker, _, sim = _harness(stop=95.0)
        entry = _bar("2024-01-02", 100.0, 101.0, 94.0, 96.0)
        _open_long(broker, ts="2024-01-01", entry_bar=entry)
        sim.step(_event("2024-01-02", entry), bar_index=1)

        placed = [row for row in sim.audit if row["action"] == "place"]
        fills = [row for row in sim.audit if row["action"] == "fill"]
        assert placed and placed[0]["stop_price"] == pytest.approx(95.0)
        assert len(fills) == 1
        assert fills[0]["stop_price"] == pytest.approx(95.0)
        assert fills[0]["fill_price"] == pytest.approx(95.0)
        assert fills[0]["note"] == CONSERVATIVE_BAR_PATH
        assert sim.triggered_stops == 1

    def test_disabled_simulator_places_nothing(self):
        portfolio, broker, _, sim = _harness(stop=95.0, enabled=False)
        entry = _bar("2024-01-02", 100.0, 101.0, 94.0, 96.0)
        _open_long(broker, ts="2024-01-01", entry_bar=entry)

        assert sim.step(_event("2024-01-02", entry), bar_index=1) == []
        assert portfolio.get_position(SYMBOL)["qty"] == pytest.approx(1.0)
        assert not [
            o for o in broker.pending_orders + broker.active_orders
            if is_protective_stop(o)
        ]

    def test_position_without_a_level_is_counted_not_flattened(self):
        """A research arm with no stop must stay visible, not be closed for it."""
        portfolio, broker, _, sim = _harness(stop=None)
        entry = _bar("2024-01-02", 100.0, 101.0, 94.0, 96.0)
        _open_long(broker, ts="2024-01-01", entry_bar=entry, stop=0.0)

        assert sim.step(_event("2024-01-02", entry), bar_index=1) == []
        assert portfolio.get_position(SYMBOL)["qty"] == pytest.approx(1.0)
        assert sim.unprotected_position_bars == 1
        assert any(
            row["reason"] == "no_protective_level" for row in sim.audit
        )


class TestOrderFilterIsolation:
    def test_filtered_pass_leaves_other_working_orders_untouched(self):
        """The stop's own matching pass must not re-price the rest of the book."""
        _, broker, _, _ = _harness(stop=95.0)
        # A resting limit that this bar cannot fill.
        order = broker.submit_order(
            SYMBOL, "buy", 1.0, price=50.0, order_type="limit",
            timestamp=pd.Timestamp("2024-01-01"), strategy_id="Fake",
        )
        bar = _bar("2024-01-02", 100.0, 102.0, 99.0, 101.0)
        assert broker.process_orders({SYMBOL: bar}, order_filter=is_protective_stop) == []
        assert order in broker.pending_orders + broker.active_orders
