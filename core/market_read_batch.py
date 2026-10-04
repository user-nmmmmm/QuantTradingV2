"""在统一截止时间内汇总只读请求；超时结果不会被后续批次接纳。"""
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
    """限制并发抓取数，同一标的的旧请求未结束时不再提交新请求。

    fetch 应只返回读取结果，运行时状态由调用方统一提交。线程无法强制中断
    已开始的网络请求，因此超时表示停止等待，不代表底层请求已经结束。
    """

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
        """按标的去重提交；排队、限流等待和请求共同消耗本批次时间预算。"""
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
                # 旧轮次即使后来成功也丢弃结果，防止过期响应覆盖新状态。
                self._jobs.pop(symbol, None)
                jobs[symbol] = self._pool.submit(self._read, fetch, symbol, deadline)
                self._jobs[symbol] = jobs[symbol]
            wait(list(jobs.values()), timeout=max(0., deadline - time.monotonic()))
            values = {}
            for symbol, job in jobs.items():
                if not job.done():
                    # cancel 只能取消尚未启动的任务；运行中的任务继续被 _jobs
                    # 跟踪，以便下一轮返回 fetch_in_progress 而非重复抓取。
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
