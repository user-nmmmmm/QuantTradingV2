"""Offline account evidence gates never disable known-position protection."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from config.config import config
from core.domain import OrderIntent, SyncResult
from core.portfolio import Portfolio
from core.risk import RiskManager
from core.state_store_v2 import StateStore
from live_trading.engine import LiveTradingEngine
from live_trading.execution_adapter import AccountEntryBlocked, RecordedExecutionAdapter
from live_trading.recovery import account_new_risk_gate, balance_sync_succeeded
from run_live import account_source_from_args, build_parser, protection_only_startup_allowed
from tests.test_account_source_integration import NOW, account, configure  # noqa: F401
from tests.test_revalidation_execution import Harness, SYMBOL, broker as execution_broker, enter  # noqa: F401
from core.account_source import AccountSourceError
from core.risk.actions import RiskActionPlan


def verified_report():
    """A protocol-test claim, never persisted as real account evidence."""
    return {"ok": True, "allows_new_risk": True, "production_account_source_verified": True,
            "checked_at": NOW.isoformat(), "maximum_snapshot_age_seconds": 90,
            "issues": [], "differences": [],
            "source": {"evidence_kind": "independent_export", "source_id": "fixture-only",
                       "sha256": "a" * 64, "captured_at": NOW.isoformat()}}


class ReportBroker:
    def reconcile_full_account(self, **kwargs):
        return deepcopy(self.account_reconciliation_report)


@pytest.mark.parametrize("fault", ["none", "missing", "unverified", "synthetic", "stale_capture",
    "stale_check", "future_capture", "naive", "no_time", "invalid_age", "internal_issue", "persistence"])
def test_each_entry_decision_requires_current_independent_verified_source(fault):
    broker = ReportBroker()
    report = verified_report()
    broker.account_reconciliation_report = report
    if fault == "missing":
        del broker.account_reconciliation_report
    elif fault == "unverified":
        report["production_account_source_verified"] = False
    elif fault == "synthetic":
        report["source"]["evidence_kind"] = "synthetic_fixture"
    elif fault == "stale_capture":
        report["source"]["captured_at"] = (NOW - timedelta(seconds=91)).isoformat()
    elif fault == "stale_check":
        report["checked_at"] = (NOW - timedelta(seconds=91)).isoformat()
    elif fault == "future_capture":
        report["source"]["captured_at"] = (NOW + timedelta(seconds=1)).isoformat()
    elif fault == "naive":
        report["source"]["captured_at"] = NOW.replace(tzinfo=None).isoformat()
    elif fault == "no_time":
        del report["source"]["captured_at"]
    elif fault == "invalid_age":
        report["maximum_snapshot_age_seconds"] = float("nan")
    elif fault == "internal_issue":
        report["issues"] = ["missing_fact"]
    gate = account_new_risk_gate(broker, NOW, persistence_failed=fault == "persistence")
    assert gate["required"]
    assert gate["allows_new_risk"] is (fault == "none")


def test_dynamic_mock_attributes_do_not_invent_a_full_account_interface():
    broker = MagicMock()
    broker.reconcile_full_account.return_value = verified_report()
    gate = account_new_risk_gate(broker, NOW)
    assert not gate["required"]
    broker.reconcile_full_account.assert_not_called()
    assert not balance_sync_succeeded(broker, SyncResult(False, NOW, "account_reconciliation_failed"))


@pytest.mark.parametrize("full_account_contract", [False, True])
@pytest.mark.parametrize("result,expected", [
    (True, True),
    (False, False),
    (None, True),
    (SyncResult(True, NOW), True),
    (SyncResult(False, NOW, "balance_fetch_failed"), False),
    (SimpleNamespace(ok=False, error="network_unavailable"), False),
])
def test_balance_sync_contract_preserves_legacy_success_and_explicit_failures(
        full_account_contract, result, expected):
    broker = ReportBroker() if full_account_contract else SimpleNamespace()
    assert balance_sync_succeeded(broker, result) is expected
    if full_account_contract:
        # Usable balance facts alone never grant independent account approval.
        assert not account_new_risk_gate(broker, NOW)["allows_new_risk"]


@pytest.mark.parametrize("full_account_contract", [False, True])
def test_only_explicit_full_account_comparison_failure_preserves_balance_facts(full_account_contract):
    broker = ReportBroker() if full_account_contract else SimpleNamespace()
    result = SyncResult(False, NOW, "account_reconciliation_failed")
    assert balance_sync_succeeded(broker, result) is full_account_contract
    if full_account_contract:
        assert not account_new_risk_gate(broker, NOW)["allows_new_risk"]


def make_engine(broker, tmp_path, *, clock=lambda: NOW):
    frame = pd.DataFrame({"open": [110.0], "high": [111.0], "low": [109.0],
                          "close": [110.0], "volume": [1000.0]},
                         index=pd.DatetimeIndex([NOW - timedelta(days=1)]))
    fetcher = MagicMock()
    fetcher.fetch_ccxt.return_value = frame
    state = StateStore(str(tmp_path / "runtime.db"))
    engine = LiveTradingEngine(symbols=["BTC/USDT"], strategies={}, broker=broker,
        risk_manager=RiskManager(), configuration=config, data_fetcher=fetcher, clock=clock,
        state_store=state, state_file=str(tmp_path / "status.json"), close_grace_seconds=0,
        reconciliation_interval_seconds=300, alert_sink=MagicMock())
    engine.data_map = {"BTC/USDT": frame}
    engine._update_data = MagicMock()
    engine._reconcile_external_positions = MagicMock(return_value=False)
    engine._reconcile_protective_orders = MagicMock()
    engine._recheck_live_entry_risk = MagicMock(return_value=True)
    broker.risk_price_facts = MagicMock(return_value={"BTC/USDT": {"price": 110, "timestamp": NOW}})
    engine.event_processor._collect_symbol_candidate = MagicMock(return_value=(object(), True))
    engine.event_processor.allocator.allocate = MagicMock()
    return engine, state


def test_periodic_and_daily_outputs_use_actual_normalized_report(account, tmp_path):
    broker, _, data = account
    configure(broker, data, tmp_path)
    engine, state = make_engine(broker, tmp_path)
    try:
        report = engine._run_reconciliation_if_due(NOW)
        assert report["scope"] == "order_and_account_reconciliation"
        assert report["ok"]
        assert not report["allows_new_risk"]
        account_report = report["account_reconciliation"]
        path = tmp_path / "account_reconciliation" / "2026-09-20.json"
        assert json.loads(path.read_text()) == account_report
        assert state.get("account_reconciliation:2026-09-20") == account_report
        later = engine._run_reconciliation_if_due(NOW + timedelta(seconds=30))
        assert later["last_run_at"] == report["last_run_at"]
        engine._run_reconciliation_if_due(NOW + timedelta(days=1), force=True)
        assert path.exists()
        assert not json.loads((path.parent / "2026-09-21.json").read_text())["ok"]
    finally:
        state.close()


@pytest.mark.parametrize("case", ["unconfigured", "fixture", "account_discrepancy"])
def test_account_block_preserves_protection_and_position_management(account, tmp_path, case):
    broker, _, data = account
    if case != "unconfigured":
        if case == "account_discrepancy":
            data["snapshot"]["cashflows"].pop()
        configure(broker, data, tmp_path)
    engine, state = make_engine(broker, tmp_path)
    try:
        assert engine._tick()
        assert engine._last_account_sync_at == NOW
        assert not engine._healthy
        assert "ACCOUNT_FACTS_UNVERIFIED" in engine.health_assessment.reason_codes
        assert engine._reconcile_protective_orders.call_count >= 1
        call = engine.event_processor._collect_symbol_candidate.call_args
        assert call.kwargs["allow_position_management"] is True
        assert call.kwargs["allow_new_entries"] is False
        engine.event_processor.allocator.allocate.assert_not_called()
        status = json.loads((tmp_path / "status.json").read_text())
        assert not status["account_entry_gate"]["allows_new_risk"]
    finally:
        state.close()


def test_hard_balance_failure_does_not_treat_stale_positions_as_current(account, tmp_path):
    broker, venue, _ = account
    engine, state = make_engine(broker, tmp_path)
    venue.fetch_balance.return_value = None
    try:
        assert engine._tick()
        assert engine._last_account_sync_at is None
        engine._reconcile_protective_orders.assert_not_called()
        engine.event_processor._collect_symbol_candidate.assert_not_called()
    finally:
        state.close()


def test_source_expiration_during_management_blocks_allocation(account, tmp_path):
    broker, _, data = account
    data["evidence_kind"] = "independent_export"
    configure(broker, data, tmp_path, provenance_verifier=MagicMock(return_value=True))
    current = {"now": NOW}
    engine, state = make_engine(broker, tmp_path, clock=lambda: current["now"])
    def management(*args, **kwargs):
        assert kwargs["allow_new_entries"]
        current["now"] += timedelta(seconds=91)
        return object(), True
    engine.event_processor._collect_symbol_candidate.side_effect = management
    try:
        assert engine._tick()
        engine.event_processor._collect_symbol_candidate.assert_called_once()
        engine.event_processor.allocator.allocate.assert_not_called()
        assert not engine._healthy
        assert engine._reconciliation_status["account_entry_gate"]["reason"] == "account_facts_stale_or_future"
    finally:
        state.close()


@pytest.mark.parametrize("error,expected", [("account_reconciliation_failed", True),
    ("NetworkError", False), ("ValueError", False)])
def test_risk_cancellation_can_continue_only_with_known_balance_facts(account, tmp_path, error, expected):
    broker, _, _ = account
    engine, state = make_engine(broker, tmp_path)
    try:
        with patch.object(broker, "sync", return_value=SyncResult(False, NOW, error)):
            assert engine._cancel_risk_orders([]) is expected
    finally:
        state.close()


def test_initialization_keeps_account_failure_visible_with_usable_balance_facts(account, tmp_path):
    broker, _, data = account
    data["snapshot"]["cashflows"].pop()
    configure(broker, data, tmp_path)
    engine, state = make_engine(broker, tmp_path)
    try:
        engine.initialize()
        assert engine._last_account_sync_at == NOW
        assert not engine._healthy
        assert not broker.account_reconciliation_report["ok"]
        assert "ACCOUNT_FACTS_UNVERIFIED" in engine.health_assessment.reason_codes
    finally:
        state.close()


def test_persistence_failure_blocks_entries_and_does_not_throw_away_protection(account, tmp_path):
    broker, _, data = account
    configure(broker, data, tmp_path)
    engine, state = make_engine(broker, tmp_path)
    try:
        with patch.object(engine, "_persist_account_reconciliation", side_effect=OSError("disk full")):
            assert engine._tick()
        assert engine._account_reconciliation_persistence_failed
        assert engine._reconcile_protective_orders.called
        engine.event_processor.allocator.allocate.assert_not_called()
    finally:
        state.close()


@pytest.mark.parametrize("arguments", [["--account-facts", "facts.json"],
    ["--account-facts-sha256", "a" * 64], ["--account-facts-source-id", "source"],
    ["--account-facts", "facts.json", "--account-facts-sha256", "a" * 64]])
def test_cli_rejects_partial_source_group_before_credentials_or_network(arguments):
    parser = build_parser()
    with pytest.raises(SystemExit) as error:
        account_source_from_args(parser.parse_args(arguments), parser)
    assert error.value.code == 2


def test_cli_pin_is_read_only_and_never_injects_production_attestation(tmp_path):
    raw = b'{"fixture_only": true}'
    path = tmp_path / "facts.json"
    path.write_bytes(raw)
    parser = build_parser()
    args = parser.parse_args(["--account-facts", str(path), "--account-facts-sha256",
                              hashlib.sha256(raw).hexdigest(), "--account-facts-source-id", "source"])
    source = account_source_from_args(args, parser)
    assert source.provenance_verifier is None
    assert path.read_bytes() == raw
    path.write_bytes(raw + b" ")
    with pytest.raises(SystemExit):
        account_source_from_args(args, parser)


def test_cli_protection_startup_does_not_hide_other_failed_checks():
    report = {"health_reason_codes": ["ACCOUNT_FACTS_UNVERIFIED"], "checks": [
        {"name": "health_baseline", "passed": False}, {"name": "account_sync_baseline", "passed": True}]}
    assert protection_only_startup_allowed(report)
    report["checks"][1]["passed"] = False
    assert not protection_only_startup_allowed(report)
    report["checks"][1]["passed"] = True
    report["health_reason_codes"].append("ORDER_STATE_UNKNOWN")
    assert not protection_only_startup_allowed(report)


@pytest.mark.parametrize("network_failure", [False, True])
def test_real_safe_broker_reduces_with_missing_export_but_never_unknown_balances(execution_broker, tmp_path, network_failure):
    broker = execution_broker
    enter(broker)
    class MissingSource:
        def read(self, **kwargs):
            raise AccountSourceError("independent_account_source_unavailable")
    broker.configure_account_source(MissingSource(), environment="sandbox")
    broker.retry_max_attempts = 1
    engine = Harness(broker)
    engine.state_store = StateStore(str(tmp_path / "protection_state.db"))
    try:
        if network_failure:
            broker.exchange.fetch_balance = MagicMock(side_effect=TimeoutError("offline network failure"))
        result = engine._reconcile_portfolio_risk_action(RiskActionPlan("fixture-reduce", "DrawdownReduce", .5))
        if network_failure:
            assert result.pending
            assert broker.portfolio.get_position(SYMBOL)["qty"] == 1
            assert not any(request["side"] == "sell" for request in broker.exchange.requests)
        else:
            assert result.performed and not result.pending
            assert broker.portfolio.get_position(SYMBOL)["qty"] == .5
            engine._reconcile_protective_orders()
            assert engine._venue_protective_orders()[0].qty == .5
            assert not broker.account_reconciliation_report["allows_new_risk"]
    finally:
        engine.state_store.close()


class SubmissionRecorder:
    def __init__(self, held):
        self.portfolio = Portfolio()
        self.portfolio.positions[SYMBOL] = {"qty": held, "avg_price": 100}
        self.submissions = []

    def submit_intent(self, intent):
        self.submissions.append(intent)
        return "submitted"

    def submit_order(self, symbol, side, qty, *, reduce_only=False):
        self.submissions.append((symbol, side, qty, reduce_only))
        return "submitted"


@pytest.mark.parametrize("canonical", [False, True])
@pytest.mark.parametrize("held,action,qty,reduce_only,allowed", [
    (1, "sell", 1, False, True), (-1, "cover", 1, False, True),
    (-1, "buy", 1, True, True), (1, "short", 1, True, True),
    (1, "buy", 1, False, False), (-1, "short", 1, False, False),
    (-1, "sell", 1, False, False), (0, "sell", 1, True, False),
    (1, "sell", 2, True, False), (-1, "cover", float("nan"), True, False)])
def test_per_order_guard_proves_direction_size_and_reduction_flag(canonical, held, action, qty, reduce_only, allowed):
    broker = SubmissionRecorder(held)
    guard = MagicMock(return_value=False)
    adapter = RecordedExecutionAdapter(broker, opening_guard=guard)
    intent = OrderIntent("fixture", "fixture", SYMBOL, "1d", NOW.isoformat(), "fixture", action, 0, qty,
                         reduce_only=reduce_only)
    submit = (lambda: adapter.submit_intent(intent)) if canonical else (
        lambda: adapter.submit_order(SYMBOL, action, qty, reduce_only=reduce_only))
    if allowed:
        assert submit() == "submitted"
        guard.assert_not_called()
    else:
        with pytest.raises(AccountEntryBlocked):
            submit()
        assert not broker.submissions


def test_per_order_guard_rechecks_between_submissions():
    broker = SubmissionRecorder(0)
    source_broker = ReportBroker()
    source_broker.account_reconciliation_report = verified_report()
    current = {"now": NOW}
    adapter = RecordedExecutionAdapter(broker, opening_guard=lambda: account_new_risk_gate(
        source_broker, current["now"])["allows_new_risk"])
    assert adapter.submit_order(SYMBOL, "buy", .1) == "submitted"
    current["now"] += timedelta(seconds=91)
    with pytest.raises(AccountEntryBlocked):
        adapter.submit_order(SYMBOL, "buy", .1)
    assert len(broker.submissions) == 1


def test_per_order_guard_preserves_durable_order_recovery(execution_broker):
    broker = execution_broker
    result = enter(broker)
    record = broker.order_store.get(result.client_order_id)
    guard = MagicMock(return_value=False)
    adapter = RecordedExecutionAdapter(broker, opening_guard=guard)
    before = len(broker.exchange.requests)
    recovered = adapter.submit_intent(OrderIntent(**record["intent"]))
    assert recovered.accepted
    assert len(broker.exchange.requests) == before
    guard.assert_not_called()


@pytest.mark.parametrize("network_failure", [False, True])
def test_strategy_exit_after_canceling_resident_stop_preserves_sync_distinction(execution_broker, network_failure):
    broker = execution_broker
    enter(broker)
    engine = Harness(broker)
    engine._reconcile_protective_orders()
    assert engine._venue_protective_orders()
    class MissingSource:
        def read(self, **kwargs):
            raise AccountSourceError("missing_independent_export")
    broker.configure_account_source(MissingSource(), environment="sandbox")
    broker.retry_max_attempts = 1
    if network_failure:
        broker.exchange.fetch_balance = MagicMock(side_effect=TimeoutError("offline network failure"))
    adapter = RecordedExecutionAdapter(broker, opening_guard=MagicMock(return_value=False))
    result = adapter.submit_order(SYMBOL, "sell", 1, reference_price=100, sequence=99)
    assert result.accepted is (not network_failure)
    assert broker.portfolio.get_position(SYMBOL)["qty"] == (1 if network_failure else 0)
