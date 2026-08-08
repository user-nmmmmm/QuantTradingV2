"""Transactional live-state and leased bar-claim store."""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from threading import RLock
from typing import Any

from core.db_utils import ensure_sqlite_integrity


class StateStore:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        with self._connection:
            self._connection.execute("PRAGMA busy_timeout=5000")
            if path != ":memory:":
                self._connection.execute("PRAGMA journal_mode=WAL")
                ensure_sqlite_integrity(self._connection, path)
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            self._connection.execute(
                """CREATE TABLE IF NOT EXISTS processed_bars (
                    bar_key TEXT PRIMARY KEY,
                    processed_at TEXT NOT NULL,
                    status TEXT NOT NULL
                )"""
            )

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._connection.execute(
                "SELECT value FROM state WHERE key=?", (key,)
            ).fetchone()
        return default if row is None else json.loads(row[0])

    def set(self, key: str, value: Any) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "INSERT INTO state(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, default=str)),
            )

    def claim_bar(self, bar_key: str, now: str, lease_seconds: float = 300.0) -> bool:
        """Claim a bar, reclaiming only stale processing claims."""
        with self._lock, self._connection:
            row = self._connection.execute(
                "SELECT processed_at, status FROM processed_bars WHERE bar_key=?",
                (bar_key,),
            ).fetchone()
            if row is None:
                self._connection.execute(
                    "INSERT INTO processed_bars VALUES(?,?,'processing')", (bar_key, now)
                )
                return True
            processed_at, status = row
            if status == "processed":
                return False
            if self._age_seconds(processed_at, now) < lease_seconds:
                return False
            self._connection.execute(
                "UPDATE processed_bars SET processed_at=?, status='processing' WHERE bar_key=?",
                (now, bar_key),
            )
            return True

    def complete_bar(self, bar_key: str, now: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE processed_bars SET processed_at=?, status='processed' WHERE bar_key=?",
                (now, bar_key),
            )

    def release_bar(self, bar_key: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "DELETE FROM processed_bars WHERE bar_key=? AND status='processing'",
                (bar_key,),
            )

    def status(self, bar_key: str) -> Any:
        with self._lock:
            row = self._connection.execute(
                "SELECT status FROM processed_bars WHERE bar_key=?", (bar_key,)
            ).fetchone()
        return None if row is None else row[0]

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    @staticmethod
    def _age_seconds(then: str, now: str) -> float:
        def parse(value: str) -> datetime:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        return max((parse(now) - parse(then)).total_seconds(), 0.0)


def default_state_db_path(state_file: str) -> str:
    return os.path.splitext(state_file)[0] + "_state.db"
