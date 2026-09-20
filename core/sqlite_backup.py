"""Rotating SQLite snapshots and manual atomic restore command."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from uuid import uuid4

from core.sqlite_utils import DatabaseIntegrityError


def validate_database(path: str | Path, *, expected_identity=None) -> None:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if result is None or str(result[0]).lower() != "ok":
            raise DatabaseIntegrityError(f"invalid SQLite snapshot: {path}")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if expected_identity is not None:
            row = connection.execute("SELECT identity FROM runtime_identity WHERE id=1").fetchone() if "runtime_identity" in tables else None
            expected = getattr(expected_identity, "canonical", expected_identity)
            if row is None or row[0] != expected:
                raise DatabaseIntegrityError("snapshot account identity mismatch or missing")
        if "schema_metadata" in tables:
            supported = {"order_store": 2, "state_store": 1, "persistent_risk_guard": 1, "event_store": 1}
            for component, version in connection.execute("SELECT component, version FROM schema_metadata"):
                if component in supported and not 0 < version <= supported[component]:
                    raise DatabaseIntegrityError("snapshot schema is incompatible")
    finally:
        connection.close()


class SQLiteSnapshotManager:
    def __init__(
        self,
        source_path: str,
        *,
        snapshot_dir: Optional[str] = None,
        retention: int = 24,
        interval_seconds: float = 3600,
    ) -> None:
        if retention <= 0:
            raise ValueError("retention must be positive")
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.source_path = Path(source_path)
        self.snapshot_dir = Path(
            snapshot_dir or f"{source_path}.snapshots"
        )
        self.retention = retention
        self.interval_seconds = interval_seconds
        self._last_snapshot_monotonic: Optional[float] = None

    def create_snapshot(self, *, now: Optional[datetime] = None) -> Path:
        if not self.source_path.exists():
            raise FileNotFoundError(self.source_path)
        timestamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        stem = self.source_path.name
        name = f"{stem}.{timestamp.strftime('%Y%m%dT%H%M%S%fZ')}.sqlite3"
        destination = self.snapshot_dir / name
        source = sqlite3.connect(str(self.source_path))
        target = sqlite3.connect(str(destination))
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        validate_database(destination)
        self._prune()
        return destination

    def run_if_due(
        self, *, monotonic_now: Optional[float] = None,
    ) -> Optional[Path]:
        current = time.monotonic() if monotonic_now is None else monotonic_now
        if (
            self._last_snapshot_monotonic is not None
            and current - self._last_snapshot_monotonic < self.interval_seconds
        ):
            return None
        snapshot = self.create_snapshot()
        self._last_snapshot_monotonic = current
        return snapshot

    def snapshots(self) -> list[Path]:
        pattern = f"{self.source_path.name}.*.sqlite3"
        return sorted(self.snapshot_dir.glob(pattern), key=lambda item: item.name)

    def _prune(self) -> None:
        snapshots = self.snapshots()
        for obsolete in snapshots[:-self.retention]:
            obsolete.unlink()


def restore_snapshot(snapshot_path: str, target_path: str, *, expected_identity=None) -> Path:
    """Restore a validated snapshot using an atomic target replacement."""
    snapshot = Path(snapshot_path)
    target = Path(target_path)
    corrupt_target = False
    if target.is_file():
        current = sqlite3.connect(target.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            if current.execute("SELECT 1 FROM sqlite_master WHERE name='runtime_identity'").fetchone():
                target_identity = current.execute("SELECT identity FROM runtime_identity WHERE id=1").fetchone()[0]
                if expected_identity is not None and getattr(expected_identity, "canonical", expected_identity) != target_identity:
                    raise ValueError("Target identity differs from requested restore identity")
                expected_identity = target_identity
        except sqlite3.DatabaseError as exc:
            # A damaged target cannot attest its account identity. Recovery is
            # permitted only against an independently supplied identity, and
            # the damaged bytes are preserved before any atomic replacement.
            if expected_identity is None:
                raise DatabaseIntegrityError(
                    "corrupt target requires an explicit expected runtime identity"
                ) from exc
            corrupt_target = True
        finally:
            current.close()
    validate_database(snapshot, expected_identity=expected_identity)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{uuid4().hex}.restore")
    try:
        source = sqlite3.connect(str(snapshot))
        restored = sqlite3.connect(str(temporary))
        try:
            source.backup(restored)
        finally:
            restored.close()
            source.close()
        validate_database(temporary, expected_identity=expected_identity)
        if corrupt_target:
            evidence = target.with_name(f"{target.name}.corrupt.{uuid4().hex}")
            shutil.copy2(target, evidence)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{target}{suffix}")
                if sidecar.exists():
                    shutil.copy2(sidecar, Path(f"{evidence}{suffix}"))
        for attempt in range(5):
            try:
                os.replace(temporary, target)
                break
            except PermissionError:
                if os.name != "nt" or attempt == 4:
                    raise
                time.sleep(.01 * (attempt + 1))
    finally:
        if temporary.exists():
            temporary.unlink()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{target}{suffix}")
        if sidecar.exists():
            sidecar.unlink()
    validate_database(target)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="SQLite snapshot operations")
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup = subparsers.add_parser("backup")
    backup.add_argument("database")
    backup.add_argument("--snapshot-dir")
    backup.add_argument("--retention", type=int, default=24)
    restore = subparsers.add_parser("restore")
    restore.add_argument("snapshot")
    restore.add_argument("database")
    restore.add_argument("--identity-file", type=Path,
                         help="JSON exchange/environment/account/market_type identity; required for a corrupt target")
    args = parser.parse_args()
    if args.command == "backup":
        path = SQLiteSnapshotManager(
            args.database,
            snapshot_dir=args.snapshot_dir,
            retention=args.retention,
        ).create_snapshot()
    else:
        identity = None
        if args.identity_file is not None:
            from core.runtime_identity import RuntimeIdentity
            identity = RuntimeIdentity(**json.loads(args.identity_file.read_text(encoding="utf-8")))
        path = restore_snapshot(args.snapshot, args.database, expected_identity=identity)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
