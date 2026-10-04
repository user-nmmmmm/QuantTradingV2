"""Phase 1 (T-1.1 / T-1.2): lot-level position ledger tests.

Covers pyramiding (multiple adds), partial reduce, direction reversal and
partial fills spread across multiple broker fills for the same order, with
hand-calculated quantity/cost assertions.
"""
import pytest

from core.lots import LotBook
from core.portfolio import Portfolio
from core.protective_orders import authoritative_position_ids


class TestLotBookPyramidAndReduce:
    def test_single_open_then_full_close_produces_one_lot_close(self):
        book = LotBook("BTC/USDT")
        closes = book.apply_fill(10.0, 100.0, order_id="o1", strategy_id="TrendBreakout")
        assert closes == []
        assert len(book.open_lots) == 1
        lot = book.open_lots[0]
        assert lot.qty_open == 10.0
        assert lot.entry_price == 100.0

        closes = book.apply_fill(-10.0, 110.0, order_id="o2", strategy_id="TrendBreakout")
        assert len(closes) == 1
        close = closes[0]
        assert close.lot_id == lot.lot_id
        assert close.qty_closed == 10.0
        assert close.entry_price == 100.0
        assert close.fully_closed is True
        assert book.open_lots == []

    def test_pyramid_two_adds_then_partial_then_full_close_fifo(self):
        book = LotBook("BTC/USDT")
        # Two separate entries (pyramiding): distinct orders -> distinct lots.
        book.apply_fill(10.0, 100.0, order_id="o1", strategy_id="S")
        book.apply_fill(5.0, 120.0, order_id="o2", strategy_id="S")
        assert len(book.open_lots) == 2
        lot1, lot2 = book.open_lots
        assert (lot1.qty_open, lot1.entry_price) == (10.0, 100.0)
        assert (lot2.qty_open, lot2.entry_price) == (5.0, 120.0)

        # Partial reduce of 4 must close against the FIFO-first lot (lot1).
        closes = book.apply_fill(-4.0, 150.0, order_id="o3", strategy_id="S")
        assert len(closes) == 1
        assert closes[0].lot_id == lot1.lot_id
        assert closes[0].qty_closed == 4.0
        assert closes[0].fully_closed is False
        assert book.open_lots[0].qty_open == pytest.approx(6.0)

        # Full close of the remaining 11 (6 from lot1 + 5 from lot2), hand-calculated.
        closes = book.apply_fill(-11.0, 160.0, order_id="o4", strategy_id="S")
        assert len(closes) == 2
        assert closes[0].lot_id == lot1.lot_id
        assert closes[0].qty_closed == pytest.approx(6.0)
        assert closes[0].fully_closed is True
        assert closes[1].lot_id == lot2.lot_id
        assert closes[1].qty_closed == pytest.approx(5.0)
        assert closes[1].fully_closed is True
        assert book.open_lots == []

    def test_partial_fill_across_two_broker_fills_same_order_merges_into_one_lot(self):
        book = LotBook("BTC/USDT")
        # Same order_id filled across two bars (volume-budget constrained partials).
        book.apply_fill(6.0, 100.0, order_id="o1", strategy_id="S")
        book.apply_fill(4.0, 102.0, order_id="o1", strategy_id="S")
        assert len(book.open_lots) == 1
        lot = book.open_lots[0]
        assert lot.qty_open == pytest.approx(10.0)
        # Weighted-average entry price: (6*100 + 4*102) / 10 = 100.8
        assert lot.entry_price == pytest.approx(100.8)

    def test_direction_reversal_in_single_fill_closes_old_and_opens_new(self):
        book = LotBook("BTC/USDT")
        book.apply_fill(10.0, 100.0, order_id="o1", strategy_id="S")
        # Sell 15: closes the 10 long, then opens a new 5-qty short lot.
        closes = book.apply_fill(-15.0, 90.0, order_id="o2", strategy_id="S")
        assert len(closes) == 1
        assert closes[0].qty_closed == 10.0
        assert closes[0].side == "long"
        assert closes[0].fully_closed is True
        assert len(book.open_lots) == 1
        new_lot = book.open_lots[0]
        assert new_lot.side == "short"
        assert new_lot.qty_open == pytest.approx(5.0)
        assert new_lot.entry_price == 90.0
        assert new_lot.position_id != closes[0].position_id


class TestPortfolioLotIntegration:
    def test_update_position_returns_lot_closes_and_stays_in_sync(self):
        pf = Portfolio(initial_capital=10_000.0)
        pf.update_position(
            "BTC/USDT", 10.0, 100.0, fee=1.0, strategy_id="S", order_id="o1"
        )
        assert pf.get_position("BTC/USDT")["qty"] == 10.0
        assert pf.open_lots("BTC/USDT")[0].qty_open == 10.0

        closes = pf.update_position(
            "BTC/USDT", -10.0, 110.0, fee=1.0, strategy_id="S", order_id="o2"
        )
        assert len(closes) == 1
        assert closes[0].qty_closed == 10.0
        assert pf.get_position("BTC/USDT") == {"qty": 0.0, "avg_price": 0.0}
        assert pf.open_lots("BTC/USDT") == []

    def test_backward_compatible_call_without_lot_kwargs(self):
        # Existing callers (tests/test_router.py etc.) call update_position with
        # only symbol/qty_delta/price - must keep working unchanged.
        pf = Portfolio(initial_capital=10_000.0)
        pf.update_position("BTC/USDT", qty_delta=2.0, price=100.0)
        assert pf.get_position("BTC/USDT")["qty"] == 2.0
        assert pf.open_lots("BTC/USDT")[0].qty_open == 2.0

    def test_initial_risk_computed_from_stop_price(self):
        pf = Portfolio(initial_capital=10_000.0)
        pf.update_position(
            "BTC/USDT", 10.0, 100.0, strategy_id="S", order_id="o1", stop_price=95.0
        )
        lot = pf.open_lots("BTC/USDT")[0]
        assert lot.initial_risk == pytest.approx(50.0)  # |100-95| * 10

    def test_update_lot_extremes_tracks_mae_mfe(self):
        pf = Portfolio(initial_capital=10_000.0)
        pf.update_position("BTC/USDT", 10.0, 100.0, strategy_id="S", order_id="o1")
        pf.update_lot_extremes("BTC/USDT", high=105.0, low=98.0)
        pf.update_lot_extremes("BTC/USDT", high=103.0, low=90.0)
        lot = pf.open_lots("BTC/USDT")[0]
        assert lot.mfe == pytest.approx(5.0)  # best high (105) - entry (100)
        assert lot.mae == pytest.approx(10.0)  # entry (100) - worst low (90)

    @pytest.mark.parametrize("account_mode,direction", [
        ("spot", 1), ("spot_margin", 1), ("spot_margin", -1),
        ("perpetual", 1), ("perpetual", -1),
    ])
    def test_real_partial_exit_dust_retires_the_aggregate_position(self, account_mode, direction):
        # WF1 low-participation UNI exit left 4.26e-14 while the last lot closed.
        pf = Portfolio(initial_capital=10_000.0, account_mode=account_mode)
        symbol, opening_qty, entry_price = "UNI/USDT", 51.91851340000002, 8.631926793666135
        pf.update_position(symbol, direction * opening_qty, entry_price, fee=.2, order_id="entry")
        old_ids = authoritative_position_ids(pf, symbol)
        exits = [(14.5496794, 7.107327977484583),
                 (16.180608300000003, 7.039169977705942),
                 (21.188225699999975, 7.006581989288353)]
        expected_cash = 10_000.0 - .2
        if account_mode == "spot":
            expected_cash -= direction * opening_qty * entry_price
        for qty, price in exits:
            closes = pf.update_position(symbol, -direction * qty, price, fee=.1, order_id="exit")
            expected_cash += (direction * qty * price if account_mode == "spot"
                              else direction * qty * (price - entry_price)) - .1
        assert closes[-1].fully_closed is True
        assert pf.open_lots(symbol) == []
        assert pf.get_position(symbol) == {"qty": 0.0, "avg_price": 0.0}
        assert symbol not in pf.positions
        assert authoritative_position_ids(pf, symbol) == ()
        assert pf.cash == pytest.approx(expected_cash, abs=1e-9)
        assert pf.get_equity({symbol: 7.}) == pf.cash
        pf.update_position(symbol, direction * 2., 9., order_id="reentry")
        assert not set(old_ids).intersection(authoritative_position_ids(pf, symbol))

    @pytest.mark.parametrize("direction", [1, -1])
    def test_above_lot_epsilon_remainder_stays_open(self, direction):
        pf = Portfolio(initial_capital=10_000.0, account_mode="spot_margin")
        symbol = "UNI/USDT"
        pf.update_position(symbol, direction * 5e-12, 8., order_id="entry")
        old_ids = authoritative_position_ids(pf, symbol)
        closes = pf.update_position(symbol, -direction * 3e-12, 7., order_id="exit")
        assert closes[-1].fully_closed is False
        assert abs(pf.get_position(symbol)["qty"]) > 1e-12
        assert len(pf.open_lots(symbol)) == 1
        assert pf.open_lots(symbol)[0].qty_open == pytest.approx(2e-12, abs=1e-24)
        assert authoritative_position_ids(pf, symbol) == old_ids
