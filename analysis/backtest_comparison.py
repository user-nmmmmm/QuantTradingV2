"""Offline comparison of frozen main and paper reports; never runs an engine."""
from __future__ import annotations

import ast
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from core.reproducibility import sha256_frame


TOLERANCES = {"economic_usdt": {"atol": 1e-8, "rtol": 1e-10},
              "quantity_price": {"atol": 1e-12, "rtol": 1e-12},
              "dimensionless": {"atol": 1e-12, "rtol": 1e-10}, "time": "exact UTC instant"}
TIME_FIELDS = {"entry_time", "exit_time", "fill_time", "signal_time", "timestamp", "at",
               "opened_at", "closed_at", "occurred_at", "available_at", "label_end_time"}
ECONOMIC_WORDS = ("pnl", "equity", "cash", "commission", "cost", "slippage", "financing",
                  "amount", "initial_risk", "notional")


def _json(path: Path, *, optional=False):
    if not path.exists() and optional:
        return None
    return json.loads(path.read_text(encoding="utf-8-sig"))


def read_report_csv(path: str | Path) -> pd.DataFrame:
    """Empty CSV artifacts are valid zero-row evidence; missing files differ."""
    path = Path(path)
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_csv(path, float_precision="round_trip")
    except pd.errors.EmptyDataError:
        return pd.DataFrame()


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _within(root: Path, relative: str, *, prefix="") -> Path:
    target = (root / prefix / relative).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError(f"Report path escapes its directory: {relative}")
    return target


def _check_hash(path: Path, expected: str) -> None:
    if not path.is_file() or _hash(path) != expected:
        raise ValueError(f"Report/input artifact hash mismatch: {path}")


def _manifest_integrity(root: Path, manifest: Mapping) -> dict:
    if not manifest.get("artifacts") or not manifest.get("data_snapshots"):
        raise ValueError("Main manifest must register artifacts and data snapshots")
    for name, entry in manifest["artifacts"].items():
        path = _within(root, name)
        _check_hash(path, entry["sha256"])
        if "size" in entry and path.stat().st_size != entry["size"]:
            raise ValueError(f"Artifact size mismatch: {name}")
    for symbol, entry in manifest["data_snapshots"].items():
        path = _within(root, entry["path"], prefix="data_inputs")
        _check_hash(path, entry["sha256"])
        if entry["sha256"] != manifest["data"]["symbols"][symbol]["sha256"]:
            raise ValueError(f"Snapshot/data identity mismatch: {symbol}")
        if len(read_report_csv(path)) != entry["rows"]:
            raise ValueError(f"Snapshot row count mismatch: {symbol}")
    return {"registered_artifacts_verified": len(manifest["artifacts"]),
            "data_snapshots_verified": len(manifest["data_snapshots"]),
            "manifest_sha256": _hash(root / "run_manifest.json")}


def verify_main_reports(old_dir: str | Path, new_dir: str | Path) -> dict:
    old_dir, new_dir = Path(old_dir), Path(new_dir)
    old, new = _json(old_dir / "run_manifest.json"), _json(new_dir / "run_manifest.json")
    integrity = {"old": _manifest_integrity(old_dir, old), "new": _manifest_integrity(new_dir, new)}
    for key in ("config", "period"):
        if old[key] != new[key]:
            raise ValueError(f"Main {key} identity differs; this is not a same-input comparison")
    if old["data"] != new["data"]:
        raise ValueError("Main market data identity differs (source/venue/type/timezone/universe/frames)")
    if old["data_snapshots"] != new["data_snapshots"]:
        raise ValueError("Main data snapshot byte identities differ")
    execution_keys = ("capital", "account_mode", "alignment_mode", "timeframe", "warmup_period",
                      "benchmark_mode", "benchmark_rebalance_cost_bps", "seed", "random_slip", "slippage",
                      "data_symbol_order", "signal_observation", "signal_meta_layer",
                      "signal_adaptive", "signal_meta_replay")
    differences = [key for key in execution_keys if old["execution"].get(key) != new["execution"].get(key)]
    if differences:
        raise ValueError(f"Main execution assumptions differ: {differences}")
    return {"status": "verified_same_inputs", "integrity": integrity, "config_period_equal": True,
            "old_code": old.get("code"), "new_code": new.get("code"),
            "source_identity_expected_to_differ": True, "period": old["period"],
            "capital": old["execution"]["capital"]}


def _paper_integrity(root: Path, registration: Mapping) -> dict:
    for symbol, entry in registration["data"]["symbols"].items():
        path = Path(entry["path"])
        _check_hash(path, entry["sha256"])
        frame = pd.read_csv(path, index_col="timestamp", parse_dates=True, float_precision="round_trip")
        if sha256_frame(frame) != registration["frame_hashes"][symbol]:
            raise ValueError(f"Paper parsed frame identity mismatch: {symbol}")
    jobs = registration["jobs"]
    if len({job["name"] for job in jobs}) != len(jobs):
        raise ValueError("Duplicate paper job identities")
    results = _json(root / "results.json")
    if results["completed_runs"] != registration["maximum_runs"]:
        raise ValueError("Paper study is incomplete")
    expected = hashlib.sha256(json.dumps(registration, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    if results.get("registration_sha256") != expected:
        raise ValueError("Paper registration/result hash mismatch")
    for job in jobs:
        folder = _within(root, job["name"], prefix="runs")
        if _json(folder / "resolved_config.json") != job["parameters"]:
            raise ValueError(f"Paper resolved config mismatch: {job['name']}")
        summary = _json(folder / "summary.json")
        for key in ("start", "end"):
            if summary[key] != job[key]:
                raise ValueError(f"Paper period mismatch: {job['name']}")
    return {"completed_runs": results["completed_runs"], "registered_jobs": len(jobs),
            "input_bytes_verified": len(registration["data"]["symbols"]),
            "registration_sha256": expected,
            "report_integrity_limit": "paper baseline has no per-artifact hash manifest; current artifact hashes are recorded"}


def verify_paper_reports(old_dir: str | Path, new_dir: str | Path) -> dict:
    old_dir, new_dir = Path(old_dir), Path(new_dir)
    old, new = _json(old_dir / "registration.json"), _json(new_dir / "registration.json")
    integrity = {"old": _paper_integrity(old_dir, old), "new": _paper_integrity(new_dir, new)}
    required = ("config_sha256", "frame_hashes", "jobs", "candidate_family", "benchmark", "label_protocol",
                "maximum_runs", "statistics", "hypotheses", "observation", "health_reference")
    differences = [key for key in required if old.get(key) != new.get(key)]
    old_bytes = {s: e["sha256"] for s, e in old["data"]["symbols"].items()}
    new_bytes = {s: e["sha256"] for s, e in new["data"]["symbols"].items()}
    if differences or old_bytes != new_bytes or old["data"] != new["data"]:
        raise ValueError(f"Paper jobs/config/period/frame-byte identity differs: {differences}")
    coverage = "runs/coverage_full_history/resolved_config.json"
    if _json(old_dir / coverage) != _json(new_dir / coverage):
        raise ValueError("Paper coverage-run resolved parameters differ")
    return {"status": "verified_same_inputs", "integrity": integrity, "jobs_parameters_equal": True,
            "frame_and_byte_hashes_equal": True, "data_identity_equal": True,
            "source_code_and_output_paths_expected_to_differ": True,
            "jobs": [job["name"] for job in old["jobs"]] + ["coverage_full_history"]}


def _value(value):
    if isinstance(value, (np.integer, np.floating)):
        value = value.item()
    if isinstance(value, (dict, list, tuple)):
        return value
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, str) and value[:1] in ("{", "["):
        try:
            return ast.literal_eval(value)
        except (ValueError, SyntaxError):
            pass
    return value


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, allow_nan=False)


def _stamp(value):
    value = _value(value)
    if value is None or value == "":
        return None
    return pd.Timestamp(value).tz_localize("UTC").isoformat() if pd.Timestamp(value).tzinfo is None else pd.Timestamp(value).tz_convert("UTC").isoformat()


def _tolerance(field):
    name = field.lower()
    if any(word in name for word in ("qty", "price", "quantity", "slip_rate")) or name == "slip":
        return TOLERANCES["quantity_price"]
    if any(word in name for word in ECONOMIC_WORDS):
        return TOLERANCES["economic_usdt"]
    return TOLERANCES["dimensionless"]


def _equal(old, new, field=""):
    old, new = _value(old), _value(new)
    if old is None or new is None:
        return old is new
    if field.rsplit(".", 1)[-1] in TIME_FIELDS:
        return _stamp(old) == _stamp(new)
    if isinstance(old, dict) and isinstance(new, dict):
        return old.keys() == new.keys() and all(_equal(old[k], new[k], str(k)) for k in old)
    if isinstance(old, (list, tuple)) and isinstance(new, (list, tuple)):
        return len(old) == len(new) and all(_equal(a, b, field) for a, b in zip(old, new))
    if isinstance(old, bool) or isinstance(new, bool):
        return old == new
    if isinstance(old, (int, float)) and isinstance(new, (int, float)):
        tolerance = _tolerance(field)
        return math.isfinite(old) and math.isfinite(new) and math.isclose(
            old, new, abs_tol=tolerance["atol"], rel_tol=tolerance["rtol"])
    return old == new


def add_closed_directions(closed: pd.DataFrame, fills: pd.DataFrame) -> pd.DataFrame:
    result = closed.copy()
    if result.empty:
        return result
    directions = {}
    for row in fills.to_dict("records"):
        for lot in _value(row.get("lot_closes")) or []:
            if isinstance(lot, dict) and lot.get("position_id") and lot.get("side"):
                directions.setdefault(str(lot["position_id"]), set()).add(lot["side"])
    existing = result["direction"] if "direction" in result else pd.Series(None, index=result.index)
    result["direction"] = [value if _value(value) else
                           next(iter(directions.get(str(pid), set()))) if len(directions.get(str(pid), set())) == 1 else None
                           for value, pid in zip(existing, result["position_id"])]
    return result


def compare_trade_records(old: pd.DataFrame, new: pd.DataFrame, *, kind="closed") -> pd.DataFrame:
    """Strict semantic matching; reused IDs and ambiguous duplicate fills stay unpaired."""
    if kind not in {"closed", "fill"}:
        raise ValueError("kind must be closed or fill")
    def key(row):
        if kind == "closed":
            parts = [_value(row.get(k)) for k in ("position_id", "symbol", "strategy", "direction")]
            parts.append(_stamp(row.get("entry_time")))
        else:
            parts = [_value(row.get(k)) for k in ("order_id", "symbol", "strategy_id", "side")]
            parts.extend(_stamp(row.get(k)) for k in ("signal_time", "fill_time"))
            parts.append(_canonical(_value(row.get("close_event_ids")) or []))
        return tuple(parts) if all(part is not None and part != "" for part in parts) else None
    old_groups, new_groups = {}, {}
    for which, records in ((old_groups, old.to_dict("records")), (new_groups, new.to_dict("records"))):
        for ordinal, row in enumerate(records):
            identity = key(row)
            which.setdefault(identity, []).append((ordinal, row))
    output = []
    def emit(status, identity, a=None, b=None, reason=None):
        changes = []
        if a is not None and b is not None:
            changes = [f for f in sorted(set(a) | set(b)) if not _equal(a.get(f), b.get(f), f)]
        exemplar = b if b is not None else a
        output.append({"kind": kind, "status": status, "reason": reason,
                       "semantic_key": _canonical(identity), "position_id": exemplar.get("position_id"),
                       "order_id": exemplar.get("order_id"), "symbol": exemplar.get("symbol"),
                       "strategy": exemplar.get("strategy", exemplar.get("strategy_id")),
                       "direction": exemplar.get("direction", exemplar.get("side")),
                       "entry_time": exemplar.get("entry_time", exemplar.get("signal_time")),
                       "exit_time": exemplar.get("exit_time", exemplar.get("fill_time")),
                       "old_net_pnl": a.get("net_pnl") if a is not None else None,
                       "new_net_pnl": b.get("net_pnl") if b is not None else None,
                       "delta_net_pnl": b["net_pnl"] - a["net_pnl"] if a is not None and b is not None
                           and isinstance(a.get("net_pnl"), (int, float)) and isinstance(b.get("net_pnl"), (int, float)) else None,
                       "changed_fields": ",".join(changes),
                       "old_record": _canonical({k: _value(v) for k, v in a.items()}) if a is not None else None,
                       "new_record": _canonical({k: _value(v) for k, v in b.items()}) if b is not None else None})
    for identity in dict.fromkeys([*old_groups, *new_groups]):
        left, right = list(old_groups.get(identity, [])), list(new_groups.get(identity, []))
        if identity is not None and len(left) == len(right) == 1:
            a, b = left[0][1], right[0][1]
            changed = any(not _equal(a.get(f), b.get(f), f) for f in set(a) | set(b))
            emit("changed" if changed else "matched", identity, a, b)
            continue
        # For repeated partial-fill semantics, consume only exact multiset matches.
        if identity is not None:
            retained = []
            for _, a in left:
                exact = next((i for i, (_, b) in enumerate(right)
                              if all(_equal(a.get(f), b.get(f), f) for f in set(a) | set(b))), None)
                if exact is not None:
                    _, b = right.pop(exact)
                    emit("matched", identity, a, b)
                else:
                    retained.append((0, a))
            left = retained
        for _, a in left:
            emit("removed", identity, a=a, reason="missing semantic identity or unmatched/reused identity")
        for _, b in right:
            emit("added", identity, b=b, reason="missing semantic identity or unmatched/reused identity")
    return pd.DataFrame(output, columns=["kind", "status", "reason", "semantic_key", "position_id",
        "order_id", "symbol", "strategy", "direction", "entry_time", "exit_time", "old_net_pnl",
        "new_net_pnl", "delta_net_pnl", "changed_fields", "old_record", "new_record"])


def _flatten(value, prefix=""):
    if isinstance(value, dict):
        result = {}
        for key, entry in value.items():
            result.update(_flatten(entry, f"{prefix}.{key}" if prefix else str(key)))
        return result
    return {prefix: value}


def compare_metrics(old, new, *, scope):
    a, b = _flatten(old), _flatten(new)
    rows = []
    for field in sorted(set(a) | set(b)):
        x, y = a.get(field), b.get(field)
        numeric = isinstance(x, (int, float)) and isinstance(y, (int, float)) and not isinstance(x, bool)
        rows.append({"scope": scope, "field": field, "old": x, "new": y,
                     "delta": y - x if numeric else None, "equal": _equal(x, y, field)})
    return rows


def compare_tables(old, new, *, scope, time_column=None):
    a, b = old.copy(), new.copy()
    if time_column and time_column in a and time_column in b:
        a[time_column], b[time_column] = pd.to_datetime(a[time_column], utc=True), pd.to_datetime(b[time_column], utc=True)
    same_axis = a.columns.tolist() == b.columns.tolist() and len(a) == len(b)
    if time_column and time_column in a and time_column in b:
        same_axis = same_axis and a[time_column].tolist() == b[time_column].tolist()
    changed_cells, max_economic_difference = 0, 0.0
    if same_axis:
        for column in a.columns:
            for x, y in zip(a[column], b[column]):
                if not _equal(x, y, column):
                    changed_cells += 1
                if any(word in column.lower() for word in ECONOMIC_WORDS):
                    if isinstance(x, (int, float)) and isinstance(y, (int, float)) and math.isfinite(x) and math.isfinite(y):
                        max_economic_difference = max(max_economic_difference, abs(y - x))
    return {"scope": scope, "old_rows": len(a), "new_rows": len(b), "axis_equal": same_axis,
            "changed_cells": changed_cells if same_axis else None,
            "maximum_economic_absolute_difference": max_economic_difference if same_axis else None,
            "equal": same_axis and changed_cells == 0}


def _jsonl(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def _decode_event(value):
    if isinstance(value, list):
        return [_decode_event(v) for v in value]
    if isinstance(value, dict):
        tag = value.get("__qt_type__")
        if tag == "datetime":
            return value["value"]
        if tag == "mapping":
            return {k: _decode_event(v) for k, v in value["items"]}
        if tag == "structured_payload":
            return _decode_event(value["data"])
        return {k: _decode_event(v) for k, v in value.items() if k != "observed_at"}
    return value


def report_trade_history(root: str | Path, *, initial_capital=10000.0):
    """Realized fill-price PnL minus commission; slippage is already in fills."""
    root = Path(root)
    fills, closed = read_report_csv(root / "trades.csv"), read_report_csv(root / "closed_trades.csv")
    closed = add_closed_directions(closed, fills)
    routing = read_report_csv(root / "routing_log.csv")
    entry = read_report_csv(root / "entry_observations.csv")
    if entry.empty:
        entry = read_report_csv(root / "entry_audit.csv")
    health = read_report_csv(root / "strategy_health_timeline.csv")
    cohorts = read_report_csv(root / "cohort_trades.csv")
    finance_path = root / "financing_ledger.csv"
    financing = read_report_csv(finance_path)
    if "amount" in financing:
        amounts = pd.to_numeric(financing["amount"], errors="raise").to_numpy(float)
        if not np.isfinite(amounts).all():
            raise ValueError("Financing amount contains missing/nonfinite expense; zero cannot be inferred")
        ledger_amount = float(amounts.sum())
    else:
        ledger_amount = 0.0 if finance_path.exists() and financing.empty else None
    records = []
    entry_orders = {}
    for fill in fills.to_dict("records"):
        for lot in _value(fill.get("lot_closes")) or []:
            if isinstance(lot, dict):
                entry_orders.setdefault(lot.get("position_id"), set()).add(lot.get("entry_order_id"))
    for row in closed.to_dict("records"):
        row = {k: _value(v) for k, v in row.items()}
        row["realized_year"] = pd.Timestamp(row["exit_time"]).year
        row["holding_days"] = (pd.Timestamp(_stamp(row["exit_time"])) - pd.Timestamp(_stamp(row["entry_time"]))).total_seconds() / 86400
        priced = []
        for fill in fills.to_dict("records"):
            if fill.get("symbol") != row["symbol"]:
                continue
            for lot in _value(fill.get("lot_closes")) or []:
                if (isinstance(lot, dict) and lot.get("position_id") == row["position_id"]
                        and lot.get("strategy_id", row.get("strategy")) == row.get("strategy")
                        and lot.get("side") == row.get("direction")):
                    quantity = lot.get("qty_closed")
                    if all(isinstance(v, (int, float)) and math.isfinite(v)
                           for v in (quantity, lot.get("entry_price"), fill.get("fill_price"))) and quantity > 0:
                        priced.append((quantity, lot["entry_price"], fill["fill_price"]))
        matched_quantity = sum(x[0] for x in priced)
        complete_prices = (isinstance(row.get("qty"), (int, float)) and row["qty"] > 0
                           and _equal(matched_quantity, row["qty"], "qty"))
        row["price_matched_close_qty"] = matched_quantity
        row["entry_fill_price"] = sum(q * p for q, p, _ in priced) / matched_quantity if complete_prices else None
        row["exit_fill_price"] = sum(q * p for q, _, p in priced) / matched_quantity if complete_prices else None
        row["fill_price_evidence"] = "complete position/symbol/strategy/direction quantity-matched closing lot records" if complete_prices else "unknown: lot evidence does not cover closed quantity"
        if finance_path.exists() and financing.empty:
            row["allocated_financing_expense"], row["financing_allocation_status"] = 0.0, "zero_posted"
        elif "position_id" in financing and "amount" in financing and (closed.position_id == row["position_id"]).sum() == 1:
            selected_finance = financing[financing.position_id == row["position_id"]]
            if "symbol" in selected_finance:
                selected_finance = selected_finance[selected_finance.symbol == row["symbol"]]
            row["allocated_financing_expense"] = float(selected_finance.amount.sum())
            row["financing_allocation_status"] = "position_id_allocated"
        else:
            row["allocated_financing_expense"], row["financing_allocation_status"] = None, "unallocated" if finance_path.exists() else "missing_ledger"
        row["entry_state"] = None
        row["entry_state_source"] = "unknown"
        signals = [f for f in fills.to_dict("records")
                   if f.get("order_id") in entry_orders.get(row["position_id"], set())
                   and _stamp(f.get("fill_time")) == _stamp(row["entry_time"])]
        if not routing.empty and "regime" in routing and "timestamp" in routing:
            dates = pd.to_datetime(routing.timestamp, utc=True)
            states = set()
            for fill in signals:
                relevant = routing[(routing.symbol == row["symbol"]) & (dates == pd.Timestamp(_stamp(fill.get("signal_time"))))]
                if len(relevant) == 1:
                    states.add(relevant.regime.iloc[0])
            if len(states) == 1:
                row["entry_state"] = next(iter(states))
                row["entry_state_source"] = "exact first-entry signal-time routing row; later add-ons excluded"
            elif len(states) > 1:
                row["entry_state_source"] = "unknown: mixed first-entry signal states"
        gross, commission, net = (row.get(k) for k in ("gross_pnl", "commission", "net_pnl"))
        row["pnl_bridge_error"] = gross - commission - net if all(isinstance(v, (int, float)) for v in (gross, commission, net)) else None
        row["embedded_execution_deviation"] = row["gross_pnl_theoretical"] - gross if isinstance(row.get("gross_pnl_theoretical"), (int, float)) and isinstance(gross, (int, float)) else None
        row["net_pnl_after_allocated_financing"] = net - row["allocated_financing_expense"] if isinstance(net, (int, float)) and row["allocated_financing_expense"] is not None else None
        records.append(row)
    history = pd.DataFrame(records)
    if not history.empty:
        history = history.sort_values(["exit_time", "symbol", "position_id"], kind="stable")
    pnls = history.net_pnl.to_numpy(float) if "net_pnl" in history else np.array([])
    if not np.isfinite(pnls).all():
        raise ValueError("Closed trade net PnL contains missing/nonfinite values")
    loss_streak = longest = 0
    streak_total = longest_total = 0.0
    for pnl in pnls:
        loss_streak = loss_streak + 1 if pnl < 0 else 0
        streak_total = streak_total + pnl if pnl < 0 else 0.0
        if loss_streak > longest:
            longest, longest_total = loss_streak, streak_total
    groups = {}
    for column in ("symbol", "realized_year", "exit_reason", "entry_state"):
        groups[column] = []
        if column in history:
            for group, rows in history.groupby(column, dropna=False, sort=True):
                groups[column].append({"group": _value(group), "closed_trades": len(rows),
                    "net_pnl_before_separate_financing": float(rows.net_pnl.sum()),
                    "commission": float(rows.commission.sum()) if "commission" in rows else None,
                    "wins": int((rows.net_pnl > 0).sum()), "losses": int((rows.net_pnl < 0).sum())})
    costs = {"fill_commission": float(fills.commission.sum()) if "commission" in fills else 0.0,
             "reported_fill_slippage": 0.0, "reported_fill_impact": 0.0,
             "separate_financing_net_expense": ledger_amount,
             "financing_evidence": "valid empty ledger: zero posted" if finance_path.exists() and financing.empty else "missing ledger: unknown" if not finance_path.exists() else "amount positive expense, negative credit",
             "semantics": "slippage/impact already reflected in fill prices; do not subtract again; financing is separately posted in equity"}
    for row in fills.to_dict("records"):
        detail = _value(row.get("costs")) or {}
        for field, key in (("slippage", "reported_fill_slippage"), ("impact", "reported_fill_impact")):
            if isinstance(detail, dict) and isinstance(detail.get(field), (int, float)):
                costs[key] += detail[field]
    yearly_fills = dict(Counter(pd.to_datetime(fills.fill_time, utc=True).dt.year)) if "fill_time" in fills else {}
    fills_known = (root / "trades.csv").exists() and (fills.empty or "fill_time" in fills)
    period_manifest = _json(root / "run_manifest.json", optional=True)
    period_summary = _json(root / "summary.json", optional=True)
    no_trade_evidence = {"2026_fills": yearly_fills.get(2026, 0) if fills_known else None,
                         "fill_evidence": "valid artifact" if fills_known else "unknown: missing artifact or fill_time",
                         "period": period_manifest.get("period") if period_manifest else
                                   {k: period_summary.get(k) for k in ("start", "end")} if period_summary else None,
                         "entry_audit_present": not entry.empty,
                         "cohort_rows": len(cohorts), "health_transitions": len(health),
                         "limitations": []}
    if "timestamp" in routing:
        subset = routing[pd.to_datetime(routing.timestamp, utc=True).dt.year == 2026]
        no_trade_evidence["2026_routing_rows"] = len(subset)
        no_trade_evidence["2026_route_states"] = dict(Counter(subset.get("regime", [])))
        no_trade_evidence["2026_routed_strategies"] = dict(Counter(subset.get("strategy", [])))
    if "at" in health:
        subset = health[pd.to_datetime(health["at"], utc=True).dt.year == 2026]
        no_trade_evidence["2026_health_transition_reasons"] = dict(Counter(subset.get("reason", [])))
    snapshot = _json(root / "strategy_health.json", optional=True)
    if snapshot:
        no_trade_evidence["health_setup_facts"] = {
            strategy: {field: state.get(field) for field in ("raw_setup_count", "last_raw_setup_at",
                "suppressed_raw_setups", "last_suppressed_setup_at", "allows_new_entries", "status",
                "risk_multiplier", "recovery_blockers")}
            for strategy, state in snapshot.items() if isinstance(state, dict)}
    if entry.empty:
        no_trade_evidence["limitations"].append("缺入场逐候选审计；routing/health/cohort仅证明已记录事实，不能判定每个未成交候选的拒绝原因")
    no_trade_evidence["limitations"].append("未被路由选中的策略潜在setup属于反事实未知；健康抑制计数为0时不能称健康规则拒绝了未记录setup")
    gains, loss_magnitude = float(pnls[pnls > 0].sum()), float(-pnls[pnls < 0].sum())
    top_losses = history[history.net_pnl < 0].nsmallest(10, "net_pnl").to_dict("records") if len(history) else []
    concentrations = {str(k): float(np.sort(pnls[pnls > 0])[-k:].sum() / gains) if gains else None for k in (1, 3, 5)}
    return history, {"closed_trades": len(history), "fills": len(fills), "fill_years": yearly_fills,
        "net_closed_pnl_before_separate_financing": float(pnls.sum()), "gross_positive_pnl": gains,
        "gross_loss_magnitude": loss_magnitude, "profit_factor": gains / loss_magnitude if loss_magnitude else None,
        "longest_consecutive_losing_closes": longest, "positive_pnl_concentration": concentrations,
        "longest_loss_streak_total": longest_total,
        "top_losses": top_losses, "groups": groups, "costs": costs,
        "2026_no_trade_evidence": no_trade_evidence,
        "initial_capital": initial_capital, "pnl_basis": "actual fill-price realized PnL minus commissions; before separate financing"}


def _status_table(table):
    return dict(Counter(table.status)) if "status" in table else {}


def _write_json(path, value):
    def normalize(v):
        if isinstance(v, dict):
            return {str(k): normalize(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [normalize(x) for x in v]
        v = _value(v)
        return None if isinstance(v, float) and not math.isfinite(v) else v
    path.write_text(json.dumps(normalize(value), ensure_ascii=False, indent=2, default=str, allow_nan=False) + "\n", encoding="utf-8")


def _plots(old, new, output, capital):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    fonts = {f.name for f in font_manager.fontManager.ttflist}
    plt.rcParams["font.sans-serif"] = [f for f in ("Microsoft YaHei", "SimHei", "DejaVu Sans") if f in fonts]
    plt.rcParams["axes.unicode_minus"] = False
    if "timestamp" not in old or "timestamp" not in new:
        raise ValueError("Equity plot needs timestamp and equity columns")
    a = old.set_index(pd.to_datetime(old.timestamp, utc=True)).equity
    b = new.set_index(pd.to_datetime(new.timestamp, utc=True)).equity
    aligned = pd.concat({"old": a, "new": b}, axis=1)
    for name, title in (("equity_comparison", "新旧净值（USDT）"), ("drawdown_comparison", "新旧回撤（%）"), ("equity_difference", "新净值减旧净值（USDT）")):
        fig, ax = plt.subplots(figsize=(11, 4.5), constrained_layout=True)
        if name == "equity_difference":
            ax.plot(aligned.index, aligned["new"] - aligned["old"], color="#7b3fa1")
            ax.axhline(0, color="#999999", linewidth=.7)
        else:
            for series, color, label in ((a, "#456a8a", "旧报告"), (b, "#c87538", "合并后报告")):
                values = series if name == "equity_comparison" else 100 * (series / series.cummax().clip(lower=capital) - 1)
                ax.plot(values.index, values, label=label, color=color, linewidth=1.2)
            ax.legend()
        ax.set_title(title)
        ax.grid(alpha=.2)
        fig.savefig(output / f"{name}.png", dpi=150)
        plt.close(fig)


def generate_comparison(old_main, new_main, old_paper, new_paper, output_dir, merge_sha):
    """Verify identities first, then create an exclusive reviewable report directory."""
    output = Path(output_dir)
    if output.exists():
        raise FileExistsError("Comparison output directory must not exist")
    old_main, new_main, old_paper, new_paper = map(Path, (old_main, new_main, old_paper, new_paper))
    if any(output.resolve().is_relative_to(root.resolve()) for root in (old_main, new_main, old_paper, new_paper)):
        raise ValueError("Comparison output cannot be inside a frozen input report")
    main_identity, paper_identity = verify_main_reports(old_main, new_main), verify_paper_reports(old_paper, new_paper)
    rows = compare_metrics(_json(old_main / "metrics.json")["metrics"], _json(new_main / "metrics.json")["metrics"], scope="main.metrics")
    for filename in ("strategy_health.json", "backtest_lifecycle.json", "accounting_check.json",
                     "account_cost_contract.json", "benchmark_metadata.json", "reconciliation.json",
                     "breaker_state.json", "protection_summary.json"):
        rows.extend(compare_metrics({"present": (old_main / filename).exists(), "value": _json(old_main / filename, optional=True)},
            {"present": (new_main / filename).exists(), "value": _json(new_main / filename, optional=True)}, scope=f"main.{filename}"))
    old_history, old_analysis = report_trade_history(old_main, initial_capital=main_identity["capital"])
    new_history, new_analysis = report_trade_history(new_main, initial_capital=main_identity["capital"])
    trade_diffs = compare_trade_records(old_history.drop(columns=["entry_state", "entry_state_source"], errors="ignore"),
        new_history.drop(columns=["entry_state", "entry_state_source"], errors="ignore"))
    fill_diffs = compare_trade_records(read_report_csv(old_main / "trades.csv"), read_report_csv(new_main / "trades.csv"), kind="fill")
    main_tables = []
    for filename in ("equity.csv", "benchmark.csv", "benchmark_fixed.csv", "benchmark_dynamic.csv",
                     "execution_audit.csv", "entry_observations.csv", "entry_audit.csv",
                     "routing_log.csv", "cohort_trades.csv", "strategy_health_timeline.csv", "financing_ledger.csv"):
        a, b = read_report_csv(old_main / filename), read_report_csv(new_main / filename)
        item = compare_tables(a, b, scope=f"main.{filename}", time_column="timestamp" if "timestamp" in a else None)
        item.update(old_present=(old_main / filename).exists(), new_present=(new_main / filename).exists())
        item["equal"] = item["equal"] and item["old_present"] == item["new_present"]
        item["evidence_status"] = "compared" if item["old_present"] and item["new_present"] else "missing_both" if not item["old_present"] and not item["new_present"] else "artifact_presence_changed"
        main_tables.append(item)
    event_rows = compare_metrics({"events": [_decode_event(e) for e in _jsonl(old_main / "event_log.jsonl")]},
        {"events": [_decode_event(e) for e in _jsonl(new_main / "event_log.jsonl")]}, scope="main.events")
    rows.extend(event_rows)
    paper_job_rows, paper_differences = [], []
    for job in paper_identity["jobs"]:
        aroot, broot = old_paper / "runs" / job, new_paper / "runs" / job
        summary_rows = compare_metrics(_json(aroot / "summary.json"), _json(broot / "summary.json"), scope=f"paper.{job}.summary")
        rows.extend(summary_rows)
        ah, aa = report_trade_history(aroot)
        bh, ba = report_trade_history(broot)
        diff = compare_trade_records(ah, bh)
        diff["job"] = job
        fills = compare_trade_records(read_report_csv(aroot / "trades.csv"), read_report_csv(broot / "trades.csv"), kind="fill")
        fills["job"] = job
        paper_differences.extend((diff, fills))
        daily = compare_tables(read_report_csv(aroot / "daily.csv"), read_report_csv(broot / "daily.csv"), scope=f"paper.{job}.daily", time_column="timestamp")
        ad, bd = _json(aroot / "digest.json"), _json(broot / "digest.json")
        paper_job_rows.append({"job": job, "summary_equal": all(r["equal"] for r in summary_rows),
            "daily_equal": daily["equal"], "digest_equal": ad == bd,
            "old_trade_count": len(ah), "new_trade_count": len(bh), "closed_statuses": _canonical(_status_table(diff)),
            "fill_statuses": _canonical(_status_table(fills)),
            "old_maximum_loss": min((r["net_pnl"] for r in aa["top_losses"]), default=None),
            "new_maximum_loss": min((r["net_pnl"] for r in ba["top_losses"]), default=None),
            "maximum_equity_difference": daily["maximum_economic_absolute_difference"],
            "old_end_return_pct": _json(aroot / "summary.json").get("return_pct"),
            "new_end_return_pct": _json(broot / "summary.json").get("return_pct"),
            "digest_changed_fields": ",".join(k for k in set(ad) | set(bd) if ad.get(k) != bd.get(k))})
    for filename in ("signal_meta_layer.json", "signal_adaptive.json", "label_chronological_baseline.json",
                     "triple_barrier.json", "label_cpcv.json", "statistics.json"):
        rows.extend(compare_metrics(_json(old_paper / filename), _json(new_paper / filename), scope=f"paper.{filename}"))
    paper_diffs = pd.concat(paper_differences, ignore_index=True)
    comparisons = pd.DataFrame(rows)
    paper_jobs = pd.DataFrame(paper_job_rows)
    main_changed = not comparisons[comparisons.scope.str.startswith("main.")]["equal"].all() or any(not t["equal"] for t in main_tables) or any(s != "matched" for s in trade_diffs.status) or any(s != "matched" for s in fill_diffs.status)
    paper_changed = not comparisons[comparisons.scope.str.startswith("paper.")]["equal"].all() or not paper_jobs.summary_equal.all() or not paper_jobs.daily_equal.all() or not paper_jobs.digest_equal.all() or any(s != "matched" for s in paper_diffs.status)
    old_manifest, new_manifest = _json(old_main / "run_manifest.json"), _json(new_main / "run_manifest.json")
    old_digests, new_digests = old_manifest["execution"].get("result_digest", {}), new_manifest["execution"].get("result_digest", {})
    economic_digests = {key: old_digests.get(key) == new_digests.get(key) for key in ("equity", "trades", "benchmark")}
    core_trade_fields = {"gross_pnl", "gross_pnl_theoretical", "net_pnl", "commission", "slippage", "qty",
                         "fill_price", "theoretical_price", "costs", "exit_time", "exit_reason", "exit_strategy"}
    def economic_trade_change(table):
        return any(row["status"] in ("added", "removed") or
                   (row["status"] == "changed" and bool(set(row["changed_fields"].split(",")) & core_trade_fields))
                   for row in table.to_dict("records"))
    economic_tables = {"main.equity.csv", "main.benchmark.csv", "main.benchmark_fixed.csv", "main.benchmark_dynamic.csv"}
    economic_metric_fields = {"CAGR", "MaxDrawdownPct", "MaxDrawdownAmount", "CurrentDrawdownPct", "SharpeRatio",
                              "EndEquity", "TotalReturn", "ProfitFactor", "WinRate", "TotalPnL", "TotalTrades"}
    main_economic_changed = (any(not table["equal"] for table in main_tables if table["scope"] in economic_tables)
        or any(not row["equal"] for row in rows if row["scope"] == "main.metrics" and row["field"] in economic_metric_fields)
        or economic_trade_change(trade_diffs) or economic_trade_change(fill_diffs))
    report_evidence_changed = (any(not table["equal"] for table in main_tables if table["scope"] not in economic_tables)
        or any(not row["equal"] for row in rows if row["scope"].startswith("main.") and row["scope"] != "main.metrics"))
    result = {"schema": "post-merge-backtest-comparison/v1", "merge_sha": merge_sha,
        "status": "changed" if main_changed or paper_changed else "identical_within_tolerance",
        "main_changed": main_changed, "paper_changed": paper_changed, "tolerances": TOLERANCES,
        "main_economic_changed": main_economic_changed, "report_evidence_changed": report_evidence_changed,
        "source_metadata_changed": old_manifest.get("code") != new_manifest.get("code"),
        "main_economic_digest_equal": economic_digests,
        "main_report_payload_digest_equal": old_digests.get("report_payload") == new_digests.get("report_payload"),
        "digest_interpretation": "exact-byte deterministic identity is reported separately from floating tolerance economic equality",
        "identity": {"main": main_identity, "paper": paper_identity}, "main_tables": main_tables,
        "main_closed_statuses": _status_table(trade_diffs), "main_fill_statuses": _status_table(fill_diffs),
        "paper_closed_and_fill_statuses": _status_table(paper_diffs),
        "main_trade_analysis": {"old": old_analysis, "new": new_analysis},
        "artifact_sources": {label: {str(p.relative_to(root)): _hash(p) for p in sorted(root.rglob("*")) if p.is_file() and p.suffix in {".json", ".jsonl", ".csv"}}
            for label, root in (("old_main", old_main), ("new_main", new_main), ("old_paper", old_paper), ("new_paper", new_paper))},
        "interpretation": "同输入离线新旧结果比较；不能证明独立样本优势、真实执行校准或资金准入"}
    output.mkdir(parents=True, exist_ok=False)
    comparisons.to_csv(output / "comparison.csv", index=False)
    pd.concat([trade_diffs, fill_diffs], ignore_index=True).to_csv(output / "trade_differences.csv", index=False)
    new_history.to_csv(output / "main_trade_history.csv", index=False)
    paper_jobs.to_csv(output / "paper_job_comparison.csv", index=False)
    paper_diffs.to_csv(output / "paper_trade_differences.csv", index=False)
    _write_json(output / "comparison.json", result)
    _plots(read_report_csv(old_main / "equity.csv"), read_report_csv(new_main / "equity.csv"), output, main_identity["capital"])
    changes = comparisons[~comparisons["equal"]]
    text = ["# 合并后回测比较", "", f"合并版本：`{merge_sha}`。比较状态：**{result['status']}**。",
        "", "旧主报告的全部已登记产物和数据快照、新主报告登记产物均通过字节哈希检查；配置与研究期间一致。论文实验的全部参数、窗口、帧身份及输入字节哈希一致。源码与运行目录变化单独记录。",
        "", f"主回测闭合交易：{_canonical(result['main_closed_statuses'])}；成交：{_canonical(result['main_fill_statuses'])}。论文研究比较了{len(paper_jobs)}个运行，包含独立的覆盖诊断运行。",
        "", f"经济结果变化：{main_economic_changed}；报告证据变化：{report_evidence_changed}；源码元数据变化：{result['source_metadata_changed']}。主经济digest逐项严格一致：{_canonical(economic_digests)}。新增空审计文件只表示证据覆盖变化，不直接说明交易行为改变；main_changed是这些比较的广义变化。",
        "", f"逐字段结果差异共{len(changes)}项；经济金额采用atol=1e-8 USDT、rtol=1e-10，数量和价格采用atol=rtol=1e-12，时间严格相同。ID相同但标的、策略、方向或入场时间变化时列为新增/移除。",
        "", "论文旧报告没有逐产物哈希manifest，因此只能核验其原登记输入与配置，并记录本次读取产物的哈希；不能宣称旧论文所有输出在生成后始终未被修改。digest变化与经济数值变化分别列出，不把源码身份变化自动解释为收益变化。",
        "", "参见comparison.csv全部字段、trade_differences.csv全部成交与闭合交易语义配对、paper_job_comparison.csv逐运行比较。差异文件保留matched行，便于逐笔复核。",
        "", "![净值比较](equity_comparison.png)", "", "![回撤比较](drawdown_comparison.png)", "", "![净值差](equity_difference.png)",
        "", "这是对已见历史的离线回归比较，不构成论文收益复现、前瞻成熟样本、真实成本校准或资金放行。"]
    (output / "comparison.md").write_text("\n".join(text) + "\n", encoding="utf-8")
    analysis = new_analysis
    text = ["# 交易历史分析", "", f"合并后主回测有{analysis['closed_trades']}笔闭合交易、{analysis['fills']}笔成交。闭合净PnL（单独融资前）{analysis['net_closed_pnl_before_separate_financing']:.8f} USDT，最长连续亏损闭合交易{analysis['longest_consecutive_losing_closes']}笔，合计{analysis['longest_loss_streak_total']:.8f} USDT。",
        "", "逐笔事实写入main_trade_history.csv；交易按退出时间稳定排序。同日不同标的的交易不能当成独立趋势样本，按交易排序的连亏并非独立事件证明。",
        "", f"成本分解：{_canonical(analysis['costs'])}。成交价格已经体现滑点与冲击，闭合交易使用成交价毛PnL减佣金，不能再次减滑点。融资账本单独披露且已经作用于权益，不再次从净值扣除。旧账本若未声明currency，USDT口径依据配置中的报价与结算身份推定，不能作为其他币种账本通用假设。",
        "", f"盈利集中度（最大1/3/5笔占正PnL）：{_canonical(analysis['positive_pnl_concentration'])}。",
        "", "## 最大亏损与变化", "", "|标的|策略|入场|退出|退出原因|净PnL USDT|", "|---|---|---|---|---|---:|"]
    for trade in analysis["top_losses"]:
        text.append(f"|{trade.get('symbol')}|{trade.get('strategy')}|{trade.get('entry_time')}|{trade.get('exit_time')}|{trade.get('exit_reason')}|{trade.get('net_pnl'):.8f}|")
    for dimension, groups in analysis["groups"].items():
        text.extend(["", f"## 按{dimension}归因", "", "|组别|闭合交易|净PnL（融资前）USDT|佣金USDT|赢/亏|", "|---|---:|---:|---:|---|"])
        for group in groups:
            text.append(f"|{group['group'] if group['group'] is not None else '未知'}|{group['closed_trades']}|{group['net_pnl_before_separate_financing']:.8f}|{group['commission']}|{group['wins']}/{group['losses']}|")
    text.extend(["", "## 2026年未成交或成交减少的证据", "", f"截至本次冻结主回测结束日，2026年成交数为{analysis['2026_no_trade_evidence']['2026_fills']}。记录事实：{_canonical(analysis['2026_no_trade_evidence'])}。",
        "", "只有与入场信号时间、标的精确对应的routing行才用于入场状态归因；缺少对应行的状态为未知。健康状态、路由和cohort可解释记录过的约束，但不能替代缺失的逐候选拒绝审计，不能把没有成交直接归因于某个未证实的过滤器。",
        "", "论文43个运行的最大亏损、闭合数量及新增/移除/变化见paper_job_comparison.csv及paper_trade_differences.csv；各运行窗口或成本不同，交易不能跨运行合并成独立样本。"])
    (output / "trade_history_analysis.md").write_text("\n".join(text) + "\n", encoding="utf-8")
    return {"output_dir": str(output.resolve()), "comparison": result,
            "files": [str(path.resolve()) for path in sorted(output.iterdir())]}
