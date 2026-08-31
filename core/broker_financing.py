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
        return entries
