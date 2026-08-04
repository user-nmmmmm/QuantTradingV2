"""Historical state store replaced by core.state_store_v2.StateStore.

This module is retained only for historical reference. Production code must use
the leased bar-claim semantics in ``core.state_store_v2``.
"""

import json
import os
import sqlite3
from threading import RLock
from typing import Any


class StateStore:
    """Legacy store without leased bar claims; do not use in production."""

    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self._lock = RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        with self._connection:
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            self._connection.execute(
                """CREATE TABLE IF NOT EXISTS processed_bars (
                    bar_key TEXT PRIMARY KEY,
                    processed_at TEXT NOT NULL
                )"""
            )

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM state WHERE key = ?", (key,)
            ).fetchone()
        return default if row is None else json.loads(row[0])

    def set(self, key: str, value: Any) -> None:
        payload = json.dumps(value, separators=(",", ":"), default=str)
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO state(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, payload),
            )

    def has_processed_bar(self, bar_key: str) -> bool:
        with self._lock:
            row = self._connection.execute(
                "SELECT 1 FROM processed_bars WHERE bar_key = ?", (bar_key,)
            ).fetchone()
        return row is not None

    def mark_processed_bar(self, bar_key: str, processed_at: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT OR IGNORE INTO processed_bars(bar_key, processed_at) VALUES(?, ?)",
                (bar_key, processed_at),
            )

    def close(self) -> None:
        with self._lock:
            self._connection.close()


def default_state_db_path(state_file: str) -> str:
    root, _ = os.path.splitext(state_file)
    return f"{root}_state.db"
