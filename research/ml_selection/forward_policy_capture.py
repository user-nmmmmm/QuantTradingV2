"""Opt-in, local append-only capture of actual policy calls and Broker events.

The registry and retained checkpoints are independent of replay receipts. This
is a local integrity boundary, not protection against an administrator replacing
both the original store and every independently retained checkpoint.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import inspect
import json
from pathlib import Path
import sqlite3

from core.broker import Broker
from core.foundation.domain import OrderIntent
from core.events import EventCodec
from core.foundation.reproducibility import canonical_json
from core.foundation.runtime import EventProcessor

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "forward-policy-capture/v1"
REGISTRY = "policy_capture_registry.sqlite3"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


def _digest(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _parse(document):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate capture JSON key")
            result[key] = value
        return result
    return json.loads(document, object_pairs_hook=unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))


def _load(path):
    return _parse(Path(path).read_text(encoding="utf-8"))


def _write_new(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False))
        handle.flush()
        __import__("os").fsync(handle.fileno())


def _time(value):
    import pandas as pd
    result = pd.Timestamp(value)
    if result.tzinfo is None or pd.isna(result):
        raise ValueError("capture requires explicit aware clocks")
    return result.tz_convert("UTC")


def _bar_label(value):
    """The original historical engine uses naive UTC economic bar labels."""
    import pandas as pd
    point = pd.Timestamp(value)
    return point.tz_localize("UTC") if point.tzinfo is None else _time(point)


def _source():
    from core.foundation.source_bytes import source_identity
    return source_identity(ROOT, paths=["core", "strategies", "router", "composition", "backtest", "research/ml_selection", "signal_meta"])


def _policy(selector):
    from research.ml_selection.selector import ResearchSelector
    if type(selector) is not ResearchSelector or "select" in vars(selector):
        raise ValueError("capture requires original ResearchSelector, not a declared decision mapping")
    settings = {name: getattr(selector, name) for name in
                ("mode", "deterministic", "threshold", "policy_threshold", "score_scale", "score_cap", "initial_capital",
                 "contract", "selector_contract", "capital_score_source", "policy_gate_mode", "gate_contract",
                 "explicit_selector_contract", "requested_context_features")}
    settings["temporal_context"] = (None if selector.temporal_context is None else selector.temporal_context.to_dict())
    artifacts = {}
    for name in ("model", "policy"):
        artifact = getattr(selector, name, None)
        if artifact is not None:
            if type(artifact).__module__ != "research.ml_selection.models" or any(callable(value) for value in vars(artifact).values()):
                raise ValueError("frozen model cannot replace its native prediction implementation")
            payload = artifact._payload()
            payload.pop("rng_state", None)
            artifacts[name] = {"model_id": artifact.model_id, "payload": payload}
    return _json({"class": type(selector).__module__ + "." + type(selector).__qualname__,
                  "settings": settings, "artifacts": artifacts})


def _validate_frozen_selection_options(protocol, candidate, policy):
    aliases = {"min_expected_return": "threshold", "policy_threshold": "policy_threshold",
               "score_scale": "score_scale", "score_cap": "score_cap", "deterministic": "deterministic",
               "selector_contract": "selector_contract", "capital_score_source": "capital_score_source",
               "initial_capital": "initial_capital", "policy_gate_mode": "policy_gate_mode"}
    sources = [protocol.get("settings", {}).get("selection", {}), candidate.get("selection", {}), candidate]
    for declared in sources:
        if not isinstance(declared, dict):
            raise ValueError("frozen selector settings must be mappings")
        for name, actual_name in aliases.items():
            if name == "policy_gate_mode" and policy["settings"]["mode"] != "policy":
                continue
            if name in declared and canonical_json(declared[name]) != canonical_json(policy["settings"][actual_name]):
                raise ValueError("actual native selector settings differ from frozen protocol/candidate")


def _strategy_identity(processor):
    result = {}
    for name, strategy in sorted(getattr(processor.router, "strategies", {}).items()):
        path = Path(inspect.getfile(type(strategy))).resolve()
        if not path.is_relative_to(ROOT):
            raise ValueError("policy capture strategy source must be inside frozen project")
        if any(callable(value) for value in vars(strategy).values()):
            raise ValueError("strategy cannot replace native methods during frozen capture")
        result[name] = {"allowed_states": sorted(value.name for value in strategy.allowed_states),
                        "exit_on_disallowed_state": strategy.exit_on_disallowed_state,
                        "exit_allowed_states": (None if strategy.exit_allowed_states is None else
                            sorted(value.name for value in strategy.exit_allowed_states)),
                        "class": type(strategy).__module__ + "." + type(strategy).__qualname__,
                        "source": path.relative_to(ROOT).as_posix(),
                        "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "params": _json(getattr(strategy, "params", {}))}
    if not result:
        raise ValueError("policy capture needs actual registered strategies")
    return result


def _role_identity(value, methods):
    path = Path(inspect.getfile(type(value))).resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError("runtime policy implementation must belong to frozen project")
    if any(name in vars(value) for name in methods):
        raise ValueError("runtime policy cannot replace its native methods")
    return {"class": type(value).__module__ + "." + type(value).__qualname__,
            "source": path.relative_to(ROOT).as_posix(),
            "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _runtime_contract(processor, broker):
    risk = processor.risk_manager
    capital = getattr(getattr(processor.allocator, "capital_planner", None), "policy", None)
    governor = getattr(getattr(processor.allocator, "risk_governor", None), "policy", None)
    return {"roles": {
        "processor": _role_identity(processor, ("process",)),
        "broker": _role_identity(broker, ("submit_order", "submit_intent", "process_orders")),
        "risk": _role_identity(risk, ("clamp_entry_qty", "check_entry_risk")),
        "allocator": _role_identity(processor.allocator, ("allocate",)),
        "router": _role_identity(processor.router, ("collect_candidate", "collect_entry_candidate", "process_position_management")),
        "state_machine": _role_identity(processor.state_machine, ("get_state",))},
        "risk_parameters": {name: getattr(risk, name, None) for name in (
            "risk_per_trade", "max_leverage", "max_pos_size_pct", "min_entry_notional_pct", "liquidity_limit_pct",
            "max_drawdown_limit", "daily_loss_limit", "portfolio_drawdown_reduce", "portfolio_drawdown_block",
            "portfolio_drawdown_liquidate", "portfolio_drawdown_lock")},
        "capital_policy": None if capital is None else _json(asdict(capital)),
        "governor_policy": None if governor is None else _json(asdict(governor)),
        "routing": _json(getattr(processor.router, "regime_map", {}))}


def _frozen_store_files(root, protocol, candidate, source_kind):
    result = {}
    for filename, expected in (("protocol.json", protocol), ("candidate.json", candidate)):
        path = root / filename
        if not path.is_file():
            if source_kind == "recorded_forward":
                raise ValueError("actual frozen protocol/candidate file missing from original forward store")
            continue
        if _load(path) != expected:
            raise ValueError("declared frozen metadata differs from original forward store bytes")
        result[filename] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _account_config(broker):
    from core.trading.accounts import AccountMode
    if broker.portfolio.account_mode is not AccountMode.SPOT:
        raise ValueError("capture v1 supports original spot accounts; margin/funding origin remains pending")
    if (any(float(p.get("qty", 0)) for p in broker.portfolio.positions.values())
            or broker.trades or broker.orders_by_id or broker.pending_orders or broker.active_orders):
        raise ValueError("capture v1 must start before orders with flat initial inventory")
    if (broker.slippage or broker.random_slip or broker.use_impact_cost or broker.spread_bps
            or broker.volatility_slippage_factor or broker.commission_rate != broker.commission_rate_maker):
        raise ValueError("capture replay supports deterministic flat spot with equal maker/taker fees")
    return {"initial_capital": float(broker.portfolio.cash), "commission_rate": broker.commission_rate,
            "max_participation_rate": broker.max_participation_rate}


def _connect(path):
    connection = sqlite3.connect(path, timeout=1)
    connection.execute("PRAGMA synchronous=FULL")
    return connection


def _read_only_connection(path):
    if not Path(path).is_file():
        raise ValueError("original policy capture database missing")
    try:
        return sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise ValueError("original policy capture database unavailable") from exc


def _immutable_table(connection, table, definition):
    connection.execute(f"CREATE TABLE IF NOT EXISTS {table} ({definition})")
    connection.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_update BEFORE UPDATE ON {table} "
                       "BEGIN SELECT RAISE(ABORT, 'append-only capture'); END")
    connection.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_delete BEFORE DELETE ON {table} "
                       "BEGIN SELECT RAISE(ABORT, 'append-only capture'); END")
    connection.commit()


class ForwardPolicyCapture:
    """Factory for one explicitly enabled original-runtime capture session.

    Disabled construction and wrap are inert. Enabling does not start an engine,
    collect network data, submit any order, or obtain trading authorization.
    """
    def __init__(self, store_root, *, enabled=False):
        self.store_root = Path(store_root).resolve()
        self.enabled = enabled

    def engine_observer(self, **options):
        """Create an inert-until-bind engine observer with no engine import below research."""
        return ForwardEngineBarCapture(self, options)

    def wrap(self, processor, *, broker, protocol_id, frozen_candidate_id, model_id,
             frozen_protocol, frozen_candidate, evidence_resolver,
             input_source_kind="synthetic_fixture"):
        if not self.enabled:
            return processor
        return CapturedForwardRuntime(processor, broker=broker, store_root=self.store_root,
            protocol_id=protocol_id, frozen_candidate_id=frozen_candidate_id, model_id=model_id,
            frozen_protocol=frozen_protocol, frozen_candidate=frozen_candidate,
            evidence_resolver=evidence_resolver, input_source_kind=input_source_kind)


class ForwardEngineBarCapture:
    """Duck-typed BacktestEngine observer; the core engine never imports research."""
    def __init__(self, factory, options):
        self.factory, self.options, self.runtime = factory, options, None

    def bind(self, engine, run_state):
        if not self.factory.enabled:
            return
        if self.runtime is not None:
            raise ValueError("capture observer cannot restart a registered account")
        self.runtime = self.factory.wrap(run_state.processor, broker=run_state.broker, **self.options)

    def process_bar(self, engine, run_state, run_bar):
        if not self.factory.enabled:
            return run_bar(run_state)
        if self.runtime is None:
            raise ValueError("original engine capture is not bound")
        return self.runtime.process_engine_bar(engine, run_state, run_bar=run_bar)

    def export_binding(self):
        if self.runtime is None:
            raise ValueError("capture observer is disabled or has no original account")
        return self.runtime.export_binding()

    def close(self):
        if self.runtime is not None:
            self.runtime.close()


class _ObservedSelector:
    def __init__(self, selector, capture):
        self.selector, self.capture = selector, capture

    def __getattr__(self, name):
        return getattr(self.selector, name)

    def select(self, candidates, **kwargs):
        capture = self.capture
        if not capture._active:
            raise ValueError("captured selector called outside captured runtime process")
        if _policy(self.selector) != capture.registration["policy"]:
            raise ValueError("frozen selector/model changed during capture")
        candidates = list(candidates)
        start = len(self.selector.audit)
        before = [_candidate(row) for row in candidates]
        selected = self.selector.select(candidates, **kwargs)
        table = self.selector.table.reset_index()
        context = self.selector._active_temporal_context
        if context is not None:
            table = table.loc[table["as_of"].eq(context.decision_cutoff)
                              & table["symbol"].isin([row.symbol for row in candidates])]
        capture._append("selection", {"candidates": before, "selected": [_candidate(row) for row in selected],
                                      "actual_feature_rows": json.loads(canonical_json(table.to_dict("records"))),
                                      "audit": json.loads(canonical_json(self.selector.audit[start:]))})
        return selected


def _candidate(candidate):
    return {"symbol": candidate.symbol, "strategy": candidate.strategy_name,
            "bar_time": _bar_label(candidate.frame.index[candidate.bar_index]).isoformat(),
            "signal": _json(dict(candidate.signal)), "score": float(candidate.score)}


class CapturedForwardRuntime:
    """EventProcessor decorator; policy and execution are called only by it.

    evidence_resolver(event) returns original observation, market_event and
    market_collections references. It must read the actual forward store; it
    must never generate an observation or fetch data implicitly for this hook.
    """
    def __init__(self, processor, *, broker, store_root, protocol_id, frozen_candidate_id,
                 model_id, frozen_protocol, frozen_candidate, evidence_resolver, input_source_kind):
        if not isinstance(processor, EventProcessor) or not isinstance(broker, Broker):
            raise ValueError("capture requires actual EventProcessor and simulated Broker")
        if processor.portfolio is not broker.portfolio:
            raise ValueError("capture processor and Broker must share the original account")
        if input_source_kind not in {"recorded_forward", "synthetic_fixture"}:
            raise ValueError("unknown capture input provenance")
        self.processor, self.broker, self.store_root = processor, broker, Path(store_root).resolve()
        self.evidence_resolver = evidence_resolver
        self._active = False
        self._closed = False
        self._step = None
        self._connection = None
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        self._signing_key = Ed25519PrivateKey.generate()
        if (not isinstance(frozen_protocol, dict) or frozen_protocol.get("protocol_id") != protocol_id
                or not isinstance(frozen_candidate, dict)
                or frozen_candidate.get("parent_model_id", frozen_candidate.get("model_id")) != model_id):
            raise ValueError("capture must retain actual frozen protocol and model candidate metadata")
        selector = _policy(processor.candidate_selector)
        _validate_frozen_selection_options(frozen_protocol, frozen_candidate, selector)
        if (selector["settings"]["mode"] == "policy"
                and selector["artifacts"].get("policy", {}).get("model_id") != frozen_candidate["model_id"]):
            raise ValueError("frozen policy does not identify actual native selector policy bytes")
        if selector["artifacts"].get("model", {}).get("model_id") != model_id:
            raise ValueError("frozen model does not identify actual selector model bytes")
        self.registration = _json({"schema": SCHEMA, "registered_at": _now(),
            "protocol_id": protocol_id, "frozen_candidate_id": frozen_candidate_id, "model_id": model_id,
            "session_id": broker.account_id, "run_id": broker.event_pipeline.run_id,
            "verification_key": self._signing_key.public_key().public_bytes_raw().hex(),
            "frozen_protocol": frozen_protocol, "frozen_candidate": frozen_candidate,
            "frozen_store_files": _frozen_store_files(self.store_root, frozen_protocol, frozen_candidate, input_source_kind),
            "policy": selector, "strategies": _strategy_identity(processor),
            "runtime_contract": _runtime_contract(processor, broker),
            "source_manifest": _source(), "account_config": _account_config(broker),
            "input_source_kind": input_source_kind, "real_orders": False, "formal_admission": False})
        self.registration["registration_id"] = _digest(self.registration)
        self.folder = self.store_root / "policy_captures" / self.registration["registration_id"]
        self.store_root.mkdir(parents=True, exist_ok=True)
        registry = _connect(self.store_root / REGISTRY)
        try:
            _immutable_table(registry, "sessions", "protocol_id TEXT, candidate_id TEXT, session_id TEXT, "
                             "registration_id TEXT UNIQUE, registration_json TEXT, "
                             "PRIMARY KEY(protocol_id,candidate_id,session_id)")
            registry.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", (protocol_id, frozen_candidate_id,
                broker.account_id, self.registration["registration_id"], canonical_json(self.registration)))
            registry.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError("capture session already registered; never restart or replace its original orders") from exc
        finally:
            registry.close()
        _write_new(self.folder / "registration.json", self.registration)
        self._connection = _connect(self.folder / "journal.sqlite3")
        _immutable_table(self._connection, "records", "sequence INTEGER PRIMARY KEY, kind TEXT, "
                         "recorded_at TEXT, previous_hash TEXT, payload_json TEXT, record_hash TEXT UNIQUE, signature TEXT")
        self._append("session", {"registration_id": self.registration["registration_id"]})
        self._unsubscribe = broker.event_pipeline.subscribe(self._on_event)
        self._original_selector = processor.candidate_selector
        processor.candidate_selector = _ObservedSelector(self._original_selector, self)
        # This is passive trace recording; no decision/gate settings are changed.
        self._original_trace = (processor.entry_audit_enabled, processor.trace_gate_facts)
        processor.entry_audit_enabled = processor.trace_gate_facts = True

    def __getattr__(self, name):
        return getattr(self.processor, name)

    def _append(self, kind, payload):
        last = self._connection.execute("SELECT sequence,record_hash FROM records ORDER BY sequence DESC LIMIT 1").fetchone()
        record = {"sequence": last[0] + 1 if last else 1, "kind": kind, "recorded_at": _now(),
                  "previous_hash": last[1] if last else None, "payload": _json(payload)}
        record["record_hash"] = _digest(record)
        record["signature"] = self._signing_key.sign(record["record_hash"].encode("ascii")).hex()
        self._connection.execute("INSERT INTO records VALUES (?,?,?,?,?,?,?)", (record["sequence"], kind,
            record["recorded_at"], record["previous_hash"], canonical_json(record["payload"]),
            record["record_hash"], record["signature"]))
        self._connection.commit()
        return record

    def _on_event(self, event):
        # Retain even out-of-step commands: those invalidate a complete policy
        # capture instead of quietly disappearing from account attribution.
        self._append("event", {"step": self._step, "event": EventCodec.encode(event)})

    def process(self, event, **kwargs):
        return self._capture_call(event, lambda: self.processor.process(event, **kwargs))

    def process_engine_bar(self, engine, run_state, *, run_bar):
        """Observe the original complete bar including matching and risk actions.

        Final-run tail activity remains outside this bar. Any such trading
        events are retained and make account provenance fail closed.
        """
        from backtest.engine import BacktestEngine
        if (not isinstance(engine, BacktestEngine) or run_state.processor is not self.processor
                or run_state.broker is not self.broker or engine.event_processor is not self.processor
                or getattr(run_bar, "__self__", None) is not engine
                or getattr(run_bar, "__func__", None) is not BacktestEngine._run_bar):
            raise ValueError("capture engine hook must own the actual full native bar, runtime and Broker")
        return self._capture_call(run_state.event, lambda: run_bar(run_state))

    def _capture_call(self, event, callback):
        if self._closed or self._active:
            raise ValueError("capture session is closed or runtime process is reentrant")
        if (_source() != self.registration["source_manifest"]
                or _policy(self._original_selector) != self.registration["policy"]
                or _strategy_identity(self.processor) != self.registration["strategies"]
                or _runtime_contract(self.processor, self.broker) != self.registration["runtime_contract"]):
            raise ValueError("frozen capture source/model bytes changed")
        evidence = _json(self.evidence_resolver(event))
        _validate_evidence(evidence, event, self.registration)
        key = evidence["market_event"]["event_id"]
        previous = read_capture_journal(self.folder)
        if any(row["kind"] == "input" and row["payload"]["market_event"]["event_id"] == key for row in previous):
            raise ValueError("captured market event already processed; do not duplicate native orders")
        if any(row["kind"] == "failed" for row in previous):
            raise ValueError("interrupted capture needs account reconciliation; no automatic order resubmission")
        self._step, self._active = key, True
        audit_start = len(self.processor.entry_audit)
        self._append("input", evidence)
        try:
            result = callback()
            if _policy(self._original_selector) != self.registration["policy"]:
                raise ValueError("selector/model mutated while original process was running")
            record = self._append("completed", {"step": key,
                "entry_audit": json.loads(canonical_json(self.processor.entry_audit[audit_start:])),
                "account": {"cash": float(self.broker.portfolio.cash),
                            "positions": json.loads(canonical_json(self.broker.portfolio.positions)),
                            "fees": sum(float(row["commission"]) for row in self.broker.trades),
                            "equity": float(self.broker.portfolio.get_total_value(
                                {symbol: float(bar["close"]) for symbol, bar in event.bars.items()}))}})
            checkpoint = {"registration_id": self.registration["registration_id"],
                          "sequence": record["sequence"], "record_hash": record["record_hash"]}
            _write_new(self.folder / "checkpoints" / f"{record['sequence']:020d}.json", checkpoint)
            self.checkpoint = checkpoint
            return result
        except BaseException as exc:
            self._append("failed", {"step": key, "error_type": type(exc).__name__})
            raise
        finally:
            self._step, self._active = None, False

    def export_binding(self):
        if self._active or self._closed or not hasattr(self, "checkpoint"):
            raise ValueError("capture export requires an open completed original runtime step")
        binding = binding_from_capture(self.store_root, self.registration["registration_id"], self.checkpoint)
        target = self.store_root / "execution_bindings" / (binding["binding_id"] + ".json")
        if target.exists():
            if _load(target) != binding:
                raise ValueError("persisted original capture binding changed")
        else:
            _write_new(target, binding)
        return binding

    def close(self):
        if not self._closed:
            self._unsubscribe()
            self.processor.candidate_selector = self._original_selector
            self.processor.entry_audit_enabled, self.processor.trace_gate_facts = self._original_trace
            self._connection.close()
            self._signing_key = None
            self._closed = True


def _validate_evidence(evidence, event, registration):
    observation = evidence["observation"]
    if _digest({k: v for k, v in observation.items() if k != "observation_id"}) != observation.get("observation_id"):
        raise ValueError("capture original observation identity mismatch")
    for key in ("protocol_id", "frozen_candidate_id", "model_id"):
        actual = observation.get(key)
        if key == "model_id":
            actual = actual or observation.get("rl_decision", {}).get("model_id")
        if actual != registration[key]:
            raise ValueError("capture original observation frozen identity mismatch")
    market = evidence["market_event"]
    bars = {symbol: {key: float(bar[key]) for key in ("open", "high", "low", "close", "volume")}
            for symbol, bar in event.bars.items()}
    if market["bars"] != bars or _time(market["timestamp"]) != _bar_label(event.timestamp):
        raise ValueError("captured runtime event differs from original market bytes")
    raw = market["raw_receipt"]
    if raw.get("bars") != bars or raw.get("received_at") != market["received_at"] or _digest(raw) != market["raw_receipt_sha256"]:
        raise ValueError("capture original raw market receipt mismatch")
    now = _time(_now())
    if (_time(observation["information_cutoff"]) > _time(observation["observed_at"])
            or _time(observation["observed_at"]) > now or _time(market["received_at"]) > now):
        raise ValueError("capture input unavailable at actual runtime clock")
    if not evidence.get("market_collections"):
        raise ValueError("capture requires independent public collection references")


def read_capture_journal(folder, checkpoint=None):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    registered = _load(Path(folder) / "registration.json")
    public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(registered["verification_key"]))
    connection = _read_only_connection(Path(folder) / "journal.sqlite3")
    try:
        rows = connection.execute("SELECT sequence,kind,recorded_at,previous_hash,payload_json,record_hash,signature "
                                  "FROM records ORDER BY sequence").fetchall()
    except sqlite3.Error as exc:
        raise ValueError("original capture journal database malformed") from exc
    finally:
        connection.close()
    records, previous = [], None
    for sequence, kind, recorded_at, parent, payload, fingerprint, signature in rows:
        record = {"sequence": sequence, "kind": kind, "recorded_at": recorded_at,
                  "previous_hash": parent, "payload": _parse(payload)}
        if sequence != len(records) + 1 or parent != previous or _digest(record) != fingerprint:
            raise ValueError("original policy capture journal changed or truncated")
        try:
            public_key.verify(bytes.fromhex(signature), fingerprint.encode("ascii"))
        except (ValueError, InvalidSignature) as exc:
            raise ValueError("original policy capture signature invalid; metadata cannot self-sign a new tail") from exc
        if records and _time(recorded_at) < _time(records[-1]["recorded_at"]):
            raise ValueError("capture wall clock moved backwards")
        record["record_hash"] = fingerprint
        record["signature"] = signature
        records.append(record)
        previous = fingerprint
    if checkpoint is not None:
        sequence = checkpoint["sequence"]
        if (not isinstance(sequence, int) or sequence < 1 or sequence > len(records)
                or records[sequence-1]["record_hash"] != checkpoint["record_hash"]
                or records[sequence-1]["kind"] != "completed"):
            raise ValueError("retained policy capture checkpoint mismatch or missing suffix")
        records = records[:sequence]
    return records


def _registration(store_root, registration_id):
    root = Path(store_root).resolve()
    if not (root / REGISTRY).is_file():
        raise ValueError("original policy capture registry missing")
    registry = _read_only_connection(root / REGISTRY)
    try:
        row = registry.execute("SELECT registration_json FROM sessions WHERE registration_id=?", (registration_id,)).fetchone()
    except sqlite3.Error as exc:
        raise ValueError("original capture registry database malformed") from exc
    finally:
        registry.close()
    if row is None:
        raise ValueError("policy capture registration absent from original store registry")
    registered = _parse(row[0])
    if _digest({k: v for k, v in registered.items() if k != "registration_id"}) != registration_id:
        raise ValueError("policy capture registration bytes changed")
    folder = (root / "policy_captures" / registration_id).resolve()
    if not folder.is_relative_to(root):
        raise ValueError("original policy capture path escapes registered store")
    if _load(folder / "registration.json") != registered:
        raise ValueError("policy capture registration differs from independent original registry")
    return registered, folder


def _capture_steps(records, registration):
    steps, events, incomplete, outside = [], {}, None, []
    for record in records[1:]:
        kind, payload = record["kind"], record["payload"]
        if kind == "input":
            if incomplete is not None:
                raise ValueError("capture incomplete step was hidden by a later input")
            incomplete = {"input": payload, "started_at": record["recorded_at"], "selections": [], "events": []}
        elif kind == "selection":
            if incomplete is None:
                raise ValueError("capture selection has no original process")
            incomplete["selections"].append(payload)
        elif kind == "event":
            event = EventCodec.decode(payload["event"])
            if str(event.event_id) in events:
                raise ValueError("duplicate original pipeline event")
            events[str(event.event_id)] = event
            if incomplete is None or payload["step"] != incomplete["input"]["market_event"]["event_id"]:
                outside.append(str(event.event_id))
            else:
                if event.run_id != registration["run_id"] or (event.account_id and event.account_id != registration["session_id"]):
                    raise ValueError("original pipeline event run/account mismatch")
                incomplete["events"].append(str(event.event_id))
        elif kind == "completed":
            if incomplete is None or payload["step"] != incomplete["input"]["market_event"]["event_id"]:
                raise ValueError("capture completion lacks its original step")
            incomplete.update(completed=payload, completed_at=record["recorded_at"])
            steps.append(incomplete)
            incomplete = None
        else:
            raise ValueError("interrupted or unknown capture record; reconcile original account")
    if incomplete is not None or outside:
        raise ValueError("original Broker activity outside captured runtime; account origin incomplete")
    return steps, events


def binding_from_capture(store_root, registration_id, checkpoint):
    """Export actual observed rows, never caller-declared decisions/intents."""
    from research.ml_selection.forward_execution_binding import command_row
    registration, folder = _registration(store_root, registration_id)
    if checkpoint.get("registration_id") != registration_id:
        raise ValueError("capture checkpoint belongs to another registration")
    retained = _load(folder / "checkpoints" / f"{checkpoint['sequence']:020d}.json")
    if retained != checkpoint:
        raise ValueError("capture binding checkpoint is not retained in original store")
    complete_journal = read_capture_journal(folder)
    _capture_steps(complete_journal, registration)
    records = read_capture_journal(folder, checkpoint)
    if records[0]["kind"] != "session" or records[0]["payload"]["registration_id"] != registration_id:
        raise ValueError("capture session root mismatch")
    steps, events = _capture_steps(records, registration)
    observations, decisions, intents, commands, markets, collections = {}, [], [], {}, [], {}
    for step in steps:
        evidence = step["input"]
        observation = evidence["observation"]
        observations[observation["observation_id"]] = _digest(observation)
        markets.append(evidence["market_event"])
        for ref in evidence["market_collections"]:
            collections[canonical_json(ref)] = ref
        submitted = []
        selected = [candidate for selection in step["selections"] for candidate in selection["selected"]]
        audit_orders = {row["order_id"] for row in step["completed"]["entry_audit"] if row.get("order_id")}
        for key in step["events"]:
            event = events[key]
            if not isinstance(event.payload, OrderIntent):
                continue
            command = event.payload
            signal = events.get(command.signal_id)
            if signal is None or signal.event_type != "signal" or command.causation_id != command.signal_id:
                raise ValueError("native OrderIntent does not reference actual original strategy signal")
            fact = signal.payload
            if (fact.get("strategy_id") != command.strategy_id or fact.get("symbol") != command.symbol
                    or fact.get("action") != command.action or _time(fact["bar_time"]) != _time(command.bar_time)):
                raise ValueError("native signal and canonical command disagree")
            if command.strategy_id not in registration["strategies"]:
                raise ValueError("native signal strategy absent from frozen runtime")
            if fact.get("signal_kind") == "entry":
                matching = [candidate for candidate in selected if candidate["symbol"] == command.symbol
                            and candidate["strategy"] == command.strategy_id
                            and candidate["signal"].get("action") == command.action]
                if len(matching) != 1 or command.client_order_id not in audit_orders:
                    raise ValueError("native entry command not linked to actual selected candidate and original order audit")
            # Broker reservation metadata may enrich the command but never its
            # signal identity or requested quantity after publication.
            native = _json(asdict(command))
            if command.client_order_id in commands and commands[command.client_order_id] != native:
                raise ValueError("same native command changed during policy capture")
            commands[command.client_order_id] = native
            decision_id = str(signal.event_id)
            decision = {"decision_id": decision_id, "observation_id": observation["observation_id"],
                        "generated_at": signal.observed_at.isoformat(), "status": "submitted"}
            if decision not in decisions:
                decisions.append(decision)
            intent = command_row(command, decision_id)
            intent.update(submitted_at=event.observed_at.isoformat(), native_bar_time=command.bar_time)
            intents.append(intent)
            submitted.append(command.client_order_id)
        if not submitted:
            decisions.append({"decision_id": _digest({"registration_id": registration_id, "step": evidence["market_event"]["event_id"]}),
                "observation_id": observation["observation_id"], "generated_at": step["completed_at"], "status": "abstained"})
    value = {"schema": "forward-execution-binding/v2", "captured_at": records[-1]["recorded_at"],
        "capture_source": "original_runtime_policy_and_broker_events",
        **{key: registration[key] for key in ("protocol_id", "frozen_candidate_id", "model_id", "session_id")},
        "observation_hashes": observations, "decisions": decisions, "intents": intents, "native_commands": commands,
        "market_events": markets, "market_collections": list(collections.values()),
        "native_account_path": [{"event_id": step["input"]["market_event"]["event_id"],
                                  **step["completed"]["account"]} for step in steps],
        "policy_capture": {"registration_id": registration_id, "checkpoint": checkpoint},
        "real_orders": False, "formal_admission": False}
    value["binding_id"] = _digest(value)
    return value


def _selection_inputs_joined(records, registration):
    from research.ml_selection.dataset import FEATURE_COLUMNS
    steps, _ = _capture_steps(records, registration)
    complete = True
    for step in steps:
        original = step["input"]["observation"]["observations"]
        for selection in step["selections"]:
            for actual in selection["actual_feature_rows"]:
                matches = [row for row in original if row.get("symbol") == actual["symbol"]]
                if len(matches) != 1 or not {"as_of", "eligible", "features"} <= set(matches[0]):
                    complete = False
                    continue
                row = matches[0]
                if any(name not in row["features"] for name in FEATURE_COLUMNS):
                    complete = False
                    continue
                if (_time(row["as_of"]) != _time(actual["as_of"]) or row["eligible"] != actual["eligible"]
                        or canonical_json({name: row["features"][name] for name in FEATURE_COLUMNS})
                        != canonical_json({name: actual[name] for name in FEATURE_COLUMNS})):
                    raise ValueError("actual policy feature inputs differ from original forward observation")
    return complete


def _input_clocks_match_original_step(records, registration):
    steps, events = _capture_steps(records, registration)
    for step in steps:
        evidence = step["input"]
        cutoff = _time(evidence["observation"]["information_cutoff"])
        if _time(evidence["market_event"]["received_at"]) > cutoff:
            return False
        if any(not (_time(step["started_at"]) <= _time(events[key].observed_at) <= _time(step["completed_at"]))
               for key in step["events"]):
            return False
        if any(_time(row["as_of"]) > cutoff for selection in step["selections"]
               for row in selection["actual_feature_rows"]):
            return False
    return True


def verify_forward_policy_capture(binding, *, store_root, account, as_of):
    reference = binding["policy_capture"]
    registration, _ = _registration(store_root, reference["registration_id"])
    original = binding_from_capture(store_root, reference["registration_id"], reference["checkpoint"])
    if canonical_json(original) != canonical_json(binding):
        raise ValueError("execution binding differs from original append-only policy capture")
    if _time(original["captured_at"]) > _time(as_of):
        raise ValueError("original policy capture unavailable at report cutoff")
    if account["account_config"] != registration["account_config"]:
        raise ValueError("replay initial account/fee/participation differs from original native account")
    _check_native_account_path(binding, account)
    for filename, fingerprint in registration["frozen_store_files"].items():
        if hashlib.sha256((Path(store_root) / filename).read_bytes()).hexdigest() != fingerprint:
            raise ValueError("original frozen protocol/candidate bytes changed after capture registration")
    for role in [*registration["strategies"].values(), *registration["runtime_contract"]["roles"].values()]:
        path = (ROOT / role["source"]).resolve()
        if not path.is_relative_to(ROOT) or hashlib.sha256(path.read_bytes()).hexdigest() != role["source_sha256"]:
            raise ValueError("original frozen runtime role source changed")
    if registration["source_manifest"] != _source():
        raise ValueError("frozen original runtime/policy source changed")
    records = read_capture_journal(Path(store_root)/"policy_captures"/reference["registration_id"], reference["checkpoint"])
    feature_inputs_joined = _selection_inputs_joined(records, registration)
    inputs = [row for row in records if row["kind"] == "input"]
    contemporaneous = all(_time(row["payload"]["observation"]["information_cutoff"]) >= _time(registration["registered_at"])
                         and _time(row["payload"]["market_event"]["received_at"]) >= _time(registration["registered_at"])
                         for row in inputs)
    contemporaneous = contemporaneous and _input_clocks_match_original_step(records, registration)
    contemporaneous = contemporaneous and all(
        _time(registration["registered_at"]) <= _time(EventCodec.decode(row["payload"]["event"]).observed_at)
        <= _time(row["recorded_at"])
        for row in records if row["kind"] == "event")
    return {"status": "verified", "registration_id": reference["registration_id"],
            "checkpoint": reference["checkpoint"], "native_command_count": len(binding["intents"]),
            "original_policy_command_origin_linked": bool(binding["intents"]),
            "actual_policy_inputs_joined": feature_inputs_joined,
            "contemporaneous_input_eligible": bool(contemporaneous and feature_inputs_joined
                and registration["input_source_kind"] == "recorded_forward"),
            "input_source_kind": registration["input_source_kind"],
            "scope": "original local registry/journal/checkpoint; not remote attestation or real orders"}


def _check_native_account_path(binding, account):
    native = binding["native_account_path"]
    if len(native) != len(account["account_path"]):
        raise ValueError("replay does not cover complete original native account path")
    for actual, replay in zip(native, account["account_path"], strict=True):
        if actual["event_id"] != replay["event_id"]:
            raise ValueError("replay event order differs from original native account")
        if any(abs(actual[key] - replay[key]) > 1e-8 for key in ("cash", "fees", "equity")):
            raise ValueError("replay economics/clocks differ from actual original native account")
        symbols = set(actual["positions"]) | set(replay["positions"])
        if any(abs(actual["positions"].get(symbol, {}).get("qty", 0)
                   - replay["positions"].get(symbol, {}).get("qty", 0)) > 1e-8 for symbol in symbols):
            raise ValueError("replay inventory differs from original native account")
