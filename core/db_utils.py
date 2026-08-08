"""Shared SQLite hardening helpers for durable stores.

Concurrent readers/writers and crash recovery are handled by WAL mode plus a
busy timeout; a corrupted file must fail closed at startup rather than
silently serving partial or garbage rows.
"""

from __future__ import annotations

import sqlite3


class DatabaseIntegrityError(RuntimeError):
    """Raised when a SQLite file fails its startup integrity check."""


def ensure_sqlite_integrity(connection: sqlite3.Connection, path: str) -> None:
    rows = connection.execute("PRAGMA integrity_check").fetchall()
    results = [str(row[0]) for row in rows]
    if results != ["ok"]:
        raise DatabaseIntegrityError(
            f"SQLite integrity check failed for {path}: {'; '.join(results)}"
        )
