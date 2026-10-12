"""Best-effort observations, separate from the authoritative order ledger.

Telemetry failures must not change exchange results, exceptions or call counts.
"""
from __future__ import annotations

from collections import deque
from contextlib import closing
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3
from threading import RLock
import time
from uuid import uuid4


_CONTEXT_FIELDS = ("account_id", "exchange_id", "environment", "market_type", "client_order_id", "symbol",
                   "decision_at", "side", "reference_price", "requested_qty")
_FIELDS = frozenset((*_CONTEXT_FIELDS, "observation_id", "operation", "sent_at",
    "received_at", "duration_seconds", "outcome", "error_category",
    "exchange_order_id", "order_status", "exchange_timestamp_ms", "filled",
    "terminal_observed", "sequence"))


def order_observation_sidecar_path(order_store_path):
    """An in-memory order store deliberately has no durable sidecar."""
    if order_store_path is None or str(order_store_path) == ":memory:":
        return None
    path = Path(order_store_path)
    return str(path.with_name(path.name+".order_observations.sqlite3"))


def _safe_scalar(value):
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return value[:512]
    if isinstance(value, (int, float)) and math.isfinite(value):
        return value
    # Never serialize nested SDK payloads or call arbitrary repr/str methods.
    return None


def _whitelist(row):
    return {key: safe for key, value in row.items() if key in _FIELDS
            and (safe := _safe_scalar(value)) is not None}


def read_order_observations(path, *, after_sequence=0, limit=None, lock_timeout_seconds=.05):
    """Read all durable rows or a cursor page without creating a database.

    Rows have stable observation_id and increasing sidecar sequence. This
    analysis API raises corrupt/missing database errors to its caller.
    """
    if path is None or str(path) == ":memory:":
        return []
    if after_sequence < 0 or (limit is not None and limit < 1):
        raise ValueError("invalid sequence or limit")
    target = Path(path).resolve()
    with closing(sqlite3.connect(target.as_uri()+"?mode=ro", uri=True,
                         timeout=max(0., min(float(lock_timeout_seconds), 1.)))) as connection:
        query = "SELECT sequence, observation_id, payload FROM order_observations WHERE sequence > ? ORDER BY sequence"
        params = [int(after_sequence)]
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        result = []
        for sequence, observation_id, payload in connection.execute(query, params):
            row = json.loads(payload)
            if not isinstance(row, dict) or row.get("observation_id") != observation_id:
                raise ValueError("invalid observation identity/payload")
            result.append({**_whitelist(row), "sequence": sequence})
        return result


class OrderLatencyRecorder:
    def __init__(self, max_records=1000, *, clock=time.monotonic,
                 persistence_path=None, lock_timeout_seconds=.05):
        if not isinstance(max_records, int) or max_records < 1:
            raise ValueError("max_records must be a positive integer")
        if not math.isfinite(lock_timeout_seconds) or not 0 <= lock_timeout_seconds <= 1:
            raise ValueError("lock timeout must be finite and between 0 and 1 seconds")
        self.records = deque(maxlen=max_records)
        self.clock = clock
        self.total_records = 0
        self.persistence_path = None if persistence_path in (None, ":memory:") else str(persistence_path)
        self.lock_timeout_seconds = float(lock_timeout_seconds)
        self._lock = RLock()
        self._persisted_window_ids = set()
        self.persisted_records = 0
        self.unrecoverable_records = 0
        self._health = {"write_failures": 0, "restore_failures": 0,
                        "read_failures": 0, "telemetry_failures": 0, "last_error": None}
        if self.persistence_path:
            try:
                self._initialize_and_restore()
            except Exception as exc:
                self._failure("restore", exc)

    def _failure(self, phase, exc):
        key = phase+"_failures" if phase in {"write", "restore", "read"} else "telemetry_failures"
        self._health[key] += 1
        # Error text can contain credentials; record only the exception type.
        self._health["last_error"] = {"phase": phase, "category": type(exc).__name__,
                                      "observed_at": datetime.now(timezone.utc).isoformat()}

    def _connect(self):
        return sqlite3.connect(self.persistence_path, timeout=self.lock_timeout_seconds)

    def _initialize_and_restore(self):
        Path(self.persistence_path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""CREATE TABLE IF NOT EXISTS order_observations (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                observation_id TEXT NOT NULL UNIQUE,
                payload TEXT NOT NULL)""")
            self.persisted_records = int(connection.execute("SELECT COUNT(*) FROM order_observations").fetchone()[0])
            rows = connection.execute(
                "SELECT sequence, observation_id, payload FROM order_observations ORDER BY sequence DESC LIMIT ?",
                (self.records.maxlen,)).fetchall()
        self.total_records = self.persisted_records
        for sequence, observation_id, payload in reversed(rows):
            try:
                row = json.loads(payload)
                if not isinstance(row, dict) or row.get("observation_id") != observation_id:
                    raise ValueError("invalid observation identity/payload")
                row = {**_whitelist(row), "sequence": sequence}
                self.records.append(row)
                self._persisted_window_ids.add(observation_id)
            except Exception as exc:
                self._failure("restore", exc)

    def _persist(self, row):
        payload = json.dumps(_whitelist(row), allow_nan=False, separators=(",", ":"))
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "INSERT INTO order_observations (observation_id, payload) VALUES (?, ?)",
                (row["observation_id"], payload))
            sequence = int(cursor.lastrowid)
        return sequence

    def _record(self, row):
        with self._lock:
            if len(self.records) == self.records.maxlen:
                evicted = self.records[0]["observation_id"]
                if evicted not in self._persisted_window_ids:
                    self.unrecoverable_records += 1
                self._persisted_window_ids.discard(evicted)
            self.records.append(row)
            self.total_records += 1
            if self.persistence_path:
                try:
                    row["sequence"] = self._persist(row)
                    self.persisted_records += 1
                    self._persisted_window_ids.add(row["observation_id"])
                except Exception as exc:
                    self._failure("write", exc)

    def call(self, operation, function, *args, context=None, **kwargs):
        row = {"observation_id": uuid4().hex, "operation": _safe_scalar(operation),
               "sent_at": datetime.now(timezone.utc).isoformat()}
        try:
            for key in _CONTEXT_FIELDS:
                value = _safe_scalar((context or {}).get(key))
                if value is not None:
                    row[key] = value
        except Exception as exc:
            self._failure("telemetry", exc)
        try:
            sent = self.clock()
        except Exception as exc:
            self._failure("telemetry", exc)
            sent = None
        try:
            result = function(*args, **kwargs)
            row["outcome"] = "ack"
            try:
                if isinstance(result, dict):
                    for field, source in (("exchange_order_id", "id"), ("order_status", "status"),
                                          ("exchange_timestamp_ms", "timestamp"), ("filled", "filled")):
                        value = _safe_scalar(result.get(source))
                        if value is not None:
                            row[field] = value
                    row["terminal_observed"] = row.get("order_status") in {"closed", "canceled", "expired", "rejected"}
            except Exception as exc:
                self._failure("telemetry", exc)
            return result
        except Exception as exc:
            row["outcome"], row["error_category"] = "error", type(exc).__name__
            raise
        finally:
            # Capture time before SQLite. The guard also protects the exchange
            # result if other telemetry internals unexpectedly malfunction.
            try:
                row["received_at"] = datetime.now(timezone.utc).isoformat()
                try:
                    duration = self.clock()-sent if sent is not None else None
                    if duration is not None:
                        if not math.isfinite(duration) or duration < 0:
                            raise ValueError("invalid duration")
                        row["duration_seconds"] = duration
                except Exception as exc:
                    self._failure("telemetry", exc)
                self._record(row)
            except Exception as exc:
                self._failure("telemetry", exc)

    def export_records(self):
        """All durable rows plus retained observations whose writes failed."""
        with self._lock:
            rows = []
            if self.persistence_path:
                try:
                    rows = read_order_observations(self.persistence_path,
                        lock_timeout_seconds=self.lock_timeout_seconds)
                except Exception as exc:
                    self._failure("read", exc)
            seen = {row["observation_id"] for row in rows}
            rows.extend(dict(row) for row in self.records if row["observation_id"] not in seen)
            return rows

    def summary(self):
        with self._lock:
            rows = [dict(row) for row in self.records]
            groups = {}
            for row in rows:
                groups.setdefault(row.get("operation", "unknown"), []).append(row)
            result = {}
            for operation, samples in groups.items():
                values = sorted(row["duration_seconds"] for row in samples if "duration_seconds" in row)
                import numpy as np
                result[operation] = {"count": len(samples), "timed_count": len(values),
                    "errors": sum(row.get("outcome") == "error" for row in samples),
                    "p50_seconds": float(np.quantile(values, .5)) if values else None,
                    "p95_seconds": float(np.quantile(values, .95)) if values else None}
            return {"scope": "client request to response; terminal observation is not exchange matching time",
                "total_records": self.total_records, "window_records": len(rows),
                "window_evicted_records": max(0, self.total_records-len(rows)),
                "unrecoverable_records": self.unrecoverable_records,
                "dropped_records": self.unrecoverable_records,
                "dropped_records_semantics": "unpersisted observations evicted this process; window trimming alone is not loss",
                "statistics_scope": "retained recent window", "statistics": result, "records": rows,
                "persistence": {"enabled": bool(self.persistence_path), "path": self.persistence_path,
                    "persisted_records": self.persisted_records,
                    "unpersisted_window_records": sum(row["observation_id"] not in self._persisted_window_ids for row in rows),
                    "status": "degraded" if any(self._health[k] for k in self._health if k != "last_error") else
                              "enabled" if self.persistence_path else "memory_only",
                    "lock_timeout_seconds": self.lock_timeout_seconds,
                    "restart_scope": "persisted history plus this process; prior unpersisted failures cannot be recovered",
                    **self._health}}


def record_order_call(owner, operation, function, *args, telemetry_context=None, **kwargs):
    recorder = getattr(owner, "order_latency", None)
    context = {"account_id": getattr(owner, "account_id", None),
               "exchange_id": getattr(owner, "exchange_id", None),
               "environment": getattr(owner, "environment", None),
               "market_type": getattr(owner, "market_type", None), **(telemetry_context or {})}
    return (recorder.call(operation, function, *args, context=context, **kwargs)
            if isinstance(recorder, OrderLatencyRecorder) else function(*args, **kwargs))
