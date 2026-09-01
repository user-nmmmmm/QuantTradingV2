"""Perpetual funding and margin-short borrow cost accrual.

Split out of core/broker.py (A4) — see docs/architecture_review.md. See
core/broker_matching.py's module docstring for why this is a mixin rather
than a standalone collaborator object.
"""
from __future__ import annotations

from typing import Any, Dict, List

import pandas as pd

from core.accounts import AccountMode


class FinancingMixin:
    """Historical funding/borrow accrual against open positions.

    Expects ``self`` to carry ``portfolio``, ``_last_funding_bucket``,
    ``_last_borrow_time``, ``funding_interval_hours``,
    ``funding_rate_required``, ``default_borrow_rate_annual``.

    SR3-4 (STR-P1-05): a spot-margin account borrows the **quote** currency to
    hold longs beyond its own equity, and that borrow costs interest. Only the
    coin borrow of a short leg used to be accrued, so a run whose gross/equity
    peaked at 1.456 reported an empty financing ledger on the long side. The
    quote leg is now accrued at the account level by
    :meth:`_accrue_quote_borrow`.
    """

    def accrue_carry(self, current_bar: Dict[str, pd.Series]) -> List[Dict[str, Any]]:
        """Accrue historical perpetual funding and margin-short borrow costs."""
        entries = []
        seconds_per_year = 365.0 * 24.0 * 3600.0
        for symbol, position in list(self.portfolio.positions.items()):
            bar = current_bar.get(symbol)
            if bar is None or position["qty"] == 0:
                continue
            timestamp = pd.Timestamp(bar.name)
            mark_price = float(bar.get("mark_price", bar.get("close", position["avg_price"])))
            notional = abs(position["qty"]) * mark_price
            if self.portfolio.account_mode is AccountMode.PERPETUAL:
                bucket_seconds = self.funding_interval_hours * 3600.0
                bucket = int(timestamp.timestamp() // bucket_seconds)
                if self._last_funding_bucket.get(symbol) == bucket:
                    continue
                rate_raw = bar.get("funding_rate")
                if rate_raw is None or pd.isna(rate_raw):
                    if self.funding_rate_required:
                        raise ValueError(
                            f"missing funding_rate for perpetual position {symbol} at {timestamp}"
                        )
                    rate = 0.0
                    source = "configured_zero_fallback"
                else:
                    rate = float(rate_raw)
                    source = "historical_bar"
                # Positive funding: longs pay, shorts receive.
                amount = position["qty"] * mark_price * rate
                entry = self.portfolio.apply_financing(
                    timestamp=timestamp,
                    symbol=symbol,
                    kind="funding",
                    rate=rate,
                    notional=notional,
                    amount=amount,
                    source=source,
                )
                self._last_funding_bucket[symbol] = bucket
                entries.append(entry.to_dict())
            elif (
                self.portfolio.account_mode is AccountMode.SPOT_MARGIN
                and position["qty"] < 0
            ):
                # Coin borrow for the short leg (the quote borrow that funds
                # leveraged longs is accrued once per bar, below).
                previous = self._last_borrow_time.get(symbol)
                self._last_borrow_time[symbol] = timestamp
                if previous is None or timestamp <= previous:
                    continue
                elapsed_seconds = (timestamp - previous).total_seconds()
                rate_raw = bar.get("borrow_rate_annual")
                if rate_raw is None or pd.isna(rate_raw):
                    rate = self.default_borrow_rate_annual
                    source = "configured_default"
                else:
                    rate = float(rate_raw)
                    source = "historical_bar"
                amount = notional * rate * elapsed_seconds / seconds_per_year
                entry = self.portfolio.apply_financing(
                    timestamp=timestamp,
                    symbol=symbol,
                    kind="borrow",
                    rate=rate,
                    notional=notional,
                    amount=amount,
                    source=source,
                )
                entries.append(entry.to_dict())
        quote_entry = self._accrue_quote_borrow(current_bar)
        if quote_entry is not None:
            entries.append(quote_entry)
        return entries

    #: Ledger symbol for account-level quote-currency borrow.
    QUOTE_BORROW_SYMBOL = "__QUOTE__"

    def _accrue_quote_borrow(self, current_bar: Dict[str, pd.Series]):
        """Accrue interest on the quote currency borrowed to fund longs.

        ``borrowed_quote = max(0, long_notional - equity)``: own funds pay for
        the first ``equity`` of long exposure, everything above it is margin
        debt. Gross/equity <= 1 therefore accrues nothing, and the accrual
        scales with how leveraged the book actually was, bar by bar.
        """
        if self.portfolio.account_mode is not AccountMode.SPOT_MARGIN:
            return None
        prices: Dict[str, float] = {}
        timestamp = None
        rate_override = None
        for symbol, bar in current_bar.items():
            if bar is None:
                continue
            price = bar.get("mark_price", bar.get("close"))
            if price is None or pd.isna(price):
                continue
            prices[symbol] = float(price)
            bar_time = pd.Timestamp(bar.name)
            timestamp = bar_time if timestamp is None else max(timestamp, bar_time)
            candidate = bar.get("quote_borrow_rate_annual", bar.get("borrow_rate_annual"))
            if rate_override is None and candidate is not None and not pd.isna(candidate):
                rate_override = float(candidate)
        if timestamp is None:
            return None
        previous = self._last_borrow_time.get(self.QUOTE_BORROW_SYMBOL)
        self._last_borrow_time[self.QUOTE_BORROW_SYMBOL] = timestamp
        if previous is None or timestamp <= previous:
            return None
        long_notional = sum(
            position["qty"] * prices.get(symbol, position["avg_price"])
            for symbol, position in self.portfolio.positions.items()
            if position["qty"] > 0
        )
        if long_notional <= 0:
            return None
        equity = self.portfolio.get_equity(prices)
        borrowed = max(0.0, long_notional - max(equity, 0.0))
        if borrowed <= 0:
            return None
        rate = (
            self.default_borrow_rate_annual if rate_override is None else rate_override
        )
        source = "configured_default" if rate_override is None else "historical_bar"
        elapsed_seconds = (timestamp - previous).total_seconds()
        amount = borrowed * rate * elapsed_seconds / (365.0 * 24.0 * 3600.0)
        if amount == 0:
            return None
        entry = self.portfolio.apply_financing(
            timestamp=timestamp,
            symbol=self.QUOTE_BORROW_SYMBOL,
            kind="quote_borrow",
            rate=rate,
            notional=borrowed,
            amount=amount,
            source=source,
        )
        return entry.to_dict()
