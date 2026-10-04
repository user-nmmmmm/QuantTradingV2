"""用共享 SQLite 预算协调 REST 请求间隔，并为 critical 请求保留容量。"""
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
    """传播单调时钟截止时间与优先级；嵌套调用只能收紧已有截止时间。"""
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
    """同一路径的 SQLite 数据库按 venue 串行批准跨进程请求。

    cost 沿用 CCXT 端点权重，基础间隔取该 venue 已见过的最严格值。
    market/research 不能消耗 critical 保留容量；critical 仍受总额与间隔限制。
    只有接入此预算且共享路径的进程参与协调，外部客户端不会自动计入。
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
                    # critical 等待时短暂阻止普通读取抢先；短租约避免请求退出
                    # 后留下长期阻塞，后续重试会按需续租。
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
    """接管兼容 SDK 的限流与请求超时；缺少对应接口的测试替身保持原样。

    每次请求将 SDK timeout 限制在剩余预算内，结束后恢复原值。调用方需保证
    同一客户端不会被多个请求线程并发使用，因为 timeout 是客户端可变属性。
    """
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
