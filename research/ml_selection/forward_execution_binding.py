"""Immutable post-allocation commands joined to independent public intake bytes.

These are local capture receipts, not remote attestation. Account replay cannot
create its own original-store command or market evidence by resigning a result.
"""
from __future__ import annotations
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from core.foundation.domain import OrderIntent
from core.foundation.reproducibility import canonical_json

SCHEMA = "forward-execution-binding/v1"


def _digest(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _clean(value):
    return json.loads(json.dumps(value, allow_nan=False, ensure_ascii=False))


def command_row(command, decision_id):
    return {"intent_id": command.client_order_id, "decision_id": decision_id,
            "symbol": command.symbol, "side": command.action, "qty": float(command.requested_qty),
            "reference_price": command.price if command.price is not None else command.reference_price,
            "submitted_at": command.bar_time, "order_type": command.order_type}


def capture_forward_execution_binding(*, broker, decisions, decision_order_ids, observations,
                                      market_events, market_collections, protocol_id,
                                      frozen_candidate_id, model_id, session_id):
    """Extract actual canonical commands from the original Broker, after approval.

    Observing a policy score or a requested quantity alone is insufficient. The
    caller retains this new receipt independently of any subsequent replay.
    """
    from core.broker import Broker
    if not isinstance(broker, Broker) or broker.account_id != session_id:
        raise ValueError("binding capture requires the actual session Broker")
    if not model_id or not protocol_id or not frozen_candidate_id:
        raise ValueError("binding needs protocol, frozen candidate and model identity")
    obs = {row["observation_id"]: row for row in observations}
    if len(obs) != len(observations):
        raise ValueError("duplicate binding observations")
    intents, commands, order_ids = [], {}, set()
    for row in decisions:
        if row.get("observation_id") not in obs:
            raise ValueError("binding decision lacks original observation")
        native_ids = decision_order_ids.get(row["decision_id"], [])
        if bool(native_ids) != (row.get("status") == "submitted"):
            raise ValueError("submitted/abstained decision disagrees with actual native orders")
        for identifier in native_ids:
            order = broker.orders_by_id.get(identifier)
            if order is None or not isinstance(order.intent, OrderIntent) or identifier in order_ids:
                raise ValueError("original Broker order/intent missing or duplicated")
            command = order.intent
            if (command.client_order_id != order.id or command.symbol != order.symbol
                    or command.action != order.side or command.requested_qty != order.qty):
                raise ValueError("actual order disagrees with its canonical native command")
            intents.append(command_row(command, row["decision_id"]))
            commands[identifier] = json.loads(canonical_json(asdict(command)))
            order_ids.add(identifier)
    value = {"schema": SCHEMA, "captured_at": datetime.now(timezone.utc).isoformat(),
             "capture_source": "original_broker_post_allocation_commands",
             "protocol_id": protocol_id, "frozen_candidate_id": frozen_candidate_id,
             "model_id": model_id, "session_id": session_id,
             "observation_hashes": {key: _digest(row) for key, row in obs.items()},
             "decisions": decisions, "intents": intents, "native_commands": commands,
             "market_events": market_events, "market_collections": market_collections,
             "real_orders": False, "formal_admission": False}
    value = _clean(value)
    value["binding_id"] = _digest(value)
    return value


def load_forward_execution_binding(value):
    value = _clean(value)
    identifier = value.get("binding_id")
    if value.get("schema") not in {SCHEMA, "forward-execution-binding/v2"} or not identifier or _digest({k:v for k,v in value.items() if k!="binding_id"}) != identifier:
        raise ValueError("original execution binding identity/schema mismatch")
    expected_source = ("original_runtime_policy_and_broker_events" if value["schema"].endswith("/v2")
                       else "original_broker_post_allocation_commands")
    if value.get("capture_source") != expected_source:
        raise ValueError("binding is not original post-allocation command evidence")
    if value.get("real_orders") is not False or value.get("formal_admission") is not False:
        raise ValueError("simulated command capture cannot assert real orders or admission")
    commands = value.get("native_commands", {})
    intents = value.get("intents", [])
    if len(intents) != len({row["intent_id"] for row in intents}):
        raise ValueError("duplicate original canonical commands")
    if set(commands) != {row["intent_id"] for row in intents}:
        raise ValueError("binding native command set differs from emitted intents")
    for row in intents:
        command = OrderIntent(**commands[row["intent_id"]])
        expected = command_row(command, row["decision_id"])
        if value["schema"].endswith("/v2"):
            expected.update(submitted_at=row["submitted_at"], native_bar_time=command.bar_time)
        if command.account != value["session_id"] or canonical_json(expected) != canonical_json(row):
            raise ValueError("binding quantity/side/order identity differs from canonical command")
    return value



def index_forward_execution_bindings(values):
    """Refuse same-session command/market conflicts instead of choosing a winner."""
    result, commands, events = {}, {}, {}
    for raw in values:
        value = load_forward_execution_binding(raw)
        identity = value["binding_id"]
        if identity in result:
            raise ValueError("duplicate original forward execution binding")
        result[identity] = value
        context = (value["protocol_id"], value["frozen_candidate_id"], value["model_id"])
        for intent in value["intents"]:
            key = (value["session_id"], intent["intent_id"])
            fingerprint = canonical_json({"context": context, "intent": intent,
                                           "command": value["native_commands"][intent["intent_id"]]})
            if key in commands and commands[key] != fingerprint:
                raise ValueError("conflicting original same-session canonical command; cannot choose a new binding winner")
            commands[key] = fingerprint
        for event in value["market_events"]:
            key = (value["session_id"], event["event_id"])
            fingerprint = canonical_json(event)
            if key in events and events[key] != fingerprint:
                raise ValueError("conflicting original same-session market receipt; cannot choose revised replay input")
            events[key] = fingerprint
    return result


def verify_forward_execution_binding(binding, *, account, observation_receipts, store_root, as_of):
    """Compare replay inputs with original store commands and independent intake.

    Market prices are read from the original immutable public collection's CSV,
    file manifest and raw collection identity; account self-signed bars do not
    satisfy this check.
    """
    from research.ml_selection.forward_evidence import load_public_forward_collection
    from research.ml_selection.selector import utc
    binding = load_forward_execution_binding(binding)
    if binding["binding_id"] != account.get("execution_binding_id"):
        raise ValueError("account binding reference differs from actual loaded binding identity")
    for key in ("protocol_id", "frozen_candidate_id", "session_id"):
        if binding[key] != account[key]:
            raise ValueError("original execution binding identity mismatch")
    if utc(binding["captured_at"]) > utc(as_of):
        raise ValueError("execution binding not available at report cutoff")
    for key in ("decisions", "intents", "market_events"):
        if canonical_json(binding[key]) != canonical_json(account[key]):
            raise ValueError(f"replay {key} differ from original store execution binding")
    expected = {row["observation_id"]: _digest(row) for row in account["observations"]}
    if binding["observation_hashes"] != expected:
        raise ValueError("execution binding original observation bytes mismatch")
    for key, fingerprint in binding["observation_hashes"].items():
        row = observation_receipts.get(key)
        if row is None or _digest(row) != fingerprint:
            raise ValueError("execution binding observation absent/changed in actual store")
        recorded_model = row.get("model_id") or row.get("rl_decision", {}).get("model_id")
        if recorded_model != binding["model_id"]:
            raise ValueError("execution binding frozen model identity mismatch")
    if store_root is None:
        raise ValueError("original public collection root required; no self-contained provenance upgrade")
    root = Path(store_root).resolve()
    symbols = sorted({symbol for event in account["market_events"] for symbol in event["bars"]})
    collections = []
    for reference in binding.get("market_collections", []):
        if Path(reference["path"]).is_absolute() or ".." in Path(reference["path"]).parts:
            raise ValueError("public collection reference escapes actual forward store")
        folder = (root/reference["path"]).resolve()
        if not folder.is_relative_to(root):
            raise ValueError("public collection reference escapes actual forward store")
        frames, evidence = load_public_forward_collection(folder, symbols, information_cutoff=as_of)
        manifest = json.loads((folder/"collection.json").read_text(encoding="utf-8"))
        if evidence["collection_id"] != reference.get("collection_id"):
            raise ValueError("original market collection identity changed")
        collections.append((frames,manifest))
    if not collections:
        raise ValueError("original independently recorded public market collections missing")
    for event in account["market_events"]:
        matched = False
        for frames,manifest in collections:
            bars,received = {},[]
            for symbol in event["bars"]:
                frame = frames.get(symbol)
                if frame is None or utc(event["timestamp"]) not in __import__('pandas').to_datetime(frame.index,utc=True):
                    break
                lookup = frame.copy()
                lookup.index=__import__('pandas').to_datetime(lookup.index,utc=True)
                bar=lookup.loc[utc(event["timestamp"])]
                bars[symbol]={key:float(bar[key]) for key in ("open","high","low","close","volume")}
                received.append(utc(manifest["market_files"][symbol]["received_at"]))
            if (bars == event["bars"] and received and max(received)==utc(event["received_at"])):
                matched=True
                break
        if not matched:
            raise ValueError("replay raw market bars/clocks do not match original public collection bytes")
    policy_proof = None
    if binding["schema"] == "forward-execution-binding/v2":
        from research.ml_selection.forward_policy_capture import verify_forward_policy_capture
        policy_proof = verify_forward_policy_capture(binding, store_root=root, account=account, as_of=as_of)
    # Old caller-declared causal metadata is never proof that policy code ran.
    linked = bool(policy_proof and policy_proof["original_policy_command_origin_linked"])
    return {"status": "verified", "binding_id": binding["binding_id"],
            "original_policy_command_origin_linked": linked,
            "policy_capture": policy_proof,
            "scope": "local original Broker events and immutable public collections; not remote attestation"}
