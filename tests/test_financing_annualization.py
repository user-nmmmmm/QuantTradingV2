"""C2: the borrow-interest annualization convention is pinned: a 365-day year over the *actual elapsed seconds*.

``Financing.accrue_carry`` once held an unused ``seconds_per_year`` local; the review found the conversion is applied
in ``_accrue_short_borrow`` and ``_accrue_quote_borrow``, not missing. These tests fix the numbers so that a change
of year length, or of "elapsed seconds" into "whole days", fails loudly.
"""
from __future__ import annotations

import pandas as pd
import pytest

from core.broker import Broker
from core.trading.portfolio import Portfolio

SECONDS_PER_YEAR = 365 * 24 * 3600
THREE_HOURS = 3 * 3600


def _bar(timestamp: str, close: float) -> pd.Series:
    bar = pd.Series({"open": close, "high": close, "low": close, "close": close, "volume": 1e9})
    bar.name = pd.Timestamp(timestamp)
    return bar


def _margin_broker(rate: float) -> Broker:
    portfolio = Portfolio(100_000.0, account_mode="spot_margin", initial_margin_rate=0.3333333333)
    return Broker(portfolio, commission_rate=0.0, default_borrow_rate_annual=rate)


def test_short_borrow_accrues_over_the_actual_elapsed_seconds_of_a_365_day_year():
    broker = _margin_broker(0.365)
    broker.portfolio.update_position("BTC/USDT", -1.0, 100.0)
    broker.accrue_carry({"BTC/USDT": _bar("2024-01-02T00:00:00Z", 100.0)})                 # starts the clock
    entries = broker.accrue_carry({"BTC/USDT": _bar("2024-01-02T03:00:00Z", 100.0)})
    borrow = [e for e in entries if e["kind"] == "borrow"]
    assert len(borrow) == 1
    assert borrow[0]["amount"] == pytest.approx(100.0 * 0.365 * THREE_HOURS / SECONDS_PER_YEAR, rel=1e-12)
    assert borrow[0]["rate"] == pytest.approx(0.365)


def test_quote_borrow_accrues_over_the_actual_elapsed_seconds_of_a_365_day_year():
    broker = _margin_broker(0.08)
    broker.portfolio.update_position("BTC/USDT", qty_delta=3.0, price=50_000.0,
                                     time=pd.Timestamp("2021-05-18T00:00:00Z"), strategy_id="s", order_id="O1")
    broker.accrue_carry({"BTC/USDT": _bar("2021-05-19T00:00:00Z", 50_000.0)})              # starts the clock
    entries = broker.accrue_carry({"BTC/USDT": _bar("2021-05-19T03:00:00Z", 50_000.0)})
    quote = [e for e in entries if e["kind"] == "quote_borrow"]
    assert len(quote) == 1
    assert quote[0]["notional"] == pytest.approx(50_000.0)       # 150k of longs against 100k equity
    assert quote[0]["amount"] == pytest.approx(50_000.0 * 0.08 * THREE_HOURS / SECONDS_PER_YEAR, rel=1e-12)
