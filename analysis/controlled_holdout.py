"""Atomic final-sample consumption and frozen prospective candidate contracts.

This controls reads through this API. It cannot stop a user reading a file
outside the API, prove external timestamps cryptographically, or grant trading.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from io import BytesIO
import importlib
import inspect
import struct
import sys
import types
import os
from pathlib import Path
import sqlite3
from typing import Callable, Mapping
from uuid import uuid4

import pandas as pd

from analysis.research_evidence import source_identity
from analysis.search_scope import _load_json, _strict_json, _timestamp
from core.foundation.reproducibility import canonical_json, sha256_bytes, sha256_file


def _digest(value):
    return sha256_bytes(canonical_json(value).encode("utf-8"))


def _now():
    return datetime.now(timezone.utc).isoformat()


def _alive(pid):
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ValueError("invalid stale lock owner")
    if os.name == "nt":
        import ctypes

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.restype = ctypes.c_void_p
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:
                return False
            raise PermissionError("cannot prove stale evaluation owner exited")
        try:
            code = ctypes.c_ulong()
            if not api.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)):
                raise PermissionError("cannot inspect evaluation owner")
            return code.value == 259
        finally:
            api.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


_CODE_IDENTITY_SCHEME = "python-executable-fields/v1"
_CODE_FIELDS = (
    "co_argcount", "co_posonlyargcount", "co_kwonlyargcount", "co_nlocals",
    "co_stacksize", "co_flags", "co_code", "co_consts", "co_names", "co_varnames",
    "co_freevars", "co_cellvars", "co_filename", "co_name", "co_qualname",
    "co_firstlineno", "co_linetable", "co_exceptiontable",
)


def _code_value(value):
    """Preserve executable values/types without marshal's reference flags."""
    if isinstance(value, types.CodeType):
        return {"type": "code", "fields": {
            name: _code_value(getattr(value, name)) for name in _CODE_FIELDS}}
    if value is None or value is Ellipsis:
        return {"type": "none" if value is None else "ellipsis"}
    if type(value) in {bool, int, str}:
        return {"type": type(value).__name__, "value": value}
    if type(value) in {float, complex}:
        parts = (value.real, value.imag) if type(value) is complex else (value,)
        return {"type": type(value).__name__,
                "bits": [struct.pack("!d", number).hex() for number in parts]}
    if type(value) is bytes:
        return {"type": "bytes", "value": value.hex()}
    if type(value) in {tuple, frozenset}:
        items = [_code_value(item) for item in value]
        if type(value) is frozenset:
            items.sort(key=canonical_json)
        return {"type": type(value).__name__, "items": items}
    raise PermissionError("unsupported evaluator executable constant type")


def _code_identity(code):
    """Scheme changes require a new registration; old bindings never upgrade."""
    return _digest({"scheme": _CODE_IDENTITY_SCHEME,
                    "runtime": sys.implementation.cache_tag, "code": _code_value(code)})


def _callback_binding(evaluator, source_root, source_hashes):
    """Verify import provenance plus executable/default/global-state identity.

    Closures and nested functions are refused. Referenced JSON global values
    are frozen too; modules are identified by name/version, while callable
    globals bind their bytecode and defaults in addition to source bytes.
    Code fields/constants use a versioned value identity: Python marshal also
    records object reference state, which can differ between identical processes.
    Existing marshal bindings remain inspectable but cannot consume under this
    scheme; they need an explicit new registration rather than a silent upgrade.
    """
    if (
        not inspect.isfunction(evaluator)
        or evaluator.__closure__
        or evaluator.__qualname__ != evaluator.__name__
    ):
        raise PermissionError("a stable importable top-level evaluator without closure is required")
    module = importlib.import_module(evaluator.__module__)
    if getattr(module, evaluator.__name__, None) is not evaluator:
        raise PermissionError("evaluator is not the actual imported function")
    path = Path(inspect.getsourcefile(evaluator) or "").resolve()
    root = Path(source_root).resolve()
    if not path.is_relative_to(root):
        raise PermissionError("evaluator source is outside registered closure")
    relative = path.relative_to(root).as_posix()
    if relative not in source_hashes or sha256_file(path) != source_hashes[relative]:
        raise PermissionError("evaluator source must be included in registered closure")
    if Path(evaluator.__code__.co_filename).resolve() != path:
        raise PermissionError("evaluator executable source origin differs")
    globals_bound = {}
    for name in evaluator.__code__.co_names:
        if name not in evaluator.__globals__:
            continue
        value = evaluator.__globals__[name]
        if isinstance(value, types.ModuleType):
            globals_bound[name] = {
                "module": value.__name__,
                "version": getattr(value, "__version__", None),
            }
        elif inspect.isfunction(value):
            if value.__closure__:
                raise PermissionError(
                    "referenced closure is not a verifiable frozen evaluator dependency"
                )
            dependency = Path(inspect.getsourcefile(value) or "").resolve()
            if (
                not dependency.is_relative_to(root)
                or dependency.relative_to(root).as_posix() not in source_hashes
            ):
                raise PermissionError("referenced evaluator helper is outside closure")
            globals_bound[name] = {
                "code": _code_identity(value.__code__),
                "defaults": _strict_json(value.__defaults__),
                "kwdefaults": _strict_json(value.__kwdefaults__),
            }
        else:
            globals_bound[name] = _strict_json(value)
    return {
        "module": evaluator.__module__,
        "qualname": evaluator.__qualname__,
        "source": relative,
        "source_sha256": source_hashes[relative],
        "code_identity_scheme": _CODE_IDENTITY_SCHEME,
        "code_sha256": _code_identity(evaluator.__code__),
        "defaults": _strict_json(evaluator.__defaults__),
        "kwdefaults": _strict_json(evaluator.__kwdefaults__),
        "referenced_globals": globals_bound,
    }


@dataclass(frozen=True)
class FinalEvaluationData:
    """Only holdout is scored; optional earlier feature-only warmup is separate."""

    holdout: pd.DataFrame
    warmup: pd.DataFrame
    evaluation_id: str
    protocol_hash: str


class AtomicHoldoutVault:
    """SQLite transactions serialize consumption; an exclusive lease spans reads.

    Same-ID recovery needs explicit ``resume=True`` and the old process must no
    longer own the lease. Completed or differently identified evaluations never
    reopen. A failed attempt remains consumed even if a same-ID recovery follows.
    """

    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def _db(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL, sha256 TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS access_log (sequence INTEGER PRIMARY KEY, payload TEXT NOT NULL, previous_sha256 TEXT, sha256 TEXT NOT NULL)"
            )
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _read(self, db):
        row = db.execute("SELECT payload,sha256 FROM state WHERE id=1").fetchone()
        if row is None:
            raise ValueError("holdout has not been registered")
        value = _load_json(row[0])
        if _digest(value) != row[1] or value.get("schema") != "controlled-holdout/v1":
            raise ValueError("holdout state identity mismatch")
        previous = None
        rows = db.execute(
            "SELECT sequence,payload,previous_sha256,sha256 FROM access_log ORDER BY sequence"
        ).fetchall()
        for expected, (sequence, payload, prev, digest) in enumerate(rows):
            if (
                sequence != expected
                or prev != previous
                or digest != _digest({"payload": _load_json(payload), "previous": prev})
            ):
                raise ValueError("holdout access log damaged")
            previous = digest
        if len(rows) != value.get("access_event_count") or previous != value.get(
            "access_log_sha256"
        ):
            raise ValueError("holdout access log truncated")
        return value

    def _store(self, db, value, event):
        value = _strict_json(value)
        previous = value.get("access_log_sha256")
        sequence = value.get("access_event_count", 0)
        event = {"sequence": sequence, "recorded_at": _now(), **event}
        event_hash = _digest({"payload": event, "previous": previous})
        db.execute(
            "INSERT INTO access_log VALUES(?,?,?,?)",
            (sequence, canonical_json(event), previous, event_hash),
        )
        value["access_event_count"] = sequence + 1
        value["access_log_sha256"] = event_hash
        db.execute(
            "INSERT OR REPLACE INTO state VALUES(1,?,?)", (canonical_json(value), _digest(value))
        )
        return value

    def register(
        self,
        protocol,
        *,
        data_path,
        source_root,
        source_paths,
        evaluator_id,
        not_before=None,
        label_columns=(),
        warmup_rows=0,
    ):
        if self.path.exists():
            with self._db() as db:
                if db.execute("SELECT 1 FROM state WHERE id=1").fetchone():
                    raise RuntimeError("holdout already registered; new version needs new store")
        source_root = Path(source_root).resolve()
        paths = tuple(source_paths)
        if not evaluator_id or ":" not in evaluator_id:
            raise ValueError("module:qualified_function evaluator identity required")
        if isinstance(warmup_rows, bool) or not isinstance(warmup_rows, int) or warmup_rows < 0:
            raise ValueError("nonnegative warmup_rows required")
        data_path = Path(data_path).resolve()
        not_before = not_before or protocol.holdout_end
        at = _timestamp(not_before, "not_before", date_allowed=True)
        final_end = _timestamp(protocol.holdout_end, "holdout_end", date_allowed=True)
        if at < final_end:
            raise ValueError("not_before cannot precede holdout end")
        value = {
            "schema": "controlled-holdout/v1",
            "status": "registered",
            "protocol": asdict(protocol),
            "protocol_hash": protocol.fingerprint,
            "registered_at": _now(),
            "data_path": str(data_path),
            "data_sha256": sha256_file(data_path),
            "source_root": str(source_root),
            "source_paths": list(paths),
            "source_hashes": source_identity(source_root, paths=paths),
            "evaluator_id": evaluator_id,
            "not_before": at.isoformat(),
            "label_columns": list(label_columns),
            "warmup_rows": warmup_rows,
            "evaluation_id": None,
            "result": None,
            "failures": [],
            "access_event_count": 0,
            "access_log_sha256": None,
            "admission_eligible": False,
            "live_authorization": False,
        }
        module_name, function_name = evaluator_id.split(":", 1)
        try:
            callback = getattr(importlib.import_module(module_name), function_name)
        except (ImportError, AttributeError) as exc:
            raise PermissionError(
                "evaluator must be an actual importable top-level function"
            ) from exc
        value["evaluator_binding"] = _callback_binding(
            callback, source_root, value["source_hashes"]
        )
        value["interval_policy"] = "closed_both_endpoints_legacy_holdout_protocol"
        with self._db() as db:
            if db.execute("SELECT 1 FROM state WHERE id=1").fetchone():
                raise RuntimeError("holdout already registered; new version needs new store")
            return self._store(
                db, value, {"action": "registered", "protocol_hash": protocol.fingerprint}
            )

    def _verify_bytes(self, value):
        if (
            source_identity(value["source_root"], paths=value["source_paths"])
            != value["source_hashes"]
        ):
            raise PermissionError("holdout source/model/config identity changed")
        if sha256_file(value["data_path"]) != value["data_sha256"]:
            raise PermissionError("holdout data identity changed")

    def freeze(self):
        with self._db() as db:
            value = self._read(db)
            if value["status"] == "frozen":
                self._verify_bytes(value)
                return value
            if value["status"] != "registered":
                raise RuntimeError("only registered protocol can be frozen")
            self._verify_bytes(value)
            value["status"] = "frozen"
            value["frozen_at"] = _now()
            return self._store(db, value, {"action": "frozen"})

    def inspect(self):
        with self._db() as db:
            return self._read(db)

    @contextmanager
    def _lease(self, evaluation_id, *, resume):
        path = self.path.with_name(self.path.name + ".evaluation.lock")
        token = uuid4().hex
        # Serialize stale-lease inspection/removal/replacement across processes.
        with self._db():
            try:
                handle = path.open("x", encoding="utf-8")
            except FileExistsError as exc:
                if not resume:
                    raise RuntimeError(
                        "another evaluation is active or needs explicit recovery"
                    ) from exc
                previous = _load_json(path.read_text(encoding="utf-8"))
                if previous.get("evaluation_id") != evaluation_id or _alive(previous.get("pid")):
                    raise RuntimeError(
                        "evaluation lease is still active or belongs to another identity"
                    ) from exc
                path.unlink()
                handle = path.open("x", encoding="utf-8")
            with handle:
                handle.write(
                    canonical_json(
                        {"pid": os.getpid(), "evaluation_id": evaluation_id, "token": token}
                    )
                )
                handle.flush()
                os.fsync(handle.fileno())
        try:
            yield
        finally:
            if path.exists() and _load_json(path.read_text(encoding="utf-8")).get("token") == token:
                path.unlink()

    def _begin_evaluation(self, evaluation_id, evaluator, at, resume):
        with self._db() as db:
            value = self._read(db)
            expected = f"{evaluator.__module__}:{evaluator.__qualname__}"
            if (
                expected != value["evaluator_id"]
                or _callback_binding(evaluator, value["source_root"], value["source_hashes"])
                != value["evaluator_binding"]
            ):
                raise PermissionError(
                    "evaluator identity differs from frozen executable/defaults/state"
                )
            if at < _timestamp(value["not_before"], "not_before"):
                raise PermissionError("final evidence has not matured; no data loaded")
            self._verify_bytes(value)
            if value["status"] == "completed":
                raise RuntimeError("final sample already consumed")
            if value["status"] not in {"frozen", "evaluating", "failed_consumed"}:
                raise RuntimeError("holdout is not frozen")
            if value["status"] != "frozen" and (
                not resume or value["evaluation_id"] != evaluation_id
            ):
                raise RuntimeError("consumed evaluation requires same-ID explicit recovery")
            if value["status"] == "evaluating":
                if _alive(value.get("owner_pid")):
                    raise RuntimeError("previous evaluation process is still alive")
                value["failures"].append(
                    {
                        "evaluation_id": evaluation_id,
                        "error_type": "ProcessInterrupted",
                        "reason": "prior owner exited before completion",
                        "recorded_at": _now(),
                    }
                )
                value = self._store(
                    db,
                    value,
                    {"action": "interrupted_consumed", "evaluation_id": evaluation_id},
                )
            value["status"] = "evaluating"
            value["owner_pid"] = os.getpid()
            value["evaluation_id"] = evaluation_id
            value["started_at"] = _now()
            self._store(
                db,
                value,
                {
                    "action": "evaluation_started",
                    "evaluation_id": evaluation_id,
                    "resume": bool(resume),
                },
            )
        return value

    def _evaluate_callback(self, value, evaluation_id, evaluator):
        raw = Path(value["data_path"]).read_bytes()
        if sha256_bytes(raw) != value["data_sha256"]:
            raise PermissionError("holdout bytes changed during read")
        frame = pd.read_csv(BytesIO(raw), index_col=0)
        frame.index = pd.to_datetime(frame.index, utc=True, errors="raise", format="mixed")
        if (
            frame.index.has_duplicates
            or not frame.index.is_monotonic_increasing
            or frame.index.hasnans
        ):
            raise ValueError("unique increasing holdout timestamps required")
        start = pd.Timestamp(value["protocol"]["holdout_start"])
        end = pd.Timestamp(value["protocol"]["holdout_end"])
        start = start.tz_localize("UTC") if start.tzinfo is None else start.tz_convert("UTC")
        end = end.tz_localize("UTC") if end.tzinfo is None else end.tz_convert("UTC")
        holdout = frame.loc[start:end].copy()
        if holdout.empty:
            raise ValueError("no observations in frozen holdout")
        warmup = frame.loc[frame.index < start].tail(value["warmup_rows"])
        warmup = warmup.drop(columns=value["label_columns"], errors="ignore").copy()
        result = _strict_json(
            evaluator(FinalEvaluationData(holdout, warmup, evaluation_id, value["protocol_hash"]))
        )
        if not isinstance(result, dict):
            raise ValueError("evaluator must return a finite JSON object")
        scoring = [t.isoformat() for t in holdout.index]
        if result.get("scored_timestamps") != scoring:
            raise ValueError("evaluator must disclose exactly the frozen scoring axis")
        return result

    def evaluate(
        self,
        *,
        evaluation_id,
        evaluator: Callable,
        now=None,
        resume=False,
        purpose="final_evaluation",
    ):
        if not isinstance(evaluation_id, str) or not evaluation_id.strip():
            raise ValueError("evaluation_id required")
        if purpose not in {"final_evaluation", "final_admission"}:
            raise PermissionError("only registered final evaluation purposes allowed")
        at = _timestamp(now or _now(), "now")
        if at > _timestamp(_now(), "actual_now"):
            raise ValueError("future clock cannot authorize final access")
        with self._lease(evaluation_id, resume=resume):
            value = self._begin_evaluation(evaluation_id, evaluator, at, resume)
            try:
                result = self._evaluate_callback(value, evaluation_id, evaluator)
                self._verify_bytes(value)
                with self._db() as db:
                    latest = self._read(db)
                    if latest["status"] != "evaluating" or latest["evaluation_id"] != evaluation_id:
                        raise RuntimeError("evaluation state changed during callback")
                    latest.update(
                        status="completed",
                        result=result,
                        completed_at=_now(),
                        result_sha256=_digest(result),
                    )
                    return self._store(
                        db, latest, {"action": "completed", "evaluation_id": evaluation_id}
                    )
            except BaseException as exc:
                with self._db() as db:
                    latest = self._read(db)
                    latest.update(status="failed_consumed")
                    latest["failures"].append(
                        {
                            "evaluation_id": evaluation_id,
                            "error_type": type(exc).__name__,
                            "reason": str(exc),
                            "recorded_at": _now(),
                        }
                    )
                    self._store(
                        db,
                        latest,
                        {
                            "action": "failed_consumed",
                            "evaluation_id": evaluation_id,
                            "reason": str(exc),
                        },
                    )
                raise


def freeze_candidate_contract(contract: Mapping, *, output, source_root, source_paths, now=None):
    """Freeze a supported successor; do not inherit prior candidate observations."""
    value = _strict_json(dict(contract))
    source_paths = tuple(source_paths)
    required = {
        "candidate_id",
        "parent_candidate_id",
        "research",
        "model_sha256",
        "config_sha256",
        "data_policy_id",
        "benchmark_ids",
        "risk_policy_id",
        "cost_policy_id",
        "observation_start",
        "observation_end",
        "required_market_states",
        "minimum_observation_days",
        "access_policy_id",
        "stop_rules",
        "adjudication_rule_id",
        "holdout_protocol_hash",
    }
    if required - value.keys():
        raise ValueError("incomplete candidate freeze contract")
    for key in (
        "candidate_id",
        "data_policy_id",
        "risk_policy_id",
        "cost_policy_id",
        "access_policy_id",
        "adjudication_rule_id",
    ):
        if not isinstance(value[key], str) or not value[key].strip():
            raise ValueError("nonempty candidate field required: " + key)
    if (
        not isinstance(value["holdout_protocol_hash"], str)
        or len(value["holdout_protocol_hash"]) != 64
        or any(c not in "0123456789abcdef" for c in value["holdout_protocol_hash"])
    ):
        raise ValueError("holdout_protocol_hash must be a SHA256 identity")
    if value["research"] != "supported":
        return {
            "status": "abstained",
            "reason": "no supported candidate to freeze",
            "live_authorization": False,
        }
    at = _timestamp(now or _now(), "now")
    if abs((at - _timestamp(_now(), "actual_now")).total_seconds()) > 5:
        raise ValueError("candidate registration cannot be backdated or future-dated")
    start = _timestamp(value["observation_start"], "observation_start")
    end = _timestamp(value["observation_end"], "observation_end")
    if start < at or end <= start or value["candidate_id"] == value["parent_candidate_id"]:
        raise ValueError("new candidate needs a distinct identity and a new forward interval")
    if (
        isinstance(value["minimum_observation_days"], bool)
        or not isinstance(value["minimum_observation_days"], int)
        or value["minimum_observation_days"] < 1
    ):
        raise ValueError("positive minimum_observation_days required")
    for name in ("benchmark_ids", "required_market_states", "stop_rules"):
        if (
            not isinstance(value[name], list)
            or not value[name]
            or len(set(value[name])) != len(value[name])
        ):
            raise ValueError("nonempty unique declarations required: " + name)
    hashes = source_identity(source_root, paths=source_paths)
    for name in ("model_sha256", "config_sha256"):
        if value[name] not in hashes.values():
            raise PermissionError(name + " must match a local frozen file")
    value.update(
        schema="prospective-candidate-contract/v1",
        interval_policy="left_closed_right_open",
        registered_at=at.isoformat(),
        source_root=str(Path(source_root).resolve()),
        source_paths=list(source_paths),
        source_hashes=hashes,
        inherited_observation_days=0,
        live_authorization=False,
    )
    value["contract_sha256"] = _digest(value)
    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(canonical_json(value))
    return value


def observation_coverage(contract, observations, *, as_of=None):
    """Inspect actual raw receipt identities without reading outcome performance."""
    value = (
        _load_json(Path(contract).read_text(encoding="utf-8"))
        if not isinstance(contract, Mapping)
        else _strict_json(contract)
    )
    expected = value.pop("contract_sha256", None)
    if expected != _digest(value):
        raise PermissionError("candidate contract changed")
    if source_identity(value["source_root"], paths=value["source_paths"]) != value["source_hashes"]:
        raise PermissionError("candidate source/model/config changed")
    at = _timestamp(as_of or _now(), "as_of")
    if at > _timestamp(_now(), "actual_now"):
        raise ValueError("future clock cannot manufacture elapsed observation evidence")
    start, end = (_timestamp(value[key], key) for key in ("observation_start", "observation_end"))
    seen, rows, errors = set(), [], []
    for row in observations:
        try:
            row = _strict_json(row)
            key = (row["session_id"], row["sequence"])
            if key in seen:
                raise ValueError("duplicate observation identity")
            seen.add(key)
            received = _timestamp(row["observed_at"], "observed_at")
            event = _timestamp(row["event_at"], "event_at")
            if row["candidate_id"] != value["candidate_id"] or not (
                start <= event < end and event <= received <= at
            ):
                raise ValueError("observation identity or actual receive time invalid")
            if sha256_file(row["raw_path"]) != row["raw_sha256"]:
                raise ValueError("raw observation receipt hash mismatch")
            for key in ("label_mature_at", "cost_mature_at"):
                if _timestamp(row[key], key) < event:
                    raise ValueError("maturity cannot precede the market event")
            rows.append({**row, "day": event.date().isoformat(), "received": received})
        except (KeyError, TypeError, ValueError, OSError) as exc:
            errors.append(str(exc))
    days = sorted({row["day"] for row in rows})
    states = sorted({row["market_state"] for row in rows})
    expected_days = (
        [
            d.date().isoformat()
            for d in pd.date_range(
                start.date(), min(end - pd.Timedelta(1, "ns"), at).date(), freq="D"
            )
        ]
        if at >= start
        else []
    )
    contiguous_days = set(expected_days) <= set(days)
    maturity = all(
        _timestamp(row[key], key) <= at
        for row in rows
        for key in ("label_mature_at", "cost_mature_at")
    )
    sequential = True
    for session in {row["session_id"] for row in rows}:
        seq = sorted(row["sequence"] for row in rows if row["session_id"] == session)
        sequential = sequential and seq == list(range(seq[0], seq[-1] + 1))
    complete = (
        not errors
        and bool(rows)
        and at >= end
        and len(days) >= value["minimum_observation_days"]
        and set(value["required_market_states"]) <= set(states)
        and maturity
        and sequential
        and contiguous_days
    )
    return {
        "status": "complete" if complete else "pending",
        "observation_days": days,
        "market_states": states,
        "errors": errors,
        "labels_and_costs_mature": maturity,
        "sequences_contiguous": sequential,
        "calendar_days_contiguous": contiguous_days,
        "interval_elapsed": at >= end,
        "performance_accessed": False,
        "live_authorization": False,
    }


def adjudicate_candidate(
    contract, observations, *, vault, evaluation_id, evaluator, as_of=None, resume=False
):
    """Do not invoke evaluator until actual interval/receipts/maturity qualify."""
    coverage = observation_coverage(contract, observations, as_of=as_of)
    if coverage["status"] != "complete":
        return {
            "research": "insufficient",
            "status": "pending",
            "coverage": coverage,
            "performance_accessed": False,
            "live_authorization": False,
        }
    value = (
        _load_json(Path(contract).read_text(encoding="utf-8"))
        if not isinstance(contract, Mapping)
        else dict(contract)
    )
    state = vault.inspect()
    if (
        value["holdout_protocol_hash"] != state["protocol_hash"]
        or value["source_hashes"] != state["source_hashes"]
    ):
        raise PermissionError(
            "candidate contract is not bound to this holdout protocol and model closure"
        )
    evaluated = vault.evaluate(
        evaluation_id=evaluation_id, evaluator=evaluator, now=as_of, resume=resume
    )
    research = evaluated["result"].get("research")
    if research not in {"supported", "not_supported", "insufficient"}:
        raise ValueError("frozen evaluator must supply a research conclusion")
    return {
        "status": "completed",
        "research": research,
        "coverage": coverage,
        "evaluation": evaluated,
        "performance_accessed": True,
        "live_authorization": False,
    }
