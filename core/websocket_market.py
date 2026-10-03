"""Public Binance closed-bar intake; reconnects and overflow require REST repair."""
import asyncio
from collections import OrderedDict
import json
import math
from threading import Event, Lock, Thread
import time

import pandas as pd

from core.timeframes import timeframe_delta


class ClosedBarBuffer:
    def __init__(self, symbols, timeframe="1m", capacity=1024):
        if type(capacity) is not int or capacity < 1:
            raise ValueError("buffer capacity must be positive")
        self.symbols = {symbol.replace("/", "").lower(): symbol for symbol in symbols}
        self.timeframe, self.capacity = timeframe, capacity
        self._step = timeframe_delta(timeframe)
        self._rows, self._latest, self._lock = OrderedDict(), {}, Lock()
        self.repair_required = set(symbols)
        self.messages = self.accepted = self.duplicates = self.overflows = 0
        self.received = Event()

    def ingest(self, message):
        data = message.get("data", message)
        kline = data.get("k", {})
        if data.get("e") != "kline" or kline.get("x") is not True:
            return False
        symbol = self.symbols.get(str(data.get("s", "")).lower())
        if symbol is None or kline.get("i") != self.timeframe:
            return False
        timestamp = pd.Timestamp(kline["t"], unit="ms", tz="UTC")
        values = {name: float(kline[key]) for name, key in
                  (("open", "o"), ("high", "h"), ("low", "l"), ("close", "c"), ("volume", "v"))}
        if not all(math.isfinite(value) for value in values.values()):
            raise ValueError("non-finite stream price")
        if min(values[name] for name in ("open", "high", "low", "close")) <= 0 or values["volume"] < 0:
            raise ValueError("invalid stream price or volume")
        if values["low"] > min(values["open"], values["close"]) or values["high"] < max(values["open"], values["close"]):
            raise ValueError("inconsistent OHLC stream")
        with self._lock:
            previous = self._latest.get(symbol)
            if previous is not None and timestamp <= previous:
                self.duplicates += 1
                return False
            if previous is not None and timestamp - previous > self._step:
                self.repair_required.add(symbol)
            self._latest[symbol] = timestamp
            self._rows[(symbol, timestamp)] = values
            if len(self._rows) > self.capacity:
                (dropped_symbol, _), _ = self._rows.popitem(last=False)
                self.repair_required.add(dropped_symbol)
                self.overflows += 1
            self.accepted += 1
            self.received.set()
        return True

    def reconnect(self):
        with self._lock:
            self.repair_required.update(self.symbols.values())

    def repair(self, symbol, latest_rest_timestamp):
        with self._lock:
            latest = self._latest.get(symbol)
            stamp = pd.Timestamp(latest_rest_timestamp)
            stamp = stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")
            if latest is None or stamp >= latest:
                self.repair_required.discard(symbol)

    def drain(self):
        with self._lock:
            rows, self._rows = self._rows, OrderedDict()
            self.received.clear()
            repair = set(self.repair_required)
        result = {}
        for (symbol, timestamp), values in rows.items():
            if symbol not in repair:
                result.setdefault(symbol, []).append({"timestamp": timestamp, **values})
        return {symbol: pd.DataFrame(values).set_index("timestamp") for symbol, values in result.items()}


class BinancePublicStream:
    def __init__(self, buffer):
        self.buffer = buffer
        self._stop, self._thread = Event(), None
        self.last_error, self.reconnects = None, 0

    async def _run(self):
        import aiohttp
        streams = "/".join(f"{symbol}@kline_{self.buffer.timeframe}" for symbol in self.buffer.symbols)
        url = "wss://stream.binance.com:9443/stream?streams=" + streams
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, connect=5)) as session:
            while not self._stop.is_set():
                try:
                    self.buffer.reconnect()
                    async with session.ws_connect(url, heartbeat=20, autoping=True) as ws:
                        while not self._stop.is_set():
                            try:
                                message = await asyncio.wait_for(ws.receive(), timeout=1.)
                            except asyncio.TimeoutError:
                                continue
                            if message.type == aiohttp.WSMsgType.TEXT:
                                self.buffer.messages += 1
                                self.buffer.ingest(json.loads(message.data))
                            elif message.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                                break
                    self.reconnects += 1
                except Exception as exc:
                    self.last_error = type(exc).__name__
                    self.reconnects += 1
                for _ in range(10):
                    if self._stop.is_set():
                        break
                    await asyncio.sleep(.1)

    def start(self):
        if self._thread is not None:
            raise RuntimeError("stream already started")
        self._thread = Thread(target=lambda: asyncio.run(self._run()), name="public-stream", daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=7.)
            if self._thread.is_alive():
                raise TimeoutError("public stream did not stop within budget")
