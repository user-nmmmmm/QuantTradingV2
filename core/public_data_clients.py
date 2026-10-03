"""Thread-owned public CCXT clients with process-wide venue pacing.

Clients and requests.Session objects are never shared between fetch workers.
The CCXT throttle hook receives each REST endpoint's calculated weight, so
metadata requests participate in the same pacing as OHLCV requests. Multiple
processes or external applications still need deployment-level IP budgets.
"""
from __future__ import annotations

import math
import time
from threading import Lock, local
from typing import Any, Callable
from core.request_budget import install_exchange_budget


class VenueRequestPacer:
    def __init__(self, *, clock=time.monotonic, sleep=time.sleep):
        self._clock = clock
        self._sleep = sleep
        self._lock = Lock()
        self._last_start = None

    def wait(self, interval_seconds: float) -> None:
        if not math.isfinite(interval_seconds) or interval_seconds < 0:
            raise ValueError("request pacing interval must be finite and nonnegative")
        with self._lock:
            if self._last_start is not None:
                delay = self._last_start + interval_seconds - self._clock()
                if delay > 0:
                    self._sleep(delay)
            self._last_start = self._clock()


_PACERS: dict[str, VenueRequestPacer] = {}
_PACERS_LOCK = Lock()


def venue_pacer(exchange_id: str) -> VenueRequestPacer:
    with _PACERS_LOCK:
        return _PACERS.setdefault(exchange_id, VenueRequestPacer())


class PublicDataClientPool:
    def __init__(self):
        self._local = local()
        self._lock = Lock()
        self._clients: list[Any] = []
        self._closed = False

    def get(self, exchange_id: str, market_type: str, factory: Callable[[], Any]):
        if self._closed:
            raise RuntimeError("public data client pool is closed")
        clients = getattr(self._local, "clients", None)
        if clients is None:
            clients = self._local.clients = {}
        key = (exchange_id, market_type)
        if key not in clients:
            exchange = factory()
            pacer = venue_pacer(exchange_id)

            def throttle(cost=None):
                weight = 1.0 if cost is None else float(cost)
                pacer.wait(float(exchange.rateLimit) * weight / 1000.0)

            exchange.throttle = throttle
            install_exchange_budget(exchange, exchange_id, priority="market")
            with self._lock:
                if self._closed:
                    exchange.session.close()
                    raise RuntimeError("public data client pool is closed")
                self._clients.append(exchange)
            clients[key] = exchange
        return clients[key]

    def close(self) -> None:
        with self._lock:
            self._closed = True
            clients, self._clients = self._clients, []
        for exchange in clients:
            session = getattr(exchange, "session", None)
            close = getattr(session, "close", None)
            if callable(close):
                close()
