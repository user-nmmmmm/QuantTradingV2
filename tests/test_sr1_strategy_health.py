"""SR1 health lifecycle tests (docs/current_strategy_remediation_roadmap.md §13.1).

These cover the exact failure that produced 2022-2026 with zero trades: one
portfolio-level risk action closing many correlated symbols was counted as many
independent strategy failures, and the resulting kill switch had no expiry, no
event and no report field.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import MagicMock

import pandas as pd

from core.lots import CloseEvent
from core.portfolio import Portfolio
from core.state_store_v2 import StateStore
from core.strategy_health import (
    HealthStatus,
    StrategyHealthMachine,
    StrategyHealthPolicy,
    classify_exit_controller,
)
from core.state import MarketState
from strategies.trend_breakout import TrendBreakoutStrategy


def _close_event(
    index: int, symbol: str, pnl: float, *, reason: str = "signal",
    timestamp: str = "2021-09-07T00:00:00Z", risk_action_id=None,
    initial_risk: float = 100.0,
) -> CloseEvent:
    return CloseEvent(
        close_event_id=f"LOT-{index:09d}:{index}",
        position_id=f"POS-{index:09d}",
        lot_id=f"LOT-{index:09d}",
        symbol=symbol,
        opening_strategy_id="TrendBreakout",
        exit_reason=reason,
        qty=1.0,
        exit_price=90.0,
        theoretical_exit_price=90.0,
        realized_pnl=pnl,
        timestamp=pd.Timestamp(timestamp),
        is_position_fully_closed=True,
        initial_risk=initial_risk,
        risk_action_id=risk_action_id,
    )


def _machine(**policy) -> StrategyHealthMachine:
    return StrategyHealthMachine("TrendBreakout", StrategyHealthPolicy(**policy))


def _ingest_losses(machine: StrategyHealthMachine, count: int, *, start_day: int = 1):
    for offset in range(count):
        machine.ingest_close(
            close_event_id=f"loss-{offset}",
            symbol="BTC/USDT",
            realized_pnl=-50.0,
            exit_reason="signal",
            initial_risk=100.0,
            timestamp=f"2021-09-{start_day + offset:02d}T00:00:00Z",
        )


class TestCohortAggregation(unittest.TestCase):
    def test_one_breaker_action_is_one_cohort_regardless_of_symbol_count(self):
        for symbol_count in (1, 5, 15):
            machine = _machine()
            for index in range(symbol_count):
                machine.ingest_close(
                    close_event_id=f"{symbol_count}-{index}",
                    symbol=f"SYM{index}/USDT",
                    realized_pnl=-100.0,
                    exit_reason="DailyLossLimit",
                    initial_risk=200.0,
                    timestamp="2021-09-07T00:00:00Z",
                    risk_action_id="epoch-0-daily-1234",
                )
            self.assertEqual(len(machine.cohorts), 1, symbol_count)
            cohort = machine.cohorts[0]
            self.assertEqual(cohort.trade_count, symbol_count)
            self.assertEqual(len(cohort.symbols), symbol_count)
            self.assertEqual(cohort.net_pnl, -100.0 * symbol_count)

    def test_duplicate_close_event_is_not_counted_twice(self):
        machine = _machine()
        for _ in range(3):
            machine.ingest_close(
                close_event_id="same-event", symbol="BTC/USDT",
                realized_pnl=-10.0, exit_reason="signal",
                initial_risk=100.0, timestamp="2021-09-07T00:00:00Z",
            )
        self.assertEqual(len(machine.cohorts), 1)
        self.assertEqual(machine.cohorts[0].trade_count, 1)
        self.assertEqual(machine.cohorts[0].net_pnl, -10.0)

    def test_strategy_and_account_risk_exits_are_separable(self):
        self.assertEqual(classify_exit_controller("signal"), "strategy")
        self.assertEqual(classify_exit_controller("hard_stop"), "strategy")
        self.assertEqual(classify_exit_controller("DailyLossLimit"), "account_risk")
        self.assertEqual(classify_exit_controller("AccountLiquidation"), "account_risk")
        self.assertEqual(classify_exit_controller("MaxHoldingPeriod"), "router")
        self.assertEqual(classify_exit_controller("EndOfBacktest"), "system")

    def test_account_risk_cohorts_never_trigger_the_health_gate(self):
        """STR-P0-04: a breaker cascade must not read as alpha death."""
        machine = _machine(consecutive_negative_cohorts=2)
        for day in range(1, 6):
            machine.ingest_close(
                close_event_id=f"risk-{day}", symbol="BTC/USDT",
                realized_pnl=-500.0, exit_reason="DailyLossLimit",
                initial_risk=200.0, timestamp=f"2021-09-{day:02d}T00:00:00Z",
                risk_action_id=f"daily-{day}",
            )
        machine.evaluate("2021-09-10T00:00:00Z")
        self.assertEqual(machine.status, HealthStatus.ACTIVE)
        # ... but they are still recorded for attribution (SR3-3).
        self.assertEqual(len(machine.cohorts), 5)
        self.assertEqual(len(machine.counted_cohorts()), 0)

    def test_cohort_r_uses_initial_risk_not_raw_dollars(self):
        machine = _machine()
        machine.ingest_close(
            close_event_id="a", symbol="BTC/USDT", realized_pnl=-250.0,
            exit_reason="signal", initial_risk=100.0,
            timestamp="2021-09-07T00:00:00Z",
        )
        self.assertAlmostEqual(machine.cohorts[0].r, -2.5)
        self.assertFalse(machine.cohorts[0].r_is_estimated)


class TestLifecycleTransitions(unittest.TestCase):
    def test_cooldown_is_bounded_and_expires_into_probation(self):
        machine = _machine(consecutive_negative_cohorts=3, cooldown_days=30)
        _ingest_losses(machine, 3)
        machine.evaluate("2021-09-03T00:00:00Z")
        self.assertEqual(machine.status, HealthStatus.COOLDOWN)
        self.assertIsNotNone(machine.cooldown_until)

        # Not yet expired.
        self.assertFalse(machine.allows_new_entries("2021-09-20T00:00:00Z"))
        # Expiry lands in PROBATION, never straight back to full risk.
        self.assertTrue(machine.allows_new_entries("2021-10-05T00:00:00Z"))
        self.assertEqual(machine.status, HealthStatus.PROBATION)
        self.assertEqual(machine.risk_multiplier, machine.policy.probation_risk_multiplier)

    def test_probation_passes_back_to_active_only_on_positive_total_r(self):
        machine = _machine(
            consecutive_negative_cohorts=3, cooldown_days=30,
            probation_required_cohorts=3,
        )
        _ingest_losses(machine, 3)
        machine.evaluate("2021-09-03T00:00:00Z")
        machine.evaluate("2021-10-05T00:00:00Z")
        self.assertEqual(machine.status, HealthStatus.PROBATION)
        for day in range(6, 9):
            machine.ingest_close(
                close_event_id=f"win-{day}", symbol="BTC/USDT",
                realized_pnl=+120.0, exit_reason="signal", initial_risk=100.0,
                timestamp=f"2021-10-{day:02d}T00:00:00Z",
            )
        machine.evaluate("2021-10-09T00:00:00Z")
        self.assertEqual(machine.status, HealthStatus.ACTIVE)
        self.assertEqual(machine.risk_multiplier, 1.0)
        self.assertEqual(machine.resume_count, 1)

    def test_two_failed_probation_cycles_reach_manual_lock_and_stay_there(self):
        machine = _machine(
            consecutive_negative_cohorts=2, cooldown_days=10,
            probation_required_cohorts=2, max_failed_probation_cycles=2,
        )
        _ingest_losses(machine, 2, start_day=1)
        machine.evaluate("2021-09-02T00:00:00Z")
        self.assertEqual(machine.status, HealthStatus.COOLDOWN)
        machine.evaluate("2021-09-20T00:00:00Z")
        self.assertEqual(machine.status, HealthStatus.PROBATION)
        # First failed probation cycle -> back to COOLDOWN, not locked yet.
        _ingest_losses(machine, 2, start_day=21)
        machine.evaluate("2021-09-23T00:00:00Z")
        self.assertEqual(machine.status, HealthStatus.COOLDOWN)
        self.assertEqual(machine.failed_probation_cycles, 1)

        # Second failed cycle reaches the only terminal state.
        machine.evaluate("2021-10-10T00:00:00Z")
        self.assertEqual(machine.status, HealthStatus.PROBATION)
        _ingest_losses(machine, 2, start_day=11)
        machine.cohorts[-1].closed_at = machine.cohorts[-1].closed_at.replace(year=2021, month=10)
        machine.cohorts[-2].closed_at = machine.cohorts[-2].closed_at.replace(year=2021, month=10)
        machine.evaluate("2021-10-14T00:00:00Z")
        self.assertEqual(machine.status, HealthStatus.MANUAL_LOCK)

        # No amount of elapsed time or profit recovers a manual lock.
        machine.ingest_close(
            close_event_id="late-win", symbol="BTC/USDT", realized_pnl=+1000.0,
            exit_reason="signal", initial_risk=100.0,
            timestamp="2026-01-01T00:00:00Z",
        )
        self.assertFalse(machine.allows_new_entries("2026-06-30T00:00:00Z"))
        self.assertEqual(machine.status, HealthStatus.MANUAL_LOCK)
        self.assertIsNotNone(machine.manual_lock_reason)

        machine.manual_resume(
            approved_by="risk_owner", reason="post-mortem complete",
            at="2026-07-01T00:00:00Z",
        )
        self.assertEqual(machine.status, HealthStatus.PROBATION)

    def test_a_win_does_not_bypass_the_state_machine(self):
        """Old bug: a profit reset consecutive_losses but never revived the gate.

        The mirror image must also hold - a profit while in COOLDOWN must not
        silently reopen the gate before the cooldown expires.
        """
        machine = _machine(consecutive_negative_cohorts=3, cooldown_days=30)
        _ingest_losses(machine, 3)
        machine.evaluate("2021-09-03T00:00:00Z")
        machine.ingest_close(
            close_event_id="win", symbol="ETH/USDT", realized_pnl=+900.0,
            exit_reason="signal", initial_risk=100.0,
            timestamp="2021-09-10T00:00:00Z",
        )
        self.assertEqual(machine.consecutive_negative_cohorts, 0)
        self.assertFalse(machine.allows_new_entries("2021-09-11T00:00:00Z"))
        self.assertEqual(machine.status, HealthStatus.COOLDOWN)

    def test_every_transition_is_logged_with_time_reason_and_multiplier(self):
        machine = _machine(consecutive_negative_cohorts=3, cooldown_days=30)
        _ingest_losses(machine, 3)
        machine.evaluate("2021-09-03T00:00:00Z")
        machine.evaluate("2021-10-05T00:00:00Z")
        self.assertEqual([row["to"] for row in machine.transitions],
                         ["cooldown", "probation"])
        for row in machine.transitions:
            self.assertIsNotNone(row["at"])
            self.assertTrue(row["reason"])
            self.assertIn("risk_multiplier", row)


class TestDurability(unittest.TestCase):
    def test_cooldown_expiry_does_not_drift_across_restart(self):
        machine = _machine(consecutive_negative_cohorts=3, cooldown_days=30)
        _ingest_losses(machine, 3)
        machine.evaluate("2021-09-03T00:00:00Z")
        payload = machine.to_dict()

        restored = _machine(consecutive_negative_cohorts=3, cooldown_days=30)
        restored.load(payload)
        self.assertEqual(restored.status, HealthStatus.COOLDOWN)
        self.assertEqual(restored.cooldown_until, machine.cooldown_until)
        # Restart does not restart the clock.
        self.assertFalse(restored.allows_new_entries("2021-09-25T00:00:00Z"))
        self.assertTrue(restored.allows_new_entries("2021-10-05T00:00:00Z"))

    def test_reingesting_the_same_events_after_restart_is_idempotent(self):
        machine = _machine()
        _ingest_losses(machine, 3)
        restored = _machine()
        restored.load(machine.to_dict())
        _ingest_losses(restored, 3)
        self.assertEqual(len(restored.cohorts), 3)

    def test_strategy_persists_and_restores_health_through_state_store(self):
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(os.path.join(directory, "state.db"))
            first = TrendBreakoutStrategy()
            first.configure_health_policy(
                StrategyHealthPolicy(consecutive_negative_cohorts=2, cooldown_days=30)
            )
            first.bind_state_store(store)
            for day in (1, 2):
                first.on_trade_closed(
                    "BTC/USDT", -50.0,
                    {
                        "close_event_id": f"e{day}", "exit_reason": "signal",
                        "initial_risk": 100.0,
                        "timestamp": pd.Timestamp(f"2021-09-{day:02d}T00:00:00Z"),
                    },
                    day,
                )
            self.assertFalse(first.check_health("2021-09-03T00:00:00Z"))

            second = TrendBreakoutStrategy()
            second.configure_health_policy(
                StrategyHealthPolicy(consecutive_negative_cohorts=2, cooldown_days=30)
            )
            second.bind_state_store(store)
            self.assertEqual(second.health.status, HealthStatus.COOLDOWN)
            self.assertFalse(second.check_health("2021-09-20T00:00:00Z"))
            self.assertTrue(second.check_health("2021-10-10T00:00:00Z"))
            store.close()


class TestStrategyIntegration(unittest.TestCase):
    def _frame(self) -> pd.DataFrame:
        index = pd.date_range("2021-09-01", periods=40, freq="D", tz="UTC")
        frame = pd.DataFrame(
            {
                "open": 100.0, "high": 110.0, "low": 90.0, "close": 105.0,
                "volume": 1000.0,
            },
            index=index,
        )
        frame.loc[frame.index[-1], "close"] = 200.0  # breakout on the last bar
        return frame

    def test_cooldown_blocks_entries_but_never_blocks_exits(self):
        strategy = TrendBreakoutStrategy()
        strategy.configure_health_policy(
            StrategyHealthPolicy(consecutive_negative_cohorts=2, cooldown_days=30)
        )
        frame = self._frame()
        portfolio = Portfolio()
        for day in (1, 2):
            strategy.on_trade_closed(
                "BTC/USDT", -50.0,
                {
                    "close_event_id": f"c{day}", "exit_reason": "signal",
                    "initial_risk": 100.0,
                    "timestamp": pd.Timestamp(f"2021-09-{day:02d}T00:00:00Z"),
                },
                day,
            )
        last = len(frame) - 1
        self.assertIsNone(
            strategy.should_enter(
                "BTC/USDT", last, frame, MarketState.TREND_UP, portfolio
            )
        )
        self.assertEqual(strategy.health.status, HealthStatus.COOLDOWN)

        # REG-01: exits stay available while new risk is blocked.
        exit_frame = frame.copy()
        exit_frame.loc[exit_frame.index[-1], "close"] = 1.0
        signal = strategy.should_exit(
            "BTC/USDT", last, exit_frame, MarketState.TREND_UP, portfolio
        )
        self.assertIsNotNone(signal)
        self.assertEqual(signal["action"], "sell")

    def test_probation_multiplier_reaches_position_sizing(self):
        strategy = TrendBreakoutStrategy()
        strategy.configure_health_policy(
            StrategyHealthPolicy(probation_risk_multiplier=0.25)
        )
        strategy.health.manual_resume(
            approved_by="test", reason="probation", at="2021-10-01T00:00:00Z",
        )
        self.assertEqual(strategy.health.status, HealthStatus.PROBATION)
        self.assertEqual(strategy.health_risk_multiplier(), 0.25)
        self.assertEqual(strategy.health_snapshot()["risk_multiplier"], 0.25)

    def test_close_events_reach_the_machine_with_their_cohort_key(self):
        strategy = TrendBreakoutStrategy()
        portfolio = Portfolio()
        broker = MagicMock()
        broker.close_events = [
            _close_event(
                index, f"SYM{index}/USDT", -100.0, reason="DailyLossLimit",
                risk_action_id="epoch-0-daily-77",
            )
            for index in range(15)
        ]
        strategy._consume_execution_trades("SYM0/USDT", 10, portfolio, broker)
        strategy._consume_execution_trades("SYM0/USDT", 11, portfolio, broker)

        self.assertEqual(strategy.observed_close_events, 15)
        self.assertEqual(len(strategy.health.cohorts), 1)
        cohort = strategy.health.cohorts[0]
        self.assertEqual(cohort.trade_count, 15)
        self.assertEqual(cohort.exit_controller, "account_risk")
        self.assertEqual(cohort.risk_action_id, "epoch-0-daily-77")
        self.assertTrue(strategy.check_health("2021-09-08T00:00:00Z"))


class TestSilentInactivityDiagnostic(unittest.TestCase):
    """SR1-4: the 2021-2026 report must never read as an uneventful success."""

    def _equity(self) -> pd.Series:
        index = pd.date_range("2017-08-17", "2026-06-30", freq="30D")
        return pd.Series(10000.0, index=index)

    def test_suppressed_setups_during_a_long_gap_are_a_p0_finding(self):
        from core.diagnostics import strategy_activity_consistency

        result = strategy_activity_consistency(
            [{"exit_time": "2021-09-22"}],
            self._equity(),
            {
                "TrendBreakout": {
                    "status": "cooldown",
                    "suppressed_raw_setups": 41,
                    "last_raw_setup_at": "2023-05-01",
                }
            },
        )
        self.assertTrue(result["silent_inactivity_detected"])
        self.assertGreaterEqual(result["longest_no_trade_days"], 365)
        self.assertEqual(result["suppressed_raw_setups"], 41)
        self.assertTrue(result["findings"])

    def test_a_quiet_market_with_no_setups_is_not_flagged(self):
        from core.diagnostics import strategy_activity_consistency

        result = strategy_activity_consistency(
            [{"exit_time": "2021-09-22"}],
            self._equity(),
            {
                "TrendBreakout": {
                    "status": "active",
                    "suppressed_raw_setups": 0,
                    "last_raw_setup_at": "2021-09-01",
                }
            },
        )
        self.assertFalse(result["silent_inactivity_detected"])
        self.assertEqual(result["findings"], [])

    def test_active_status_with_setups_inside_the_gap_is_flagged(self):
        from core.diagnostics import strategy_activity_consistency

        result = strategy_activity_consistency(
            [{"exit_time": "2021-09-22"}],
            self._equity(),
            {
                "TrendBreakout": {
                    "status": "active",
                    "suppressed_raw_setups": 0,
                    "last_raw_setup_at": "2024-01-01",
                }
            },
        )
        self.assertTrue(result["silent_inactivity_detected"])
        self.assertEqual(result["strategies_with_setups_in_gap"], ["TrendBreakout"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
