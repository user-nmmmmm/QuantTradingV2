from __future__ import annotations

import json
import os
import sqlite3
from threading import RLock
from typing import Any, Dict, List, Optional

from core.domain import FillRecord, OrderIntent, OrderStatus
from core.orders import TERMINAL_STATUSES, validate_transition
from core.sqlite_backup import SQLiteSnapshotManager
from core.sqlite_utils import ensure_schema_version, open_durable_connection


class OrderStoreClosedError(RuntimeError):
    """Raised when an operation is attempted after the ledger is closed."""


class _ClosedConnection:
    def execute(self, *_args, **_kwargs):
        raise OrderStoreClosedError("OrderStore connection is closed")

    def close(self) -> None:
        return None

    def __enter__(self):
        raise OrderStoreClosedError("OrderStore connection is closed")

    def __exit__(self, *_args):
        return False


class OrderStore:
    """Authoritative, restart-safe order and fill ledger."""

    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self._lock = RLock()
        self._connection = open_durable_connection(path)
        self._connection.row_factory = sqlite3.Row
        with self._connection:
            ensure_schema_version(self._connection, "order_store", 1)
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

            self._connection.execute(
                '''CREATE TABLE IF NOT EXISTS operator_order_resolutions (
                    resolution_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    client_order_id TEXT NOT NULL,
                    previous_status TEXT NOT NULL,
                    resolution TEXT NOT NULL,
                    confirmed_by TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    resolved_at TEXT NOT NULL,
                    FOREIGN KEY(client_order_id) REFERENCES orders(client_order_id)
                )'''
            )
            self._connection.execute(
                '''CREATE INDEX IF NOT EXISTS idx_order_resolutions_client
                   ON operator_order_resolutions(client_order_id, resolved_at)'''
            )
        self._snapshot_manager = (
            None if path == ":memory:" else SQLiteSnapshotManager(path)
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

    def list_with_fills(self) -> List[Dict[str, Any]]:
        """Return every durable order with an authoritative fill quantity.

        Post-fill controls must survive a process restart, so they cannot rely
        on the in-memory event pipeline or only inspect non-terminal orders.
        """

        with self._lock:
            rows = self._connection.execute(
                """SELECT * FROM orders
                   WHERE filled_qty > 0
                   ORDER BY updated_at, client_order_id"""
            ).fetchall()
        return [self._decode_order(row) for row in rows]

    def mark_submission_attempted(self, client_order_id: str, now: str) -> None:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE orders SET submission_attempted=1, updated_at=? WHERE client_order_id=?",
                (now, client_order_id),
            )

    def resolve_as_unsubmitted(
        self,
        client_order_id: str,
        *,
        confirmed_by: str,
        reason: str,
        now: str,
    ) -> Dict[str, Any]:
        '''Durably terminalize an order confirmed never to have reached venue.'''
        confirmed_by = str(confirmed_by).strip()
        reason = str(reason).strip()
        if not confirmed_by:
            raise ValueError('confirmed_by is required')
        if not reason:
            raise ValueError('reason is required')

        with self._lock, self._connection:
            row = self._connection.execute(
                'SELECT * FROM orders WHERE client_order_id=?', (client_order_id,)
            ).fetchone()
            if row is None:
                raise KeyError(client_order_id)
            current = self._decode_order(row)
            current_status = OrderStatus(current['status'])
            if current_status not in {OrderStatus.SUBMITTING, OrderStatus.UNKNOWN}:
                raise ValueError(
                    'only SUBMITTING or UNKNOWN orders can be resolved as unsubmitted'
                )
            if current.get('exchange_order_id'):
                raise ValueError(
                    'order has an exchange_order_id and cannot be resolved as unsubmitted'
                )
            has_fills = self._connection.execute(
                'SELECT 1 FROM fills WHERE client_order_id=? LIMIT 1',
                (client_order_id,),
            ).fetchone() is not None
            if float(current.get('filled_qty') or 0.0) > 0 or has_fills:
                raise ValueError('order has fills and cannot be resolved as unsubmitted')

            validate_transition(current['status'], OrderStatus.EXPIRED_UNSUBMITTED)
            message = f'confirmed unsubmitted by {confirmed_by}: {reason}'
            self._connection.execute(
                '''INSERT INTO operator_order_resolutions
                   (client_order_id, previous_status, resolution, confirmed_by,
                    reason, resolved_at)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (
                    client_order_id,
                    current['status'],
                    OrderStatus.EXPIRED_UNSUBMITTED.value,
                    confirmed_by,
                    reason,
                    now,
                ),
            )
            self._connection.execute(
                '''UPDATE orders
                   SET status=?, error_code=?, error_message=?, updated_at=?
                   WHERE client_order_id=?''',
                (
                    OrderStatus.EXPIRED_UNSUBMITTED.value,
                    'safety_policy',
                    message,
                    now,
                    client_order_id,
                ),
            )

        resolved = self.get(client_order_id)
        if resolved is None:
            raise RuntimeError('resolved order disappeared from durable store')
        return resolved

    def resolutions_for(self, client_order_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                '''SELECT * FROM operator_order_resolutions
                   WHERE client_order_id=? ORDER BY resolved_at, resolution_id''',
                (client_order_id,),
            ).fetchall()
        return [dict(row) for row in rows]

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

    def snapshot_if_due(self):
        if self._snapshot_manager is None:
            return None
        return self._snapshot_manager.run_if_due()

    def close(self) -> None:
        with self._lock:
            connection = getattr(self, "_connection", None)
            if connection is not None and not isinstance(connection, _ClosedConnection):
                connection.close()
                self._connection = _ClosedConnection()

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
