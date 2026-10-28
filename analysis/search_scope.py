"""Finite local contracts backed by the existing ResearchEvidenceRun journal.

The checkpoint detects lost journal tails; it is not a second attempt log.
This module neither runs models nor certifies statistical support or admission.
"""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
from typing import Mapping
from uuid import uuid4

import pandas as pd

from analysis.research_evidence import ResearchEvidenceRun, source_identity
from core.foundation.reproducibility import canonical_json, sha256_bytes
from core.foundation.data_identity import EXACT_DATA_IDENTITY, LEGACY_DATA_IDENTITY, data_hashes


PROTOCOL_SCHEMA = "paper-research-protocol/v1"
_TERMINAL = {"completed", "failed", "interrupted", "abstained"}
_HASH = re.compile(r"[0-9a-f]{64}")


def _strict_json(value):
    """Copy JSON without canonical_json's intentional NaN-to-null coercion."""
    try:
        return json.loads(json.dumps(value, allow_nan=False, ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("finite JSON values required") from exc


def _load_json(text):
    def constant(value):
        raise ValueError(f"nonfinite JSON constant: {value}")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return _strict_json(json.loads(text, parse_constant=constant, object_pairs_hook=unique_object))


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name}: nonempty string required")


def _positive_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name}: positive integer required")


def _timestamp(value, name, *, date_allowed=False):
    """Validate ISO syntax while preserving exact nanoseconds for gate checks.

    datetime.fromisoformat is used for syntax only; its microsecond-limited
    value must never decide whether a final window or observation has matured.
    """
    _text(value, name)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        result = pd.Timestamp(value)
        if pd.isna(result):
            raise ValueError("timestamp required")
        if date_allowed and len(value) == 10 and result.tzinfo is None:
            result = result.tz_localize("UTC")
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError("timezone required")
        return result.tz_convert("UTC")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name}: ISO timestamp with timezone required") from exc


def _ids(value, name):
    if not isinstance(value, list) or not value:
        raise ValueError(f"{name}: nonempty ID list required")
    for item in value:
        _text(item, name)
    if len(set(value)) != len(value):
        raise ValueError(f"{name}: duplicate IDs")


_REQUIRED_PROTOCOL_FIELDS = {
    "schema",
    "study_id",
    "registered_at",
    "paper_refs",
    "source_manifest_sha256",
    "config_sha256",
    "data_manifest_sha256",
    "evaluation_kind",
    "information_policy",
    "candidate_ids",
    "benchmark_ids",
    "search_scope",
    "historical_global_search_complete",
    "primary_metric",
    "economic_or_precision_target",
    "training_selection_rule",
    "evaluation_windows",
    "cost_policy_id",
    "risk_policy_id",
    "resampling",
    "attempt_budget",
    "resource_budget",
    "stop_rules",
    "final_access_policy_id",
}


def _validate_protocol_windows(value, registered):
    windows = value["evaluation_windows"]
    if not isinstance(windows, list) or not windows:
        raise ValueError("evaluation_windows: nonempty window list required")
    window_ids = []
    for window in windows:
        if not isinstance(window, dict):
            raise ValueError("evaluation window object required")
        _text(window.get("id"), "window.id")
        _text(window.get("role"), "window.role")
        start = _timestamp(window.get("start"), "window.start", date_allowed=True)
        end = _timestamp(window.get("end"), "window.end", date_allowed=True)
        if start >= end:
            raise ValueError("evaluation window start must precede end")
        if value["evaluation_kind"] == "prospective" and start < registered:
            raise ValueError("prospective windows cannot precede registration")
        window_ids.append(window["id"])
        if window["role"] in {"final", "holdout"}:
            _text(value["final_access_policy_id"], "final_access_policy_id for final window")
    if len(set(window_ids)) != len(window_ids):
        raise ValueError("duplicate evaluation window ID")
    if value["final_access_policy_id"] is not None:
        _text(value["final_access_policy_id"], "final_access_policy_id")


def _validate_protocol_sampling_and_budget(value):
    resampling = value["resampling"]
    if not isinstance(resampling, dict):
        raise ValueError("resampling object required")
    if resampling.get("method") == "not_applicable":
        _text(resampling.get("reason"), "resampling.reason")
    else:
        if resampling.get("method") not in {"stationary", "circular", "moving_block"}:
            raise ValueError("unsupported resampling method")
        blocks = resampling.get("block_lengths")
        if not isinstance(blocks, list) or not blocks:
            raise ValueError("resampling block_lengths required")
        for block in blocks:
            _positive_int(block, "block length")
        if len(set(blocks)) != len(blocks):
            raise ValueError("duplicate block lengths")
        _positive_int(resampling.get("iterations"), "resampling.iterations")
        if isinstance(resampling.get("seed"), bool) or not isinstance(resampling.get("seed"), int):
            raise ValueError("resampling.seed: integer required")
    _positive_int(value["attempt_budget"], "attempt_budget")
    arms = list(dict.fromkeys(value["candidate_ids"] + value["benchmark_ids"]))
    if value["attempt_budget"] < len(arms):
        raise ValueError("attempt_budget cannot cover all declared arms")
    budget = value["resource_budget"]
    if not isinstance(budget, dict):
        raise ValueError("resource_budget object required")
    for key in ("wall_time_seconds", "memory_mb"):
        number = budget.get(key)
        if (
            isinstance(number, bool)
            or not isinstance(number, (int, float))
            or not math.isfinite(number)
            or number <= 0
        ):
            raise ValueError(f"resource_budget.{key}: positive finite number required")
    _positive_int(budget.get("max_workers"), "resource_budget.max_workers")
    _ids(value["stop_rules"], "stop_rules")


def validate_protocol(protocol: Mapping) -> dict:
    """Validate the v1 declaration and detach it from caller mutation."""
    if not isinstance(protocol, Mapping):
        raise ValueError("protocol object required")
    value = _strict_json(dict(protocol))
    missing = sorted(_REQUIRED_PROTOCOL_FIELDS - value.keys())
    if missing:
        raise ValueError(f"missing protocol fields: {', '.join(missing)}")
    if value["schema"] != PROTOCOL_SCHEMA:
        raise ValueError("unsupported protocol schema")
    if "data_identity_version" in value:
        if not isinstance(value["data_identity_version"], str) or value[
            "data_identity_version"
        ] not in {EXACT_DATA_IDENTITY, LEGACY_DATA_IDENTITY}:
            raise ValueError("unsupported data identity version")
        if (
            value["evaluation_kind"] in {"strict", "prospective"}
            and value["data_identity_version"] != EXACT_DATA_IDENTITY
        ):
            raise ValueError("strict/prospective data requires exact/v2 identity")
    for key in (
        "study_id",
        "primary_metric",
        "training_selection_rule",
        "cost_policy_id",
        "risk_policy_id",
    ):
        _text(value[key], key)
    registered = _timestamp(value["registered_at"], "registered_at")
    if registered > datetime.now(timezone.utc):
        raise ValueError("registered_at cannot be in the future")
    for key in ("source_manifest_sha256", "config_sha256", "data_manifest_sha256"):
        if not isinstance(value[key], str) or not _HASH.fullmatch(value[key]):
            raise ValueError(f"{key}: lowercase SHA256 required")
    for key in ("paper_refs", "candidate_ids", "benchmark_ids"):
        _ids(value[key], key)
    if value["search_scope"] != "current_local_family":
        raise ValueError("only current_local_family can be verified")
    if value["historical_global_search_complete"] is not False:
        raise ValueError("local registration cannot certify complete historical global search")
    if value["evaluation_kind"] not in {"retrospective", "strict", "prospective"}:
        raise ValueError("unsupported evaluation_kind")
    policy = value["information_policy"]
    if (
        not isinstance(policy, dict)
        or policy.get("mode") not in {"retrospective", "strict"}
        or policy.get("knowledge") not in {"local", "published"}
        or policy.get("unknown") not in {"exclude", "raise"}
    ):
        raise ValueError("invalid information_policy")
    if value["evaluation_kind"] in {"strict", "prospective"} and policy["mode"] != "strict":
        raise ValueError("strict/prospective evaluation requires strict information policy")
    target = value["economic_or_precision_target"]
    if not (isinstance(target, str) and target.strip() or isinstance(target, dict) and target):
        raise ValueError("economic_or_precision_target: explicit target and rationale required")
    if isinstance(target, dict):
        _text(target.get("rationale"), "economic_or_precision_target.rationale")
    _validate_protocol_windows(value, registered)
    _validate_protocol_sampling_and_budget(value)
    return value


def load_protocol(path_or_mapping) -> dict:
    """Read strict JSON or validate a mapping; old runner YAML is not accepted."""
    if isinstance(path_or_mapping, Mapping):
        return validate_protocol(path_or_mapping)
    return validate_protocol(_load_json(Path(path_or_mapping).read_text(encoding="utf-8")))


def data_manifest(data_map, *, version=EXACT_DATA_IDENTITY) -> dict:
    """Versioned research inputs; exact v2 preserves ordered nanosecond values."""
    return data_hashes(data_map, version=version)


def _manifest_digest(value):
    return sha256_bytes(canonical_json(value).encode("utf-8"))


class SearchScope:
    """Finite family, one append-only journal; every start consumes budget.

    Resumed active attempts become explicitly interrupted. Failed/abstained
    arms remain in the family; no return series are synthesized. Resource
    declarations must additionally be enforced by the calling execution runner.
    """

    @classmethod
    def register(cls, *, protocol, data_map, output, source_root, source_paths=None):
        protocol = load_protocol(protocol)
        protocol.setdefault("data_identity_version", EXACT_DATA_IDENTITY)
        protocol = validate_protocol(protocol)
        if isinstance(source_paths, (str, bytes, Path)):
            raise ValueError("source_paths must be a sequence of paths")
        source_paths = None if source_paths is None else tuple(source_paths)
        hashes = source_identity(source_root, paths=source_paths)
        input_hashes = data_manifest(data_map, version=protocol["data_identity_version"])
        cls._check_manifests(protocol, hashes, input_hashes)
        scope = cls()
        scope.protocol = protocol
        scope._data_map = data_map
        scope.run = ResearchEvidenceRun(
            candidates=list(dict.fromkeys(protocol["candidate_ids"] + protocol["benchmark_ids"])),
            parameters={"research_protocol": protocol},
            data_map=data_map,
            output=output,
            source_root=source_root,
            source_paths=source_paths,
            data_identity_version=protocol["data_identity_version"],
        )
        cls._check_manifests(
            protocol, scope.run.registration["source_hashes"], scope.run.registration["data_hashes"]
        )
        scope._checkpoint()
        return scope

    @staticmethod
    def _check_manifests(protocol, source_hashes, data_hashes):
        if _manifest_digest(source_hashes) != protocol["source_manifest_sha256"]:
            raise ValueError("protocol source manifest does not match local bytes")
        if _manifest_digest(data_hashes) != protocol["data_manifest_sha256"]:
            raise ValueError("protocol data manifest does not match supplied data")
        if protocol["config_sha256"] not in source_hashes.values():
            raise ValueError("protocol config must be a byte-verified file in the source closure")

    @property
    def output(self):
        return self.run.output

    @contextmanager
    def _writer(self):
        lock = self.output / ".search_scope_writer.lock"
        try:
            handle = lock.open("x", encoding="utf-8")
        except FileExistsError as exc:
            raise ValueError(
                "search scope writer already active; preserve stale locks for investigation"
            ) from exc
        try:
            with handle:
                handle.write(str(os.getpid()))
            yield
        finally:
            lock.unlink()

    def _checkpoint(self):
        journal = (self.output / "attempts.jsonl").read_bytes()
        state = {
            "schema": "research-journal-checkpoint/v1",
            "identity": self.run.identity,
            "event_count": len(self.run.events),
            "journal_bytes": len(journal),
            "journal_sha256": sha256_bytes(journal),
        }
        temporary = self.output / f".journal_state.{uuid4().hex}.tmp"
        try:
            temporary.write_text(canonical_json(state), encoding="utf-8")
            os.replace(temporary, self.output / "journal_state.json")
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _read(output):
        output = Path(output)
        registration = _load_json((output / "registration.json").read_text(encoding="utf-8"))
        if not isinstance(registration, dict):
            raise ValueError("registration object required")
        identity = _manifest_digest(registration)
        state = _load_json((output / "journal_state.json").read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("journal checkpoint object required")
        journal = (output / "attempts.jsonl").read_bytes()
        if (
            state.get("schema") != "research-journal-checkpoint/v1"
            or state.get("identity") != identity
            or state.get("journal_bytes") != len(journal)
            or state.get("journal_sha256") != sha256_bytes(journal)
        ):
            raise ValueError("journal checkpoint mismatch: truncated, altered or incomplete write")
        if not journal or not journal.endswith(b"\n"):
            raise ValueError("journal missing or unterminated")
        events = [_load_json(line) for line in journal.decode("utf-8").splitlines()]
        if state.get("event_count") != len(events):
            raise ValueError("journal event count mismatch")
        for index, event in enumerate(events):
            if (
                not isinstance(event, dict)
                or event.get("sequence") != index
                or event.get("identity") != identity
            ):
                raise ValueError("journal sequence or identity mismatch")
            _timestamp(event.get("recorded_at"), "event.recorded_at")
        return registration, identity, events

    @staticmethod
    def _states(protocol, events):
        arms = list(dict.fromkeys(protocol["candidate_ids"] + protocol["benchmark_ids"]))
        states, attempts, failed = {}, {arm: 0 for arm in arms}, []
        for event in events:
            if event.get("phase") != "run":
                if event.get("phase") != "identity":
                    raise ValueError("unknown journal phase")
                continue
            arm, status = event.get("candidate"), event.get("status")
            if arm not in attempts:
                raise ValueError("journal contains undeclared arm")
            previous = states.get(arm)
            valid = status == "planned" and previous is None
            if status == "started":
                valid = previous in {"planned", "failed", "interrupted"}
                attempts[arm] += 1
            elif status in {"completed", "failed", "interrupted"}:
                valid = previous == "started"
            elif status == "abstained":
                valid = previous in {"planned", "started"}
            if not valid:
                raise ValueError(f"invalid transition for {arm}: {previous} -> {status}")
            if status != "planned" and event.get("attempt") != attempts[arm]:
                raise ValueError("journal attempt number mismatch")
            if status in {"failed", "interrupted", "abstained"}:
                _text(event.get("reason"), "terminal reason")
            if status in {"failed", "interrupted"}:
                failed.append(deepcopy(event))
            states[arm] = status
        if set(states) != set(arms):
            raise ValueError("missing declared arm in journal")
        if sum(attempts.values()) > protocol["attempt_budget"]:
            raise ValueError("journal exceeds registered attempt budget")
        return states, attempts, failed

    def audit(self):
        """Re-read persisted bytes; completeness never means profitable/admissible."""
        result = {
            "schema": "research-search-scope-audit/v1",
            "status": "invalid",
            "local_family_complete": False,
            "results_complete": False,
            "historical_global_search_complete": False,
            "admission_eligible": False,
            "research": "not_evaluated",
            "errors": [],
            "resource_budget_enforcement": "declared; execution runner must enforce wall time, memory and workers",
        }
        try:
            registration, identity, events = self._read(self.output)
            if (
                identity != self.run.identity
                or registration != self.run.registration
                or events != self.run.events
            ):
                raise ValueError("persisted journal changed since this writer loaded it")
            protocol = validate_protocol(registration["parameters"]["research_protocol"])
            if protocol != self.protocol:
                raise ValueError("in-memory protocol changed")
            if registration["candidates"] != list(
                dict.fromkeys(protocol["candidate_ids"] + protocol["benchmark_ids"])
            ):
                raise ValueError("registered candidate set differs from protocol")
            hashes = source_identity(self.run._source_root, paths=self.run._source_paths)
            version = registration.get("data_identity_version", LEGACY_DATA_IDENTITY)
            if (
                protocol.get("data_identity_version", LEGACY_DATA_IDENTITY) != version
                or self.run._data_identity_version != version
            ):
                raise ValueError("registered data identity policy changed")
            current_data = data_manifest(self._data_map, version=version)
            self._check_manifests(protocol, hashes, current_data)
            if current_data != registration["data_hashes"]:
                raise ValueError("registered data changed")
            if hashes != registration["source_hashes"]:
                raise ValueError("registered source closure changed")
            states, attempts, failed = self._states(protocol, events)
            result.update(
                status="ok",
                identity=identity,
                data_identity_version=version,
                states=states,
                attempts=attempts,
                attempts_used=sum(attempts.values()),
                attempt_budget=protocol["attempt_budget"],
                local_family_complete=all(status in _TERMINAL for status in states.values()),
                results_complete=all(status == "completed" for status in states.values()),
                failed_or_interrupted_attempts=failed,
                pending_arms=[arm for arm, status in states.items() if status not in _TERMINAL],
            )
        except (OSError, ValueError, TypeError, KeyError) as exc:
            result["errors"].append(str(exc))
        return result

    def record(self, *, candidate, status, reason=None, **detail):
        """Append a validated state transition without resetting failed history."""
        if set(detail) & {"sequence", "recorded_at", "identity", "phase", "attempt"}:
            raise ValueError("journal identity and sequence fields are reserved")
        detail = _strict_json(detail)
        with self._writer():
            audit = self.audit()
            if audit["status"] != "ok":
                raise ValueError("; ".join(audit["errors"]))
            if candidate not in audit["states"]:
                raise ValueError("undeclared candidate or benchmark")
            event = {
                "candidate": candidate,
                "phase": "run",
                "status": status,
                "attempt": audit["attempts"][candidate] + int(status == "started"),
                **detail,
            }
            if reason is not None:
                event["reason"] = reason
            event = _strict_json(event)
            self._states(self.protocol, self.run.events + [event])
            row = self.run.record(**event)
            self._checkpoint()
            return deepcopy(row)

    @classmethod
    def resume(cls, *, output, data_map, source_root, source_paths=None):
        """Restore byte-consistent runs; active attempts become interrupted.

        Inputs are rehashed before writes. Incomplete journals or stale writer
        locks require investigation, rather than silent tail repair.
        """
        if (Path(output) / ".search_scope_writer.lock").exists():
            raise ValueError(
                "search scope writer already active; preserve stale locks for investigation"
            )
        registration, identity, events = cls._read(output)
        protocol = validate_protocol(registration["parameters"]["research_protocol"])
        recorded_paths = registration.get("source_scope", {}).get("paths")
        if source_paths is not None and list(source_paths) != recorded_paths:
            raise ValueError("resume cannot change the registered source closure")
        hashes = source_identity(source_root, paths=recorded_paths)
        version = registration.get("data_identity_version", LEGACY_DATA_IDENTITY)
        if protocol.get("data_identity_version", LEGACY_DATA_IDENTITY) != version:
            raise ValueError("registered data identity policy changed")
        restored_data = data_manifest(data_map, version=version)
        cls._check_manifests(protocol, hashes, restored_data)
        if hashes != registration["source_hashes"] or restored_data != registration["data_hashes"]:
            raise ValueError("resume source or data identity changed")
        cls._states(protocol, events)
        scope = cls()
        scope.protocol = protocol
        scope._data_map = data_map
        scope.run = ResearchEvidenceRun.__new__(ResearchEvidenceRun)
        scope.run.output = Path(output)
        scope.run.registration, scope.run.identity, scope.run.events = (
            registration,
            identity,
            events,
        )
        scope.run._source_root = Path(source_root).resolve()
        scope.run._source_paths = None if recorded_paths is None else tuple(recorded_paths)
        scope.run._verify_local_sources = True
        scope.run._data_identity_version = version
        audit = scope.audit()
        if audit["status"] != "ok":
            raise ValueError("; ".join(audit["errors"]))
        for candidate, status in audit["states"].items():
            if status == "started":
                scope.record(
                    candidate=candidate,
                    status="interrupted",
                    reason="unfinished attempt identified during recovery",
                )
        return scope
