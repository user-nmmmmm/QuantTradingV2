"""Bounded read-only batches; late results never touch runtime state."""
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
import math
from threading import Lock
import time

from core.request_budget import request_scope


@dataclass(frozen=True)
class ReadBatchResult:
    values: dict
    failures: dict
    elapsed_seconds: float


class BoundedMarketReads:
    def __init__(self, workers=4):
        if type(workers) is not int or not 1 <= workers <= 32:
            raise ValueError("workers must be between 1 and 32")
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="public-reads")
        self._jobs, self._lock = {}, Lock()
        self._closed = False

    @staticmethod
    def _read(fetch, symbol, deadline):
        with request_scope(deadline=deadline, priority="market"):
            return fetch(symbol)

    def read(self, symbols, fetch, *, timeout_seconds=5.):
        if isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("batch timeout must be finite and positive")
        with self._lock:
            if self._closed:
                raise RuntimeError("read batch closed")
            started = time.monotonic()
            deadline = started + timeout_seconds
            jobs, failures = {}, {}
            for symbol in dict.fromkeys(symbols):
                old = self._jobs.get(symbol)
                if old is not None and not old.done():
                    failures[symbol] = "fetch_in_progress"
                    continue
                # A timed-out round's completed value is never adopted later.
                self._jobs.pop(symbol, None)
                jobs[symbol] = self._pool.submit(self._read, fetch, symbol, deadline)
                self._jobs[symbol] = jobs[symbol]
            wait(list(jobs.values()), timeout=max(0., deadline - time.monotonic()))
            values = {}
            for symbol, job in jobs.items():
                if not job.done():
                    job.cancel()
                    failures[symbol] = "refresh_timeout"
                else:
                    self._jobs.pop(symbol, None)
                    try:
                        values[symbol] = job.result()
                    except Exception as exc:
                        failures[symbol] = type(exc).__name__
            return ReadBatchResult(values, failures, time.monotonic() - started)

    def close(self, *, wait=True):
        with self._lock:
            self._closed = True
            self._pool.shutdown(wait=wait, cancel_futures=True)
