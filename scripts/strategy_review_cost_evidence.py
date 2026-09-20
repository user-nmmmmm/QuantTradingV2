"""Read-only cost evidence; never invent a financing allocation to exit cohorts.

The engine charges financing to account cash separately from CloseEvent PnL.
Quote borrowing has account identity, not lot identity. Reconstructing exposure
does not identify which lot borrowed the fungible quote currency, so a new
proportional allocation would be a new convention rather than recovered fact.
"""
from __future__ import annotations

import csv
from datetime import datetime
import json
import math
from pathlib import Path


LEDGER_COLUMNS = {"timestamp", "symbol", "kind", "rate", "notional", "amount", "source"}
COHORT_COST_GATES = {"profit_factor", "remove_top5_top10"}


def _finite(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("Nonfinite numeric fact")
    return value


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _csv_rows(path, required):
    if not path.is_file():
        raise ValueError("missing_file")
    text = path.read_text(encoding="utf-8-sig")
    if not text.strip():
        # pd.DataFrame([]).to_csv(index=False) emits a blank newline. Its
        # enclosing run receipt supplies integrity; absence is not emptiness.
        return []
    reader = csv.DictReader(text.splitlines())
    if not required.issubset(set(reader.fieldnames or [])):
        raise ValueError("missing_required_columns")
    rows = list(reader)
    if any(None in row or any(value is None for value in row.values()) for row in rows):
        raise ValueError("malformed_csv_row")
    return rows


def _ending_account_evidence(folder, result):
    """Aggregate closed net PnL only if flatness and the cash identity prove it."""
    result.update(close_event_pnl_ex_financing=None, actual_close_event_pnl_ex_financing=None,
                  valuation_close_event_pnl=None, account_net_pnl_all_costs=None,
                  all_positions_fully_closed=None, net_closed_after_financing=None,
                  closed_net_reconciliation_status="unavailable")
    try:
        summary = _read_json(folder / "summary.json")
        events = _read_json(folder / "close_events.json")
        accounting = _read_json(folder / "accounting_check.json")
        if not isinstance(summary, dict) or not isinstance(events, list):
            return
        forced = bool(summary.get("forced_exit", False))
        all_pnl = math.fsum(_finite(event["realized_pnl"]) for event in events)
        actual_pnl = math.fsum(_finite(event["realized_pnl"]) for event in events
                              if forced or event.get("exit_reason") != "EndOfBacktest")
        account_net = _finite(summary["final_equity"]) - _finite(summary["initial_capital"])
        result.update(close_event_pnl_ex_financing=all_pnl,
                      actual_close_event_pnl_ex_financing=actual_pnl,
                      valuation_close_event_pnl=all_pnl - actual_pnl,
                      account_net_pnl_all_costs=account_net)
        trades = _csv_rows(folder / "trades.csv", {"symbol", "side", "qty"})
        balances, volumes = {}, {}
        signs = {"buy": 1, "cover": 1, "sell": -1, "short": -1}
        for row in trades:
            if not forced and row.get("exit_reason") == "EndOfBacktest":
                raise ValueError("valuation_transfer_in_actual_trade_file")
            symbol, quantity = row["symbol"], _finite(row["qty"])
            if quantity <= 0 or row["side"] not in signs:
                raise ValueError("invalid_actual_trade")
            balances.setdefault(symbol, []).append(signs[row["side"]] * quantity)
            volumes[symbol] = volumes.get(symbol, 0.0) + quantity
        equity_path = folder / "equity_engine.csv"
        if not equity_path.exists():
            equity_path = folder / "equity.csv"
        equity = _csv_rows(equity_path, {"equity", "gross_exposure"})
        if not equity:
            return
        flat_trades = all(abs(math.fsum(values)) <= max(1e-8, volumes[symbol] * 1e-12)
                          for symbol, values in balances.items())
        flat = flat_trades and abs(_finite(equity[-1]["gross_exposure"])) <= 1e-12
        result["all_positions_fully_closed"] = flat
        if not flat:
            result["closed_net_reconciliation_status"] = "open_or_valuation_only_positions"
            return
        if not isinstance(accounting, dict) or accounting.get("ok") is not True:
            result["closed_net_reconciliation_status"] = "accounting_not_verified"
            return
        expense = result["financing_net_expense"]
        if expense is None:
            result["closed_net_reconciliation_status"] = "financing_unknown"
            return
        net = actual_pnl - expense
        difference = net - account_net
        result["closed_net_reconciliation_difference"] = difference
        if abs(difference) > max(1e-6, abs(account_net) * 1e-8):
            result["closed_net_reconciliation_status"] = "mismatch"
            return
        result.update(net_closed_after_financing=net, closed_net_reconciliation_status="verified_flat_account")
    except (ValueError, TypeError, KeyError, OSError, UnicodeError):
        result["closed_net_reconciliation_status"] = "invalid_or_missing_account_evidence"


def financing_evidence(folder):
    """Return account carry facts and eligibility of unallocated cohort PnL.

    Any nonzero row matters, including credits or debit/credit cancellation.
    A known ledger expense does not make the engine run incomplete. It makes
    the cost-complete cohort PF/bootstrap/concentration claim unsupported.
    """
    folder = Path(folder)
    result = dict(financing_ledger_status="unknown", financing_ledger_nonempty=None,
        financing_row_count=None, financing_nonzero_row_count=None, financing_kinds=[],
        financing_by_kind={}, financing_net_expense=None, financing_gross_expense=None,
        financing_gross_credit=None, financing_attribution_status="unknown",
        cost_scope="trade_costs_included_financing_unknown", cohort_admission_status="insufficient",
        financing_allocated_to_cohorts=False, account_return_cost_scope="engine_equity_includes_separate_financing",
        cohort_pnl_cost_scope="CloseEvent_realized_pnl_excludes_separately_accrued_financing")
    try:
        rows = _csv_rows(folder / "financing_ledger.csv", LEDGER_COLUMNS)
        parsed = []
        by_kind = {}
        for row in rows:
            if any(not str(row[key]).strip() for key in LEDGER_COLUMNS):
                raise ValueError("missing_ledger_fact")
            datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            amount = _finite(row["amount"])
            _finite(row["rate"])
            _finite(row["notional"])
            parsed.append(amount)
            by_kind.setdefault(row["kind"], []).append(amount)
        nonzero = sum(amount != 0 for amount in parsed)
        result.update(financing_ledger_status="verified_nonzero" if nonzero else "verified_zero",
            financing_ledger_nonempty=bool(rows), financing_row_count=len(rows),
            financing_nonzero_row_count=nonzero, financing_kinds=sorted(by_kind),
            financing_by_kind={kind: math.fsum(values) for kind, values in by_kind.items()},
            financing_net_expense=math.fsum(parsed),
            financing_gross_expense=math.fsum(max(amount, 0) for amount in parsed),
            financing_gross_credit=math.fsum(max(-amount, 0) for amount in parsed),
            financing_attribution_status="unallocated_signed_carry" if nonzero else "no_carry_to_allocate",
            cost_scope="trade_costs_included_financing_separate_unallocated" if nonzero else "trade_costs_included_financing_verified_zero",
            cohort_admission_status="insufficient" if nonzero else "cost_complete")
    except (ValueError, TypeError, KeyError, OSError, UnicodeError) as exc:
        result["financing_ledger_error"] = str(exc)
    _ending_account_evidence(folder, result)
    return result


def cost_adjusted_gates(gates, evidence):
    """Mask unsupported positive claims; retain previously observed failures."""
    adjusted = dict(gates)
    if evidence["cohort_admission_status"] != "cost_complete":
        for key in COHORT_COST_GATES:
            if adjusted.get(key) == "pass":
                adjusted[key] = "insufficient"
    return adjusted


def cost_adjusted_status(raw_status, evidence):
    if raw_status == "pass" and evidence["cohort_admission_status"] != "cost_complete":
        return "insufficient"
    return raw_status
