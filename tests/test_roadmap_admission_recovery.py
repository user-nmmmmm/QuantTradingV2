"""R-series admission, checkpoint and wire migration regression evidence."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Barrier
from unittest.mock import MagicMock, patch

import pytest

from core.admission_gates import audit_paper_run, evaluate_phase6, reconcile_lifecycle, review_admission
from core.events import EventCodec, OrderEvent, TradingEventPipeline
from core.events.codec import _decode_value
from core.events.store import SQLiteEventStore
from core.risk import RiskManager
from core.state_store_v2 import StateStore
from core.strategy_health import HealthStatus, StrategyHealthMachine, StrategyHealthPolicy
from research.audit.ledger import CashEvent, MarkPriceEvent
from tests.test_phase6_operational_readiness import lifecycle, passing_bundle
from tests.test_r7_fault_injection import CircuitBreakerRecoveryFaultInjectionTests, NOW


def test_monitoring_health_failure_blocks_admission_even_when_delivery_passes():
    bundle = passing_bundle()
    bundle["monitoring_snapshots"][0]["costs"] = {"ok": False, "reason": "fee mismatch"}
    report = evaluate_phase6(bundle)
    monitoring = report["tasks"]["T-6.5"]
    assert monitoring["delivery_evidence"]["verified"]
    assert monitoring["health_issues"] == ["snapshot_1:costs:unhealthy"]
    assert not monitoring["passed"] and not report["admission_passed"]
    assert monitoring["generated_alerts"][0]["dimension"] == "costs"


def test_reconciliation_union_rejects_extra_business_field_and_ignores_allowed_metadata():
    expected, actual = lifecycle(), lifecycle()
    actual["positions"][0]["balance"] = 1
    report = reconcile_lifecycle(expected, actual)
    assert report["layers"]["positions"]["mismatches"][0]["fields"] == ["balance"]
    assert not report["passed"]
    del actual["positions"][0]["balance"]
    actual["positions"][0]["received_at"] = "transport only"
    assert reconcile_lifecycle(expected, actual)["passed"]
    assert not reconcile_lifecycle(expected, actual, ignored_fields=())["passed"]


@pytest.mark.parametrize("change", ["endpoints", "gap", "duplicate", "reversed", "missing_field", "naive"])
def test_paper_rejects_sparse_invalid_or_incomplete_evidence(change):
    rows = passing_bundle()["paper_observations"]
    if change == "endpoints":
        rows = [rows[0], rows[-1]]
    elif change == "gap":
        rows.pop(10)
    elif change == "duplicate":
        rows.insert(10, rows[10])
    elif change == "reversed":
        rows[10:12] = reversed(rows[10:12])
    elif change == "missing_field":
        del rows[10]["incident_status"]
    elif change == "naive":
        rows[10]["timestamp"] = rows[10]["timestamp"].replace("+00:00", "")
    assert not audit_paper_run(rows)["passed"]


def test_paper_frequency_and_required_window_are_enforced():
    rows = passing_bundle()["paper_observations"]
    assert audit_paper_run(rows)["passed"]
    assert not audit_paper_run(rows, maximum_gap_seconds=3600)["passed"]
    assert not audit_paper_run(rows, required_start="2025-12-31T00:00:00Z")["passed"]
    assert not audit_paper_run(rows, required_end="2026-02-26T00:00:00Z")["passed"]


def test_old_passed_reports_cannot_inherit_new_admission():
    reports = evaluate_phase6(passing_bundle())["tasks"]
    bundle = passing_bundle()
    report = review_admission(p0_issues=bundle["p0_issues"], holdout_report=bundle["holdout_report"],
        shadow_report=reports["T-6.1"], paper_report={"schema_version": 1, "passed": True},
        reconciliation_report=reports["T-6.3"], calibration_report=reports["T-6.4"],
        monitoring_report=reports["T-6.5"], approval=bundle["admission_approval"])
    assert not report["passed"] and not report["gates"]["paper_duration_and_regimes"]


def recovered_health():
    # Frozen policy: 0.10 -> 0.25 -> 0.50 -> 1, 30 days per stage,
    # five new cohorts / three symbols / positive after removing best.
    policy = StrategyHealthPolicy(consecutive_negative_cohorts=3, cooldown_days=30,
        probation_risk_multiplier=0.10, probation_required_cohorts=5,
        probation_min_distinct_symbols=3, probation_require_positive_without_best=True,
        repeated_failure_action="extended_cooldown", recovery_stages=(0.10, 0.25, 0.50, 1.0),
        recovery_stage_min_days=30, unified_recovery=True)
    machine = StrategyHealthMachine("alpha", policy)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for day in range(3):
        machine.ingest_close(close_event_id=f"initial-{day}", symbol="BTC", realized_pnl=-1,
                             initial_risk=1, timestamp=start + timedelta(days=day))
    machine.evaluate(start + timedelta(days=2))
    machine.evaluate(start + timedelta(days=32))
    for stage in range(3):
        for offset, value in enumerate((10, 10, -1, -1, -1)):
            machine.ingest_close(close_event_id=f"recovery-{stage}-{offset}", symbol=f"SYM{offset % 3}",
                                 realized_pnl=value, initial_risk=1,
                                 timestamp=start + timedelta(days=33 + stage * 31 + offset))
        machine.evaluate(start + timedelta(days=62 + stage * 31))
    return machine, start + timedelta(days=125)


def test_successful_probation_consumes_tail_losses_but_new_losses_trigger():
    machine, now = recovered_health()
    assert machine.status is HealthStatus.ACTIVE
    assert machine.consecutive_negative_cohorts == 0
    assert machine.evaluate(now) is HealthStatus.ACTIVE
    checkpoint = machine.to_dict()
    restored = StrategyHealthMachine("alpha", machine.policy)
    restored.load(checkpoint)
    assert restored.evaluate(now) is HealthStatus.ACTIVE
    assert len(restored.cohorts) == 18  # history retained
    for day in range(3):
        restored.ingest_close(close_event_id=f"new-{day}", symbol="BTC", realized_pnl=-1,
                              initial_risk=1, timestamp=now + timedelta(days=day))
    assert restored.evaluate(now + timedelta(days=3)) is HealthStatus.COOLDOWN


def test_old_active_probation_checkpoint_migrates_consumed_boundary():
    machine, now = recovered_health()
    data = machine.to_dict()
    data.pop("streak_baseline_version")
    data["streak_baseline_cohort_ids"] = [c.cohort_id for c in machine.cohorts[:-5]]
    restored = StrategyHealthMachine("alpha", machine.policy)
    restored.load(data)
    assert restored.evaluate(now) is HealthStatus.ACTIVE
    assert restored.migration_audit[-1]["kind"] == "probation_streak_baseline_v2"


def live_engine(store, risk, now=NOW):
    return CircuitBreakerRecoveryFaultInjectionTests()._engine(store, risk, now)


def test_legacy_torn_checkpoint_cannot_clear_daily_halt(tmp_path):
    path = tmp_path / "legacy.db"
    store = StateStore(str(path))
    risk = RiskManager()
    risk.check_circuit_breaker(7000, 10000, occurred_at=NOW)
    store.set("portfolio_breaker_checkpoint", risk.breaker_checkpoint())
    store.set("circuit_breaker_day", (NOW - timedelta(days=1)).date().isoformat())
    engine = live_engine(store, RiskManager())
    engine._reset_daily_risk_if_needed(NOW)
    assert engine.risk_manager.daily_loss_triggered
    saved = store.get("portfolio_breaker_checkpoint")
    assert saved["trading_day"] == NOW.date().isoformat()
    assert saved["checkpoint_version"] == 2
    assert any(call.args[1] == "legacy_breaker_day_unverified" for call in engine.alert_sink.notify.call_args_list)
    store.close()
    store = StateStore(str(path))
    engine = live_engine(store, RiskManager())
    engine._reset_daily_risk_if_needed(NOW)
    assert engine.risk_manager.daily_loss_triggered
    engine._reset_daily_risk_if_needed(NOW + timedelta(days=1))
    assert not engine.risk_manager.daily_loss_triggered
    store.close()


def test_control_state_batch_rolls_back_on_mid_transaction_failure(tmp_path):
    store = StateStore(str(tmp_path / "atomic.db"))
    store.set_many({"checkpoint": "old", "day": "old"})
    store._connection.execute("CREATE TRIGGER inject_crash BEFORE UPDATE ON state WHEN NEW.key='day' BEGIN SELECT RAISE(ABORT, 'crash'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.set_many({"checkpoint": "new", "day": "new"})
    assert store.get("checkpoint") == store.get("day") == "old"
    store.close()


@pytest.mark.parametrize("day", ["corrupt", "2099-01-01", "2026-8-8"])
def test_invalid_atomic_day_never_clears_daily_halt(tmp_path, day):
    store = StateStore(str(tmp_path / "invalid.db"))
    risk = RiskManager()
    risk.check_circuit_breaker(7000, 10000, occurred_at=NOW)
    store.set("portfolio_breaker_checkpoint", dict(risk.breaker_checkpoint(), trading_day=day, checkpoint_version=2))
    engine = live_engine(store, RiskManager())
    with pytest.raises(ValueError):
        engine._reset_daily_risk_if_needed(NOW)
    assert engine.risk_manager.daily_loss_triggered
    store.close()


@pytest.mark.parametrize("data_failure", [False, True])
def test_automatic_breaker_recovery_refreshes_same_tick_health(tmp_path, data_failure):
    store = StateStore(str(tmp_path / "health.db"))
    risk = RiskManager(recovery_policy={"enabled": True})
    risk.check_circuit_breaker(8300, 8300, occurred_at=NOW - timedelta(days=32))
    risk.check_circuit_breaker(7000, 7000, occurred_at=NOW - timedelta(days=31))
    engine = live_engine(store, risk)
    if data_failure:
        engine._update_data = MagicMock(side_effect=RuntimeError("missing data"))
    assert engine._tick()
    assert risk.circuit_breaker_triggered is data_failure
    assert engine._operational_state == ("RISK_HALTED" if data_failure else "HEALTHY")
    report = json.loads(Path(engine.state_file).read_text())
    assert report["operational_state"] == engine._operational_state
    store.close()


def test_same_process_concurrent_exports_have_unique_temporary_files(tmp_path):
    store = StateStore(str(tmp_path / "export.db"))
    engines = [live_engine(store, RiskManager()) for _ in range(4)]
    rendezvous = Barrier(len(engines))
    import live_trading.state_export as module
    original = module.os.replace
    original_fsync = module.os.fsync
    seen = []
    def replace_snapshot(source, target):
        seen.append(source)
        assert json.loads(Path(source).read_text())["schema_version"] == 1
        original(source, target)
    def sync_snapshot(descriptor):
        original_fsync(descriptor)
        rendezvous.wait(timeout=5)
    with patch.object(module.os, "replace", side_effect=replace_snapshot), patch.object(module.os, "fsync", side_effect=sync_snapshot):
        with ThreadPoolExecutor(max_workers=4) as pool:
            assert all(pool.map(lambda engine: engine._export_state(), engines))
    assert len(set(seen)) == 4
    assert json.loads(Path(engines[0].state_file).read_text())["positions"] == {}
    assert not list(tmp_path.glob("*.tmp"))
    store.close()


def test_failed_export_preserves_target_and_other_sessions_temporary_file(tmp_path):
    store = StateStore(str(tmp_path / "export.db"))
    engine = live_engine(store, RiskManager())
    assert engine._export_state()
    before = Path(engine.state_file).read_bytes()
    unrelated = tmp_path / "export.json.other-session.tmp"
    unrelated.write_text("owned by another writer")
    with patch("live_trading.state_export.os.replace", side_effect=OSError("disk unavailable")):
        assert not engine._export_state()
    assert Path(engine.state_file).read_bytes() == before
    assert unrelated.read_text() == "owned by another writer"
    assert list(tmp_path.glob("*.tmp")) == [unrelated]
    store.close()


def test_multiple_processes_export_complete_snapshots(tmp_path):
    script = (
        "import sys; from core.state_store_v2 import StateStore; from core.risk import RiskManager; "
        "from tests.test_roadmap_admission_recovery import live_engine; "
        "store=StateStore(':memory:'); engine=live_engine(store,RiskManager()); "
        "engine.state_file=sys.argv[1]; "
        "results=[engine._export_state() for _ in range(10)]; "
        "store.close(); sys.exit(0 if all(results) else 1)"
    )
    target = tmp_path / "shared.json"
    processes = [subprocess.Popen([sys.executable, "-c", script, str(target)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for _ in range(3)]
    outputs = [(process, process.communicate(timeout=30)) for process in processes]
    assert all(process.returncode == 0 for process, _ in outputs), outputs
    assert json.loads(target.read_text())["schema_version"] == 1
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("payload,legacy", [
    (CashEvent("USDT", Decimal("100")), "core.ledger:CashEvent"),
    (MarkPriceEvent("BTC/USDT", Decimal("100"), "USDT", NOW), "core.ledger:MarkPriceEvent"),
    (OrderEvent("old-order", "accepted", 1, 0, 1), "core.events:OrderEvent"),
    (OrderEvent("split-order", "accepted", 1, 0, 1), "core.event_types:OrderEvent"),
])
def test_legacy_sqlite_events_decode_retry_idempotently_without_rewrite(tmp_path, payload, legacy):
    event = TradingEventPipeline(run_id="migration", clock=lambda: NOW).publish(
        payload, event_type="legacy", occurred_at=NOW, source="test", account_id="primary", idempotency_key="same")
    document = EventCodec.encode(event)
    raw = json.loads(document)
    raw["payload"]["class"] = legacy
    old_document = json.dumps(raw)
    path = str(tmp_path / "legacy.db")
    store = SQLiteEventStore(path)
    store._connection.execute("INSERT INTO events(event_id,event_type,run_id,account_id,occurred_at,document) VALUES(?,?,?,?,?,?)",
                              (str(event.event_id), event.event_type, event.run_id, event.account_id, event.occurred_at.isoformat(), old_document))
    store._connection.commit()
    store.close()
    store = SQLiteEventStore(path)
    assert store.read()[0] == event
    assert not store.append(replace(event, observed_at=NOW + timedelta(seconds=3)))
    assert store._connection.execute("SELECT document FROM events").fetchone()[0] == old_document
    with pytest.raises(ValueError, match="Idempotency conflict"):
        store.append(replace(event, payload={"amount": "changed"}))
    store.close()


def test_unknown_wire_class_is_not_dynamically_imported():
    with pytest.raises(ValueError, match="Unregistered"):
        _decode_value({"__qt_type__": "dataclass", "class": "os:system", "fields": {}})


def test_documented_reconciliation_command_runs_real_offline_cli(tmp_path):
    external = tmp_path / "external.json"
    external.write_text(json.dumps({"cash": {"USDT": "0"}, "positions": {}}))
    result = subprocess.run([sys.executable, "-m", "research.audit.reconciliation_job",
        "--ledger-db", str(tmp_path / "ledger.db"), "--account-id", "primary", "--base-currency", "USDT",
        "--external-state", str(external), "--output-dir", str(tmp_path / "reports")],
        capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["ok"] and json.loads(Path(report["report"]).read_text())["discrepancy_count"] == 0
