"""Read-only comparison of independent, explicitly normalized account facts.

This is not a ledger and does not manufacture opening capital or cash flows.
Callers supply the local projection and the independently captured venue facts.
Unknown facts fail closed. Both periodic and EOD consumers can persist the same
report, but the live account/cash-flow adapters must establish those inputs.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any, Mapping


_METADATA = frozenset({"captured_at", "snapshot_id", "transport_latency_ms"})


def _report_value(value):
    """Keep invalid numeric evidence readable in a strict JSON failure report."""
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, dict):
        return {key: _report_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_report_value(item) for item in value]
    return value


def reconcile_account_snapshots(
    expected: Mapping[str, Any], actual: Mapping[str, Any], *,
    checked_at: datetime, numeric_tolerance: float = 1e-8,
    maximum_snapshot_age_seconds: float = 90,
) -> dict:
    if checked_at.tzinfo is None or checked_at.utcoffset() is None:
        raise ValueError("checked_at must be timezone-aware")
    if not math.isfinite(numeric_tolerance) or numeric_tolerance < 0:
        raise ValueError("numeric_tolerance must be finite and nonnegative")
    if not math.isfinite(maximum_snapshot_age_seconds) or maximum_snapshot_age_seconds <= 0:
        raise ValueError("maximum_snapshot_age_seconds must be finite and positive")
    issues = []
    differences = []
    bridges = {}
    def number(value, path):
        try:
            if value is None or isinstance(value, bool):
                raise ValueError()
            value = float(value)
            if not math.isfinite(value):
                raise ValueError()
            return value
        except (TypeError, ValueError, OverflowError):
            issues.append(f"{path}:numeric_fact_unavailable")
            return None
    def timestamp(value, path):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError()
            return parsed.astimezone(timezone.utc)
        except ValueError:
            issues.append(f"{path}:timestamp_unavailable")
            return None
    def indexed(rows, path):
        if not isinstance(rows, list):
            issues.append(f"{path}:records_unavailable")
            return {}
        result = {}
        for row in rows:
            if not isinstance(row, dict) or not row.get("record_id"):
                issues.append(f"{path}:record_id_unavailable")
                continue
            record_id = str(row["record_id"])
            if record_id in result:
                issues.append(f"{path}:duplicate:{record_id}")
            result[record_id] = row
        return result
    def compare(left, right, path):
        if isinstance(left, dict) and isinstance(right, dict):
            for key in sorted(set(left) | set(right)):
                if key in _METADATA:
                    continue
                if key not in left or key not in right:
                    differences.append({"field": f"{path}.{key}", "reason": "missing_or_extra_business_field"})
                else:
                    compare(left[key], right[key], f"{path}.{key}")
        elif isinstance(left, (int, float)) and not isinstance(left, bool) and isinstance(right, (int, float)) and not isinstance(right, bool):
            if not math.isfinite(left) or not math.isfinite(right):
                differences.append({"field": path, "reason": "nonfinite_numeric_fact",
                                    "expected": repr(left), "actual": repr(right)})
            elif abs(left - right) > numeric_tolerance:
                differences.append({"field": path, "expected": left, "actual": right})
        elif left != right:
            differences.append({"field": path, "expected": left, "actual": right})
    normalized = []
    for label, snapshot in (("expected", expected), ("actual", actual)):
        data = dict(snapshot)
        captured = timestamp(data.get("captured_at"), label + ".captured_at")
        if captured is not None and not 0 <= (checked_at - captured).total_seconds() <= maximum_snapshot_age_seconds:
            issues.append(label + ":account_snapshot_stale_or_future")
        identity = data.get("identity")
        if not isinstance(identity, dict) or any(not identity.get(key) for key in ("exchange", "environment", "account", "market_type", "base_currency")):
            issues.append(f"{label}.identity:unavailable")
        elif identity["market_type"] not in {"spot", "spot_margin"}:
            issues.append(f"{label}.identity:unsupported_account_mode")
        for section in ("positions", "orders", "fills", "cashflows"):
            data[section] = indexed(data.get(section), f"{label}.{section}")
        filled_by_order = {}
        total_fees = 0.0
        fees_known = True
        for record_id, fill in data["fills"].items():
            path = f"{label}.fills.{record_id}"
            order_id = str(fill.get("order_id", ""))
            qty = number(fill.get("qty"), path + ".qty")
            fee = number(fill.get("fee_base_amount"), path + ".fee_base_amount")
            if order_id not in data["orders"]:
                issues.append(path + ":approved_order_unavailable")
            if qty is not None:
                if qty <= 0:
                    issues.append(path + ":invalid_fill_quantity")
                filled_by_order[order_id] = filled_by_order.get(order_id, 0.0) + qty
            if not fill.get("fee_currency") or not fill.get("fee_conversion_source") or fee is None:
                fees_known = False
                issues.append(path + ":fee_evidence_unavailable")
            else:
                total_fees += fee
        for record_id, order in data["orders"].items():
            path = f"{label}.orders.{record_id}"
            requested, filled, remaining = [number(order.get(key), path + "." + key) for key in ("requested_qty", "filled_qty", "remaining_qty")]
            if all(value is not None for value in (requested, filled, remaining)):
                if min(requested, filled, remaining) < 0 or abs(requested - filled - remaining) > numeric_tolerance:
                    issues.append(path + ":order_quantity_mismatch")
                if abs(filled - filled_by_order.get(record_id, 0.0)) > numeric_tolerance:
                    issues.append(path + ":fill_quantity_mismatch")
        cash = data.get("cash") if isinstance(data.get("cash"), dict) else {}
        free, locked, total = [number(cash.get(key), f"{label}.cash.{key}") for key in ("free", "locked", "total")]
        if all(value is not None for value in (free, locked, total)) and abs(free + locked - total) > numeric_tolerance:
            issues.append(f"{label}.cash:free_locked_total_mismatch")
        valuation = 0.0
        valued = True
        for record_id, position in data["positions"].items():
            path = f"{label}.positions.{record_id}"
            qty = number(position.get("qty"), path + ".qty")
            price = number(position.get("mark_price"), path + ".mark_price")
            maximum_age = number(position.get("max_price_age_seconds"), path + ".max_price_age_seconds")
            observed = timestamp(position.get("price_at"), path + ".price_at")
            if not position.get("price_source") or observed is None or maximum_age is None or maximum_age < 0:
                issues.append(path + ":valuation_provenance_unavailable")
            elif not 0 <= (checked_at - observed).total_seconds() <= maximum_age:
                issues.append(path + ":valuation_stale_or_future")
            if qty is None or price is None or price <= 0:
                valued = False
                issues.append(path + ":valuation_unknown")
            else:
                valuation += qty * price
        equity = number(data.get("equity"), label + ".equity")
        bridge = data.get("capital_bridge") if isinstance(data.get("capital_bridge"), dict) else {}
        components = [number(bridge.get(key), f"{label}.capital_bridge.{key}") for key in
            ("initial_capital", "net_cashflows", "realized_gross_pnl", "unrealized_gross_pnl", "costs")]
        capital_equity = None
        if all(value is not None for value in components):
            capital_equity = sum(components[:4]) - components[4]
            if equity is not None and abs(capital_equity - equity) > numeric_tolerance:
                issues.append(label + ":capital_bridge_mismatch")
        financing = number(bridge.get("financing_costs"), label + ".capital_bridge.financing_costs")
        if fees_known and financing is not None and components[4] is not None and abs(total_fees + financing - components[4]) > numeric_tolerance:
            issues.append(label + ":cost_bridge_mismatch")
        cashflow_amounts = [number(row.get("amount"), f"{label}.cashflows.{key}.amount") for key, row in data["cashflows"].items()]
        if components[1] is not None and all(value is not None for value in cashflow_amounts) and abs(sum(cashflow_amounts) - components[1]) > numeric_tolerance:
            issues.append(label + ":cashflow_bridge_mismatch")
        marked_equity = total + valuation if total is not None and valued else None
        if marked_equity is not None and equity is not None and abs(marked_equity - equity) > numeric_tolerance:
            issues.append(label + ":cash_position_equity_mismatch")
        bridges[label] = {"reported_equity": equity, "cash_plus_net_position_value": marked_equity,
                          "capital_cashflow_pnl_less_costs": capital_equity}
        normalized.append(data)
    compare(normalized[0], normalized[1], "account")
    return {
        "schema_version": 1, "scope": "independent_normalized_account_snapshots",
        "checked_at": checked_at.astimezone(timezone.utc).isoformat(),
        "ok": not issues and not differences, "allows_new_risk": not issues and not differences,
        "numeric_tolerance": numeric_tolerance, "ignored_metadata": sorted(_METADATA),
        "issues": sorted(set(issues)), "differences": _report_value(differences), "equity_bridges": bridges,
        "production_account_source_verified": False,
    }
