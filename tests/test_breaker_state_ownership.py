"""B6 (ADR 0027): the circuit breaker's durable state is owned by ``CircuitBreaker``; ``RiskManager`` keeps the names.

The golden rows were recorded on the code BEFORE the state moved: after every step of three scripted runs the
breaker checkpoint (all thirteen state attributes) and the decision must be identical, and every old attribute on
``RiskManager`` must still read and write the same value.
"""
from __future__ import annotations

import pandas as pd
import pytest

from core.risk import BreakerAction, RiskManager

START = pd.Timestamp("2024-01-01", tz="UTC")
KEYS = ('action', 'high_water_equity', 'last_drawdown', 'daily_loss_triggered', 'circuit_breaker_triggered', 'blocked_until', 'probation_equity', 'recovery_count', 'breaker_epoch', 'transition_sequence', 'current_transition_id', 'current_daily_action_id')
STATE_NAMES = ("blocked_until", "probation_equity", "recovery_count", "circuit_breaker_triggered", "daily_loss_triggered",
               "high_water_equity", "portfolio_breaker_action", "last_drawdown", "breaker_epoch",
               "_breaker_transition_sequence", "current_transition_id", "current_daily_action_id", "health_assessment")


def _manager(daily=0.50, **extra):
    return RiskManager(daily_loss_limit=daily, portfolio_drawdown_reduce=0.10, portfolio_drawdown_block=0.15,
                       portfolio_drawdown_liquidate=0.20, portfolio_drawdown_lock=0.25, **extra)


def _snap(risk, decision):
    checkpoint = risk.breaker_checkpoint()
    row = {key: (str(checkpoint[key]) if key == "blocked_until" and checkpoint[key] is not None else checkpoint[key]) for key in KEYS}
    row["last_drawdown"] = round(row["last_drawdown"], 6)
    row["decision"] = (decision.action.value, decision.allow_new_entries, decision.terminal)
    return row


def _run(risk, steps, daily_reset):
    rows = []
    for day, equity, daily_start in steps:
        if daily_reset:
            risk.reset_daily_breaker()
        rows.append(_snap(risk, risk.check_circuit_breaker(equity, daily_start, occurred_at=START + pd.Timedelta(days=day), bar_index=day)))
    return rows


GOLDEN_ESCALATION = [{'action': 'normal',
  'high_water_equity': 10000.0,
  'last_drawdown': 0.0,
  'daily_loss_triggered': False,
  'circuit_breaker_triggered': False,
  'blocked_until': None,
  'probation_equity': None,
  'recovery_count': 0,
  'breaker_epoch': 0,
  'transition_sequence': 0,
  'current_transition_id': None,
  'current_daily_action_id': None,
  'decision': ('normal', True, False)},
 {'action': 'normal',
  'high_water_equity': 10000.0,
  'last_drawdown': 0.02,
  'daily_loss_triggered': False,
  'circuit_breaker_triggered': False,
  'blocked_until': None,
  'probation_equity': None,
  'recovery_count': 0,
  'breaker_epoch': 0,
  'transition_sequence': 0,
  'current_transition_id': None,
  'current_daily_action_id': None,
  'decision': ('normal', True, False)},
 {'action': 'reduce',
  'high_water_equity': 10000.0,
  'last_drawdown': 0.11,
  'daily_loss_triggered': False,
  'circuit_breaker_triggered': False,
  'blocked_until': None,
  'probation_equity': None,
  'recovery_count': 0,
  'breaker_epoch': 0,
  'transition_sequence': 1,
  'current_transition_id': 'epoch-0-transition-1',
  'current_daily_action_id': None,
  'decision': ('reduce', True, False)},
 {'action': 'block_new',
  'high_water_equity': 10000.0,
  'last_drawdown': 0.16,
  'daily_loss_triggered': False,
  'circuit_breaker_triggered': False,
  'blocked_until': None,
  'probation_equity': None,
  'recovery_count': 0,
  'breaker_epoch': 0,
  'transition_sequence': 2,
  'current_transition_id': 'epoch-0-transition-2',
  'current_daily_action_id': None,
  'decision': ('block_new', False, False)},
 {'action': 'liquidate',
  'high_water_equity': 10000.0,
  'last_drawdown': 0.21,
  'daily_loss_triggered': False,
  'circuit_breaker_triggered': False,
  'blocked_until': None,
  'probation_equity': None,
  'recovery_count': 0,
  'breaker_epoch': 0,
  'transition_sequence': 3,
  'current_transition_id': 'epoch-0-transition-3',
  'current_daily_action_id': None,
  'decision': ('liquidate', False, True)},
 {'action': 'liquidate',
  'high_water_equity': 10000.0,
  'last_drawdown': 0.05,
  'daily_loss_triggered': False,
  'circuit_breaker_triggered': False,
  'blocked_until': None,
  'probation_equity': None,
  'recovery_count': 0,
  'breaker_epoch': 0,
  'transition_sequence': 3,
  'current_transition_id': 'epoch-0-transition-3',
  'current_daily_action_id': None,
  'decision': ('liquidate', False, True)},
 {'action': 'liquidate',
  'high_water_equity': 10200.0,
  'last_drawdown': 0.0,
  'daily_loss_triggered': False,
  'circuit_breaker_triggered': False,
  'blocked_until': None,
  'probation_equity': None,
  'recovery_count': 0,
  'breaker_epoch': 0,
  'transition_sequence': 3,
  'current_transition_id': 'epoch-0-transition-3',
  'current_daily_action_id': None,
  'decision': ('liquidate', False, True)},
 {'action': 'locked',
  'high_water_equity': 10200.0,
  'last_drawdown': 0.27451,
  'daily_loss_triggered': False,
  'circuit_breaker_triggered': False,
  'blocked_until': None,
  'probation_equity': None,
  'recovery_count': 0,
  'breaker_epoch': 0,
  'transition_sequence': 4,
  'current_transition_id': 'epoch-0-transition-4',
  'current_daily_action_id': None,
  'decision': ('locked', False, True)},
 {'action': 'locked',
  'high_water_equity': 10200.0,
  'last_drawdown': 0.313725,
  'daily_loss_triggered': False,
  'circuit_breaker_triggered': False,
  'blocked_until': None,
  'probation_equity': None,
  'recovery_count': 0,
  'breaker_epoch': 0,
  'transition_sequence': 4,
  'current_transition_id': 'epoch-0-transition-4',
  'current_daily_action_id': None,
  'decision': ('locked', False, True)}]

GOLDEN_DAILY_LOSS = [{'action': 'normal',
  'high_water_equity': 10000.0,
  'last_drawdown': 0.0,
  'daily_loss_triggered': False,
  'circuit_breaker_triggered': False,
  'blocked_until': None,
  'probation_equity': None,
  'recovery_count': 0,
  'breaker_epoch': 0,
  'transition_sequence': 0,
  'current_transition_id': None,
  'current_daily_action_id': None,
  'decision': ('normal', True, False)},
 {'action': 'normal',
  'high_water_equity': 10000.0,
  'last_drawdown': 0.03,
  'daily_loss_triggered': False,
  'circuit_breaker_triggered': False,
  'blocked_until': None,
  'probation_equity': None,
  'recovery_count': 0,
  'breaker_epoch': 0,
  'transition_sequence': 0,
  'current_transition_id': None,
  'current_daily_action_id': None,
  'decision': ('normal', True, False)},
 {'action': 'normal',
  'high_water_equity': 10000.0,
  'last_drawdown': 0.06,
  'daily_loss_triggered': True,
  'circuit_breaker_triggered': True,
  'blocked_until': None,
  'probation_equity': None,
  'recovery_count': 0,
  'breaker_epoch': 0,
  'transition_sequence': 0,
  'current_transition_id': None,
  'current_daily_action_id': 'epoch-0-daily-2',
  'decision': ('normal', False, False)},
 {'action': 'normal',
  'high_water_equity': 10000.0,
  'last_drawdown': 0.06,
  'daily_loss_triggered': False,
  'circuit_breaker_triggered': False,
  'blocked_until': None,
  'probation_equity': None,
  'recovery_count': 0,
  'breaker_epoch': 0,
  'transition_sequence': 0,
  'current_transition_id': None,
  'current_daily_action_id': None,
  'decision': ('normal', True, False)}]

GOLDEN_RECOVERY = [{'action': 'normal',
  'high_water_equity': 100.0,
  'last_drawdown': 0.0,
  'daily_loss_triggered': False,
  'circuit_breaker_triggered': False,
  'blocked_until': None,
  'probation_equity': None,
  'recovery_count': 0,
  'breaker_epoch': 0,
  'transition_sequence': 0,
  'current_transition_id': None,
  'current_daily_action_id': None,
  'decision': ('normal', True, False)},
 {'action': 'block_new',
  'high_water_equity': 100.0,
  'last_drawdown': 0.16,
  'daily_loss_triggered': False,
  'circuit_breaker_triggered': False,
  'blocked_until': '2024-02-01T00:00:00+00:00',
  'probation_equity': None,
  'recovery_count': 0,
  'breaker_epoch': 0,
  'transition_sequence': 1,
  'current_transition_id': 'epoch-0-transition-1',
  'current_daily_action_id': None,
  'decision': ('block_new', False, False)},
 {'action': 'block_new',
  'high_water_equity': 100.0,
  'last_drawdown': 0.16,
  'daily_loss_triggered': False,
  'circuit_breaker_triggered': True,
  'blocked_until': '2024-02-01T00:00:00+00:00',
  'probation_equity': None,
  'recovery_count': 0,
  'breaker_epoch': 0,
  'transition_sequence': 1,
  'current_transition_id': 'epoch-0-transition-1',
  'current_daily_action_id': None,
  'decision': ('block_new', False, False)},
 {'action': 'reduce',
  'high_water_equity': 100.0,
  'last_drawdown': 0.16,
  'daily_loss_triggered': False,
  'circuit_breaker_triggered': False,
  'blocked_until': None,
  'probation_equity': 84.0,
  'recovery_count': 1,
  'breaker_epoch': 0,
  'transition_sequence': 2,
  'current_transition_id': 'epoch-0-recovery-2',
  'current_daily_action_id': None,
  'decision': ('reduce', True, False)},
 {'action': 'block_new',
  'high_water_equity': 100.0,
  'last_drawdown': 0.19,
  'daily_loss_triggered': False,
  'circuit_breaker_triggered': False,
  'blocked_until': '2024-03-03T00:00:00+00:00',
  'probation_equity': None,
  'recovery_count': 1,
  'breaker_epoch': 0,
  'transition_sequence': 3,
  'current_transition_id': 'epoch-0-transition-3',
  'current_daily_action_id': None,
  'decision': ('block_new', False, False)},
 {'action': 'block_new',
  'high_water_equity': 100.0,
  'last_drawdown': 0.19,
  'daily_loss_triggered': False,
  'circuit_breaker_triggered': True,
  'blocked_until': '2024-03-03T00:00:00+00:00',
  'probation_equity': None,
  'recovery_count': 1,
  'breaker_epoch': 0,
  'transition_sequence': 3,
  'current_transition_id': 'epoch-0-transition-3',
  'current_daily_action_id': None,
  'decision': ('block_new', False, False)},
 {'action': 'reduce',
  'high_water_equity': 100.0,
  'last_drawdown': 0.19,
  'daily_loss_triggered': False,
  'circuit_breaker_triggered': False,
  'blocked_until': None,
  'probation_equity': 81.0,
  'recovery_count': 2,
  'breaker_epoch': 0,
  'transition_sequence': 4,
  'current_transition_id': 'epoch-0-recovery-4',
  'current_daily_action_id': None,
  'decision': ('reduce', True, False)}]


def test_escalation_path_matches_the_recorded_states():
    steps = [(day, equity, 10000.0) for day, equity in enumerate([10000, 9800, 8900, 8400, 7900, 9500, 10200, 7400, 7000])]
    assert _run(_manager(), steps, daily_reset=False) == GOLDEN_ESCALATION


def test_daily_loss_path_matches_the_recorded_states():
    steps = [(0, 10000, 10000), (1, 9700, 10000), (2, 9400, 10000), (3, 9400, 9400)]
    assert _run(_manager(daily=0.05), steps, daily_reset=True) == GOLDEN_DAILY_LOSS


def test_cooldown_probation_and_recovery_path_matches_the_recorded_states():
    steps = [(0, 100, 100), (1, 84, 84), (30, 84, 84), (31, 84, 84), (32, 81, 81), (61, 81, 81), (62, 81, 81)]
    assert _run(_manager(daily=0.05, recovery_policy={"enabled": True}), steps, daily_reset=True) == GOLDEN_RECOVERY


def test_the_recorded_runs_really_exercise_the_state():
    """Guards the golden data itself: every attribute that can change does change somewhere in the three runs."""
    seen = {key: set() for key in KEYS}
    for rows in (GOLDEN_ESCALATION, GOLDEN_DAILY_LOSS, GOLDEN_RECOVERY):
        for row in rows:
            for key in KEYS:
                seen[key].add(str(row[key]))
    for key in ("action", "high_water_equity", "last_drawdown", "daily_loss_triggered", "blocked_until", "probation_equity",
                "recovery_count", "transition_sequence", "current_transition_id"):
        assert len(seen[key]) > 1, key


@pytest.mark.parametrize("name, value", [
    ("blocked_until", pd.Timestamp("2024-02-01", tz="UTC")), ("probation_equity", 91.5), ("recovery_count", 3),
    ("circuit_breaker_triggered", True), ("daily_loss_triggered", True), ("high_water_equity", 123.0),
    ("portfolio_breaker_action", BreakerAction.BLOCK_NEW), ("last_drawdown", 0.17), ("breaker_epoch", 4),
    ("_breaker_transition_sequence", 9), ("current_transition_id", "epoch-4-transition-9"),
    ("current_daily_action_id", "daily-1"), ("health_assessment", object())])
def test_every_state_name_still_reads_and_writes_on_the_risk_manager(name, value):
    risk = _manager()
    setattr(risk, name, value)
    assert getattr(risk, name) is value or getattr(risk, name) == value
    other = _manager()
    assert getattr(other, name) != value or name == "health_assessment"          # instances do not share state


def test_a_checkpoint_round_trip_restores_every_attribute():
    risk = _manager()
    for day, equity in enumerate([10000, 8900, 8400]):
        risk.check_circuit_breaker(equity, 10000.0, occurred_at=START + pd.Timedelta(days=day), bar_index=day)
    saved = risk.breaker_checkpoint()
    fresh = _manager()
    fresh.restore_breaker_checkpoint(saved)
    assert fresh.breaker_checkpoint() == saved
    assert fresh.portfolio_breaker_action is BreakerAction.BLOCK_NEW and fresh.high_water_equity == 10000.0


def test_defaults_of_a_fresh_risk_manager():
    risk = _manager()
    assert (risk.blocked_until, risk.probation_equity, risk.recovery_count) == (None, None, 0)
    assert (risk.circuit_breaker_triggered, risk.daily_loss_triggered) == (False, False)
    assert (risk.high_water_equity, risk.last_drawdown, risk.breaker_epoch) == (None, 0.0, 0)
    assert risk.portfolio_breaker_action is BreakerAction.NORMAL
    assert (risk._breaker_transition_sequence, risk.current_transition_id, risk.current_daily_action_id) == (0, None, None)
    assert risk.health_assessment is None and risk.breaker_audit == []

