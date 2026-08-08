from __future__ import annotations

import json
import os
import sqlite3
from threading import RLock
from typing import Any, Dict, List, Optional

from core.db_utils import ensure_sqlite_integrity
from core.domain import FillRecord, OrderIntent, OrderStatus
from core.orders import TERMINAL_STATUSES, validate_transition


class OrderStore:
    """Authoritative, restart-safe order and fill ledger."""

    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self._lock = RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        with self._connection:
            self._connection.execute("PRAGMA busy_timeout=5000")
            if path != ":memory:":
                self._connection.execute("PRAGMA journal_mode=WAL")
                ensure_sqlite_integrity(self._connection, path)
            self._connection.execute(
                """CREATE TABLE IF NOT EXISTS orders (
                    client_order_id TEXT PRIMARY KEY,
                    exchange_order_id TEXT,
                    exchange TEXT NOT NULL DEFAULT '',
                    account TEXT NOT NULL DEFAULT '',
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    order_type TEXT NOT NULL DEFAULT 'market',
                    price REAL,
                    requested_qty REAL NOT NULL,
                    filled_qty REAL NOT NULL DEFAULT 0,
                    remaining_qty REAL NOT NULL DEFAULT 0,
                    average_fill_price REAL,
                    status TEXT NOT NULL,
                    error_code TEXT NOT NULL DEFAULT 'none',
                    error_message TEXT,
                    submission_attempted INTEGER NOT NULL DEFAULT 0,
                    intent TEXT NOT NULL DEFAULT '{}',
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            self._migrate_orders()
            self._connection.execute(
                """CREATE TABLE IF NOT EXISTS fills (
                    fill_id TEXT PRIMARY KEY,
                    client_order_id TEXT NOT NULL,
                    exchange_order_id TEXT,
                    qty REAL NOT NULL,
                    price REAL NOT NULL,
                    fee REAL NOT NULL DEFAULT 0,
                    fee_currency TEXT,
                    timestamp TEXT,
                    payload TEXT NOT NULL,
                    FOREIGN KEY(client_order_id) REFERENCES orders(client_order_id)
                )"""
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)"
            )
            self._connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_fills_client ON fills(client_order_id)"
            )

    def _migrate_orders(self) -> None:
        existing = {
            row[1] for row in self._connection.execute("PRAGMA table_info(orders)").fetchall()
        }
        additions = {
            "exchange": "TEXT NOT NULL DEFAULT ''",
            "account": "TEXT NOT NULL DEFAULT ''",
            "order_type": "TEXT NOT NULL DEFAULT 'market'",
            "price": "REAL",
            "remaining_qty": "REAL NOT NULL DEFAULT 0",
            "average_fill_price": "REAL",
            "error_code": "TEXT NOT NULL DEFAULT 'none'",
            "error_message": "TEXT",
            "submission_attempted": "INTEGER NOT NULL DEFAULT 0",
            "intent": "TEXT NOT NULL DEFAULT '{}'",
        }
        for name, declaration in additions.items():
            if name not in existing:
                self._connection.execute(
                    f"ALTER TABLE orders ADD COLUMN {name} {declaration}"
                )

    def create_intent(self, intent: OrderIntent, now: str) -> bool:
        record = {
            "client_order_id": intent.client_order_id,
            "exchange_order_id": None,
            "exchange": intent.exchange,
            "account": intent.account,
            "symbol": intent.symbol,
            "side": intent.action,
            "order_type": intent.order_type,
            "price": intent.price,
            "requested_qty": intent.requested_qty,
            "filled_qty": 0.0,
            "remaining_qty": intent.requested_qty,
            "average_fill_price": None,
            "status": OrderStatus.SUBMITTING.value,
            "error_code": "none",
            "error_message": None,
            "submission_attempted": False,
            "intent": intent.__dict__,
            "payload": {},
            "updated_at": now,
        }
        return self.create(record)

    def create(self, record: Dict[str, Any]) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """INSERT OR IGNORE INTO orders
                   (client_order_id, exchange_order_id, exchange, account, symbol,
                    side, order_type, price, requested_qty, filled_qty, remaining_qty,
                    average_fill_price, status, error_code, error_message,
                    submission_attempted, intent, payload, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record["client_order_id"], record.get("exchange_order_id"),
                    record.get("exchange", ""), record.get("account", ""),
                    record["symbol"], record["side"], record.get("order_type", "market"),
                    record.get("price"), record["requested_qty"],
                    record.get("filled_qty", 0.0),
                    record.get("remaining_qty", record["requested_qty"]),
                    record.get("average_fill_price"), record["status"],
                    record.get("error_code", "none"), record.get("error_message"),
                    int(bool(record.get("submission_attempted", False))),
                    json.dumps(record.get("intent", {}), default=str),
                    json.dumps(record.get("payload", {}), default=str),
                    record["updated_at"],
                ),
            )
        return cursor.rowcount == 1

    def get(self, client_order_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM orders WHERE client_order_id=?", (client_order_id,)
            ).fetchone()
        return None if row is None else self._decode_order(row)

    def find_by_exchange_id(self, exchange_order_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM orders WHERE exchange_order_id=?", (exchange_order_id,)
            ).fetchone()
        return None if row is None else self._decode_order(row)

    def list_non_terminal(self) -> List[Dict[str, Any]]:
        terminal = tuple(status.value for status in TERMINAL_STATUSES)
        placeholders = ",".join("?" for _ in terminal)
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM orders WHERE status NOT IN ({placeholders}) ORDER BY updated_at",
                terminal,
            ).fetchall()
        return [self._decode_order(row) for row in rows]

    def mark_submission_attempted(self, client_order_id: str, now: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE orders SET submission_attempted=1, updated_at=? WHERE client_order_id=?",
                (now, client_order_id),
            )

    def transition(
        self, client_order_id: str, status: OrderStatus, now: str, **changes: Any
    ) -> Dict[str, Any]:
        current = self.get(client_order_id)
        if current is None:
            raise KeyError(client_order_id)
        validate_transition(current["status"], status)
        changes["status"] = status.value
        changes["updated_at"] = now
        return self.update(client_order_id, **changes)

    def update(self, client_order_id: str, **changes: Any) -> Dict[str, Any]:
        record = self.get(client_order_id)
        if record is None:
            raise KeyError(client_order_id)
        record.update(changes)
        with self._lock, self._connection:
            self._connection.execute(
                """UPDATE orders SET exchange_order_id=?, filled_qty=?, remaining_qty=?,
                   average_fill_price=?, status=?, error_code=?, error_message=?,
                   submission_attempted=?, payload=?, updated_at=?
                   WHERE client_order_id=?""",
                (
                    record.get("exchange_order_id"), record.get("filled_qty", 0.0),
                    record.get("remaining_qty", 0.0), record.get("average_fill_price"),
                    record["status"], record.get("error_code", "none"),
                    record.get("error_message"),
                    int(bool(record.get("submission_attempted", False))),
                    json.dumps(record.get("payload", {}), default=str),
                    record["updated_at"], client_order_id,
                ),
            )
        return record

    def add_fill(self, fill: FillRecord) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """INSERT OR IGNORE INTO fills
                   (fill_id, client_order_id, exchange_order_id, qty, price, fee,
                    fee_currency, timestamp, payload)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    fill.fill_id, fill.client_order_id, fill.exchange_order_id,
                    fill.qty, fill.price, fill.fee, fill.fee_currency,
                    fill.timestamp, json.dumps(fill.payload, default=str),
                ),
            )
        return cursor.rowcount == 1

    def fills_for(self, client_order_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM fills WHERE client_order_id=? ORDER BY timestamp, fill_id",
                (client_order_id,),
            ).fetchall()
        return [self._decode_fill(row) for row in rows]

    def close(self) -> None:
        with self._lock:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
                self._connection = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    @staticmethod
    def _decode_order(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result["submission_attempted"] = bool(result["submission_attempted"])
        result["intent"] = json.loads(result.get("intent") or "{}")
        result["payload"] = json.loads(result.get("payload") or "{}")
        return result

    @staticmethod
    def _decode_fill(row: sqlite3.Row) -> Dict[str, Any]:
        result = dict(row)
        result["payload"] = json.loads(result.get("payload") or "{}")
        return result
