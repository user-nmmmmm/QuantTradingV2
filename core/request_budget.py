"""Weighted REST pacing shared by managed processes, with critical capacity."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import math
import os
from pathlib import Path
import sqlite3
import time


_DEADLINE = ContextVar("exchange_request_deadline", default=None)
_PRIORITY = ContextVar("exchange_request_priority", default=None)


class RequestBudgetExpired(TimeoutError):
    pass


@contextmanager
def request_scope(*, deadline=None, priority=None):
    inherited = _DEADLINE.get()
    if inherited is not None:
        deadline = inherited if deadline is None else min(inherited, deadline)
    deadline_token = _DEADLINE.set(deadline)
    priority_token = _PRIORITY.set(priority or _PRIORITY.get())
    try:
        yield
    finally:
        _PRIORITY.reset(priority_token)
        _DEADLINE.reset(deadline_token)


def remaining_seconds():
    deadline = _DEADLINE.get()
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RequestBudgetExpired("exchange request deadline expired")
    return remaining


class SharedRequestBudget:
    """SQLite serializes admission across processes using one deployment path.

    SDK cost units are paced at the strictest observed CCXT base interval.
    Public/research calls cannot consume the critical reserve. Critical calls
    retain the same total limit; they receive priority over waiting reads.
    """

    def __init__(self, path, *, window_seconds=60., reserve_fraction=.10,
                 clock=time.time, sleep=time.sleep):
        if not math.isfinite(window_seconds) or window_seconds <= 0:
            raise ValueError("budget window must be finite and positive")
        if not 0 < reserve_fraction < 1:
            raise ValueError("critical reserve must lie between zero and one")
        self.path = str(Path(path).resolve())
        self.window = float(window_seconds)
        self.reserve = float(reserve_fraction)
        self.clock, self.sleep = clock, sleep
        self._ready = False

    def _connect(self):
        if not self._ready:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=.05)
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS venue_budget ("
                         "venue TEXT PRIMARY KEY, interval REAL NOT NULL, "
                         "window_start REAL NOT NULL, used REAL NOT NULL, "
                         "last_start REAL NOT NULL, critical_until REAL NOT NULL)")
            self._ready = True
            return conn
        except Exception:
            conn.close()
            raise

    def wait(self, venue, *, interval_seconds, cost=1., priority="market"):
        if not (math.isfinite(interval_seconds) and interval_seconds > 0
                and math.isfinite(cost) and cost > 0):
            raise ValueError("request interval and cost must be finite and positive")
        if priority not in {"market", "research", "critical"}:
            raise ValueError("unknown request priority")
        while True:
            remaining = remaining_seconds()
            conn = None
            try:
                conn = self._connect()
                conn.execute("BEGIN IMMEDIATE")
                now = self.clock()
                row = conn.execute("SELECT interval,window_start,used,last_start,critical_until "
                                   "FROM venue_budget WHERE venue=?", (venue,)).fetchone()
                base, start, used, last, critical_until = row or (
                    interval_seconds, now, 0., now - interval_seconds * cost, 0.)
                base = max(base, interval_seconds)
                capacity = self.window / base
                allowance = capacity if priority == "critical" else capacity * (1. - self.reserve)
                if cost > allowance:
                    raise ValueError("request cost exceeds deployment budget window")
                if now < start or now - start >= self.window:
                    start, used = now, 0.
                    last, critical_until = min(last, now), 0.
                delay = max(0., last + base * cost - now)
                if used + cost > allowance:
                    delay = max(delay, start + self.window - now)
                if priority != "critical":
                    delay = max(delay, critical_until - now)
                elif delay > 0:
                    # A short lease blocks reads while critical admission waits.
                    critical_until = now + min(max(delay, .02), .25)
                if delay <= 0:
                    used, last = used + cost, now
                    if priority == "critical":
                        critical_until = 0.
                conn.execute("INSERT INTO venue_budget VALUES(?,?,?,?,?,?) "
                             "ON CONFLICT(venue) DO UPDATE SET interval=excluded.interval,"
                             "window_start=excluded.window_start,used=excluded.used,"
                             "last_start=excluded.last_start,critical_until=excluded.critical_until",
                             (venue, base, start, used, last, critical_until))
                conn.commit()
                if delay <= 0:
                    return now
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    raise
                delay = .01
            finally:
                if conn is not None:
                    conn.close()
            delay = min(max(delay, .001), .05)
            if remaining is not None:
                delay = min(delay, remaining_seconds())
            self.sleep(delay)


def deployment_budget():
    root = Path(__file__).resolve().parents[1]
    return SharedRequestBudget(os.getenv("QUANT_REQUEST_BUDGET_DB") or
                               root / "reports" / "request_budget.sqlite3")


def install_exchange_budget(exchange, venue, *, priority="market", budget=None):
    """Hook real SDK clients; minimal test doubles retain their existing API."""
    if not isinstance(getattr(exchange, "rateLimit", None), (int, float)):
        return
    if not callable(getattr(exchange, "fetch", None)):
        return
    if getattr(exchange, "_quant_budget_installed", False) is True:
        return
    budget = budget or deployment_budget()
    fetch = exchange.fetch

    def throttle(cost=None):
        budget.wait(venue, interval_seconds=float(exchange.rateLimit) / 1000.,
                    cost=1. if cost is None else float(cost),
                    priority=_PRIORITY.get() or priority)

    def bounded_fetch(*args, **kwargs):
        remaining = remaining_seconds()
        previous = exchange.timeout
        if remaining is not None:
            exchange.timeout = min(previous, max(1, int(remaining * 1000)))
        try:
            return fetch(*args, **kwargs)
        finally:
            exchange.timeout = previous

    exchange.throttle, exchange.fetch = throttle, bounded_fetch
    exchange._quant_budget_installed = True
