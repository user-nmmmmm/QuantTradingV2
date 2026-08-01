import json
import os
import sqlite3
from threading import RLock
from typing import Any, Dict, Optional


class OrderStore:
    """Persistent idempotency and order-status store."""

    def __init__(self, path: str):
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self._lock = RLock()
        self._connection = sqlite3.connect(path, check_same_thread=False)
        with self._connection:
            self._connection.execute(
                """CREATE TABLE IF NOT EXISTS orders (
                    client_order_id TEXT PRIMARY KEY,
                    exchange_order_id TEXT,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    requested_qty REAL NOT NULL,
                    filled_qty REAL NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )

    def get(self, client_order_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._connection.execute(
                """SELECT client_order_id, exchange_order_id, symbol, side,
                          requested_qty, filled_qty, status, payload, updated_at
                   FROM orders WHERE client_order_id = ?""",
                (client_order_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "client_order_id": row[0], "exchange_order_id": row[1],
            "symbol": row[2], "side": row[3], "requested_qty": row[4],
            "filled_qty": row[5], "status": row[6],
            "payload": json.loads(row[7]), "updated_at": row[8],
        }

    def create(self, record: Dict[str, Any]) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute(
                """INSERT OR IGNORE INTO orders
                   (client_order_id, exchange_order_id, symbol, side, requested_qty,
                    filled_qty, status, payload, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record["client_order_id"], record.get("exchange_order_id"),
                    record["symbol"], record["side"], record["requested_qty"],
                    record.get("filled_qty", 0.0), record["status"],
                    json.dumps(record.get("payload", {}), default=str),
                    record["updated_at"],
                ),
            )
        return cursor.rowcount == 1

    def update(self, client_order_id: str, **changes: Any) -> Dict[str, Any]:
        record = self.get(client_order_id)
        if record is None:
            raise KeyError(client_order_id)
        record.update(changes)
        with self._lock, self._connection:
            self._connection.execute(
                """UPDATE orders SET exchange_order_id=?, filled_qty=?, status=?,
                   payload=?, updated_at=? WHERE client_order_id=?""",
                (
                    record.get("exchange_order_id"), record.get("filled_qty", 0.0),
                    record["status"], json.dumps(record.get("payload", {}), default=str),
                    record["updated_at"], client_order_id,
                ),
            )
        return record

