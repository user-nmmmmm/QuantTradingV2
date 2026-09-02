"""Headline trade metrics count round trips, not FIFO legs.

``_reconstruct_closed_trades`` matches each closing fill against the opening
fills it retires, so one position split by the participation cap produces
several *legs*.  Reporting those as trades inflated ``TotalTrades`` and moved
``WinRate``/``ProfitFactor``/``Expectancy`` toward whatever the split happened
to produce - worst precisely on the large-capital runs ``backtest/capacity.py``
exists to evaluate.  It also fed ``calculate_r_multiple_stats`` the whole
lot's ``initial_risk`` once per leg, dividing a 1R trade down to 0.33R.

``_aggregate_round_trips`` folds legs back into one record per position, and
these pin that fold: what it merges, what it must not double count, and the
one consumer (``lifecycle_coverage``) that has to stay leg-level.
"""
from __future__ import annotations

import tempfile

import pandas as pd
import pytest

from backtest.reporting import ReportGenerator
from core.broker import Broker
from core.diagnostics import build_diagnostics
from core.portfolio import Portfolio

SYMBOL = "BTC/USDT"


def _bar(timestamp: str, price: float, volume: float = 100.0) -> pd.Series:
    return pd.Series(
        {
            "open": price, "high": price + 1.0, "low": price - 1.0,
            "close": price, "volume": volume,
        },
        name=pd.Timestamp(timestamp),
    )


@pytest.fixture
def reporter():
    with tempfile.TemporaryDirectory() as directory:
        yield ReportGenerator(directory)


def _broker() -> Broker:
    """A 10%-participation broker: a 25-unit order needs three 100-volume bars."""
    return Broker(
        Portfolio(initial_capital=1_000_000.0),
        commission_rate=0.0, commission_rate_maker=0.0, slippage=0.0,
        max_participation_rate=0.1,
    )


def _drain(broker: Broker, days: range | list, price: float) -> None:
    for day in days:
        broker.process_orders({SYMBOL: _bar(f"2024-01-{day:02d}", price)})


def _split_round_trip(exit_prices: tuple[float, ...] = (110.0, 110.0, 110.0)) -> Broker:
    """One 25-unit position, opened over three bars and closed over three.

    ``exit_prices`` prices each closing bar separately, so a caller can make
    the legs of a single position disagree about win/loss.
    """
    broker = _broker()
    broker.submit_order(
        SYMBOL, "buy", 25.0, price=100.0, order_type="market",
        timestamp=pd.Timestamp("2024-01-01"), stop_loss=90.0,
    )
    _drain(broker, [2, 3, 4], 100.0)
    broker.submit_order(
        SYMBOL, "sell", 25.0, price=100.0, order_type="market",
        timestamp=pd.Timestamp("2024-01-05"),
    )
    for day, price in zip((6, 7, 8), exit_prices):
        broker.process_orders({SYMBOL: _bar(f"2024-01-{day:02d}", price)})
    return broker


class TestOnePositionIsOneTrade:
    def test_a_split_position_is_three_legs_and_one_round_trip(self, reporter):
        broker = _split_round_trip()
        assert broker.portfolio.get_position(SYMBOL)["qty"] == pytest.approx(0.0)

        legs = reporter._reconstruct_closed_trades(pd.DataFrame(broker.trades))
        round_trips = reporter._aggregate_round_trips(legs)

        assert len(legs) == 3
        assert len(round_trips) == 1
        assert round_trips[0]["legs"] == 3
        assert round_trips[0]["position_id"] == legs[0]["position_id"]

    def test_headline_metrics_count_round_trips(self, reporter):
        broker = _split_round_trip()

        metrics = reporter.generate(
            broker.trades, _equity_curve(), metrics_only=True,
        )

        assert metrics["TotalTrades"] == 1
        assert metrics["ClosedTradeLegs"] == 3

    def test_pnl_totals_are_unchanged_by_the_fold(self, reporter):
        broker = _split_round_trip()
        legs = reporter._reconstruct_closed_trades(pd.DataFrame(broker.trades))
        round_trips = reporter._aggregate_round_trips(legs)

        for field in ("gross_pnl", "net_pnl", "commission", "slippage"):
            assert sum(trip[field] for trip in round_trips) == pytest.approx(
                sum(leg[field] for leg in legs)
            )

    def test_the_open_and_close_of_the_whole_position_are_kept(self, reporter):
        broker = _split_round_trip()
        legs = reporter._reconstruct_closed_trades(pd.DataFrame(broker.trades))
        trip = reporter._aggregate_round_trips(legs)[0]

        assert trip["entry_time"] == pd.Timestamp("2024-01-02")  # first opening fill
        assert trip["exit_time"] == pd.Timestamp("2024-01-08")   # last closing fill


class TestLegsMustNotSkewWinRate:
    """The regression: legs of one position can disagree about win/loss."""

    def test_a_net_winner_closed_partly_at_a_loss_is_one_win(self, reporter):
        # +10/unit on 10 units, +10 on 10, -5 on 5 => net +175 on one position.
        broker = _split_round_trip(exit_prices=(110.0, 110.0, 95.0))
        legs = reporter._reconstruct_closed_trades(pd.DataFrame(broker.trades))

        assert [leg["net_pnl"] > 0 for leg in legs] == [True, True, False]

        metrics = reporter.generate(broker.trades, _equity_curve(), metrics_only=True)

        assert metrics["TotalTrades"] == 1
        assert metrics["WinRate"] == pytest.approx(1.0)  # legs alone said 0.667
        assert metrics["NetPnL"] == pytest.approx(175.0)


class TestInitialRiskIsNotDoubleCounted:
    """Every partial close repeats its lot's *whole* initial risk."""

    def test_legs_each_repeat_the_lots_full_risk(self, reporter):
        broker = _split_round_trip()
        legs = reporter._reconstruct_closed_trades(pd.DataFrame(broker.trades))

        # 25 units, entry 100, stop 90 => 250 of risk, reported on all 3 legs.
        assert [leg["initial_risk"] for leg in legs] == [250.0, 250.0, 250.0]
        assert len({leg["lot_id"] for leg in legs}) == 1

    def test_the_round_trip_counts_that_risk_once(self, reporter):
        broker = _split_round_trip()
        legs = reporter._reconstruct_closed_trades(pd.DataFrame(broker.trades))
        trip = reporter._aggregate_round_trips(legs)[0]

        assert trip["initial_risk"] == pytest.approx(250.0)
        # Summing legs would have said 750 and turned a 1R trade into 0.33R.
        assert trip["net_pnl"] / trip["initial_risk"] == pytest.approx(1.0)

    def test_distinct_lots_in_one_position_do_add_up(self, reporter):
        """Two separately-sized entries are two lots and two risk amounts."""
        broker = _broker()
        for order_price_stop in ((10.0, 90.0), (10.0, 80.0)):
            qty, stop = order_price_stop
            broker.submit_order(
                SYMBOL, "buy", qty, price=100.0, order_type="market",
                timestamp=pd.Timestamp("2024-01-01"), stop_loss=stop,
            )
        _drain(broker, [2, 3], 100.0)
        broker.submit_order(
            SYMBOL, "sell", 20.0, price=100.0, order_type="market",
            timestamp=pd.Timestamp("2024-01-04"),
        )
        _drain(broker, [5, 6], 110.0)

        legs = reporter._reconstruct_closed_trades(pd.DataFrame(broker.trades))
        trip = reporter._aggregate_round_trips(legs)[0]

        assert len({leg["lot_id"] for leg in legs}) == 2
        # 10 * |100-90| + 10 * |100-80|
        assert trip["initial_risk"] == pytest.approx(300.0)


class TestGroupingBoundaries:
    def test_two_positions_in_one_symbol_stay_separate(self, reporter):
        broker = _broker()
        for open_day, close_day in ((1, 3), (5, 7)):
            broker.submit_order(
                SYMBOL, "buy", 5.0, price=100.0, order_type="market",
                timestamp=pd.Timestamp(f"2024-01-{open_day:02d}"),
            )
            _drain(broker, [open_day + 1], 100.0)
            broker.submit_order(
                SYMBOL, "sell", 5.0, price=100.0, order_type="market",
                timestamp=pd.Timestamp(f"2024-01-{close_day - 1:02d}"),
            )
            _drain(broker, [close_day], 110.0)

        legs = reporter._reconstruct_closed_trades(pd.DataFrame(broker.trades))
        round_trips = reporter._aggregate_round_trips(legs)

        assert len(round_trips) == 2
        assert len({trip["position_id"] for trip in round_trips}) == 2

    def test_legs_without_a_position_id_stay_one_trip_each(self, reporter):
        """Fill files recorded before ``lot_closes`` existed keep old behaviour."""
        legs = [
            {"net_pnl": 10.0, "gross_pnl": 10.0, "commission": 0.0, "slippage": 0.0,
             "symbol": SYMBOL, "strategy": "A", "position_id": None, "lot_id": None},
            {"net_pnl": -4.0, "gross_pnl": -4.0, "commission": 0.0, "slippage": 0.0,
             "symbol": SYMBOL, "strategy": "A", "position_id": None, "lot_id": None},
        ]

        round_trips = reporter._aggregate_round_trips(legs)

        assert len(round_trips) == 2
        assert [trip["net_pnl"] for trip in round_trips] == [10.0, -4.0]

    def test_a_position_entered_by_two_strategies_is_flagged(self, reporter):
        legs = [
            {"net_pnl": 10.0, "gross_pnl": 10.0, "commission": 0.0, "slippage": 0.0,
             "symbol": SYMBOL, "strategy": "A", "position_id": "P1", "lot_id": "L1"},
            {"net_pnl": 5.0, "gross_pnl": 5.0, "commission": 0.0, "slippage": 0.0,
             "symbol": SYMBOL, "strategy": "B", "position_id": "P1", "lot_id": "L2"},
        ]

        trip = reporter._aggregate_round_trips(legs)[0]

        assert trip["strategy"] == "A"  # attributed to the first entry
        assert trip["mixed_entry_strategies"] is True


class TestLifecycleCoverageStaysLegLevel:
    """Its counterpart, ``Strategy.observed_close_events``, counts lot closes."""

    def test_coverage_compares_against_legs_not_round_trips(self, reporter):
        broker = _split_round_trip()
        legs = reporter._reconstruct_closed_trades(pd.DataFrame(broker.trades))
        round_trips = reporter._aggregate_round_trips(legs)
        observed = {"Manual": len(broker.close_events)}
        assert observed["Manual"] == 3

        diagnostics = build_diagnostics(
            round_trips, _equity_curve()["equity"], observed, closed_legs=legs,
        )

        coverage = diagnostics["lifecycle_coverage"]
        assert coverage["overall_coverage"] == pytest.approx(1.0)
        assert coverage["blind_strategies"] == []

    def test_round_trips_alone_would_overstate_coverage(self, reporter):
        """Pins why the extra argument exists rather than reusing one list."""
        broker = _split_round_trip()
        legs = reporter._reconstruct_closed_trades(pd.DataFrame(broker.trades))
        round_trips = reporter._aggregate_round_trips(legs)

        diagnostics = build_diagnostics(
            round_trips, _equity_curve()["equity"], {"Manual": 3},
        )

        assert diagnostics["lifecycle_coverage"]["overall_coverage"] == pytest.approx(3.0)

    def test_the_other_diagnostics_see_round_trips(self, reporter):
        broker = _split_round_trip()
        legs = reporter._reconstruct_closed_trades(pd.DataFrame(broker.trades))
        round_trips = reporter._aggregate_round_trips(legs)

        diagnostics = build_diagnostics(
            round_trips, _equity_curve()["equity"], {"Manual": 3}, closed_legs=legs,
        )

        assert diagnostics["pnl_concentration"]["sample_size"] == 1
        assert diagnostics["exit_attribution"]["sample_size"] == 1


def _equity_curve() -> pd.DataFrame:
    index = pd.date_range("2024-01-01", periods=10, freq="D")
    return pd.DataFrame(
        {"equity": [1_000_000.0 + 25.0 * i for i in range(10)], "cash": 1_000_000.0},
        index=index,
    )
