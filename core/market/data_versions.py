"""Small immutable research store with explicit publication and observation time.

An immutable snapshot proves which bytes were used, not that the bytes were
available historically. Imported files remain point-in-time unknown. Records
with historical availability must carry explicit provenance. ``local`` queries
model this collector's actual knowledge; ``published`` queries model documented
source publication and may include records collected later.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Mapping


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_SCHEMA = "research_data_snapshot/v1"


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _time(value, name: str, *, optional=False) -> str | None:
    if value is None:
        if optional:
            return None
        raise ValueError(f"{name} is required")
    if not isinstance(value, (str, datetime)):
        raise ValueError(f"{name} requires an explicit ISO timestamp")
    if re.search(r"[.,]\d{7,}", str(value)):
        raise ValueError(f"{name} exceeds supported microsecond precision")
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid {name}") from exc
    if stamp.tzinfo is None:
        raise ValueError(f"{name} requires an explicit timezone")
    return stamp.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _name(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _resolved(path: Path) -> Path:
    value = str(path.resolve())
    # Windows may retain its extended-path prefix when resolve races with an
    # atomic publication. Normalize equivalent absolute spellings before the
    # containment check; do not normalize or accept any user path components.
    if os.name == "nt" and value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif os.name == "nt" and value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


def _record(raw: Mapping) -> dict:
    result = dict(raw)
    for key in ("record_id", "revision_id"):
        result[key] = _name(result.get(key), key)
    for key in ("event_time", "observed_at"):
        result[key] = _time(result.get(key), key)
    for key in ("available_at", "published_at", "revision_at"):
        result[key] = _time(result.get(key), key, optional=True)
    if "data" not in result or not isinstance(result["data"], Mapping):
        raise ValueError("record data must be a mapping")
    if result.get("record_type", "observation") not in {"observation", "tombstone"}:
        raise ValueError("record_type must be observation or tombstone")
    if result.get("record_type") == "tombstone":
        if result["data"] or result["available_at"] is None:
            raise ValueError("tombstone requires empty data and documented availability")
        _name(result.get("retraction_reason"), "retraction_reason")
    evidence = result.get("availability_evidence")
    if result["available_at"] is None:
        if evidence or result.get("availability_status") not in {None, "unknown"}:
            raise ValueError("unknown available_at cannot claim historical availability")
        result["availability_status"] = "unknown"
        result["availability_evidence"] = None
    else:
        if result.get("availability_status") not in {None, "documented"}:
            raise ValueError("available_at requires documented availability_status")
        if (not isinstance(evidence, Mapping)
                or evidence.get("kind") not in {"source_publication", "collector_capture", "local_receipt"}
                or not isinstance(evidence.get("reference"), str)
                or not evidence["reference"].strip()):
            raise ValueError("available_at requires explicit historical availability_evidence")
        if evidence["kind"] in {"collector_capture", "local_receipt"} and result["available_at"] < result["observed_at"]:
            raise ValueError("collector capture cannot establish earlier historical availability")
        for key in ("published_at", "revision_at"):
            if result[key] is not None and result["available_at"] < result[key]:
                raise ValueError(f"available_at precedes {key}")
        result["availability_status"] = "documented"
        result["availability_evidence"] = dict(evidence)
    # Strict JSON is intentional: no NaN -> null coercion or arbitrary repr().
    return json.loads(_json_bytes(result))


class DataVersionStore:
    """Content addressed bytes, manifests, and immutable record revision claims.

    ``create_snapshot`` accepts records with record_id, revision_id, event_time,
    observed_at, available_at (nullable), and data. Reusing a revision identity
    for changed content is an error, including across distinct snapshots.
    Caller supplied source/config/code references participate in the snapshot
    hash. They should be exact hashes or frozen identities, not moving branches.
    Documentary references are caller assertions, not independent authentication
    of a provider's timestamps; use the separate input audit for research admission.
    """

    def __init__(self, root: str | Path):
        self.root = _resolved(Path(root))
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, category: str, digest: str) -> Path:
        if category not in {"objects", "snapshots", "revisions"} or not _DIGEST.fullmatch(digest):
            raise ValueError("invalid content identity")
        path = self.root / category / (digest + ".json" if category != "objects" else digest)
        resolved = _resolved(path)
        if not resolved.is_relative_to(self.root):
            # Keep exact paths in this internal failure: useful when a store was
            # moved or redirected while a reader/writer was active.
            raise ValueError(f"store path escapes root: {resolved!s} outside {self.root!s}")
        return path

    def _put(self, category: str, digest: str, payload: bytes) -> None:
        path = self._path(category, digest)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Publish only complete bytes. Windows rename refuses an existing target;
        # POSIX rename overwrites, so use an exclusive hard-link publication there.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".pending-", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                if os.name == "nt":
                    os.rename(temporary, path)
                else:
                    os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != payload:
                    raise ValueError("immutable content conflict or tampering detected")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _object(self, payload: bytes) -> dict:
        digest = _hash(payload)
        self._put("objects", digest, payload)
        return {"sha256": digest, "size": len(payload)}

    def _read_object(self, reference: Mapping) -> bytes:
        raw = self._path("objects", reference["sha256"]).read_bytes()
        if _hash(raw) != reference["sha256"] or len(raw) != reference["size"]:
            raise ValueError("object hash/size mismatch: tampering detected")
        return raw

    def _manifest(self, payload: dict) -> dict:
        raw = _json_bytes(payload)
        digest = _hash(raw)
        self._put("snapshots", digest, raw)
        return {"snapshot_id": digest, **payload}

    def create_snapshot(self, dataset_id: str, records, *, source_refs=None,
                        code_refs=None, config_refs=None, metadata=None) -> dict:
        dataset_id = _name(dataset_id, "dataset_id")
        normalized = [_record(value) for value in records]
        normalized.sort(key=lambda row: (row["record_id"], row["revision_id"]))
        seen = set()
        for record in normalized:
            key = (record["record_id"], record["revision_id"])
            if key in seen:
                raise ValueError("duplicate record/revision identity in snapshot")
            seen.add(key)
        # One record can have multiple revisions. Equal visibility timestamps
        # require explicit revision_at to avoid choosing via opaque revision IDs.
        ordering = set()
        for record in normalized:
            if record["available_at"] is not None:
                key = (record["record_id"], record["revision_at"] or record["available_at"])
                if key in ordering:
                    raise ValueError("ambiguous revision ordering; provide distinct revision_at")
                ordering.add(key)
        for record in normalized:
            claim = _hash(_json_bytes([dataset_id, record["record_id"], record["revision_id"]]))
            self._put("revisions", claim, _json_bytes(record))
        reference = self._object(_json_bytes(normalized))
        known = sum(record["available_at"] is not None for record in normalized)
        return self._manifest({
            "schema": _SCHEMA, "kind": "records", "dataset_id": dataset_id,
            "records": reference, "record_count": len(normalized),
            "availability": {"documented": known, "unknown": len(normalized) - known,
                             "point_in_time_complete": bool(normalized) and known == len(normalized)},
            "source_refs": dict(source_refs or {}), "code_refs": dict(code_refs or {}),
            "config_refs": dict(config_refs or {}), "metadata": dict(metadata or {}),
        })

    def freeze_files(self, dataset_id: str, files: Mapping[str, str | Path], *,
                     observed_at, source_refs=None, code_refs=None, config_refs=None,
                     metadata=None) -> dict:
        """Copy exact source bytes; import time never fabricates historical PIT.

        Logical file names must be leaves. No file is read through a manifest
        supplied path; read_file always resolves an object identity in this store.
        """
        frozen = {}
        for name, source in sorted(files.items()):
            if (not isinstance(name, str) or not name or name in {".", ".."}
                    or any(char in name for char in ("/", "\\", ":"))):
                raise ValueError("logical filename must not contain path components")
            frozen[name] = self._object(Path(source).read_bytes())
        if not frozen:
            raise ValueError("at least one input file is required")
        return self._manifest({
            "schema": _SCHEMA, "kind": "files", "dataset_id": _name(dataset_id, "dataset_id"),
            "observed_at": _time(observed_at, "observed_at"), "available_at": None,
            "files": frozen, "availability": {"status": "unknown", "point_in_time_complete": False},
            "source_refs": dict(source_refs or {}), "code_refs": dict(code_refs or {}),
            "config_refs": dict(config_refs or {}), "metadata": dict(metadata or {}),
        })

    def read_snapshot(self, snapshot_id: str) -> dict:
        raw = self._path("snapshots", snapshot_id).read_bytes()
        if _hash(raw) != snapshot_id:
            raise ValueError("snapshot manifest hash mismatch: tampering detected")
        manifest = json.loads(raw)
        if manifest.get("schema") != _SCHEMA:
            raise ValueError("unsupported snapshot schema")
        records = []
        if manifest["kind"] == "records":
            records = json.loads(self._read_object(manifest["records"]))
            if len(records) != manifest["record_count"]:
                raise ValueError("record count mismatch")
            for record in records:
                if _record(record) != record:
                    raise ValueError("invalid normalized record")
                claim = _hash(_json_bytes([manifest["dataset_id"], record["record_id"], record["revision_id"]]))
                if self._path("revisions", claim).read_bytes() != _json_bytes(record):
                    raise ValueError("revision claim mismatch: tampering detected")
        elif manifest["kind"] == "files":
            for reference in manifest["files"].values():
                self._read_object(reference)
        else:
            raise ValueError("unsupported snapshot kind")
        return {"manifest": {"snapshot_id": snapshot_id, **manifest}, "records": records}

    def verify_snapshot(self, snapshot_id: str) -> dict:
        return self.read_snapshot(snapshot_id)["manifest"]

    def read_file(self, snapshot_id: str, name: str) -> bytes:
        manifest = self.verify_snapshot(snapshot_id)
        if manifest["kind"] != "files":
            raise ValueError("snapshot does not contain raw files")
        return self._read_object(manifest["files"][name])

    def as_of(self, snapshot_id: str, at, *, knowledge="local", unknown="exclude",
              include_tombstones=False) -> list[dict]:
        """Latest documented revision visible at cutoff; no forward fills.

        ``published`` explicitly ignores local observation latency. It requires
        source-publication evidence for each selected record. Unknown availability
        is excluded or rejected; it is never inferred from event or import time.
        Event time must also be at/before cutoff. Boundary comparisons are inclusive.
        """
        cutoff = _time(at, "at")
        if knowledge not in {"local", "published"} or unknown not in {"exclude", "raise"}:
            raise ValueError("invalid as-of policy")
        snapshot = self.read_snapshot(snapshot_id)
        if snapshot["manifest"]["kind"] != "records":
            raise ValueError("raw file snapshot has unknown historical availability")
        latest = {}
        for record in snapshot["records"]:
            if record["event_time"] > cutoff:
                continue
            if record["available_at"] is None:
                if unknown == "raise":
                    raise ValueError("historical availability is unknown")
                continue
            if record["available_at"] > cutoff:
                continue
            if knowledge == "local" and record["observed_at"] > cutoff:
                continue
            if knowledge == "published" and record["availability_evidence"]["kind"] != "source_publication":
                continue
            key = record["record_id"]
            rank = record["revision_at"] or record["available_at"]
            if key not in latest or rank > (latest[key]["revision_at"] or latest[key]["available_at"]):
                latest[key] = record
        # Select latest first: excluding a tombstone before version selection
        # would resurrect a withdrawn old value.
        return [latest[key] for key in sorted(latest)
                if include_tombstones or latest[key].get("record_type") != "tombstone"]


def freeze_files(root, dataset_id, files, **kwargs) -> dict:
    """Convenience entrypoint matching DataVersionStore.freeze_files."""
    return DataVersionStore(root).freeze_files(dataset_id, files, **kwargs)
