"""Scoped historical entry matching delay; production broker is unchanged.

Already accepted entries keep their reservations and strategy cancellation
rules while waiting. Exits and protective orders keep their original timing.
The existing engine and configuration are process-global, so this context,
like the research environment, must only be used by sequential episodes.
"""
from contextlib import contextmanager
import importlib

import pandas as pd
from core.broker import TimeInForce


@contextmanager
def opening_delay(bars):
    if type(bars) is not int or bars < 0:
        raise ValueError("opening delay must be a nonnegative integer")
    if not bars:
        yield
        return
    module = importlib.import_module("backtest.engine")
    original = module.Broker

    class DelayedBroker(original):
        def process_orders(self, current_bar, *, order_filter=None):
            def eligible(order):
                if order_filter is not None and not order_filter(order):
                    return False
                if order.side not in {"buy", "short"}:
                    return True
                bar = current_bar.get(order.symbol)
                if bar is None or order.timestamp is None:
                    return False
                submitted = pd.to_datetime(order.timestamp, utc=True)
                now = pd.to_datetime(bar.name, utc=True)
                # A waiting entry must retain the original working lifetime.
                # Let the normal matcher retire expired orders; filtering an
                # expired order out would incorrectly leave it in the book.
                original_ready = (now >= pd.to_datetime(order.match_not_before, utc=True)
                                  if order.match_not_before is not None else now > submitted)
                if not original_ready:
                    return False
                if order.expire_time is not None and now > pd.to_datetime(order.expire_time, utc=True):
                    return True
                if order.time_in_force == TimeInForce.DAY and order.submitted_date is not None and now.date() > order.submitted_date:
                    return True
                ttl = self.opening_order_ttl_bars
                next_age = order.age_bars + int(order.last_age_bar != bar.name)
                if ttl > 0 and next_age > ttl:
                    return True
                ready = submitted + pd.Timedelta(days=1 + bars)
                if now < ready:
                    self._expire_aged_opening_order(order, bar.name)
                    self._audit_order(order, bar.name, "waiting", "research_opening_delay",
                                      opening_delay_bars=bars, earliest_match_at=ready.isoformat())
                    return False
                return True
            return super().process_orders(current_bar, order_filter=eligible)

    module.Broker = DelayedBroker
    try:
        yield
    finally:
        module.Broker = original
