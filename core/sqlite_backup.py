"""Rotating SQLite snapshots and manual atomic restore command."""

from __future__ import annotations

import argparse
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.sqlite_utils import DatabaseIntegrityError


def validate_database(path: str | Path) -> None:
    connection = sqlite3.connect(str(path))
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if result is None or str(result[0]).lower() != "ok":
            raise DatabaseIntegrityError(f"invalid SQLite snapshot: {path}")
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


def restore_snapshot(snapshot_path: str, target_path: str) -> Path:
    """Restore a validated snapshot using an atomic target replacement."""
    snapshot = Path(snapshot_path)
    target = Path(target_path)
    validate_database(snapshot)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.restore")
    if temporary.exists():
        temporary.unlink()
    source = sqlite3.connect(str(snapshot))
    restored = sqlite3.connect(str(temporary))
    try:
        source.backup(restored)
    finally:
        restored.close()
        source.close()
    validate_database(temporary)
    os.replace(temporary, target)
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
    args = parser.parse_args()
    if args.command == "backup":
        path = SQLiteSnapshotManager(
            args.database,
            snapshot_dir=args.snapshot_dir,
            retention=args.retention,
        ).create_snapshot()
    else:
        path = restore_snapshot(args.snapshot, args.database)
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
