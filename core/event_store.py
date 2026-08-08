from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from threading import RLock
from typing import Iterable, Optional, Tuple
from uuid import UUID

from core.db_utils import ensure_sqlite_integrity
from core.events import EventCodec, EventEnvelope


@dataclass(frozen=True)
class StoredEvent:
    sequence: int
    event: EventEnvelope


class SQLiteEventStore:
    """Durable append-only event store with deterministic idempotency.

    Rows can only be inserted. SQLite triggers reject UPDATE and DELETE even if
    a caller obtains the underlying connection accidentally.
    """

    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._connection:
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA busy_timeout=5000")
            if path != ":memory:":
                self._connection.execute("PRAGMA journal_mode=WAL")
                self._connection.execute("PRAGMA synchronous=FULL")
                ensure_sqlite_integrity(self._connection, path)
            self._connection.execute(
                """CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    account_id TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    document TEXT NOT NULL
                )"""
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_account_sequence "
                "ON events(account_id, sequence)"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_events_type_sequence "
                "ON events(event_type, sequence)"
            )
            self._connection.execute(
                """CREATE TRIGGER IF NOT EXISTS events_no_update
                   BEFORE UPDATE ON events
                   BEGIN SELECT RAISE(ABORT, 'event store is append-only'); END"""
            )
            self._connection.execute(
                """CREATE TRIGGER IF NOT EXISTS events_no_delete
                   BEFORE DELETE ON events
                   BEGIN SELECT RAISE(ABORT, 'event store is append-only'); END"""
            )

    def append(self, event: EventEnvelope) -> bool:
        return self.append_many((event,)) == 1

    def append_many(self, events: Iterable[EventEnvelope]) -> int:
        batch = tuple(events)
        if any(not isinstance(event, EventEnvelope) for event in batch):
            raise TypeError("event store accepts only EventEnvelope values")
        inserted = 0
        with self._lock, self._connection:
            for event in batch:
                document = EventCodec.encode(event)
                existing = self._connection.execute(
                    "SELECT document FROM events WHERE event_id=?",
                    (str(event.event_id),),
                ).fetchone()
                if existing is not None:
                    if not self._same_business_event(existing["document"], document):
                        raise ValueError(
                            f"Idempotency conflict for event_id={event.event_id}"
                        )
                    continue
                self._connection.execute(
                    """INSERT INTO events
                       (event_id, event_type, run_id, account_id, occurred_at, document)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        str(event.event_id),
                        event.event_type,
                        event.run_id,
                        event.account_id,
                        event.occurred_at.isoformat(),
                        document,
                    ),
                )
                inserted += 1
        return inserted

    def read(
        self,
        *,
        after_sequence: int = 0,
        through_sequence: Optional[int] = None,
        account_id: Optional[str] = None,
        run_id: Optional[str] = None,
        event_type: Optional[str] = None,
    ) -> Tuple[EventEnvelope, ...]:
        return tuple(
            item.event
            for item in self.read_records(
                after_sequence=after_sequence,
                through_sequence=through_sequence,
                account_id=account_id,
                run_id=run_id,
                event_type=event_type,
            )
        )

    read_all = read

    def read_records(
        self,
        *,
        after_sequence: int = 0,
        through_sequence: Optional[int] = None,
        account_id: Optional[str] = None,
        run_id: Optional[str] = None,
        event_type: Optional[str] = None,
    ) -> Tuple[StoredEvent, ...]:
        if after_sequence < 0:
            raise ValueError("after_sequence cannot be negative")
        clauses = ["sequence > ?"]
        values = [after_sequence]
        if through_sequence is not None:
            if through_sequence < after_sequence:
                return ()
            clauses.append("sequence <= ?")
            values.append(through_sequence)
        for column, value in (
            ("account_id", account_id),
            ("run_id", run_id),
            ("event_type", event_type),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                values.append(value)
        query = (
            "SELECT sequence, document FROM events WHERE "
            + " AND ".join(clauses)
            + " ORDER BY sequence"
        )
        with self._lock:
            rows = self._connection.execute(query, tuple(values)).fetchall()
        return tuple(
            StoredEvent(int(row["sequence"]), EventCodec.decode(row["document"]))
            for row in rows
        )

    @property
    def last_sequence(self) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) AS value FROM events"
            ).fetchone()
        return int(row["value"])

    def get(self, event_id: UUID | str) -> Optional[StoredEvent]:
        with self._lock:
            row = self._connection.execute(
                "SELECT sequence, document FROM events WHERE event_id=?",
                (str(event_id),),
            ).fetchone()
        if row is None:
            return None
        return StoredEvent(int(row["sequence"]), EventCodec.decode(row["document"]))

    def close(self) -> None:
        with self._lock:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
                self._connection = None

    def __enter__(self) -> "SQLiteEventStore":
        return self

    def __exit__(self, *_args) -> None:
        self.close()

    @staticmethod
    def _same_business_event(left: str, right: str) -> bool:
        left_doc = json.loads(left)
        right_doc = json.loads(right)
        left_doc["observed_at"] = right_doc.get("observed_at")
        return left_doc == right_doc


class InMemoryEventStore(SQLiteEventStore):
    def __init__(self):
        super().__init__(":memory:")


__all__ = ["StoredEvent", "SQLiteEventStore", "InMemoryEventStore"]
