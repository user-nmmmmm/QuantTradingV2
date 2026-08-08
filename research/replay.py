"""Recorded order/fill replay utilities."""

from __future__ import annotations

from typing import Iterable

from core.events import EventEnvelope
from live_trading.execution_adapter import RecordedExecutionAdapter


def replay_execution_events(
    adapter: RecordedExecutionAdapter,
    events: Iterable[EventEnvelope],
    *,
    apply_fills: bool = True,
) -> tuple[EventEnvelope, ...]:
    """Replay recorded exchange facts without resubmitting any order."""

    return adapter.replay(events, apply_fills=apply_fills)

