"""The public stream buffer accepts only sane closed bars and never serves a symbol that needs REST repair (C5)."""
from __future__ import annotations

import asyncio

import pandas as pd
import pytest

from core.market.websocket_market import BinancePublicStream, ClosedBarBuffer

MINUTE = 60_000


def kline(symbol="BTCUSDT", start=0, *, closed=True, interval="1m", o="100", h="101", low="99", c="100.5", v="3"):
    return {"data": {"e": "kline", "s": symbol, "k": {"t": start * MINUTE, "i": interval, "x": closed,
                                                       "o": o, "h": h, "l": low, "c": c, "v": v}}}


def test_capacity_must_be_a_positive_integer():
    for bad in (0, -1, 1.5, True):
        with pytest.raises(ValueError, match="capacity"):
            ClosedBarBuffer(["BTC/USDT"], capacity=bad)


@pytest.mark.parametrize("message", [
    {"data": {"e": "trade", "s": "BTCUSDT"}},                         # not a kline
    kline(closed=False),                                              # bar still open
    kline(symbol="ETHUSDT"),                                          # not subscribed
    kline(interval="5m"),                                             # wrong timeframe
])
def test_unrelated_or_unfinished_messages_are_ignored(message):
    buffer = ClosedBarBuffer(["BTC/USDT"])
    assert buffer.ingest(message) is False and buffer.accepted == 0 and not buffer.received.is_set()


@pytest.mark.parametrize("fields, message", [
    ({"c": "nan"}, "non-finite"),
    ({"h": "inf"}, "non-finite"),
    ({"o": "0"}, "invalid stream price"),
    ({"low": "-1"}, "invalid stream price"),
    ({"v": "-0.1"}, "invalid stream price or volume"),
    ({"low": "100.9"}, "inconsistent OHLC"),                          # low above close
    ({"h": "100.2"}, "inconsistent OHLC"),                            # high below close
])
def test_corrupt_bars_raise_instead_of_entering_the_buffer(fields, message):
    buffer = ClosedBarBuffer(["BTC/USDT"])
    with pytest.raises(ValueError, match=message):
        buffer.ingest(kline(**fields))
    assert buffer.accepted == 0


def test_duplicates_gaps_and_overflow_mark_the_symbol_for_repair_and_drain_withholds_it():
    buffer = ClosedBarBuffer(["BTC/USDT", "ETH/USDT"], capacity=3)
    assert buffer.repair_required == {"BTC/USDT", "ETH/USDT"}          # a fresh buffer has seen no history
    buffer.repair("BTC/USDT", pd.Timestamp(0, unit="ms"))
    buffer.repair("ETH/USDT", pd.Timestamp(0, unit="ms", tz="UTC"))
    assert buffer.repair_required == set()

    assert buffer.ingest(kline(start=0)) and buffer.ingest(kline(start=1))
    assert buffer.ingest(kline(start=1)) is False and buffer.duplicates == 1       # replayed bar
    assert buffer.ingest(kline(start=5)) and buffer.repair_required == {"BTC/USDT"}  # minutes 2-4 are missing
    assert buffer.ingest(kline("ETHUSDT", start=0)) and buffer.overflows == 1        # the oldest row is dropped
    assert buffer.repair_required == {"BTC/USDT"}

    drained = buffer.drain()
    assert set(drained) == {"ETH/USDT"} and list(drained["ETH/USDT"].columns) == ["open", "high", "low", "close", "volume"]
    assert buffer.drain() == {} and not buffer.received.is_set()

    buffer.repair("BTC/USDT", pd.Timestamp(5 * MINUTE, unit="ms"))                 # REST caught up to the stream
    assert buffer.repair_required == set()
    buffer.reconnect()
    assert buffer.repair_required == {"BTC/USDT", "ETH/USDT"}                      # any reconnect forces a repair


def test_repair_that_is_still_behind_the_stream_keeps_the_symbol_blocked():
    buffer = ClosedBarBuffer(["BTC/USDT"])
    buffer.ingest(kline(start=10))
    buffer.repair("BTC/USDT", pd.Timestamp(5 * MINUTE, unit="ms"))
    assert buffer.repair_required == {"BTC/USDT"}


def test_stream_start_is_single_use_and_close_waits_for_the_worker(monkeypatch):
    async def idle(self):
        while not self._stop.is_set():
            await asyncio.sleep(0.01)
    monkeypatch.setattr(BinancePublicStream, "_run", idle)
    stream = BinancePublicStream(ClosedBarBuffer(["BTC/USDT"]))
    stream.close()                                                   # closing before start is harmless
    stream._stop.clear()
    stream.start()
    with pytest.raises(RuntimeError, match="already started"):
        stream.start()
    stream.close()
    assert not stream._thread.is_alive()


def test_close_reports_a_worker_that_will_not_stop(monkeypatch):
    class Stuck:
        def join(self, timeout=None):
            pass

        def is_alive(self):
            return True
    stream = BinancePublicStream(ClosedBarBuffer(["BTC/USDT"]))
    stream._thread = Stuck()
    with pytest.raises(TimeoutError):
        stream.close()
