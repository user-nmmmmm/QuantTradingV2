"""Fail-closed SQLite connection setup for live state stores."""

from __future__ import annotations

import sqlite3


class DatabaseIntegrityError(RuntimeError):
    """Raised when a live-state database fails SQLite integrity validation."""


class DatabaseSchemaError(RuntimeError):
    """Raised when a live-state database schema is newer than this runtime."""


def ensure_schema_version(
    connection: sqlite3.Connection, component: str, version: int,
) -> None:
    if version <= 0:
        raise ValueError("schema version must be positive")
    connection.execute(
        """CREATE TABLE IF NOT EXISTS schema_metadata (
            component TEXT PRIMARY KEY,
            version INTEGER NOT NULL
        )"""
    )
    row = connection.execute(
        "SELECT version FROM schema_metadata WHERE component=?", (component,)
    ).fetchone()
    if row is not None and int(row[0]) > version:
        raise DatabaseSchemaError(
            f"{component} schema version {row[0]} is newer than supported {version}"
        )
    connection.execute(
        "INSERT INTO schema_metadata(component, version) VALUES(?, ?) "
        "ON CONFLICT(component) DO UPDATE SET version=excluded.version",
        (component, version),
    )


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
