"""B6 (ADR 0030): reconciliation and export state is owned by ``Recovery`` and ``StateExport``; the engine keeps the names.

The golden rows were recorded on the code BEFORE the state moved: after every step of the scripted run the seven
state attributes (including "not bound yet" for the one that callers probe with ``getattr``) must be identical, and
every old attribute on ``LiveTradingEngine`` must still read and write the same value.
"""
from __future__ import annotations

import datetime
from datetime import timedelta

from tests import test_account_runtime_gate as _gate

NOW = _gate.NOW
make_engine = _gate.make_engine
account = _gate.account  # the fixture, re-exported so pytest finds it in this module
configure = _gate.configure

_ABSENT = "<absent>"
NAMES = ("_unresolved_unknown_cache", "_account_reconciliation_persistence_failed", "_last_reconciliation_at",
         "_last_order_sync_at", "_last_export_tick", "_last_exported_critical_state")


def _snap(engine, step):
    status = engine._reconciliation_status
    row = {name: getattr(engine, name, _ABSENT) for name in NAMES}
    row["_last_exported_critical_state"] = repr(row["_last_exported_critical_state"])
    row.update(step=step, status_keys=sorted(status), status_ok=status.get("ok"), status_allows=status.get("allows_new_risk"),
               status_checked=status.get("checked_count"), status_last_run=status.get("last_run_at"))
    return row


def _run(account, tmp_path):
    broker, _, data = account
    configure(broker, data, tmp_path)
    engine, state = make_engine(broker, tmp_path)
    try:
        rows = [_snap(engine, "fresh")]
        engine._run_reconciliation_if_due(NOW)
        rows.append(_snap(engine, "reconciled"))
        engine._run_reconciliation_if_due(NOW + timedelta(seconds=30))
        rows.append(_snap(engine, "not due"))
        engine._tick_count = 3
        engine._maybe_export_state(force=True)
        rows.append(_snap(engine, "exported"))
        engine._persist_account_reconciliation = lambda report, now: (_ for _ in ()).throw(OSError("disk"))
        engine._run_reconciliation_if_due(NOW + timedelta(days=1), force=True)
        rows.append(_snap(engine, "persistence failed"))
        return rows
    finally:
        state.close()


GOLDEN = [{'_account_reconciliation_persistence_failed': '<absent>',
  '_last_export_tick': None,
  '_last_exported_critical_state': 'None',
  '_last_order_sync_at': None,
  '_last_reconciliation_at': None,
  '_unresolved_unknown_cache': None,
  'status_allows': None,
  'status_checked': 0,
  'status_keys': ['checked_count', 'discrepancy_count', 'last_run_at', 'ok'],
  'status_last_run': None,
  'status_ok': None,
  'step': 'fresh'},
 {'_account_reconciliation_persistence_failed': False,
  '_last_export_tick': None,
  '_last_exported_critical_state': 'None',
  '_last_order_sync_at': datetime.datetime(2026, 9, 20, 12, 0, tzinfo=datetime.timezone.utc),
  '_last_reconciliation_at': datetime.datetime(2026, 9, 20, 12, 0, tzinfo=datetime.timezone.utc),
  '_unresolved_unknown_cache': False,
  'status_allows': False,
  'status_checked': 0,
  'status_keys': ['account_entry_gate',
                  'account_reconciliation',
                  'allows_new_risk',
                  'checked_count',
                  'discrepancy_count',
                  'last_run_at',
                  'ok',
                  'schema_version',
                  'scope'],
  'status_last_run': '2026-09-20T12:00:00+00:00',
  'status_ok': True,
  'step': 'reconciled'},
 {'_account_reconciliation_persistence_failed': False,
  '_last_export_tick': None,
  '_last_exported_critical_state': 'None',
  '_last_order_sync_at': datetime.datetime(2026, 9, 20, 12, 0, tzinfo=datetime.timezone.utc),
  '_last_reconciliation_at': datetime.datetime(2026, 9, 20, 12, 0, tzinfo=datetime.timezone.utc),
  '_unresolved_unknown_cache': False,
  'status_allows': False,
  'status_checked': 0,
  'status_keys': ['account_entry_gate',
                  'account_reconciliation',
                  'allows_new_risk',
                  'checked_count',
                  'discrepancy_count',
                  'last_run_at',
                  'ok',
                  'schema_version',
                  'scope'],
  'status_last_run': '2026-09-20T12:00:00+00:00',
  'status_ok': True,
  'step': 'not due'},
 {'_account_reconciliation_persistence_failed': False,
  '_last_export_tick': 3,
  '_last_exported_critical_state': "(False, 'STARTING', False, (), (), 'normal', None, None, False, False, "
                                   "'independent_account_facts_unverified')",
  '_last_order_sync_at': datetime.datetime(2026, 9, 20, 12, 0, tzinfo=datetime.timezone.utc),
  '_last_reconciliation_at': datetime.datetime(2026, 9, 20, 12, 0, tzinfo=datetime.timezone.utc),
  '_unresolved_unknown_cache': False,
  'status_allows': False,
  'status_checked': 0,
  'status_keys': ['account_entry_gate',
                  'account_reconciliation',
                  'allows_new_risk',
                  'checked_count',
                  'discrepancy_count',
                  'last_run_at',
                  'ok',
                  'schema_version',
                  'scope'],
  'status_last_run': '2026-09-20T12:00:00+00:00',
  'status_ok': True,
  'step': 'exported'},
 {'_account_reconciliation_persistence_failed': True,
  '_last_export_tick': 3,
  '_last_exported_critical_state': "(False, 'STARTING', False, (), (), 'normal', None, None, False, False, "
                                   "'independent_account_facts_unverified')",
  '_last_order_sync_at': datetime.datetime(2026, 9, 21, 12, 0, tzinfo=datetime.timezone.utc),
  '_last_reconciliation_at': datetime.datetime(2026, 9, 21, 12, 0, tzinfo=datetime.timezone.utc),
  '_unresolved_unknown_cache': False,
  'status_allows': False,
  'status_checked': 0,
  'status_keys': ['account_entry_gate',
                  'account_reconciliation',
                  'allows_new_risk',
                  'checked_count',
                  'discrepancy_count',
                  'last_run_at',
                  'ok',
                  'schema_version',
                  'scope'],
  'status_last_run': '2026-09-21T12:00:00+00:00',
  'status_ok': False,
  'step': 'persistence failed'}]


def test_reconciliation_and_export_golden_run(account, tmp_path):
    assert _run(account, tmp_path) == GOLDEN


def test_the_old_names_read_and_write_through_the_engine(account, tmp_path):
    broker, _, _ = account
    engine, state = make_engine(broker, tmp_path)
    try:
        assert engine._last_reconciliation_at is None and engine._last_order_sync_at is None
        assert engine._last_export_tick is None and engine._last_exported_critical_state is None
        assert engine._unresolved_unknown_cache is None
        assert getattr(engine, "_account_reconciliation_persistence_failed", None) is None
        status = {"ok": True}
        for name, value in (("_unresolved_unknown_cache", True), ("_account_reconciliation_persistence_failed", True),
                            ("_last_reconciliation_at", NOW), ("_reconciliation_status", status), ("_last_order_sync_at", NOW),
                            ("_last_export_tick", 4), ("_last_exported_critical_state", ("x",))):
            setattr(engine, name, value)
            assert getattr(engine, name) is value
    finally:
        state.close()
