"""Exchange-versus-host clock offset gate for exchange-connected runs.

A host clock that disagrees with the exchange makes the engine misjudge bar closes:
it reads a bar that has not closed yet, or misses the close window. Historical
backtests use static timestamps and cannot see this, so the offset is measured
against the exchange's own server time and treated as a health fact.

The offset is *bounded*, never estimated. The exchange stamps ``serverTime`` at
some moment between the local send and the local receive, so for one sample::

    server - received  <=  exchange_minus_local  <=  server - sent

Several samples are intersected. The check passes only when the whole interval
lies inside ``[-max_offset, +max_offset]``; anything else (too large, not
provable, not measurable, inconsistent) fails closed.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from core.foundation.clock import Clock, ClockLike, coerce_clock
from core.foundation.logger import get_logger
from ops.health import HealthReason

logger = get_logger(__name__)

SCHEMA = "exchange-clock-offset/v1"
STATUS_OK = "ok"
STATUS_EXCEEDED = "exceeded"          # the whole interval is outside the limit
STATUS_UNPROVEN = "unproven"          # the interval straddles the limit (slow round trips)
STATUS_UNMEASURED = "unmeasured"      # no usable sample
STATUS_INCONSISTENT = "inconsistent"  # samples contradict each other (a clock moved meanwhile)

# serverTime has millisecond resolution; allow that much slack before calling samples contradictory.
_RESOLUTION_SECONDS = 0.002


@dataclass(frozen=True)
class ClockOffsetPolicy:
    """Limits for the exchange clock check. The offset limit is a hard gate, not a tuning knob."""

    max_offset_seconds: float = 1.0
    startup_samples: int = 5
    runtime_samples: int = 3
    check_interval_seconds: float = 300.0
    retry_interval_seconds: float = 30.0
    step_tolerance_seconds: float = 0.25
    # No new request is started once one check has run this long, so an unreachable
    # time endpoint costs the tick loop about one request timeout, not one per sample.
    sample_budget_seconds: float = 5.0

    def __post_init__(self) -> None:
        for name in ("max_offset_seconds", "check_interval_seconds", "retry_interval_seconds",
                     "step_tolerance_seconds", "sample_budget_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        for name in ("startup_samples", "runtime_samples"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")


def _server_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value / 1000.0, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def measure_exchange_clock_offset(
    fetch_server_time_ms: Callable[[], Any],
    *,
    samples: int,
    max_offset_seconds: float,
    clock: Optional[ClockLike] = None,
    monotonic: Callable[[], float] = time.monotonic,
    step_tolerance_seconds: float = 0.25,
    sample_budget_seconds: float = 5.0,
) -> Dict[str, Any]:
    """Bound ``exchange - local`` from up to ``samples`` server-time reads; never raises.

    At least one read is made; further reads stop once ``sample_budget_seconds`` have passed.

    A sample is discarded when the fetch fails, the value is not a usable epoch in
    milliseconds, or the local wall clock and the monotonic clock disagree about how
    long the request took (the wall clock was stepped during the request). Only the
    exception type is recorded, so the report carries no request detail.
    """
    wall: Clock = coerce_clock(clock)
    lowers: List[float] = []
    uppers: List[float] = []
    round_trips: List[float] = []
    errors: List[str] = []
    attempted, begun = 0, monotonic()
    for _ in range(samples):
        if attempted and monotonic() - begun >= sample_budget_seconds:
            break
        attempted += 1
        sent, started = wall.now(), monotonic()
        try:
            raw = fetch_server_time_ms()
        except Exception as exc:  # fail closed: any failure is an unusable sample
            errors.append(type(exc).__name__)
            continue
        received, elapsed = wall.now(), monotonic() - started
        server = _server_datetime(raw)
        if server is None:
            errors.append("InvalidServerTime")
            continue
        if elapsed < 0 or abs((received - sent).total_seconds() - elapsed) > step_tolerance_seconds:
            errors.append("LocalClockStepped")
            continue
        lowers.append((server - received).total_seconds())
        uppers.append((server - sent).total_seconds())
        round_trips.append(elapsed)

    report: Dict[str, Any] = {
        "schema": SCHEMA,
        "convention": "exchange_minus_local_seconds",
        "measured_at": wall.now().astimezone(timezone.utc).isoformat(),
        "max_offset_seconds": float(max_offset_seconds),
        "samples_requested": int(samples),
        "samples_attempted": attempted,
        "samples_valid": len(lowers),
        "offset_lower_seconds": None,
        "offset_upper_seconds": None,
        "offset_abs_bound_seconds": None,
        "round_trip_seconds_min": min(round_trips) if round_trips else None,
        "errors": errors,
    }
    if not lowers:
        status = STATUS_UNMEASURED
    else:
        lower, upper = max(lowers), min(uppers)
        report["offset_lower_seconds"] = lower
        report["offset_upper_seconds"] = upper
        report["offset_abs_bound_seconds"] = max(abs(lower), abs(upper))
        if lower > upper + _RESOLUTION_SECONDS:
            status = STATUS_INCONSISTENT
        elif report["offset_abs_bound_seconds"] <= max_offset_seconds:
            status = STATUS_OK
        elif lower > max_offset_seconds or upper < -max_offset_seconds:
            status = STATUS_EXCEEDED
        else:
            status = STATUS_UNPROVEN
    report["status"] = status
    report["passed"] = status == STATUS_OK
    return report


def describe_clock_offset(report: Optional[Dict[str, Any]]) -> str:
    """One secret-free line for logs, startup evidence and health messages."""
    if not report:
        return "exchange clock offset has not been measured"
    lower, upper = report.get("offset_lower_seconds"), report.get("offset_upper_seconds")
    bounds = "n/a" if lower is None or upper is None else f"[{lower:+.3f}s, {upper:+.3f}s]"
    errors = ",".join(report.get("errors") or []) or "none"
    return (f"status={report.get('status')} exchange_minus_local={bounds} "
            f"limit={float(report.get('max_offset_seconds') or 0.0):.3f}s "
            f"samples={report.get('samples_valid')}/{report.get('samples_requested')} errors={errors}")


class ExchangeClockGuard:
    """Measures the exchange clock offset at startup and re-measures it while running.

    The guard never trades and never corrects a clock. It reports a health reason while
    the last measurement did not pass, which blocks new risk through the engine's health
    assessment; reductions, stops and protective-order reconciliation keep working.
    """

    def __init__(
        self,
        fetch_server_time_ms: Callable[[], Any],
        policy: Optional[ClockOffsetPolicy] = None,
        *,
        clock: Optional[ClockLike] = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fetch = fetch_server_time_ms
        self.policy = policy or ClockOffsetPolicy()
        self._clock: Clock = coerce_clock(clock)
        self._monotonic = monotonic
        self.last_report: Optional[Dict[str, Any]] = None
        self._next_due: Optional[float] = None

    def measure(self, *, startup: bool = False) -> Dict[str, Any]:
        """Measure now, remember the result and schedule the next check."""
        previous = self.last_report
        report = measure_exchange_clock_offset(
            self._fetch,
            samples=self.policy.startup_samples if startup else self.policy.runtime_samples,
            max_offset_seconds=self.policy.max_offset_seconds,
            clock=self._clock,
            monotonic=self._monotonic,
            step_tolerance_seconds=self.policy.step_tolerance_seconds,
            sample_budget_seconds=self.policy.sample_budget_seconds,
        )
        self.last_report = report
        delay = (self.policy.check_interval_seconds if report["passed"]
                 else self.policy.retry_interval_seconds)
        self._next_due = self._monotonic() + delay
        was_passing = previous is None or bool(previous.get("passed"))
        if not report["passed"]:
            log = logger.critical if was_passing else logger.warning
            log("Exchange clock check failed: %s", describe_clock_offset(report))
        elif not was_passing:
            logger.info("Exchange clock check recovered: %s", describe_clock_offset(report))
        return report

    def check_if_due(self) -> Optional[Dict[str, Any]]:
        """Re-measure when the schedule says so; return the new report or ``None``."""
        if self._next_due is not None and self._monotonic() < self._next_due:
            return None
        return self.measure()

    def health_reason(self) -> Optional[HealthReason]:
        """``None`` while the last measurement passed; otherwise the reason that blocks new risk."""
        report = self.last_report
        if report is not None and report.get("passed"):
            return None
        exceeded = report is not None and report.get("status") == STATUS_EXCEEDED
        return HealthReason(
            code="EXCHANGE_CLOCK_OFFSET" if exceeded else "EXCHANGE_CLOCK_UNVERIFIED",
            category="clock",
            subject="exchange",
            message=describe_clock_offset(report),
            limit_seconds=self.policy.max_offset_seconds,
        )
