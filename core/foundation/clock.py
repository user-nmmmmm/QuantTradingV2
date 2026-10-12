"""Explicit time source used by live scheduling and health decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Protocol, Union


class Clock(Protocol):
    def now(self) -> datetime: ...


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass(frozen=True)
class CallableClock:
    function: Callable[[], datetime]

    def now(self) -> datetime:
        return as_utc(self.function())


ClockLike = Union[Clock, Callable[[], datetime]]


def coerce_clock(value: ClockLike | None) -> Clock:
    if value is None:
        return SystemClock()
    if callable(getattr(value, "now", None)):
        return value  # type: ignore[return-value]
    if callable(value):
        return CallableClock(value)
    raise TypeError("clock must expose now() or be a zero-argument callable")
