"""Single-owner schedule: independent deadlines without parallel broker calls."""
from dataclasses import dataclass
import math
import time


@dataclass(frozen=True)
class RuntimeSchedulePolicy:
    # Opt-in required before replacing any production scheduling behavior.
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
    """Caller owns all callbacks and state; public read workers own no broker."""
    def __init__(self, interval_seconds, *, clock=time.monotonic):
        self.interval, self.clock = interval_seconds, clock
        self._next = None
        self._running = False

    def run_if_due(self, protect):
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
