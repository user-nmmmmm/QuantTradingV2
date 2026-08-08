"""Fail-closed SQLite connection setup for live state stores."""

from __future__ import annotations

import sqlite3


class DatabaseIntegrityError(RuntimeError):
    """Raised when a live-state database fails SQLite integrity validation."""


def open_durable_connection(
    path: str, *, busy_timeout_ms: int = 5000, check_same_thread: bool = False,
) -> sqlite3.Connection:
    connection = sqlite3.connect(
        path, timeout=busy_timeout_ms / 1000.0,
        check_same_thread=check_same_thread,
    )
    try:
        connection.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        row = connection.execute("PRAGMA integrity_check").fetchone()
        result = "" if row is None else str(row[0])
        if result.lower() != "ok":
            raise DatabaseIntegrityError(
                f"SQLite integrity check failed for {path}: {result or 'no result'}"
            )
        connection.execute("PRAGMA journal_mode=WAL").fetchone()
        return connection
    except Exception:
        connection.close()
        raise
