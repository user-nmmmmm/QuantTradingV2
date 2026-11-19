"""Original-engine account receipts, distinct from policy scores and proxy labels.

A local byte receipt proves a reproducible simulated account on the recorded
inputs. It does not remotely attest a provider clock, real execution or alpha.
The builder always labels later computation as a replay, never a live decision.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Mapping

import pandas as pd

from core.foundation.reproducibility import canonical_json

SCHEMA = "forward-account-receipt/v1"
ROOT = Path(__file__).resolve().parents[2]
_HASH = re.compile(r"[0-9a-f]{64}")
_FACTS = ("orders", "fills", "lot_closes", "account_path", "checkpoints")


def _finite_json(value):
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("receipt requires finite JSON values") from exc


def _digest(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty identity")
    return value


def _time(value, name):
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} requires an explicit UTC-aware timestamp") from exc
    if pd.isna(stamp) or stamp.tzinfo is None:
        raise ValueError(f"{name} requires an explicit UTC-aware timestamp")
    return stamp.tz_convert("UTC")


def _number(value, name, *, positive=False, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} requires a finite number")
    if positive and value <= 0 or nonnegative and value < 0:
        raise ValueError(f"{name} outside its supported domain")
    return float(value)


def engine_source_manifest(root=ROOT):
    """Explicit execution closure; source bytes are rechecked during verify."""
    from core.foundation.source_bytes import source_identity
    return source_identity(root, paths=["core", "research/ml_selection/forward_account_trajectory.py", "research/ml_selection/forward_execution_binding.py", "research/ml_selection/forward_policy_capture.py"])


@dataclass(frozen=True)
class ForwardAccountReceipt:
    """Detached receipt wrapper; use verifier before interpreting account returns."""
    payload: dict

    def to_dict(self):
        return deepcopy(self.payload)

    @property
    def receipt_id(self):
        return self.payload["receipt_id"]


def load_forward_account_receipt(path_or_mapping):
    if isinstance(path_or_mapping, ForwardAccountReceipt):
        value = path_or_mapping.to_dict()
    elif isinstance(path_or_mapping, Mapping):
        value = _finite_json(dict(path_or_mapping))
    else:
        def pairs(items):
            result = {}
            for key, item in items:
                if key in result:
                    raise ValueError("duplicate receipt JSON key")
                result[key] = item
            return result
        value = _finite_json(json.loads(Path(path_or_mapping).read_text(encoding="utf-8"), object_pairs_hook=pairs))
    identity = value.get("receipt_id")
    payload = {key: item for key, item in value.items() if key != "receipt_id"}
    if value.get("schema") != SCHEMA or not isinstance(identity, str) or _digest(payload) != identity:
        raise ValueError("account receipt identity/schema mismatch")
    _validate_inputs(payload)
    if any(not isinstance(value.get(key), list) for key in _FACTS):
        raise ValueError("original-engine account fact tables missing")
    return ForwardAccountReceipt(value)


def _validate_inputs(value):
    for key in ("protocol_id", "frozen_candidate_id", "session_id"):
        _text(value.get(key), key)
    if value.get("real_orders") is not False or value.get("formal_admission") is not False:
        raise ValueError("account replay cannot assert real orders or admission")
    if value.get("execution_binding_id") is not None and (not isinstance(value["execution_binding_id"], str) or not _HASH.fullmatch(value["execution_binding_id"])):
        raise ValueError("execution binding requires a canonical SHA-256 identity")
    start = _time(value.get("session_started_at"), "session_started_at")
    generated = _time(value.get("generated_at"), "generated_at")
    if generated < start:
        raise ValueError("account computation predates its session")
    if value.get("source_kind") != "recorded_forward_input_simulated_replay":
        raise ValueError("this receipt implementation verifies recorded-input replay, not contemporaneous decisions")
    if value.get("input_source_kind") not in {"synthetic_fixture", "recorded_forward"}:
        raise ValueError("unknown account input provenance")
    if not isinstance(value.get("source_manifest"), dict) or not value["source_manifest"]:
        raise ValueError("execution source manifest missing")
    for key, digest in value["source_manifest"].items():
        if (Path(key).is_absolute() or ".." in Path(key).parts
                or not isinstance(digest, str) or not _HASH.fullmatch(digest)):
            raise ValueError("invalid execution source path/hash")
    if _digest(value["source_manifest"]) != value.get("source_manifest_sha256"):
        raise ValueError("execution source manifest digest mismatch")
    cfg = value.get("account_config", {})
    _number(cfg.get("initial_capital"), "initial_capital", positive=True)
    _number(cfg.get("commission_rate"), "commission_rate", nonnegative=True)
    _number(cfg.get("max_participation_rate"), "max_participation_rate", positive=True)
    if cfg["max_participation_rate"] > 1:
        raise ValueError("participation rate exceeds one")
    observations = value.get("observations")
    if not isinstance(observations, list) or not observations:
        raise ValueError("session needs original observation receipts, including abstentions")
    obs = {}
    for row in observations:
        identity = row.get("observation_id")
        if identity in obs:
            raise ValueError("duplicate account observation")
        _text(identity, "observation_id")
        if _digest({key: item for key, item in row.items() if key != "observation_id"}) != identity:
            raise ValueError("original observation receipt changed")
        observed = _time(row.get("observed_at"), "observation observed_at")
        if observed < start or observed > generated:
            raise ValueError("observation outside account session")
        if _time(row.get("information_cutoff"), "information_cutoff") > observed:
            raise ValueError("observation uses unavailable information")
        if row.get("protocol_id") != value["protocol_id"] or row.get("frozen_candidate_id") != value["frozen_candidate_id"]:
            raise ValueError("observation account identities disagree")
        obs[identity] = row
    prior = _validate_market_events(value, start, generated)
    decisions = value.get("decisions")
    if not isinstance(decisions, list):
        raise ValueError("decision ledger missing")
    seen = set()
    for row in decisions:
        identifier = _text(row.get("decision_id"), "decision_id")
        if identifier in seen or row.get("observation_id") not in obs:
            raise ValueError("duplicate decision or missing observation linkage")
        decision_time = _time(row.get("generated_at"), "decision generated_at")
        if decision_time < _time(obs[row["observation_id"]]["observed_at"], "observed_at") or decision_time > generated:
            raise ValueError("decision predates observed inputs or exceeds computation")
        if row.get("status") not in {"submitted", "abstained", "risk_rejected", "health_paused", "unknown"}:
            raise ValueError("unknown decision status")
        seen.add(identifier)
    if set(obs) != {row["observation_id"] for row in decisions}:
        raise ValueError("each observation needs an explicit submitted or abstained decision")
    intents = value.get("intents")
    if not isinstance(intents, list):
        raise ValueError("intent ledger missing")
    decision_map = {row["decision_id"]: row for row in decisions}
    intent_ids, covered_decisions = set(), set()
    for row in intents:
        identifier = _text(row.get("intent_id"), "intent_id")
        decision = decision_map.get(row.get("decision_id"))
        if identifier in intent_ids or decision is None or decision["status"] != "submitted":
            raise ValueError("duplicate intent or missing submitted decision")
        submitted = _time(row.get("submitted_at"), "intent submitted_at")
        if submitted < _time(decision["generated_at"], "decision generated_at") or submitted > generated:
            raise ValueError("intent clock precedes decision")
        _text(row.get("symbol"), "intent symbol")
        if row.get("side") not in {"buy", "sell"} or row.get("order_type", "market") not in {"market", "limit"}:
            raise ValueError("v1 supports explicit spot buy/sell market/limit intents")
        _number(row.get("qty"), "intent qty", nonnegative=True)
        if row.get("reference_price") is not None:
            _number(row["reference_price"], "reference_price", positive=True)
        if submitted >= prior:
            raise ValueError("intent has no later recorded account event")
        covered_decisions.add(row["decision_id"])
        intent_ids.add(identifier)
    if {d["decision_id"] for d in decisions if d["status"] == "submitted"} != covered_decisions:
        raise ValueError("submitted decision lacks its order intent")


def _validate_market_events(value, start, generated):
    events = value.get("market_events")
    if not isinstance(events, list) or not events:
        raise ValueError("account trajectory requires recorded market events")
    event_ids, prior = set(), None
    for event in events:
        identifier = _text(event.get("event_id"), "market event_id")
        stamp = _time(event.get("timestamp"), "market timestamp")
        received = _time(event.get("received_at"), "market received_at")
        if identifier in event_ids or prior is not None and stamp <= prior:
            raise ValueError("duplicate or unordered market events")
        if stamp < start or received < stamp or received > generated:
            raise ValueError("market event unavailable to account computation")
        if not isinstance(event.get("bars"), dict) or not event["bars"]:
            raise ValueError("market event needs bars")
        for symbol, bar in event["bars"].items():
            _text(symbol, "bar symbol")
            for field in ("open", "high", "low", "close"):
                _number(bar.get(field), field, positive=True)
            _number(bar.get("volume"), "volume", nonnegative=True)
            if bar["low"] > min(bar["open"], bar["close"]) or bar["high"] < max(bar["open"], bar["close"]) or bar["low"] > bar["high"]:
                raise ValueError("inconsistent recorded OHLC")
        raw = event.get("raw_receipt")
        if (not isinstance(raw, dict) or raw.get("received_at") != event["received_at"]
                or raw.get("bars") != event["bars"] or _digest(raw) != event.get("raw_receipt_sha256")):
            raise ValueError("market raw receipt missing or changed")
        prior = stamp
        event_ids.add(identifier)
    return prior


def _run_engine(value):
    """Replay only simulated spot matching, with deterministic canonical ids."""
    from core.broker import Broker
    from core.trading.portfolio import Portfolio
    cfg = value["account_config"]
    portfolio = Portfolio(cfg["initial_capital"])
    broker = Broker(portfolio, commission_rate=cfg["commission_rate"],
                    commission_rate_maker=cfg["commission_rate"], slippage=0.0,
                    random_slip=False, max_participation_rate=cfg["max_participation_rate"],
                    exchange_id="recorded_forward_simulation", account_id=value["session_id"], timeframe="1d")
    orders, submitted, path, checkpoints, marks = {}, set(), [], [], {}
    prior_hash = None
    for event in value["market_events"]:
        stamp = _time(event["timestamp"], "event timestamp")
        for sequence, intent in enumerate(value["intents"]):
            if intent["intent_id"] in submitted or _time(intent["submitted_at"], "intent time") >= stamp:
                continue
            order = broker.submit_order(intent["symbol"], intent["side"], intent["qty"],
                price=intent.get("reference_price"), order_type=intent.get("order_type", "market"),
                timestamp=_time(intent["submitted_at"], "intent time"), strategy_id="frozen_forward_candidate",
                sequence=sequence)
            orders[intent["intent_id"]] = order
            submitted.add(intent["intent_id"])
        bars = {symbol: pd.Series(bar, name=stamp) for symbol, bar in event["bars"].items()}
        broker.process_orders(bars)
        marks.update({symbol: float(bar["close"]) for symbol, bar in event["bars"].items()})
        positions = {symbol: dict(position) for symbol, position in sorted(portfolio.positions.items())}
        lots = {symbol: [asdict(lot) for lot in portfolio.open_lots(symbol)] for symbol in positions}
        row = {"event_id": event["event_id"], "timestamp": stamp.isoformat(), "cash": portfolio.cash,
               "positions": positions, "marks": dict(marks), "fees": sum(t["commission"] for t in broker.trades),
               "equity": portfolio.get_total_value(marks), "open_lots": lots}
        row = json.loads(canonical_json(row))
        path.append(row)
        state = {"event_id": event["event_id"], "previous_checkpoint_id": prior_hash,
                 "account_sha256": _digest(row), "submitted_intent_ids": sorted(submitted),
                 "pending_order_ids": sorted(o.id for o in list(broker.pending_orders) + list(broker.active_orders))}
        state["checkpoint_id"] = _digest(state)
        prior_hash = state["checkpoint_id"]
        checkpoints.append(state)
    order_rows = [{"intent_id": key, "order_id": o.id, "symbol": o.symbol, "side": o.side,
                   "requested_qty": o.qty, "filled_qty": o.filled_qty, "remaining_qty": o.remaining_qty,
                   "status": o.status.value, "average_fill_price": o.avg_fill_price}
                  for key, o in orders.items()]
    fills = []
    for index, trade in enumerate(broker.trades):
        order = [key for key, item in orders.items() if item.id == trade["order_id"]]
        fills.append({"fill_id": f"{value['session_id']}:fill:{index}", "intent_id": order[0],
                      **{key: trade[key] for key in ("order_id", "symbol", "side", "qty", "fill_price", "commission", "fill_time")}})
    return json.loads(canonical_json({"orders": order_rows, "fills": fills,
                                     "lot_closes": [asdict(e) for e in broker.close_events],
                                     "account_path": path, "checkpoints": checkpoints}))


def build_forward_account_receipt(*, protocol_id, frozen_candidate_id, session_id,
                                  session_started_at, observations, market_events, decisions, intents,
                                  initial_capital=10000.0, commission_rate=0.001,
                                  max_participation_rate=1.0, input_source_kind="synthetic_fixture",
                                  source_root=ROOT, resumed_from=None, execution_binding_id=None):
    """Compute original Broker facts on recorded inputs, explicitly as later replay."""
    if Path(source_root).resolve() != ROOT:
        raise ValueError("account execution must bind its actual source root")
    source = engine_source_manifest(source_root)
    value = {"schema": SCHEMA, "protocol_id": protocol_id, "frozen_candidate_id": frozen_candidate_id,
             "session_id": session_id, "session_started_at": session_started_at,
             "generated_at": datetime.now(timezone.utc).isoformat(),
             "source_kind": "recorded_forward_input_simulated_replay", "input_source_kind": input_source_kind,
             "source_manifest": source, "source_manifest_sha256": _digest(source),
             "account_config": {"initial_capital": initial_capital, "commission_rate": commission_rate,
                                "max_participation_rate": max_participation_rate},
             "observations": observations, "market_events": market_events,
             "decisions": decisions, "intents": intents, "resumed_from": resumed_from,
             "execution_binding_id": execution_binding_id, "real_orders": False, "formal_admission": False}
    value = _finite_json(value)
    _validate_inputs(value)
    value.update(_run_engine(value))
    if engine_source_manifest(source_root) != source:
        raise ValueError("execution code changed during account replay")
    value["receipt_id"] = _digest(value)
    return ForwardAccountReceipt(value)


def verify_forward_account_receipt(receipt, *, protocol_id, frozen_candidate_id,
                                   qualifying_observation_ids=(), observation_receipts=None,
                                   as_of=None, source_root=ROOT,
                                   expected_source_manifest_sha256=None, execution_bindings=None, binding_store_root=None):
    """Verify actual bytes, full linkage and original-engine account equality.

    Synthetic or unqualified observations can pass account engineering while
    leaving forward portfolio evidence pending. Multiple seeds do not add dates.
    """
    if Path(source_root).resolve() != ROOT:
        raise ValueError("account verification must bind its actual source root")
    value = load_forward_account_receipt(receipt).to_dict()
    if value["protocol_id"] != protocol_id or value["frozen_candidate_id"] != frozen_candidate_id:
        raise ValueError("account protocol/candidate identity mismatch")
    cutoff = _time(as_of or datetime.now(timezone.utc), "as_of")
    if cutoff > _time(datetime.now(timezone.utc), "clock"):
        raise ValueError("account report cutoff cannot be in the future")
    if _time(value["generated_at"], "generated_at") > cutoff:
        raise ValueError("account receipt not available at this report cutoff")
    if engine_source_manifest(source_root) != value["source_manifest"]:
        raise ValueError("original-engine source bytes changed")
    if expected_source_manifest_sha256 is not None and value["source_manifest_sha256"] != expected_source_manifest_sha256:
        raise ValueError("account source identity differs from frozen expected identity")
    actual = _run_engine(value)
    if any(canonical_json(value.get(key)) != canonical_json(actual[key]) for key in _FACTS):
        raise ValueError("account facts disagree with original-engine deterministic replay")
    # Independent cash/inventory bridge, without Portfolio's own equity method.
    cash, positions, fees, maximum = value["account_config"]["initial_capital"], {}, 0.0, 0.0
    for row in value["account_path"]:
        for fill in value["fills"]:
            if _time(fill["fill_time"], "fill time") != _time(row["timestamp"], "account time"):
                continue
            delta = fill["qty"] * (1 if fill["side"] == "buy" else -1)
            positions[fill["symbol"]] = positions.get(fill["symbol"], 0.0) + delta
            fees += fill["commission"]
            cash -= delta * fill["fill_price"] + fill["commission"]
        bridge = cash + sum(qty * row["marks"][symbol] for symbol, qty in positions.items())
        differences = [abs(cash - row["cash"]), abs(fees - row["fees"]), abs(bridge - row["equity"])]
        differences.extend(abs(qty - row["positions"].get(symbol, {}).get("qty", 0.0)) for symbol, qty in positions.items())
        maximum = max(maximum, *differences)
    if maximum > 1e-8:
        raise ValueError("independent account cash/inventory/fee/equity bridge failed")
    observed = {row["observation_id"]: row for row in value["observations"]}
    qualified = set(qualifying_observation_ids)
    original = observation_receipts or {}
    evidence_gaps = []
    binding_proof = None
    binding_id = value.get("execution_binding_id")
    if binding_id is None or binding_id not in (execution_bindings or {}):
        evidence_gaps.append("explicit_intent_replay_not_candidate_account_missing_original_execution_binding")
    else:
        from research.ml_selection.forward_execution_binding import verify_forward_execution_binding, index_forward_execution_bindings
        original_bindings = index_forward_execution_bindings(execution_bindings.values())
        if set(original_bindings) != set(execution_bindings):
            raise ValueError("binding mapping keys do not match actual original receipt identities")
        binding_proof = verify_forward_execution_binding(original_bindings[binding_id], account=value,
            observation_receipts=original, store_root=binding_store_root, as_of=cutoff)
        if not binding_proof["original_policy_command_origin_linked"]:
            evidence_gaps.append("explicit_local_command_replay_original_frozen_policy_origin_pending")
        policy_capture = binding_proof.get("policy_capture")
        if policy_capture is not None and not policy_capture["actual_policy_inputs_joined"]:
            evidence_gaps.append("captured_policy_feature_inputs_not_joined_to_original_observation")
        if policy_capture is not None and not policy_capture["contemporaneous_input_eligible"]:
            evidence_gaps.append("captured_policy_uses_synthetic_or_retrospective_inputs")
    if value["input_source_kind"] != "recorded_forward":
        evidence_gaps.append("synthetic_input_is_not_actual_forward_evidence")
    if any(row.get("source_kind") != "immutable_public_forward_receipt" or row.get("retrospective_observation") is not False
           for row in observed.values()):
        evidence_gaps.append("observation_not_from_recorded_forward_intake")
    if not observed.keys() <= qualified:
        evidence_gaps.append("account_contains_unqualified_or_missing_forward_observations")
    if any(key not in original or canonical_json(row) != canonical_json(original[key]) for key, row in observed.items()):
        evidence_gaps.append("account_observation_bytes_not_joined_to_verified_forward_store")
    if any(row["status"] == "unknown" for row in value["decisions"]):
        evidence_gaps.append("unknown_order_decision_status")
    final = value["account_path"][-1]
    result_return = final["equity"] / value["account_config"]["initial_capital"] - 1
    return {"schema": "forward-account-verification/v1", "status": "verified" if not evidence_gaps else "engineering_verified_forward_pending",
            "engineering": "pass", "receipt_id": value["receipt_id"], "session_id": value["session_id"],
            "source_kind": value["source_kind"], "input_source_kind": value["input_source_kind"],
            "original_execution_binding": binding_proof,
            "source_manifest_sha256": value["source_manifest_sha256"],
            "observation_ids": sorted(observed), "decision_count": len(value["decisions"]),
            "fill_count": len(value["fills"]), "order_count": len(value["orders"]),
            "portfolio_return": result_return if not evidence_gaps else None,
            "simulated_replay_return": result_return,
            "initial_equity": value["account_config"]["initial_capital"], "final_equity": final["equity"],
            "max_account_bridge_error": maximum, "forward_evidence_eligible": not evidence_gaps,
            "pending_reasons": evidence_gaps, "resumed_from": value.get("resumed_from"),
            "real_orders": False, "formal_admission": False,
            "limitation": "Locally recorded-input simulated replay; not contemporaneous real-time decisions, provider attestation or real trading."}


def resume_forward_account_receipt(previous, *, market_events, decisions=None, intents=None,
                                   observations=None, source_root=ROOT, execution_binding_id=None):
    """Reconstruct the same account once from an immutable prefix, without re-adding fills.

    All old event/decision/intent prefixes must be byte-equal. The previous
    account is verified first; the new receipt binds its predecessor identity.
    """
    old = load_forward_account_receipt(previous).to_dict()
    verify_forward_account_receipt(previous, protocol_id=old["protocol_id"],
                                  frozen_candidate_id=old["frozen_candidate_id"], source_root=source_root)
    replacements = {"market_events": market_events, "decisions": decisions if decisions is not None else old["decisions"],
                    "intents": intents if intents is not None else old["intents"],
                    "observations": observations if observations is not None else old["observations"]}
    for key, rows in replacements.items():
        if len(rows) < len(old[key]) or canonical_json(rows[:len(old[key])]) != canonical_json(old[key]):
            raise ValueError(f"recovery changes recorded {key} prefix")
    # New knowledge is appended after the recorded account checkpoint, even
    # when it describes an older economic event. Never backfill a decision as
    # though it was already made before the interruption.
    last_received = _time(old["market_events"][-1]["received_at"], "last checkpoint input")
    for key, field in (("observations", "observed_at"), ("decisions", "generated_at"), ("intents", "submitted_at")):
        if any(_time(row[field], field) <= last_received for row in replacements[key][len(old[key]):]):
            raise ValueError("recovery backfills knowledge or decisions before its checkpoint")
    if len(market_events) == len(old["market_events"]):
        if any(canonical_json(rows) != canonical_json(old[key]) for key, rows in replacements.items()):
            raise ValueError("recovery cannot inject decisions without new market events")
        return ForwardAccountReceipt(old)
    cfg = old["account_config"]
    new = build_forward_account_receipt(protocol_id=old["protocol_id"], frozen_candidate_id=old["frozen_candidate_id"],
        session_id=old["session_id"], session_started_at=old["session_started_at"], **replacements,
        initial_capital=cfg["initial_capital"], commission_rate=cfg["commission_rate"],
        max_participation_rate=cfg["max_participation_rate"], input_source_kind=old["input_source_kind"],
        source_root=source_root, resumed_from=old["receipt_id"], execution_binding_id=execution_binding_id)
    for key in ("account_path", "checkpoints", "fills", "lot_closes"):
        if canonical_json(new.payload[key][:len(old[key])]) != canonical_json(old[key]):
            raise ValueError("recovered account prefix differs; do not duplicate execution")
    return new


def summarize_forward_account_receipts(receipts, *, protocol_id, frozen_candidate_id,
                                       qualifying_observation_ids, observation_receipts,
                                       as_of=None, source_root=ROOT, execution_bindings=None, binding_store_root=None):
    """Join a complete, unbranched replay chain without summing independent books."""
    reports, values, errors = {}, {}, []
    for receipt in receipts:
        try:
            loaded = load_forward_account_receipt(receipt)
            identifier = loaded.receipt_id
            if identifier in values:
                raise ValueError("duplicate account receipt identity")
            values[identifier] = loaded.to_dict()
            reports[identifier] = verify_forward_account_receipt(loaded, protocol_id=protocol_id,
                frozen_candidate_id=frozen_candidate_id, qualifying_observation_ids=qualifying_observation_ids,
                observation_receipts=observation_receipts, as_of=as_of, source_root=source_root,
                execution_bindings=execution_bindings, binding_store_root=binding_store_root)
        except (ValueError, TypeError, KeyError, OSError) as exc:
            errors.append(str(exc))
    children = {}
    for identifier, value in values.items():
        parent = value.get("resumed_from")
        if parent:
            if parent not in values:
                errors.append("account recovery predecessor receipt missing")
                continue
            old = values[parent]
            if old["session_id"] != value["session_id"] or _time(old["generated_at"], "old generated") > _time(value["generated_at"], "new generated"):
                errors.append("account recovery changes session or reverses generation time")
            for key in ("observations", "market_events", "decisions", "intents", "account_path", "checkpoints", "fills", "lot_closes"):
                if canonical_json(value[key][:len(old[key])]) != canonical_json(old[key]):
                    errors.append("account recovery changes immutable predecessor prefix")
            if parent in children:
                errors.append("account recovery fork; no post-hoc winning branch selection")
            children[parent] = identifier
    leaves = [identifier for identifier in values if identifier not in children]
    if len({values[key]["session_id"] for key in leaves}) != len(leaves):
        errors.append("multiple unjoined accounts use the same session identity")
    pending = []
    if not values:
        pending.append("original_engine_simulated_fill_and_equity_evidence_pending")
    if len(leaves) != 1:
        pending.append("single_continuous_account_trajectory_pending")
    candidate = reports.get(leaves[0]) if len(leaves) == 1 else None
    if candidate and not set(qualifying_observation_ids) <= set(candidate["observation_ids"]):
        pending.append("account_does_not_cover_all_verified_forward_observations")
    if candidate:
        pending.extend(candidate["pending_reasons"])
    eligible = bool(candidate and candidate["forward_evidence_eligible"] and not pending and not errors)
    return {"schema": "forward-account-store-evidence/v1",
            "status": "verified" if eligible else "invalid" if errors else "pending",
            "portfolio_return": candidate["portfolio_return"] if eligible else None,
            "reason": "verified_recorded_forward_input_simulated_replay" if eligible else "original_engine_account_evidence_incomplete",
            "receipt_count": len(values), "leaf_receipt_ids": leaves,
            "receipts": list(reports.values()), "pending_reasons": sorted(set(pending)), "errors": errors,
            "contemporaneous_real_time_decisions_proven": False, "formal_admission": False, "real_orders": False}
