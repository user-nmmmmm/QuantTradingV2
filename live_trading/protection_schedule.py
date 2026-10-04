"""在引擎线程内按独立节奏检查保护任务，避免并发修改 broker 状态。"""
from dataclasses import dataclass
import math
import time


@dataclass(frozen=True)
class RuntimeSchedulePolicy:
    """显式启用限时行情读取、保护调度及状态补帧；默认保留原调度路径。"""

    enabled: bool = False
    market_timeout_seconds: float = 5.
    protection_interval_seconds: float = 5.
    catchup_max_bars: int = 100

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ValueError("runtime enabled must be a boolean")
        for value in (self.market_timeout_seconds, self.protection_interval_seconds):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError("runtime timing must be finite and positive")
        if type(self.catchup_max_bars) is not int or self.catchup_max_bars < 1:
            raise ValueError("catchup limit must be a positive integer")


class ProtectionSchedule:
    """由单一调用线程执行保护回调；不创建线程，也不抢占正在执行的任务。

    _running 仅防止同线程递归调用，不是线程锁。下次到期时间从回调结束时
    起算，即使回调抛错也会推进；异常由调用方处理。
    """
    def __init__(self, interval_seconds, *, clock=time.monotonic):
        self.interval, self.clock = interval_seconds, clock
        self._next = None
        self._running = False

    def run_if_due(self, protect):
        """到期且未重入时执行一次；返回值只表示是否调用，不代表保护成功。"""
        now = self.clock()
        if self._running or (self._next is not None and now < self._next):
            return False
        self._running = True
        try:
            protect()
        finally:
            self._next = self.clock() + self.interval
            self._running = False
        return True

    def seconds_until_due(self):
        return 0. if self._next is None else max(0., self._next - self.clock())
