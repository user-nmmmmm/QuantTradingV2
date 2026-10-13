"""SQLite snapshots refuse the wrong account, an incompatible schema and bad settings; restore keeps evidence (C5)."""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

from core.foundation.runtime_identity import RuntimeIdentity
from core.trading import sqlite_backup as backup
from core.trading.sqlite_utils import DatabaseIntegrityError

IDENTITY = RuntimeIdentity("binance", "sandbox", "acct-1", "spot")
OTHER = RuntimeIdentity("binance", "sandbox", "acct-2", "spot")


def _database(path, *, identity=None, schema=None, rows=1):
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE payload (n INTEGER)")
    connection.executemany("INSERT INTO payload VALUES(?)", [(n,) for n in range(rows)])
    if identity is not None:
        connection.execute("CREATE TABLE runtime_identity (id INTEGER PRIMARY KEY CHECK(id=1), identity TEXT NOT NULL)")
        connection.execute("INSERT INTO runtime_identity VALUES(1, ?)", (identity.canonical,))
    if schema is not None:
        connection.execute("CREATE TABLE schema_metadata (component TEXT, version INTEGER)")
        connection.executemany("INSERT INTO schema_metadata VALUES(?, ?)", schema)
    connection.commit()
    connection.close()
    return path


def _count(path):
    connection = sqlite3.connect(path)
    try:
        return connection.execute("SELECT COUNT(*) FROM payload").fetchone()[0]
    finally:
        connection.close()


# ---- validation ------------------------------------------------------------------------------------------------

def test_validation_requires_an_existing_readable_database(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup.validate_database(tmp_path / "missing.sqlite3")
    garbage = tmp_path / "garbage.sqlite3"
    garbage.write_bytes(b"this is not a database" * 100)
    with pytest.raises(sqlite3.DatabaseError):
        backup.validate_database(garbage)


def test_validation_checks_the_account_identity_when_one_is_expected(tmp_path):
    bound = _database(tmp_path / "bound.sqlite3", identity=IDENTITY)
    unbound = _database(tmp_path / "unbound.sqlite3")
    backup.validate_database(bound, expected_identity=IDENTITY)
    backup.validate_database(bound, expected_identity=IDENTITY.canonical)            # a canonical string works too
    for path, expected in ((bound, OTHER), (unbound, IDENTITY)):
        with pytest.raises(DatabaseIntegrityError, match="identity mismatch or missing"):
            backup.validate_database(path, expected_identity=expected)
    backup.validate_database(unbound)                                                   # no identity expected: fine


@pytest.mark.parametrize("component, version, ok", [
    ("order_store", 2, True), ("order_store", 3, False), ("state_store", 0, False),
    ("event_store", 1, True), ("unknown_component", 99, True)])
def test_validation_rejects_schema_versions_this_code_does_not_understand(tmp_path, component, version, ok):
    path = _database(tmp_path / "s.sqlite3", schema=[(component, version)])
    if ok:
        backup.validate_database(path)
    else:
        with pytest.raises(DatabaseIntegrityError, match="schema is incompatible"):
            backup.validate_database(path)


# ---- snapshot manager ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("kwargs, message", [({"retention": 0}, "retention"), ({"interval_seconds": 0}, "interval_seconds")])
def test_manager_rejects_non_positive_settings(tmp_path, kwargs, message):
    with pytest.raises(ValueError, match=message):
        backup.SQLiteSnapshotManager(str(tmp_path / "db.sqlite3"), **kwargs)


def test_snapshot_of_a_missing_source_fails_and_old_snapshots_are_pruned_to_the_retention(tmp_path):
    source = tmp_path / "db.sqlite3"
    manager = backup.SQLiteSnapshotManager(str(source), retention=2)
    with pytest.raises(FileNotFoundError):
        manager.create_snapshot()
    _database(source, rows=3)
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    made = [manager.create_snapshot(now=start + timedelta(hours=n)) for n in range(4)]
    kept = manager.snapshots()
    assert kept == made[-2:] and all(_count(path) == 3 for path in kept)
    assert not made[0].exists() and manager.snapshot_dir == tmp_path / "db.sqlite3.snapshots"


def test_run_if_due_waits_for_the_interval(tmp_path):
    source = _database(tmp_path / "db.sqlite3")
    manager = backup.SQLiteSnapshotManager(str(source), interval_seconds=100)
    first = manager.run_if_due(monotonic_now=1000.0)
    assert first is not None and first.exists()
    assert manager.run_if_due(monotonic_now=1050.0) is None
    assert manager.run_if_due(monotonic_now=1100.0) is not None


# ---- restore ---------------------------------------------------------------------------------------------------

def test_restore_replaces_the_target_atomically_and_clears_stale_wal_files(tmp_path):
    snapshot = _database(tmp_path / "snap.sqlite3", identity=IDENTITY, rows=5)
    target = _database(tmp_path / "live.sqlite3", identity=IDENTITY, rows=1)
    for suffix in ("-wal", "-shm"):
        (tmp_path / f"live.sqlite3{suffix}").write_bytes(b"stale")
    assert backup.restore_snapshot(str(snapshot), str(target)) == target
    assert _count(target) == 5
    assert not (tmp_path / "live.sqlite3-wal").exists() and not (tmp_path / "live.sqlite3-shm").exists()
    assert not list(tmp_path.glob(".*.restore"))


def test_restore_refuses_a_target_that_belongs_to_a_different_account(tmp_path):
    snapshot = _database(tmp_path / "snap.sqlite3", identity=IDENTITY, rows=5)
    target = _database(tmp_path / "live.sqlite3", identity=IDENTITY, rows=1)
    with pytest.raises(ValueError, match="Target identity differs"):
        backup.restore_snapshot(str(snapshot), str(target), expected_identity=OTHER)
    assert _count(target) == 1


def test_restore_refuses_a_snapshot_from_a_different_account_and_leaves_the_target_alone(tmp_path):
    snapshot = _database(tmp_path / "snap.sqlite3", identity=OTHER, rows=5)
    target = _database(tmp_path / "live.sqlite3", identity=IDENTITY, rows=1)
    with pytest.raises(DatabaseIntegrityError, match="identity mismatch"):
        backup.restore_snapshot(str(snapshot), str(target))
    assert _count(target) == 1


def test_a_corrupt_target_needs_an_explicit_identity_and_its_bytes_are_preserved(tmp_path):
    snapshot = _database(tmp_path / "snap.sqlite3", identity=IDENTITY, rows=5)
    target = tmp_path / "live.sqlite3"
    target.write_bytes(b"not a sqlite file at all" * 200)
    (tmp_path / "live.sqlite3-wal").write_bytes(b"wal bytes")
    with pytest.raises(DatabaseIntegrityError, match="explicit expected runtime identity"):
        backup.restore_snapshot(str(snapshot), str(target))
    assert target.read_bytes().startswith(b"not a sqlite")

    backup.restore_snapshot(str(snapshot), str(target), expected_identity=IDENTITY)
    assert _count(target) == 5
    evidence = sorted(tmp_path.glob("live.sqlite3.corrupt.*"))
    assert len(evidence) >= 2                                                          # the database, its WAL (and a -shm if SQLite made one)
    assert any(path.read_bytes().startswith(b"not a sqlite") for path in evidence)
    assert any(path.name.endswith("-wal") and path.read_bytes() == b"wal bytes" for path in evidence)


# ---- command line ----------------------------------------------------------------------------------------------

def test_command_line_backup_then_restore_with_an_identity_file(tmp_path, monkeypatch, capsys):
    source = _database(tmp_path / "db.sqlite3", identity=IDENTITY, rows=4)
    snapshots = tmp_path / "snaps"
    monkeypatch.setattr(sys, "argv", ["sqlite_backup", "backup", str(source), "--snapshot-dir", str(snapshots), "--retention", "3"])
    assert backup.main() == 0
    snapshot = capsys.readouterr().out.strip()
    assert snapshot.startswith(str(snapshots))

    identity_file = tmp_path / "identity.json"
    identity_file.write_text(json.dumps({"exchange": "binance", "environment": "sandbox", "account": "acct-1",
                                         "market_type": "spot"}), encoding="utf-8")
    restored = tmp_path / "restored.sqlite3"
    monkeypatch.setattr(sys, "argv", ["sqlite_backup", "restore", snapshot, str(restored), "--identity-file", str(identity_file)])
    assert backup.main() == 0
    assert capsys.readouterr().out.strip() == str(restored) and _count(restored) == 4
