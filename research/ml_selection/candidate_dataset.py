"""Read-only strategy-candidate datasets and strict paired account comparisons.

The existing 21 market features stay unchanged. Context is recorded separately
from targets; actual exits, independent proxy trades and portfolio marginal
value are different estimands. Rejected/unfilled/unclosed candidates stay in
their original daily groups and never acquire a zero-return target.
"""
from __future__ import annotations

import ast
from collections import Counter, defaultdict
from collections.abc import Mapping
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

from core.reproducibility import canonical_json, sha256_file
from research.ml_selection.dataset import FEATURE_COLUMNS


CONTEXT_COLUMNS = (
    "strategy", "action", "native_score", "signal_stop_loss", "signal_price",
    "stop_distance_fraction", "strategy_health", "health_multiplier", "market_multiplier",
    "market_state", "cash_fraction", "gross_exposure_fraction", "portfolio_drawdown",
    "held_count_fraction", "pending_count_fraction", "same_day_candidate_count",
)
PAIR_IDENTITY_FIELDS = (
    "data_identity", "universe_identity", "initial_state_identity", "account_mode",
    "account_configuration_identity", "execution_identity", "strategy_exit_identity",
    "health_risk_identity", "randomness_identity", "evaluation_start", "evaluation_end",
    "terminal_policy", "initial_capital", "timeframe",
)
CANDIDATE_COLUMNS = tuple(dict.fromkeys((
    "candidate_id", "decision_id", "account_id", "data_identity", "protocol_id", "account_mode",
    "initial_capital", "account_identity", "symbol", "as_of", "candidate_day", "bar_time",
    "original_signal", "candidate_source", "decision_context_available_at", "decision_identity_status",
    "feature_status", "data_eligible", "membership_basis", "target_type", *FEATURE_COLUMNS,
    *CONTEXT_COLUMNS, "audit_gate_facts", "audit_final_reason", "audit_health_multiplier",
    "audit_market_multiplier", "audit_capital_allocation", "audit_target_qty", "audit_approved_qty",
    "audit_selected", "label_net_return", "label_net_pnl", "label_available_at", "label_exit_reason",
    "label_status", "label_basis", "actual_fill_net_pnl", "actual_entry_notional",
    "portfolio_marginal_net_return", "training_eligible", "candidate_group_id", "same_day_labeled_count",
    "daily_group_complete",
)))


def _rows(value):
    return value.to_dict("records") if isinstance(value, pd.DataFrame) else [dict(r) for r in value]


def _missing(value):
    return value is None or (isinstance(value, (float, np.floating)) and not math.isfinite(value)) or value is pd.NaT


def _number(value):
    if _missing(value) or isinstance(value, (bool, np.bool_)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _stamp(value):
    if _missing(value) or value == "":
        return None
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        return None
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _structure(value, default):
    if _missing(value) or value == "":
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            try:
                return ast.literal_eval(value)
            except (ValueError, SyntaxError) as exc:
                raise ValueError("invalid structured ledger field") from exc
    return value


def _true(value):
    return value is True or isinstance(value, np.bool_) and bool(value) or isinstance(value, str) and value.lower() == "true"


def _identifier(value):
    return None if _missing(value) or value == "" else str(value)


def _pending_count(value):
    """Accept engine order collections and explicit legacy nonnegative counts."""
    if isinstance(value, (list, tuple, Mapping)):
        return len(value)
    count = _number(value)
    if count is None or count < 0 or not count.is_integer():
        return None
    return int(count)


def _actual_exit(decision, fills, identity, cutoff, financing_costs):
    """Use authoritative order/lot facts; never FIFO-match by symbol or date."""
    result = {"label_net_return": np.nan, "label_net_pnl": np.nan,
              "label_available_at": pd.NaT, "label_exit_reason": None,
              "label_status": "unknown_unfilled", "actual_fill_net_pnl": np.nan,
              "actual_entry_notional": np.nan, "portfolio_marginal_net_return": np.nan}
    order = _identifier(decision.get("order_id"))
    if order is None:
        return result
    openings = [fill for fill in fills if _identifier(fill.get("order_id")) == order
                and fill.get("side") in {"buy", "short"}]
    if not openings:
        return result
    side = openings[0]["side"]
    symbol = decision.get("symbol")
    if any(fill.get("side") != side or fill.get("symbol") != symbol for fill in openings):
        raise ValueError("opening order has conflicting side or symbol")
    quantity, notional, entry_cost = 0., 0., 0.
    availability = []
    decision_time = _stamp(decision.get("as_of"))
    for fill in openings:
        qty, price, fee = (_number(fill.get(key)) for key in ("qty", "fill_price", "commission"))
        time = _stamp(fill.get("fill_time"))
        if qty is None or qty <= 0 or price is None or price <= 0 or fee is None or fee < 0 or time is None:
            result["label_status"] = "unknown_invalid_opening_fill"
            return result
        if decision_time is not None and time < decision_time:
            result["label_status"] = "unknown_fill_precedes_decision"
            return result
        quantity += qty
        notional += qty * price
        entry_cost += fee
        availability.append(time.normalize() + pd.Timedelta(days=1))
        if "available_at" in fill:
            supplied = _stamp(fill["available_at"])
            if supplied is None:
                result["label_status"] = "unknown_fill_availability"
                return result
            availability.append(supplied)
    result["actual_entry_notional"] = notional
    closed_qty, pnl, allocated_entry_cost, allocated_entry_notional = 0., 0., 0., 0.
    seen_closes, reasons = set(), []
    synthetic_exit = False
    for fill in fills:
        details = _structure(fill.get("lot_closes"), [])
        if not isinstance(details, (list, tuple)) or any(not isinstance(d, Mapping) for d in details):
            raise ValueError("lot_closes must contain structured authoritative facts")
        linked = [d for d in details if _identifier(d.get("entry_order_id")) == order]
        if not linked:
            continue
        if fill.get("symbol") != symbol or fill.get("side") != ("sell" if side == "buy" else "cover"):
            raise ValueError("linked closing fill has conflicting side or symbol")
        if fill.get("exit_reason") == "EndOfBacktest":
            synthetic_exit = True
        total, price, fee = (_number(fill.get(key)) for key in ("qty", "fill_price", "commission"))
        time = _stamp(fill.get("fill_time"))
        all_closed = [_number(detail.get("qty_closed")) for detail in details]
        if (total is None or total <= 0 or price is None or price <= 0 or fee is None or fee < 0
                or time is None or any(q is None or q <= 0 for q in all_closed)
                or sum(all_closed) > total + max(1e-9, total * 1e-9)):
            result["label_status"] = "unknown_invalid_closing_fill"
            return result
        availability.append(time.normalize() + pd.Timedelta(days=1))
        if "available_at" in fill:
            supplied = _stamp(fill["available_at"])
            if supplied is None:
                result["label_status"] = "unknown_fill_availability"
                return result
            availability.append(supplied)
        for detail in linked:
            close_id = _identifier(detail.get("close_event_id"))
            if close_id is not None:
                if close_id in seen_closes:
                    raise ValueError("duplicate authoritative close_event_id")
                seen_closes.add(close_id)
            qty, entry, cost = (_number(detail.get(key)) for key in ("qty_closed", "entry_price", "entry_cost_share"))
            if (entry is None or entry <= 0 or cost is None or cost < 0
                    or time < min(_stamp(f.get("fill_time")) for f in openings)):
                result["label_status"] = "unknown_incomplete_lot_costs"
                return result
            direction = 1. if side == "buy" else -1.
            pnl += direction * (price - entry) * qty - cost - fee * qty / total
            allocated_entry_cost += cost
            allocated_entry_notional += entry * qty
            closed_qty += qty
        reasons.append(str(fill.get("exit_reason", "unknown")))
    if closed_qty > quantity + max(1e-9, quantity * 1e-9):
        raise ValueError("closing quantities exceed observed opening fills")
    if abs(closed_qty - quantity) > max(1e-9, quantity * 1e-9):
        result["label_status"] = "unknown_unclosed_or_partial_exit"
        return result
    if abs(allocated_entry_cost - entry_cost) > max(1e-9, entry_cost * 1e-9):
        result["label_status"] = "unknown_entry_cost_reconciliation"
        return result
    if abs(allocated_entry_notional - notional) > max(1e-9, notional * 1e-9):
        result["label_status"] = "unknown_entry_price_reconciliation"
        return result
    result["actual_fill_net_pnl"] = pnl
    if synthetic_exit:
        result["label_status"] = "unknown_terminal_liquidation_not_strategy_exit"
        return result
    if not _true(identity.get("accounting_ok")) or _true(identity.get("terminated_by_risk")):
        result["label_status"] = "unknown_accounting_or_risk_termination"
        return result
    pending_count = _pending_count(identity.get("pending_orders"))
    if pending_count is None:
        result["label_status"] = "unknown_pending_order_state"
        return result
    if pending_count:
        result["label_status"] = "unknown_pending_order_completion"
        return result
    key = _identifier(decision.get("decision_id"))
    if identity.get("account_mode") != "spot":
        finance = _number(financing_costs.get(key))
        if not _true(identity.get("financing_attribution_complete")) or finance is None:
            result["label_status"] = "unknown_unattributed_financing"
            return result
        pnl -= finance
    mature = max(availability)
    result["label_available_at"] = mature
    result["label_exit_reason"] = "|".join(dict.fromkeys(reasons))
    if mature > cutoff:
        result["label_status"] = "unknown_not_yet_mature"
        return result
    result.update(label_status="mature_actual_exit", label_net_return=pnl / notional, label_net_pnl=pnl)
    return result


def build_candidate_dataset(decisions, fills, snapshots, *, identity, labels_as_of,
                            target_type="actual_exit", financing_costs=None):
    """Preserve every observed original-strategy candidate and daily group.

    ``snapshots`` is an already-created causal daily table; this function does
    not download data, rebuild features, replay accounts, or train a model.
    Post-decision gate facts are audit columns, never frozen market features.
    """
    if target_type not in {"actual_exit", "proxy"}:
        raise ValueError("target_type must be actual_exit or proxy; marginal targets require paired accounts")
    if not isinstance(identity, Mapping) or not identity.get("account_id") or not identity.get("data_identity"):
        raise ValueError("candidate datasets require account_id and data_identity")
    cutoff = _stamp(labels_as_of)
    if cutoff is None:
        raise ValueError("labels_as_of must be finite")
    decisions, fills = _rows(decisions), _rows(fills)
    snapshot_rows = _rows(snapshots)
    lookup = {}
    for source in snapshot_rows:
        as_of = _stamp(source.get("as_of"))
        if as_of is None or not source.get("symbol"):
            raise ValueError("feature snapshots require symbol/as_of")
        key = (str(source["symbol"]), as_of)
        if key in lookup:
            raise ValueError("duplicate causal feature snapshots")
        lookup[key] = source
    ids, orders, candidates = set(), set(), []
    for index, decision in enumerate(decisions):
        signal = _structure(decision.get("original_signal", decision.get("signal")), {})
        signal = signal if isinstance(signal, Mapping) else {}
        original = _true(decision.get("original_strategy_signal"))
        legacy = _true(decision.get("raw_setup")) or bool(signal) and _number(decision.get("native_score")) is not None
        if not original and not legacy:
            continue
        identifier = _identifier(decision.get("decision_id"))
        if identifier is not None and identifier in ids:
            raise ValueError("duplicate candidate decision_id")
        if identifier is not None:
            ids.add(identifier)
        order = _identifier(decision.get("order_id"))
        if order is not None and order in orders:
            raise ValueError("opening order linked to multiple candidates")
        if order is not None:
            orders.add(order)
        as_of = _stamp(decision.get("as_of"))
        # No symbol/date guess repairs a missing decision id or as-of time.
        snapshot = lookup.get((str(decision.get("symbol")), as_of), {})
        causal = as_of is not None
        if "available_at" in snapshot:
            supplied = _stamp(snapshot.get("available_at"))
            causal = causal and supplied is not None and supplied <= as_of
        if "history_available" in snapshot:
            causal = causal and _true(snapshot["history_available"])
        features = {name: _number(snapshot.get(name)) if causal else None for name in FEATURE_COLUMNS}
        feature_complete = bool(snapshot) and causal and all(value is not None for value in features.values())
        decision_context = _structure(decision.get("decision_context"), {})
        context_time = _stamp(decision.get("decision_context_available_at"))
        if not isinstance(decision_context, Mapping) or context_time is None or as_of is None or context_time > as_of:
            decision_context = {}
        price = _number(signal.get("price"))
        if price is None or price <= 0:
            price = _number(decision_context.get("current_price"))
        stop = _number(signal.get("stop_loss"))
        gates = _structure(decision.get("gate_facts"), [])
        gates = gates if isinstance(gates, list) else []
        post_gate = {"health_multiplier", "market_multiplier"}
        context = {name: decision.get(name) for name in CONTEXT_COLUMNS if name not in post_gate}
        for name in ("strategy_health", "health_multiplier", "market_multiplier"):
            # Health at sizing/submission is post-selection evidence. Only an
            # explicitly timed pre-decision snapshot may become a feature.
            context[name] = decision_context.get(name)
        context.update({"action": signal.get("action", decision.get("action")),
                        "signal_price": price, "signal_stop_loss": stop,
                        "stop_distance_fraction": abs(price - stop) / price if price and price > 0 and stop else None})
        row = {"candidate_id": identifier or f"unidentified:{index}", "decision_id": identifier,
               "account_id": identity["account_id"], "data_identity": identity["data_identity"],
               "protocol_id": identity.get("protocol_id"), "account_mode": identity.get("account_mode"),
               "initial_capital": identity.get("initial_capital"), "account_identity": dict(identity),
               "symbol": decision.get("symbol"), "as_of": as_of, "candidate_day": as_of.normalize() if as_of is not None else pd.NaT,
               "bar_time": _stamp(decision.get("bar_time", decision.get("timestamp"))),
               "original_signal": dict(signal), "candidate_source": "original_strategy" if original else "legacy_candidate",
               "decision_context_available_at": context_time if decision_context else None,
               "decision_identity_status": "identified" if identifier is not None else "unknown",
               "feature_status": "complete_causal_snapshot" if feature_complete else "unknown_or_incomplete_snapshot",
               "data_eligible": snapshot.get("eligible"), "membership_basis": snapshot.get("membership_basis"),
               "target_type": target_type, **features, **context,
               "audit_gate_facts": gates, "audit_final_reason": decision.get("reason"),
               "audit_health_multiplier": decision.get("health_multiplier"),
               "audit_market_multiplier": decision.get("market_multiplier"),
               "audit_capital_allocation": _structure(decision.get("capital_allocation"), {}),
               "audit_target_qty": _number(decision.get("target_qty")),
               "audit_approved_qty": _number(decision.get("approved_qty")),
               "audit_selected": decision.get("selected"), "portfolio_marginal_net_return": np.nan}
        if target_type == "actual_exit":
            target = _actual_exit(decision, fills, identity, cutoff, financing_costs or {})
            row.update(target, label_basis="observed_strategy_account_exit_not_independent_or_marginal")
        else:
            maturity = _stamp(snapshot.get("label_available_at"))
            value = _number(snapshot.get("label_net_return"))
            known = (maturity is not None and as_of is not None and as_of < maturity <= cutoff
                     and value is not None)
            basis = snapshot.get("label_basis", "independent_fixed_window_atr_proxy_not_actual_or_portfolio")
            direction_known = context["action"] == "buy"
            basis_known = isinstance(basis, str) and "proxy" in basis.lower()
            known = known and direction_known and basis_known
            row.update(label_net_return=value if known else np.nan, label_net_pnl=np.nan,
                       label_available_at=maturity, label_exit_reason=snapshot.get("label_exit_reason"),
                       label_status="mature_proxy" if known else "unknown_proxy_direction_or_basis" if not direction_known or not basis_known else "unknown_proxy_or_not_yet_mature",
                       label_basis=basis)
        if identifier is None or as_of is None:
            row.update(label_net_return=np.nan, label_net_pnl=np.nan, label_status="unknown_decision_identity_or_time")
        row["training_eligible"] = (feature_complete and identifier is not None and as_of is not None
                                    and row["label_status"] in {"mature_proxy", "mature_actual_exit"})
        candidates.append(row)
    frame = pd.DataFrame(candidates).reindex(columns=CANDIDATE_COLUMNS)
    frame["candidate_group_id"] = pd.Series(index=frame.index, dtype="object")
    frame["daily_group_complete"] = False
    frame["same_day_candidate_count"] = 0
    frame["same_day_labeled_count"] = 0
    groups = []
    if len(frame):
        frame = frame.sort_values(["as_of", "symbol", "candidate_id"], na_position="last").reset_index(drop=True)
        for day, group in frame.groupby("candidate_day", dropna=False, sort=True):
            indices = group.index
            group_id = f"{identity['account_id']}|{day.isoformat() if pd.notna(day) else 'unknown_day'}"
            frame.loc[indices, "candidate_group_id"] = group_id
            frame.loc[indices, "same_day_candidate_count"] = len(group)
            known = int(group.label_net_return.notna().sum())
            frame.loc[indices, "same_day_labeled_count"] = known
            frame.loc[indices, "daily_group_complete"] = known == len(group) and group.training_eligible.all()
            groups.append({"candidate_group_id": group_id, "candidates": len(group), "known_targets": known,
                           "unknown_targets": len(group) - known,
                           "complete_training_group": bool(known == len(group) and group.training_eligible.all())})
    contract = {"schema": "ml-strategy-candidates/v1", "target_type": target_type,
                "labels_as_of": cutoff.isoformat(), "account_identity": dict(identity),
                "market_feature_columns": list(FEATURE_COLUMNS), "context_columns": list(CONTEXT_COLUMNS),
                "post_decision_columns": "audit_* and all targets; prohibited as decision-time features",
                "unknown_target": "NaN; never zero-filled or silently dropped",
                "grouping": "complete observed account-day candidates, including rejected and unknown targets",
                "sampling_limits": "Unvisited original signals remain unknown. Filled targets are selection-conditioned.",
                "independent_holdout": False, "training_performed": False,
                "rows": len(frame), "label_status_counts": dict(Counter(frame.label_status)) if len(frame) else {},
                "groups": groups}
    contract["contract_id"] = hashlib.sha256(canonical_json(contract).encode()).hexdigest()
    return frame, contract


def compare_paired_accounts(control, treatment, *, labels_as_of):
    """Compare full aligned paths under an explicit single-candidate contract.

    This reads two already-run account paths. Identity declarations do not
    independently prove that the engine replay obeyed the intervention.
    """
    left, right = control.get("contract", {}), treatment.get("contract", {})
    for name in PAIR_IDENTITY_FIELDS:
        if not left.get(name) or not right.get(name):
            raise ValueError(f"paired accounts require {name}")
        if canonical_json(left[name]) != canonical_json(right[name]):
            raise ValueError(f"paired account identity mismatch: {name}")
    capital = _number(left["initial_capital"])
    if capital is None or capital <= 0:
        raise ValueError("paired initial_capital must be positive")
    a, b = left.get("intervention", {}), right.get("intervention", {})
    for name in ("candidate_id", "as_of"):
        if not a.get(name) or a.get(name) != b.get(name):
            raise ValueError(f"paired accounts require the same intervention {name}")
    if a.get("include_candidate") is not False or b.get("include_candidate") is not True:
        raise ValueError("paired intervention must exclude in control and include in treatment")
    at = _stamp(a["as_of"])
    start, end, cutoff = (_stamp(value) for value in
                          (left["evaluation_start"], left["evaluation_end"], labels_as_of))
    if start is None or end is None or at is None or cutoff is None or not start <= at <= end:
        raise ValueError("paired evaluation and intervention times must be ordered and finite")
    if left["timeframe"] != "1d":
        raise ValueError("paired candidate labels currently require daily equity observations")
    curves = []
    for account in (control, treatment):
        curve = account.get("equity_curve")
        if not isinstance(curve, pd.DataFrame) or curve.empty or "equity" not in curve:
            raise ValueError("paired accounts require complete equity curves")
        stamps = pd.DatetimeIndex(pd.to_datetime(curve.index, utc=True))
        values = pd.to_numeric(curve.equity, errors="coerce").to_numpy(dtype=float)
        if (stamps.hasnans or stamps.has_duplicates or not stamps.is_monotonic_increasing
                or not np.isfinite(values).all() or (values <= 0).any()):
            raise ValueError("paired equity curves require unique ordered finite positive observations")
        curves.append(pd.Series(values, index=stamps))
    if not curves[0].index.equals(curves[1].index):
        raise ValueError("paired equity time grids differ; intersection is prohibited")
    if curves[0].index[0] != start or curves[0].index[-1] != end:
        raise ValueError("paired equity curves do not cover the declared complete evaluation window")
    before = curves[0].index < at
    if not np.allclose(curves[0].to_numpy()[before], curves[1].to_numpy()[before], atol=1e-10, rtol=0):
        raise ValueError("paired accounts differ before the intervention")
    maturity = curves[0].index[-1].normalize() + pd.Timedelta(days=1)
    known = maturity <= cutoff and all(_true(account.get("accounting_ok")) and account.get("status") == "complete"
                and not _true(account.get("terminated_by_risk")) for account in (control, treatment))
    delta = float(curves[1].iloc[-1] - curves[0].iloc[-1]) if known else None
    return {"schema": "ml-paired-candidate-value/v1", "candidate_id": a["candidate_id"],
            "target_type": "paired_portfolio_marginal", "status": "paired_declared_contract" if known else "unknown_incomplete_account",
            "portfolio_marginal_net_pnl": delta,
            "portfolio_marginal_net_return": delta / capital if delta is not None else None,
            "label_available_at": maturity.isoformat(), "labels_as_of": cutoff.isoformat(),
            "contract_identity": {name: left[name] for name in PAIR_IDENTITY_FIELDS},
            "causal_scope": "Declared single-candidate intervention; execution compliance requires replay receipts.",
            "individual_trade_return": None, "independent_holdout": False}


def build_from_episode(run_dir, episode, output, *, labels_as_of, dataset_path=None, target_type="actual_exit"):
    """Read a frozen run and save a new dataset outside its source tree."""
    root, target = Path(run_dir).resolve(), Path(output).resolve()
    source = (root / episode).resolve()
    if not root.is_dir() or not source.is_relative_to(root) or not source.is_dir():
        raise FileNotFoundError(f"archived episode is missing or outside the run: {source}")
    if target == root or target.is_relative_to(root):
        raise ValueError("candidate output must be outside the immutable source run")
    if target.exists():
        raise FileExistsError("candidate output already exists; preserve prior datasets")
    manifest_path = root / "artifacts.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    receipts = {}

    def verified(path, *, allow_external=False):
        path = path.resolve()
        if not path.is_relative_to(root) and not allow_external:
            raise ValueError("archived candidate input escapes the immutable source run")
        if not path.is_file():
            raise FileNotFoundError(f"required archived candidate input is missing: {path}")
        digest = sha256_file(path)
        relative = path.relative_to(root).as_posix() if path.is_relative_to(root) else None
        expected = manifest.get(relative, manifest.get(relative.replace("/", "\\"))) if relative is not None else None
        if manifest and relative is not None and expected is None:
            raise ValueError(f"archived candidate input is unregistered: {relative}")
        if expected is not None and expected != digest:
            raise ValueError(f"archived candidate input changed: {relative}")
        receipts[str(path)] = {"sha256": digest, "manifest_verified": expected is not None}
        return path

    def csv(path, *, allow_external=False):
        try:
            return pd.read_csv(verified(path, allow_external=allow_external), float_precision="round_trip")
        except pd.errors.EmptyDataError:
            return pd.DataFrame()

    protocol = json.loads(verified(root / "protocol.json").read_text(encoding="utf-8"))
    protocol_verified = False
    if protocol.get("protocol_id"):
        value = {k: v for k, v in protocol.items() if k != "protocol_id"}
        if hashlib.sha256(canonical_json(value).encode()).hexdigest() != protocol["protocol_id"]:
            raise ValueError("frozen candidate protocol content changed")
        protocol_verified = True
    summary = json.loads(verified(source / "summary.json").read_text(encoding="utf-8"))
    decisions, fills = csv(source / "decision_ledger.csv"), csv(source / "fill_ledger.csv")
    snapshots = csv(Path(dataset_path) if dataset_path is not None else root / "dataset.csv", allow_external=dataset_path is not None)
    evidence = protocol.get("data_evidence", {})
    identity = {"account_id": source.relative_to(root).as_posix(), "protocol_id": protocol.get("protocol_id"),
                "data_identity": hashlib.sha256(canonical_json(evidence).encode()).hexdigest() if evidence else None,
                "data_evidence": evidence, "account_mode": protocol.get("engine_options", {}).get("account_mode", protocol.get("settings", {}).get("account_mode")),
                "parameters_identity": hashlib.sha256(canonical_json(protocol.get("parameters", {})).encode()).hexdigest(),
                "initial_capital": summary.get("initial_capital"), "accounting_ok": summary.get("accounting_ok"),
                "terminated_by_risk": summary.get("terminated_by_risk"), "terminal_policy": summary.get("terminal_policy"),
                "pending_orders": summary.get("pending_orders"),
                "source_protocol_verified": protocol_verified,
                "source_identity_status": "manifest_verified" if manifest and all(r["manifest_verified"] for r in receipts.values()) else "partial_missing_manifest_or_external_dataset"}
    frame, contract = build_candidate_dataset(decisions, fills, snapshots, identity=identity,
        labels_as_of=labels_as_of, target_type=target_type)
    contract["source_receipts"] = receipts
    target.mkdir(parents=True, exist_ok=False)
    export = frame.copy()
    for column in ("account_identity", "original_signal", "audit_gate_facts", "audit_capital_allocation"):
        if column in export:
            export[column] = export[column].map(canonical_json)
    export.to_csv(target / "candidates.csv", index=False)
    contract["candidate_file"] = {"path": "candidates.csv", "sha256": sha256_file(target / "candidates.csv"), "rows": len(frame)}
    contract["contract_id"] = hashlib.sha256(canonical_json({k: v for k, v in contract.items() if k != "contract_id"}).encode()).hexdigest()
    (target / "contract.json").write_text(json.dumps(json.loads(canonical_json(contract)), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return frame, contract


def load_candidate_training_rows(directory, *, target_type, account_mode, data_identity, training_as_of=None):
    """Validate an opt-in candidate artifact for future model fitting.

    This adapter never fits a model. It preserves complete account-day cohorts
    and returns an explicit receipt for exclusions. Observed actual-exit
    targets are conditional on historical execution, not marginal values or
    outcomes of rejected proposals. An insufficient dataset fails visibly.
    """
    root = Path(directory).resolve()
    path = root / "contract.json"
    if not path.is_file():
        raise FileNotFoundError(f"candidate training contract is missing: {path}")
    contract = json.loads(path.read_text(encoding="utf-8"))
    identity = contract.get("account_identity", {})
    if contract.get("schema") != "ml-strategy-candidates/v1":
        raise ValueError("unsupported candidate training artifact schema")
    expected = hashlib.sha256(canonical_json({k: v for k, v in contract.items() if k != "contract_id"}).encode()).hexdigest()
    if contract.get("contract_id") != expected:
        raise ValueError("candidate training contract content changed")
    if target_type not in {"proxy", "actual_exit"} or contract.get("target_type") != target_type:
        raise ValueError("candidate training target type mismatch")
    if not account_mode or identity.get("account_mode") != account_mode:
        raise ValueError("candidate training account mode mismatch")
    if not data_identity or identity.get("data_identity") != data_identity:
        raise ValueError("candidate training data identity mismatch")
    if identity.get("source_identity_status") != "manifest_verified" or identity.get("source_protocol_verified") is not True:
        raise ValueError("candidate training requires a verified frozen protocol and source manifest")
    if contract.get("market_feature_columns") != list(FEATURE_COLUMNS):
        raise ValueError("candidate training market feature contract differs from frozen features")
    sources = contract.get("source_receipts", {})
    if not sources or any(receipt.get("manifest_verified") is not True for receipt in sources.values()):
        raise ValueError("candidate training source receipts are incomplete")
    for source, receipt in sources.items():
        source_path = Path(source)
        if not source_path.is_file() or sha256_file(source_path) != receipt.get("sha256"):
            raise ValueError(f"candidate training source artifact missing or changed: {source_path}")
    artifact = contract.get("candidate_file", {})
    file_path = (root / artifact.get("path", "")).resolve()
    if not file_path.is_relative_to(root) or not file_path.is_file() or sha256_file(file_path) != artifact.get("sha256"):
        raise ValueError("candidate training rows missing or changed")
    frame = pd.read_csv(file_path, float_precision="round_trip")
    required = {*CANDIDATE_COLUMNS}
    if not required <= set(frame):
        raise ValueError("candidate training rows violate the column contract")
    if len(frame) != artifact.get("rows") or len(frame) != contract.get("rows"):
        raise ValueError("candidate training row count differs from its contract")
    if frame.empty:
        raise ValueError("insufficient candidate training data: no original-strategy candidates")
    for field, value in (("account_id", identity["account_id"]), ("account_mode", account_mode),
                         ("data_identity", data_identity), ("protocol_id", identity.get("protocol_id")), ("target_type", target_type)):
        if not value or not frame[field].eq(value).all():
            raise ValueError(f"candidate training row identity mismatch: {field}")
    identified = frame.decision_id.dropna().astype(str)
    if identified.duplicated().any() or frame.candidate_id.astype(str).duplicated().any():
        raise ValueError("candidate training rows contain duplicate identities")
    for name in ("as_of", "candidate_day", "label_available_at"):
        frame[name] = pd.to_datetime(frame[name], utc=True, errors="coerce")
    cutoff = _stamp(training_as_of if training_as_of is not None else contract["labels_as_of"])
    original_cutoff = _stamp(contract["labels_as_of"])
    if cutoff is None or original_cutoff is None:
        raise ValueError("candidate training requires a finite maturity cutoff")
    cutoff = min(cutoff, original_cutoff)
    for name in (*FEATURE_COLUMNS, "label_net_return"):
        frame[name] = pd.to_numeric(frame[name], errors="coerce")
    valid = (np.isfinite(frame[list(FEATURE_COLUMNS) + ["label_net_return"]].to_numpy(dtype=float)).all(axis=1)
             & frame.as_of.notna() & frame.label_available_at.notna()
             & (frame.label_available_at > frame.as_of) & (frame.label_available_at <= cutoff)
             & frame.decision_id.notna() & frame.feature_status.eq("complete_causal_snapshot")
             & frame.label_status.eq("mature_proxy" if target_type == "proxy" else "mature_actual_exit")
             & frame.training_eligible.map(_true))
    declared = {group["candidate_group_id"]: group for group in contract.get("groups", [])}
    included: list[str] = []
    excluded: list[str] = []
    seen = set()
    for group_id, group in frame.groupby("candidate_group_id", dropna=False, sort=True):
        item = declared.get(group_id)
        if item is None or len(group) != item.get("candidates") or group_id in seen:
            raise ValueError("candidate training groups differ from their complete-cohort contract")
        seen.add(group_id)
        days = group.as_of.dt.normalize()
        if days.nunique(dropna=False) != 1 or not days.equals(group.candidate_day):
            raise ValueError("candidate training account-day grouping is inconsistent")
        if (not group.same_day_candidate_count.eq(len(group)).all()
                or not group.same_day_labeled_count.eq(group.label_net_return.notna().sum()).all()):
            raise ValueError("candidate training group counters are inconsistent")
        complete = bool(valid.loc[group.index].all() and item.get("complete_training_group") is True
                        and group.daily_group_complete.map(_true).all())
        (included if complete else excluded).append(group_id)
    if seen != set(declared):
        raise ValueError("candidate training group inventory differs from its contract")
    if not included:
        raise ValueError("insufficient candidate training data: no complete mature account-day cohort; unknown targets cannot be zero-filled")
    selected = frame.loc[frame.candidate_group_id.isin(included)].copy().reset_index(drop=True)
    selected["eligible"] = True
    selected["exclusion_reason"] = ""
    receipt = {"schema": "ml-candidate-training-receipt/v1", "contract_id": contract["contract_id"],
               "candidate_file_sha256": artifact["sha256"], "source_protocol_id": identity.get("protocol_id"),
               "source_protocol_verified": True, "all_source_receipts_verified": True,
               "source_identity_status": identity["source_identity_status"], "data_identity": data_identity,
               "account_mode": account_mode, "target_type": target_type, "training_as_of": cutoff.isoformat(),
               "source_rows": len(frame), "admitted_rows": len(selected), "excluded_rows": len(frame) - len(selected),
               "complete_group_ids": included, "excluded_incomplete_group_ids": excluded,
               "label_provenance": "selection_conditioned_observed_strategy_exits" if target_type == "actual_exit" else "independent_long_proxy",
               "decision_features": list(FEATURE_COLUMNS), "audit_columns_are_predictors": False,
               "training_performed": False, "independent_holdout": False}
    selected.attrs["training_data_receipt"] = receipt
    return selected, receipt
