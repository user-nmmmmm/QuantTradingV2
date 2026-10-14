"""B6 (ADR 0031): the tick loop's own bookkeeping is owned by ``TickOrchestrator``; the engine keeps the names.

The golden rows were recorded on the code BEFORE the state moved: after every tick of the scripted runs the eight
bookkeeping attributes must be identical, and every old attribute on ``LiveTradingEngine``  must still read and write the same value.
"""
from __future__ import annotations

import tempfile
from unittest.mock import MagicMock

from tests.test_p1_remaining import TestRemainingP1LiveSafety

NAMES = ("_tick_count", "_consecutive_tick_crashes", "_next_retry_delay", "_consecutive_strategy_failures",
         "_last_strategy_error", "_last_tick_seconds", "_alerted_health_transitions", "_live_fill_risk_audit")


def _snap(engine, step):
    # ``_alerted_health_transitions`` used to be created on the first health-transition alert and is an empty set from
    # construction since ADR 0031; nothing outside the orchestrator reads it, so "absent" and "empty" are the same row.
    row = {name: getattr(engine, name, set() if name == "_alerted_health_transitions" else "<absent>") for name in NAMES}
    row["_last_tick_seconds"] = row["_last_tick_seconds"] is not None
    row.update(step=step, healthy=engine._healthy, state=engine._operational_state)
    return row


def _engine(directory):
    case = TestRemainingP1LiveSafety("test_strategy_failures_degrade_then_halt_and_are_exported")
    return case._strategy_failure_engine(directory)


def _failure_run():
    with tempfile.TemporaryDirectory() as directory:
        engine, store = _engine(directory)
        try:
            rows = [_snap(engine, "fresh")]
            for index in range(2):
                engine._tick()
                rows.append(_snap(engine, f"strategy failure {index + 1}"))
            return rows
        finally:
            store.close()


def _crash_run():
    with tempfile.TemporaryDirectory() as directory:
        engine, store = _engine(directory)
        try:
            engine.failure_backoff_base_seconds, engine.failure_backoff_max_seconds = 2, 3
            engine._tick_once = MagicMock(side_effect=[RuntimeError("first"), RuntimeError("second"), None])
            rows = []
            for index in range(3):
                engine._tick()
                rows.append(_snap(engine, f"tick {index + 1}"))
            return rows
        finally:
            store.close()


GOLDEN_FAILURE = [{'_alerted_health_transitions': set(),
  '_consecutive_strategy_failures': 0,
  '_consecutive_tick_crashes': 0,
  '_last_strategy_error': None,
  '_last_tick_seconds': False,
  '_live_fill_risk_audit': [],
  '_next_retry_delay': 0.0,
  '_tick_count': 0,
  'healthy': False,
  'state': 'STARTING',
  'step': 'fresh'},
 {'_alerted_health_transitions': set(),
  '_consecutive_strategy_failures': 1,
  '_consecutive_tick_crashes': 0,
  '_last_strategy_error': 'RuntimeError',
  '_last_tick_seconds': True,
  '_live_fill_risk_audit': [],
  '_next_retry_delay': 0.0,
  '_tick_count': 1,
  'healthy': False,
  'state': 'DEGRADED',
  'step': 'strategy failure 1'},
 {'_alerted_health_transitions': set(),
  '_consecutive_strategy_failures': 2,
  '_consecutive_tick_crashes': 0,
  '_last_strategy_error': 'RuntimeError',
  '_last_tick_seconds': True,
  '_live_fill_risk_audit': [],
  '_next_retry_delay': 0.0,
  '_tick_count': 2,
  'healthy': False,
  'state': 'HALTED',
  'step': 'strategy failure 2'}]
GOLDEN_CRASH = [{'_alerted_health_transitions': set(),
  '_consecutive_strategy_failures': 0,
  '_consecutive_tick_crashes': 1,
  '_last_strategy_error': None,
  '_last_tick_seconds': True,
  '_live_fill_risk_audit': [],
  '_next_retry_delay': 2,
  '_tick_count': 0,
  'healthy': False,
  'state': 'HALTED',
  'step': 'tick 1'},
 {'_alerted_health_transitions': set(),
  '_consecutive_strategy_failures': 0,
  '_consecutive_tick_crashes': 2,
  '_last_strategy_error': None,
  '_last_tick_seconds': True,
  '_live_fill_risk_audit': [],
  '_next_retry_delay': 3,
  '_tick_count': 0,
  'healthy': False,
  'state': 'HALTED',
  'step': 'tick 2'},
 {'_alerted_health_transitions': set(),
  '_consecutive_strategy_failures': 0,
  '_consecutive_tick_crashes': 0,
  '_last_strategy_error': None,
  '_last_tick_seconds': True,
  '_live_fill_risk_audit': [],
  '_next_retry_delay': 0.0,
  '_tick_count': 0,
  'healthy': False,
  'state': 'HALTED',
  'step': 'tick 3'}]


def test_strategy_failure_golden_run():
    assert _failure_run() == GOLDEN_FAILURE


def test_crash_backoff_golden_run():
    assert _crash_run() == GOLDEN_CRASH


def test_the_old_names_read_and_write_through_the_engine():
    with tempfile.TemporaryDirectory() as directory:
        engine, store = _engine(directory)
        try:
            values = {"_tick_count": 9, "_consecutive_tick_crashes": 2, "_next_retry_delay": 1.5, "_consecutive_strategy_failures": 3,
                      "_last_strategy_error": "X", "_last_tick_seconds": 0.25, "_alerted_health_transitions": {("a", "b")},
                      "_live_fill_risk_audit": [{"k": 1}]}
            for name, value in values.items():
                setattr(engine, name, value)
                assert getattr(engine, name) is value
        finally:
            store.close()
