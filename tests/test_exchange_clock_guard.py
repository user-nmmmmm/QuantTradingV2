"""The exchange clock gate bounds the host clock offset and fails closed.

Convention throughout: offset = exchange - local, in seconds. A host that is 31 s slow
(the recorded incident) has offset +31.
"""
from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.foundation.live_safety import StartupSafetyPolicy
from core.market.request_budget import RequestBudgetExpired, remaining_seconds
from core.live_broker import LiveBroker
from ops.clock_guard import (
    STATUS_EXCEEDED,
    STATUS_INCONSISTENT,
    STATUS_OK,
    STATUS_UNMEASURED,
    STATUS_UNPROVEN,
    ClockOffsetPolicy,
    ExchangeClockGuard,
    describe_clock_offset,
    measure_exchange_clock_offset,
)
from ops.health import HealthAssessment
from ops.startup_preflight import build_startup_report
import run_live
from run_live import protection_only_startup_allowed, refuse_startup_for_clock
from tests.test_account_runtime_gate import make_engine
from tests.test_account_source_integration import NOW, account, configure  # noqa: F401
from tests.test_revalidation_execution import NOW as ENTRY_BAR, SYMBOL, broker as execution_broker, enter  # noqa: F401

START = datetime(2026, 11, 21, 0, 0, tzinfo=timezone.utc)


class Venue:
    """A local wall clock, a monotonic clock and an exchange whose clock is ``offset`` ahead."""

    def __init__(self, offset=0.0, round_trip=0.1):
        self.wall = START
        self.mono = 1000.0
        self.offset = offset
        self.round_trip = round_trip
        self.calls = 0
        self.fault = None          # an exception to raise, or a raw value to return
        self.step_during_request = 0.0

    def now(self):
        return self.wall

    def monotonic(self):
        return self.mono

    def advance(self, seconds):
        self.wall += timedelta(seconds=seconds)
        self.mono += seconds

    def fetch(self):
        self.calls += 1
        self.advance(self.round_trip / 2)
        server_ms = (self.wall + timedelta(seconds=self.offset)).timestamp() * 1000.0
        self.advance(self.round_trip / 2)
        self.wall += timedelta(seconds=self.step_during_request)
        if isinstance(self.fault, Exception):
            raise self.fault
        return server_ms if self.fault is None else self.fault["value"]

    def measure(self, samples=3, limit=1.0):
        return measure_exchange_clock_offset(self.fetch, samples=samples, max_offset_seconds=limit,
                                             clock=self.now, monotonic=self.monotonic)

    def guard(self, policy=None):
        return ExchangeClockGuard(self.fetch, policy, clock=self.now, monotonic=self.monotonic)


@pytest.mark.parametrize("offset", [0.0, 0.5, -0.5, 0.9, -0.9])
def test_an_offset_inside_the_limit_passes_and_is_bounded(offset):
    report = Venue(offset).measure()
    assert report["status"] == STATUS_OK and report["passed"] is True
    assert report["offset_lower_seconds"] <= offset <= report["offset_upper_seconds"]
    assert report["offset_abs_bound_seconds"] <= 1.0
    assert report["samples_valid"] == 3 and report["errors"] == []


@pytest.mark.parametrize("offset", [5.0, -5.0, 31.0, -31.0, 1.2, -1.2])
def test_an_offset_beyond_the_limit_is_refused(offset):
    report = Venue(offset).measure()
    assert report["status"] == STATUS_EXCEEDED and report["passed"] is False
    assert report["offset_lower_seconds"] <= offset <= report["offset_upper_seconds"]


def test_the_recorded_31_second_incident_is_refused_with_its_sign():
    report = Venue(31.04).measure(samples=5)
    assert report["status"] == STATUS_EXCEEDED
    assert 30.9 < report["offset_lower_seconds"] <= report["offset_upper_seconds"] < 31.1


def test_a_slow_round_trip_cannot_prove_the_offset_and_fails_closed():
    report = Venue(0.0, round_trip=3.0).measure()
    assert report["status"] == STATUS_UNPROVEN and report["passed"] is False
    assert report["offset_lower_seconds"] < -1.0 and report["offset_upper_seconds"] > 1.0


def test_a_failed_request_is_unmeasured_and_leaks_no_request_detail():
    venue = Venue()
    venue.fault = ConnectionError("GET https://venue.example/time?signature=private-detail")
    report = venue.measure()
    assert report["status"] == STATUS_UNMEASURED and report["passed"] is False
    assert report["errors"] == ["ConnectionError"] * 3
    assert "private-detail" not in json.dumps(report)


@pytest.mark.parametrize("value", [None, True, "1700000000000", -1, 0, float("nan"), float("inf")])
def test_an_unusable_server_time_is_unmeasured(value):
    venue = Venue()
    venue.fault = {"value": value}
    report = venue.measure()
    assert report["status"] == STATUS_UNMEASURED
    assert report["errors"] == ["InvalidServerTime"] * 3


def test_a_wall_clock_step_during_the_request_discards_that_sample():
    venue = Venue()
    venue.step_during_request = 10.0
    report = venue.measure()
    assert report["status"] == STATUS_UNMEASURED
    assert report["errors"] == ["LocalClockStepped"] * 3


def test_an_unreachable_time_endpoint_is_not_asked_again_once_the_budget_is_spent():
    venue = Venue(round_trip=6.0)                # each request hangs for 6 s, then fails
    venue.fault = TimeoutError("no answer")
    report = venue.measure(samples=5)
    assert venue.calls == 1 and report["samples_attempted"] == 1 and report["samples_requested"] == 5
    assert report["status"] == STATUS_UNMEASURED and report["errors"] == ["TimeoutError"]


def test_fast_samples_are_all_taken_inside_the_budget():
    report = Venue(0.1, round_trip=0.2).measure(samples=5)
    assert report["samples_attempted"] == 5 and report["samples_valid"] == 5


def test_usable_samples_survive_a_partial_failure():
    venue = Venue(0.3)
    fetch, state = venue.fetch, {"n": 0}

    def flaky():
        state["n"] += 1
        if state["n"] == 2:
            venue.advance(0.05)
            raise TimeoutError("slow")
        return fetch()

    report = measure_exchange_clock_offset(flaky, samples=3, max_offset_seconds=1.0,
                                           clock=venue.now, monotonic=venue.monotonic)
    assert report["status"] == STATUS_OK
    assert report["samples_valid"] == 2 and report["errors"] == ["TimeoutError"]


def test_contradicting_samples_are_inconsistent_not_averaged():
    venue = Venue(0.0)
    fetch = venue.fetch

    def drifting():
        value = fetch()
        venue.offset += 0.8
        return value

    report = measure_exchange_clock_offset(drifting, samples=2, max_offset_seconds=1.0,
                                           clock=venue.now, monotonic=venue.monotonic)
    assert report["status"] == STATUS_INCONSISTENT and report["passed"] is False
    assert report["offset_lower_seconds"] > report["offset_upper_seconds"]


@pytest.mark.parametrize("field,value", [
    ("max_offset_seconds", 0), ("max_offset_seconds", float("nan")), ("check_interval_seconds", -1),
    ("retry_interval_seconds", float("inf")), ("step_tolerance_seconds", True),
    ("startup_samples", 0), ("runtime_samples", 1.5), ("runtime_samples", True),
    ("sample_budget_seconds", 0), ("read_timeout_seconds", 0), ("read_timeout_seconds", float("inf")),
])
def test_policy_rejects_values_that_would_weaken_or_break_the_gate(field, value):
    with pytest.raises(ValueError):
        ClockOffsetPolicy(**{field: value})


def test_guard_reports_unverified_until_the_first_measurement():
    reason = Venue().guard().health_reason()
    assert reason.code == "EXCHANGE_CLOCK_UNVERIFIED" and reason.category == "clock"
    assert reason.limit_seconds == 1.0


def test_guard_uses_more_samples_at_startup_and_rechecks_on_schedule():
    venue = Venue(0.2)
    guard = venue.guard()
    assert guard.measure(startup=True)["samples_requested"] == 5
    assert guard.health_reason() is None
    venue.advance(299.0)
    assert guard.check_if_due() is None and venue.calls == 5
    venue.advance(1.5)
    assert guard.check_if_due()["samples_requested"] == 3 and venue.calls == 8


def test_guard_blocks_while_the_offset_is_exceeded_retries_sooner_and_recovers():
    venue = Venue(0.2)
    guard = venue.guard()
    guard.measure(startup=True)
    venue.offset = 31.0
    venue.advance(301.0)
    assert guard.check_if_due()["status"] == STATUS_EXCEEDED
    reason = guard.health_reason()
    assert reason.code == "EXCHANGE_CLOCK_OFFSET" and "status=exceeded" in reason.message
    venue.advance(10.0)
    assert guard.check_if_due() is None          # the retry interval has not elapsed
    venue.offset = 0.1
    venue.advance(21.0)
    assert guard.check_if_due()["passed"] is True
    assert guard.health_reason() is None


def test_guard_treats_an_unreachable_exchange_clock_as_unverified():
    venue = Venue()
    guard = venue.guard()
    guard.measure(startup=True)
    venue.fault = TimeoutError("no answer")
    venue.advance(301.0)
    guard.check_if_due()
    assert guard.health_reason().code == "EXCHANGE_CLOCK_UNVERIFIED"


def test_description_is_one_line_with_bounds_limit_and_errors():
    assert describe_clock_offset(None) == "exchange clock offset has not been measured"
    line = describe_clock_offset(Venue(31.0).measure())
    assert "\n" not in line and "status=exceeded" in line and "limit=1.000s" in line and "samples=3/3" in line


def test_broker_reads_the_exchange_clock_once_without_retry():
    exchange = SimpleNamespace(fetch_time=MagicMock(return_value=1795219200123.0))
    stub = SimpleNamespace(exchange=exchange, _retry_exchange_call=MagicMock())
    assert LiveBroker.exchange_server_time_ms(stub) == 1795219200123
    exchange.fetch_time.assert_called_once_with()
    stub._retry_exchange_call.assert_not_called()


def test_broker_clock_read_runs_inside_a_request_deadline():
    """Without a request scope the shared budget wait has no limit and would stall the tick."""
    seen = []

    def fetch_time():
        seen.append(remaining_seconds())
        return 1795219200123

    stub = SimpleNamespace(exchange=SimpleNamespace(fetch_time=fetch_time))
    assert remaining_seconds() is None                      # no deadline outside the call
    LiveBroker.exchange_server_time_ms(stub)
    LiveBroker.exchange_server_time_ms(stub, timeout_seconds=0.5)
    assert 0 < seen[0] <= 2.0 and 0 < seen[1] <= 0.5
    assert remaining_seconds() is None                      # and none is left behind


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf"), True])
def test_broker_clock_read_rejects_an_unusable_deadline(timeout):
    stub = SimpleNamespace(exchange=SimpleNamespace(fetch_time=MagicMock(return_value=1)))
    with pytest.raises(ValueError):
        LiveBroker.exchange_server_time_ms(stub, timeout_seconds=timeout)
    stub.exchange.fetch_time.assert_not_called()


def test_a_stalled_request_budget_ends_at_the_deadline_and_fails_closed():
    """A throttle that would wait forever is cut off by the deadline and reported as unmeasured."""
    waits = []

    def stalled_fetch_time():
        while True:                                         # what SharedRequestBudget.wait does when exhausted
            waits.append(remaining_seconds())

    stub = SimpleNamespace(exchange=SimpleNamespace(fetch_time=stalled_fetch_time))
    with pytest.raises(RequestBudgetExpired):
        LiveBroker.exchange_server_time_ms(stub, timeout_seconds=0.05)
    assert waits and remaining_seconds() is None
    report = measure_exchange_clock_offset(
        lambda: LiveBroker.exchange_server_time_ms(stub, timeout_seconds=0.02), samples=2, max_offset_seconds=1.0)
    assert report["status"] == STATUS_UNMEASURED and report["errors"] == ["RequestBudgetExpired"] * 2


def _policy():
    return StartupSafetyPolicy(
        sandbox=True, exchange_id="binance", account_type="spot", symbols=("BTC/USDT",),
        allowed_exchanges=("binance",), allowed_account_types=("spot",), allowed_symbols=("BTC/USDT",),
        base_currency="USDT", max_order_notional=1000, max_daily_new_risk=5000,
    )


def _healthy_engine():
    return SimpleNamespace(
        health_assessment=SimpleNamespace(healthy=True, reason_codes=[]),
        risk_manager=SimpleNamespace(circuit_breaker_triggered=False),
        _last_account_sync_at=START, _last_order_sync_at=START,
    )


def _startup_report(clock_offset):
    with patch.dict("os.environ", {"QUANT_KILL_SWITCH": ""}):
        return build_startup_report(_policy(), {"apiKey": "set", "secret": "set"}, _healthy_engine(),
                                    checked_at=START, clock_offset=clock_offset)


def _clock_check(report):
    return next(item for item in report["checks"] if item["name"] == "exchange_clock_offset")


def test_startup_report_passes_only_with_a_passing_clock_measurement():
    report = _startup_report(Venue(0.3).measure())
    assert report["ok"] is True and _clock_check(report)["passed"] is True
    assert report["exchange_clock_offset"]["status"] == STATUS_OK


@pytest.mark.parametrize("clock_offset", [
    None,
    Venue(5.0).measure(),
    Venue(-31.0).measure(),
    Venue(0.0, round_trip=3.0).measure(),
    {"status": "ok"},                       # a claim without ``passed`` is not a measurement
    {"status": "ok", "passed": "yes"},
])
def test_startup_report_fails_closed_without_a_passing_measurement(clock_offset):
    report = _startup_report(clock_offset)
    assert report["ok"] is False and _clock_check(report)["passed"] is False
    assert not protection_only_startup_allowed(report)


def test_a_failed_clock_check_is_not_startable_as_protection_only():
    report = _startup_report(Venue(31.0).measure())
    report["health_reason_codes"] = ["ACCOUNT_FACTS_UNVERIFIED"]
    assert not protection_only_startup_allowed(report)


def test_refused_startup_writes_evidence_and_returns_exit_code_2(tmp_path):
    path = tmp_path / "preflight" / "startup.json"
    risk_manager = SimpleNamespace(circuit_breaker_triggered=False)   # no engine exists this early
    with patch.dict("os.environ", {"QUANT_KILL_SWITCH": ""}):
        code = refuse_startup_for_clock(_policy(), {"apiKey": "private-key", "secret": "private-secret"},
                                        risk_manager, Venue(31.0).measure(), str(path))
    written = json.loads(path.read_text(encoding="utf-8"))
    assert code == 2 and written["ok"] is False
    assert written["initialization"] == "skipped_exchange_clock_offset"
    assert written["allows_new_risk"] is False and written["protection_only_startup_allowed"] is False
    assert written["exchange_clock_offset"]["status"] == STATUS_EXCEEDED
    assert "private-key" not in path.read_text(encoding="utf-8")
    failed = {item["name"] for item in written["checks"] if not item["passed"]}
    assert failed == {"exchange_clock_offset", "account_sync_baseline", "order_sync_baseline", "health_baseline"}


def test_live_entry_measures_the_clock_before_any_signed_request():
    """With a skewed clock the venue rejects signed requests, so the gate has to run first.

    Live permission verification and engine initialization are the first signed requests.
    """
    source = inspect.getsource(run_live.main)
    measured = source.index("clock_guard.measure(startup=True)")
    refused = source.index("refuse_startup_for_clock(")
    assert measured < refused < source.index("gray_policy.validate(") < source.index("engine.initialize()")
    assert source.count("measure(startup=True)") == 1
    # Every read carries the policy's deadline, so a stalled request budget cannot hold up a tick.
    assert "exchange_server_time_ms(timeout_seconds=clock_policy.read_timeout_seconds)" in source


def _verified_engine(account, tmp_path):  # noqa: F811
    """An engine whose account evidence allows new entries, so only the clock can block them."""
    broker, _, data = account
    data["evidence_kind"] = "independent_export"
    configure(broker, data, tmp_path, provenance_verifier=MagicMock(return_value=True))
    return make_engine(broker, tmp_path)


def test_engine_without_a_guard_is_unchanged(account, tmp_path):  # noqa: F811
    engine, state = _verified_engine(account, tmp_path)
    try:
        assert engine.clock_guard is None
        assert engine._tick()
        assert not any(code.startswith("EXCHANGE_CLOCK") for code in engine.health_assessment.reason_codes)
        assert engine.event_processor._collect_symbol_candidate.call_args.kwargs["allow_new_entries"] is True
    finally:
        state.close()


def test_a_passing_guard_does_not_block_new_entries(account, tmp_path):  # noqa: F811
    engine, state = _verified_engine(account, tmp_path)
    venue = Venue(0.2)
    engine.clock_guard = venue.guard()
    try:
        assert engine._tick()
        assert venue.calls == 3                       # no startup measurement was made: the first tick measures
        assert not any(code.startswith("EXCHANGE_CLOCK") for code in engine.health_assessment.reason_codes)
        assert engine.event_processor._collect_symbol_candidate.call_args.kwargs["allow_new_entries"] is True
    finally:
        state.close()


@pytest.mark.parametrize("offset,fault,code", [
    (31.0, None, "EXCHANGE_CLOCK_OFFSET"),
    (-5.0, None, "EXCHANGE_CLOCK_OFFSET"),
    (0.0, TimeoutError("no answer"), "EXCHANGE_CLOCK_UNVERIFIED"),
])
def test_a_failing_guard_blocks_new_entries_but_keeps_protection(account, tmp_path, offset, fault, code):  # noqa: F811
    engine, state = _verified_engine(account, tmp_path)
    venue = Venue(offset)
    venue.fault = fault
    engine.clock_guard = venue.guard()
    try:
        assert engine._tick()
        assert code in engine.health_assessment.reason_codes
        assert engine.health_assessment.allows_new_risk is False and not engine._healthy
        assert engine._reconcile_protective_orders.call_count >= 1
        call = engine.event_processor._collect_symbol_candidate.call_args
        assert call.kwargs["allow_position_management"] is True
        assert call.kwargs["allow_new_entries"] is False
        # The broker holds the same assessment; its submission gate is what refuses an opening order.
        assert engine.broker.health_assessment.allows_new_risk is False
        status = json.loads((tmp_path / "status.json").read_text())
        assert code in json.dumps(status)
    finally:
        state.close()


def test_new_entries_resume_after_the_clock_recovers(account, tmp_path):  # noqa: F811
    engine, state = _verified_engine(account, tmp_path)
    venue = Venue(31.0)
    engine.clock_guard = venue.guard()
    try:
        assert engine._tick()
        assert "EXCHANGE_CLOCK_OFFSET" in engine.health_assessment.reason_codes
        venue.offset = 0.1
        venue.advance(10.0)
        assert engine._tick()                        # the retry interval has not elapsed: still blocked
        assert "EXCHANGE_CLOCK_OFFSET" in engine.health_assessment.reason_codes
        venue.advance(21.0)
        assert engine._tick()
        assert not any(c.startswith("EXCHANGE_CLOCK") for c in engine.health_assessment.reason_codes)
        assert engine.health_assessment.allows_new_risk is True and engine._healthy
        assert engine.broker.health_assessment.allows_new_risk is True
    finally:
        state.close()


def test_broker_refuses_an_opening_order_but_still_reduces_while_the_clock_check_fails(execution_broker):  # noqa: F811
    broker = execution_broker
    enter(broker)                                    # a position opened while healthy
    venue = Venue(31.0)
    guard = venue.guard()
    guard.measure(startup=True)
    broker.set_health_assessment(HealthAssessment(NOW, (guard.health_reason(),)))
    # A new bar, so the next buy is a new intent and not a replay of the entry above.
    broker.set_bar_context("1d", ENTRY_BAR + timedelta(days=1))

    opening = broker.submit_order(SYMBOL, "buy", 1, reference_price=100, stop_loss=90, strategy_id="TrendBreakout")
    assert not opening.accepted
    assert "health fail-closed: EXCHANGE_CLOCK_OFFSET" in opening.message
    assert broker.portfolio.get_position(SYMBOL)["qty"] == 1

    reducing = broker.submit_order(SYMBOL, "sell", 0.5, reference_price=100, strategy_id="TrendBreakout")
    assert reducing.accepted, reducing.message
    assert broker.portfolio.get_position(SYMBOL)["qty"] == pytest.approx(0.5)
