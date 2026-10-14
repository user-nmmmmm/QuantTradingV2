"""B6 (ADR 0028): the order books are owned by ``Matching`` and the risk-action targets by ``Liquidation``.

The golden rows were recorded on the code BEFORE the state moved: after every step of the scripted runs the pending
and working order queues and the risk-action targets must be identical, and every old attribute on ``Broker`` must
still read and write the same value.
"""
from __future__ import annotations

import pandas as pd
import pytest

from core.broker import Broker
from core.trading.portfolio import Portfolio

DAY = pd.Timestamp("2024-01-01")


def _bar(day: int, price: float = 100.0, volume: float = 1_000_000.0) -> pd.Series:
    return pd.Series(dict(open=price, high=price, low=price, close=price, volume=volume), name=DAY + pd.Timedelta(days=day))


def _broker() -> Broker:
    return Broker(Portfolio(1_000_000.0), commission_rate=0.0, commission_rate_maker=0.0, slippage=0.0)


def _queue(orders):
    return [(o.symbol, o.side, getattr(o.order_type, "value", o.order_type), o.exit_reason, o.remaining_qty) for o in orders]


def _snap(broker: Broker, label: str, extra=None):
    row = {"step": label, "pending": _queue(broker.pending_orders), "active": _queue(broker.active_orders)}
    if extra is not None:
        row["extra"] = extra
    return row


def _order_book_run():
    broker = _broker()
    rows = [_snap(broker, "empty")]
    broker.submit_order("A", "buy", 1, 50, order_type="limit", timestamp=_bar(0).name)
    broker.submit_order("B", "buy", 1, 50, order_type="limit", timestamp=_bar(0).name)
    broker.submit_order("A", "buy", 1, 100, timestamp=_bar(0).name)
    rows.append(_snap(broker, "submitted"))
    broker.process_orders({"A": _bar(1), "B": _bar(1)})
    rows.append(_snap(broker, "after the first bar"))
    broker.submit_order("A", "sell", 1, 95, order_type="stop", timestamp=_bar(1).name, exit_reason="protective_stop")
    rows.append(_snap(broker, "protective stop submitted"))
    rows.append(_snap(broker, "protective stops cancelled", broker.cancel_protective_stops(["A"])))
    rows.append(_snap(broker, "B opening cancelled", broker.cancel_opening_orders(["B"], timestamp=_bar(1).name)))
    broker.submit_order("C", "buy", 1, 50, order_type="limit", timestamp=_bar(1).name)
    broker.process_orders({"A": _bar(2), "C": _bar(2)})
    rows.append(_snap(broker, "C resting"))
    rows.append(_snap(broker, "C cancelled", broker.cancel_symbol_orders("C")))
    return rows


def _risk_action_run():
    broker = _broker()
    for symbol in ("A", "B"):
        broker.portfolio.update_position(symbol, 10, 100, strategy_id="Probe", order_id=f"entry-{symbol}", stop_price=50)
    rows = []
    for step in range(2):
        broker.force_liquidate(
            {"A": _bar(step + 1), "B": _bar(step + 1)}, timestamp=_bar(step + 1).name, reason="DrawdownReduce",
            risk_action_id="act-1", remaining_fraction=0.5,
        )
        targets = {s: (t["target_qty"], t["status"]) for s, t in broker.risk_action_targets["act-1"].items()}
        rows.append({"call": step, "targets": targets, "qty": {s: broker.portfolio.get_position(s)["qty"] for s in ("A", "B")},
                     "queues": [len(broker.pending_orders), len(broker.active_orders)]})
    return rows


GOLDEN_ORDER_BOOK = [{'active': [], 'pending': [], 'step': 'empty'},
 {'active': [],
  'pending': [('A', 'buy', 'limit', 'signal', 1), ('B', 'buy', 'limit', 'signal', 1), ('A', 'buy', 'market', 'signal', 1)],
  'step': 'submitted'},
 {'active': [('A', 'buy', 'limit', 'signal', 1), ('B', 'buy', 'limit', 'signal', 1)],
  'pending': [],
  'step': 'after the first bar'},
 {'active': [('A', 'buy', 'limit', 'signal', 1), ('B', 'buy', 'limit', 'signal', 1)],
  'pending': [('A', 'sell', 'stop', 'protective_stop', 1)],
  'step': 'protective stop submitted'},
 {'active': [('A', 'buy', 'limit', 'signal', 1), ('B', 'buy', 'limit', 'signal', 1)],
  'extra': 1,
  'pending': [],
  'step': 'protective stops cancelled'},
 {'active': [('A', 'buy', 'limit', 'signal', 1)], 'extra': 1, 'pending': [], 'step': 'B opening cancelled'},
 {'active': [('A', 'buy', 'limit', 'signal', 1), ('C', 'buy', 'limit', 'signal', 1)], 'pending': [], 'step': 'C resting'},
 {'active': [('A', 'buy', 'limit', 'signal', 1)], 'extra': 1, 'pending': [], 'step': 'C cancelled'}]
GOLDEN_RISK_ACTION = [{'call': 0, 'qty': {'A': 5.0, 'B': 5.0}, 'queues': [0, 0], 'targets': {'A': (5.0, 'pending'), 'B': (5.0, 'pending')}},
 {'call': 1, 'qty': {'A': 5.0, 'B': 5.0}, 'queues': [0, 0], 'targets': {'A': (5.0, 'complete'), 'B': (5.0, 'complete')}}]


def test_order_book_golden_run():
    assert _order_book_run() == GOLDEN_ORDER_BOOK


def test_risk_action_golden_run():
    assert _risk_action_run() == GOLDEN_RISK_ACTION


def test_the_old_names_read_and_write_through_the_broker():
    broker = _broker()
    assert broker.pending_orders == [] and broker.active_orders == []
    assert broker.pending_orders is broker.pending_orders
    assert not getattr(broker, "risk_action_targets", None)
    order = broker.submit_order("A", "buy", 1, 50, order_type="limit", timestamp=_bar(0).name)
    assert broker.pending_orders == [order]
    replacement = [order]
    broker.active_orders = replacement
    broker.pending_orders = []
    assert broker.active_orders is replacement and broker.pending_orders == []
    targets = {"x": {"A": {"target_qty": 1.0, "status": "pending"}}}
    broker.risk_action_targets = targets
    assert broker.risk_action_targets is targets


if __name__ == "__main__":
    import pprint
    pprint.pprint(_order_book_run(), width=140)
    pprint.pprint(_risk_action_run(), width=140)
