"""SQLite-backed research history, annotations and validated strategy presets."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
_METADATA = {"name", "tags", "notes", "favorite"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def valid_id(value: Any) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError("Invalid experiment identifier")
    return value


def _text(value: Any, name: str, maximum: int) -> str:
    if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{name} must contain at most {maximum} characters")
    return value.strip()


def validate_metadata(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or not payload or set(payload) - _METADATA:
        raise ValueError("Expected name, tags, notes or favorite")
    result = {}
    for key, value in payload.items():
        if key in {"name", "notes"}:
            result[key] = _text(value, key, 120 if key == "name" else 4000)
        elif key == "favorite":
            if not isinstance(value, bool):
                raise ValueError("favorite must be boolean")
            result[key] = value
        else:
            if not isinstance(value, list) or len(value) > 12:
                raise ValueError("tags must contain at most 12 strings")
            tags = [_text(item, "tag", 40) for item in value]
            if any(not tag for tag in tags):
                raise ValueError("Tags cannot be empty")
            result[key] = list(dict.fromkeys(tags))
    return result


class ExperimentStore:
    """Short locked transactions; no database connection crosses a thread."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS experiments (
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, payload TEXT NOT NULL,
                    name TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
                    tags TEXT NOT NULL DEFAULT '[]', favorite INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS experiment_order ON experiments(created_at DESC);
                CREATE INDEX IF NOT EXISTS experiment_filter ON experiments(status,kind,favorite);
                CREATE TABLE IF NOT EXISTS presets (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
                    strategy TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                PRAGMA user_version=1;
            """)

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _record(row: sqlite3.Row, *, logs: bool = True) -> dict[str, Any]:
        record = json.loads(row["payload"])
        record.update(id=row["id"], kind=row["kind"], status=row["status"],
                      created_at=row["created_at"], updated_at=row["updated_at"],
                      name=row["name"], notes=row["notes"], tags=json.loads(row["tags"]),
                      favorite=bool(row["favorite"]))
        if not logs:
            record.pop("logs", None)
        return record

    def save_job(self, job: dict[str, Any]) -> dict[str, Any]:
        """更新任务事实，保留用户单独编辑的名称、笔记、标签与收藏。

        私有运行字段不落盘，日志按展示上限截断。冲突更新只覆盖任务列，
        避免后台日志刷新覆盖用户刚保存的研究记录。
        """
        identifier = valid_id(job.get("id"))
        payload = {key: value for key, value in job.items() if not key.startswith("_") and key not in _METADATA}
        payload["logs"] = [str(line)[:2048] for line in list(payload.get("logs", []))[-160:]]
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 1024 * 1024:
            raise ValueError("Experiment record exceeds size limit")
        created = payload.get("created_at") or utc_now()
        updated = utc_now()
        with self._lock, self._connect() as connection:
            connection.execute("""INSERT INTO experiments(id,kind,status,created_at,updated_at,payload)
                VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                kind=excluded.kind,status=excluded.status,updated_at=excluded.updated_at,payload=excluded.payload""",
                (identifier, payload.get("kind", "backtest"), payload.get("status", "unknown"), created, updated, encoded))
        return self.get(identifier)

    def get(self, identifier: str) -> dict[str, Any]:
        valid_id(identifier)
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM experiments WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise FileNotFoundError("Experiment not found")
        return self._record(row)

    def register_report(self, run_id: str, parameters: dict | None = None,
                        modified_at: str | None = None) -> dict[str, Any]:
        """Caller must first validate the report via the existing report reader."""
        valid_id(run_id)
        created = modified_at or utc_now()
        payload = {"id": run_id, "run_id": run_id, "kind": "backtest", "status": "succeeded",
                   "parameters": parameters or {}, "created_at": created, "finished_at": created,
                   "logs": [], "imported": True}
        with self._lock, self._connect() as connection:
            connection.execute("""INSERT OR IGNORE INTO experiments(id,kind,status,created_at,updated_at,payload)
                VALUES(?,?,?,?,?,?)""", (run_id, "backtest", "succeeded", created, utc_now(),
                                         json.dumps(payload, ensure_ascii=False, allow_nan=False)))
        return self.get(run_id)

    def update_metadata(self, identifier: str, payload: Any) -> dict[str, Any]:
        valid_id(identifier)
        values = validate_metadata(payload)
        columns = []
        parameters = []
        for key, value in values.items():
            columns.append(key + "=?")
            parameters.append(json.dumps(value, ensure_ascii=False) if key == "tags" else int(value) if key == "favorite" else value)
        with self._lock, self._connect() as connection:
            cursor = connection.execute("UPDATE experiments SET " + ",".join(columns) + ",updated_at=? WHERE id=?",
                                        [*parameters, utc_now(), identifier])
            if cursor.rowcount == 0:
                raise FileNotFoundError("Experiment not found")
        return self.get(identifier)

    def search(self, query: str = "", status: str = "", favorite: bool | None = None,
               tag: str = "", kind: str = "", limit: int = 50, offset: int = 0) -> dict[str, Any]:
        query, status, tag, kind = (_text(value, name, maximum) for value, name, maximum in
                                    ((query, "query", 200), (status, "status", 40), (tag, "tag", 40), (kind, "kind", 40)))
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 100_000:
            raise ValueError("offset must be between 0 and 100000")
        if favorite is not None and not isinstance(favorite, bool):
            raise ValueError("favorite must be boolean")
        clauses, parameters = [], []
        if query:
            # instr treats user input literally, including SQL LIKE wildcards.
            clauses.append("(instr(lower(id),lower(?)) OR instr(lower(name),lower(?)) OR instr(lower(notes),lower(?)) OR instr(lower(payload),lower(?)))")
            parameters.extend([query] * 4)
        for key, value in (("status", status), ("kind", kind)):
            if value:
                clauses.append(key + "=?")
                parameters.append(value)
        if favorite is not None:
            clauses.append("favorite=?")
            parameters.append(int(favorite))
        if tag:
            clauses.append("EXISTS(SELECT 1 FROM json_each(experiments.tags) WHERE value=?)")
            parameters.append(tag)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock, self._connect() as connection:
            total = connection.execute("SELECT COUNT(*) FROM experiments" + where, parameters).fetchone()[0]
            rows = connection.execute("SELECT * FROM experiments" + where + " ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                                      [*parameters, limit, offset]).fetchall()
        return {"items": [self._record(row, logs=False) for row in rows], "total": total,
                "limit": limit, "offset": offset}

    def recent_jobs(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute("SELECT * FROM experiments WHERE json_extract(payload,'$.imported') IS NULL ORDER BY created_at DESC,id DESC LIMIT ?",
                                      (limit,)).fetchall()
        return [self._record(row) for row in reversed(rows)]

    def interrupt_active(self) -> int:
        """把上次服务遗留的活动记录标为 interrupted，返回更新条数。

        此方法只修正持久状态，不恢复或终止旧进程；调用方在启动时执行，
        防止把失去进程上下文的 queued/running 记录继续展示为活动任务。
        """
        with self._lock, self._connect() as connection:
            rows = connection.execute("SELECT * FROM experiments WHERE status IN ('queued','running')").fetchall()
            for row in rows:
                job = json.loads(row["payload"])
                now = utc_now()
                job.update(status="interrupted", finished_at=now, run_id=None,
                           error="Dashboard restarted while this job was active; it was not resumed")
                job["logs"] = [*job.get("logs", []), job["error"]][-160:]
                connection.execute("UPDATE experiments SET status='interrupted',updated_at=?,payload=? WHERE id=?",
                                   (now, json.dumps(job, ensure_ascii=False, allow_nan=False), row["id"]))
        return len(rows)

    def list_presets(self) -> list[dict[str, Any]]:
        with self._lock, self._connect() as connection:
            rows = connection.execute("SELECT * FROM presets ORDER BY updated_at DESC,id DESC LIMIT 100").fetchall()
        return [{**dict(row), "strategy": json.loads(row["strategy"])} for row in rows]

    def save_preset(self, *, identifier: str, name: str, description: str,
                    strategy: dict[str, Any]) -> dict[str, Any]:
        valid_id(identifier)
        name = _text(name, "name", 100)
        if not name:
            raise ValueError("Preset name is required")
        description = _text(description, "description", 1000)
        now = utc_now()
        with self._lock, self._connect() as connection:
            count = connection.execute("SELECT COUNT(*) FROM presets").fetchone()[0]
            existing = connection.execute("SELECT 1 FROM presets WHERE id=?", (identifier,)).fetchone()
            if count >= 100 and existing is None:
                raise ValueError("At most 100 presets may be saved")
            connection.execute("""INSERT INTO presets VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                name=excluded.name,description=excluded.description,strategy=excluded.strategy,updated_at=excluded.updated_at""",
                (identifier, name, description, json.dumps(strategy, ensure_ascii=False, allow_nan=False), now, now))
        return next(row for row in self.list_presets() if row["id"] == identifier)

    def delete_preset(self, identifier: str) -> None:
        valid_id(identifier)
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM presets WHERE id=?", (identifier,))
            if not cursor.rowcount:
                raise FileNotFoundError("Preset not found")
