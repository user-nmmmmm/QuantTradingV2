"""Account-scoped persistence; an unidentified legacy database is never adopted."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path


@dataclass(frozen=True)
class RuntimeIdentity:
    exchange: str
    environment: str
    account: str
    market_type: str

    def __post_init__(self):
        if self.environment not in {"sandbox", "live"} or not all(asdict(self).values()):
            raise ValueError("complete exchange/environment/account/market identity is required")

    @property
    def canonical(self):
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @property
    def key(self):
        return hashlib.sha256(self.canonical.encode()).hexdigest()[:24]

    @property
    def directory(self):
        return Path("reports/runtime") / self.key


def bind_database_identity(connection, identity):
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "runtime_identity" not in tables:
        for name in ("orders", "state", "daily_risk", "events"):
            if name in tables and connection.execute(f"SELECT 1 FROM {name} LIMIT 1").fetchone():
                raise ValueError("unidentified legacy shared database requires explicit recovery")
        connection.execute("CREATE TABLE runtime_identity (id INTEGER PRIMARY KEY CHECK(id=1), identity TEXT NOT NULL)")
        connection.execute("INSERT INTO runtime_identity VALUES(1, ?)", (identity.canonical,))
    row = connection.execute("SELECT identity FROM runtime_identity WHERE id=1").fetchone()
    if row is None or row[0] != identity.canonical:
        raise ValueError("runtime account identity mismatch")
