"""Automatic candidate -> opening order -> FIFO close -> cash-flow evidence.

Uses the Broker's lot-close allocations; never attributes an exit to whichever
signal happened most recently. No quantity, fees or financing are fabricated.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import math

import pandas as pd

from analysis.paper_label_execution import reconcile_label_cashflows


def _time(value):
    at = pd.Timestamp(value)
    if pd.isna(at):
        raise ValueError("finite trade/financing time required")
    return at.tz_localize("UTC") if at.tzinfo is None else at.tz_convert("UTC")


def _number(value, *, positive=False):
    result = float(value)
    if not math.isfinite(result) or (positive and result <= 0):
        raise ValueError("invalid cashflow amount")
    return result


def reconcile_candidate_account(labels, order_bindings, trades, financing=(), *,
                                initial_capital=None, final_equity=None, final_marks=None,
                                financing_complete=False, financing_timing="unknown",
                                valuation_only=False, source="simulation", quote_currency=None):
    """把候选绑定到开仓订单，按权威 lot_closes 对账并解释账户权益变化。

    labels 每个候选一条；order_bindings 只建立候选与开仓订单的归属关系。
    平仓必须由 Broker 记录的批次分摊完整解释，不能按最近信号猜测归属；
    不同计价币种必须先提供换汇事实，函数不自行换算。

    融资与成交同一时间时，financing_timing 必须声明 before_fills 或
    after_fills。未知先后保留为未分摊费用，空融资账本也需调用方通过
    financing_complete 显式声明完整。仅完全平仓且成本完整的候选才产生
    actual_net_bps，账户核对还需要期初资本、期末权益和未平库存估值。

    valuation_only 会将 EndOfBacktest 转移保留为未平库存。标签期限可能
    与策略实际退出不同，账目闭合与标签误差均不构成交易准入证据。
    """
    if source not in {"simulation", "sandbox", "live"}:
        raise ValueError("explicit evidence source required")
    if financing_timing not in {"unknown", "before_fills", "after_fills"}:
        raise ValueError("invalid financing timing")
    account_currency = quote_currency
    def currency_for(fact):
        nonlocal account_currency
        symbol = str(fact.get("symbol", ""))
        encoded = symbol.split("/")[-1].split(":")[0] if "/" in symbol else None
        explicit = fact.get("quote_currency")
        currency = explicit or encoded or account_currency
        if not isinstance(currency, str) or not currency:
            raise ValueError("cashflow quote currency must be explicit or encoded in slash symbol")
        if (encoded and encoded != currency) or (account_currency and account_currency != currency):
            raise ValueError("currency conversion facts required for cashflow reconciliation")
        account_currency = currency
        return currency
    label_map = {}
    for row in labels:
        cid = str(row["candidate_id"])
        if cid in label_map:
            raise ValueError("one label per candidate required")
        label_map[cid] = dict(row)
    bindings = {}
    for row in order_bindings:
        order_id, cid = row.get("order_id"), row.get("candidate_id")
        if not order_id or not cid:
            continue
        order_id, cid = str(order_id), str(cid)
        if cid not in label_map:
            raise ValueError("order binding references unknown candidate")
        if order_id in bindings and bindings[order_id] != cid:
            raise ValueError("an opening order cannot belong to multiple candidates")
        bindings[order_id] = cid
    trades, financing = list(trades), list(financing)
    fill_times = {_time(t["fill_time"]) for t in trades}
    events = [(_time(t["fill_time"]), 1, i, "fill", t) for i, t in enumerate(trades)]
    priority = 0 if financing_timing == "before_fills" else 2
    events += [(_time(f["timestamp"]), priority, i, "financing", f) for i, f in enumerate(financing)]
    events.sort(key=lambda x: x[:3])
    books, matched, issues, allocations, valuations = {}, [], [], [], []
    financing_total = financing_unallocated = 0.
    commission_total = 0.
    seen_close_ids, seen_financing = set(), {}
    entered = defaultdict(float)
    labels_with_orders = set(bindings.values())
    for at, _, sequence, kind, fact in events:
        if kind == "financing":
            currency_for(fact)
            amount = _number(fact.get("amount", fact.get("cost_quote")))
            eid = str(fact.get("event_id") or f"ledger-row-{sequence}")
            if eid in seen_financing:
                if seen_financing[eid] != fact:
                    raise ValueError("conflicting financing event identity")
                continue
            seen_financing[eid] = fact
            financing_total += amount
            eligible = [(oid, lot) for oid, lot in books.items()
                if lot["symbol"] == fact["symbol"] and lot["quantity"] > 1e-10]
            total = sum(lot["quantity"] for _, lot in eligible)
            ambiguous = financing_timing == "unknown" and at in fill_times
            if total <= 0 or ambiguous:
                financing_unallocated += amount
                issues.append({"event_id": eid, "reason": "financing_fill_order_unknown" if ambiguous else "financing_without_open_inventory"})
                continue
            for oid, lot in eligible:
                cost = amount * lot["quantity"] / total
                lot["financing"] += cost
                allocations.append({"event_id": eid, "opening_order_id": oid,
                    "candidate_id": lot["candidate_id"], "cost_quote": cost,
                    "rule": "same-symbol open-quantity share at explicit settlement time"})
            continue
        if valuation_only and fact.get("exit_reason") == "EndOfBacktest":
            valuations.append(fact)
            continue
        currency = currency_for(fact)
        oid, symbol, side = str(fact["order_id"]), str(fact["symbol"]), str(fact["side"])
        quantity, price = _number(fact["qty"], positive=True), _number(fact["fill_price"], positive=True)
        fee = _number(fact["commission"])
        if fee < 0:
            raise ValueError("negative fee requires an explicit rebate accounting contract")
        commission_total += fee
        if side in {"buy", "short"}:
            direction = "long" if side == "buy" else "short"
            cid = bindings.get(oid)
            if cid and (label_map[cid].get("symbol", symbol) != symbol or label_map[cid]["direction"] != direction):
                raise ValueError("candidate direction/symbol does not match opening order")
            lot = books.setdefault(oid, {"candidate_id": cid, "symbol": symbol, "direction": direction,
                "quantity": 0., "entry_notional": 0., "entry_fees": 0., "financing": 0.})
            if lot["symbol"] != symbol or lot["direction"] != direction:
                raise ValueError("opening order identity reused across instruments or direction")
            lot["quantity"] += quantity
            lot["entry_notional"] += quantity * price
            lot["entry_fees"] += fee
            entered[cid or "unattributed:" + oid] += quantity
            if cid is None:
                issues.append({"order_id": oid, "reason": "opening_fill_without_candidate"})
            continue
        if side not in {"sell", "cover"}:
            raise ValueError("unsupported fill side")
        details = fact.get("lot_closes") or []
        if not details or not math.isclose(sum(_number(d["qty_closed"]) for d in details), quantity, rel_tol=1e-9, abs_tol=1e-10):
            raise ValueError("authoritative lot closes must explain the whole closing fill")
        for detail in details:
            close_id = str(detail["close_event_id"])
            if close_id in seen_close_ids:
                raise ValueError("duplicate close allocation")
            seen_close_ids.add(close_id)
            entry_order = str(detail["entry_order_id"])
            if entry_order not in books:
                raise ValueError("closing allocation has no observed opening inventory")
            lot = books[entry_order]
            q = _number(detail["qty_closed"], positive=True)
            if lot["symbol"] != symbol or lot["direction"] != ("long" if side == "sell" else "short") or q > lot["quantity"] + 1e-9:
                raise ValueError("closing allocation exceeds matching observed inventory")
            entry_price = _number(detail["entry_price"], positive=True)
            entry_fee = _number(detail["entry_cost_share"])
            if entry_fee < 0 or entry_fee > lot["entry_fees"] + 1e-8:
                raise ValueError("invalid allocated entry commission")
            if (not math.isclose(entry_price, lot["entry_notional"] / lot["quantity"], rel_tol=1e-8, abs_tol=1e-8)
                    or not math.isclose(entry_fee, lot["entry_fees"] * q / lot["quantity"], rel_tol=1e-8, abs_tol=1e-8)):
                raise ValueError("closing cost basis does not match observed opening inventory")
            carry = lot["financing"] * q / lot["quantity"]
            lot["quantity"] -= q
            lot["entry_notional"] -= q * entry_price
            lot["entry_fees"] -= entry_fee
            lot["financing"] -= carry
            cid = lot["candidate_id"] or "unattributed:" + entry_order
            label = label_map.get(cid, {"candidate_id": cid, "direction": lot["direction"]})
            parts = [{"candidate_id": cid, "fill_id": close_id + ":entry-share", "leg": "entry", "qty": q,
                "price": entry_price, "fee_quote": entry_fee, "quote_currency": currency,
                "occurred_at": _time(detail["entry_time"]).isoformat()},
                {"candidate_id": cid, "fill_id": close_id + ":exit-share", "leg": "exit", "qty": q,
                "price": price, "fee_quote": fee * q / quantity, "quote_currency": currency,
                "occurred_at": at.isoformat()}]
            report = reconcile_label_cashflows(label, parts,
                [{"candidate_id": cid, "event_id": close_id + ":carry-share", "cost_quote": carry, "quote_currency": currency}],
                financing_complete=financing_complete and not any(i["reason"].startswith("financing_") for i in issues))
            matched.append({**report, "close_event_id": close_id, "lot_id": detail["lot_id"],
                "opening_order_id": entry_order, "exit_order_id": oid, "quantity": q,
                "entry_time": parts[0]["occurred_at"], "exit_time": at.isoformat(),
                "allocation_scope": "authoritative FIFO lot-share, not a new exchange fill"})
    complete_costs = bool(financing_complete and not any(i["reason"].startswith("financing_") for i in issues))
    for match in matched:
        if not complete_costs:
            match.update(status="incomplete_costs", actual_net_bps=None, label_error_bps=None, financing_complete=False)
    open_rows = []
    for oid, lot in books.items():
        if lot["quantity"] <= 1e-9:
            if max(abs(lot[k]) for k in ("entry_notional", "entry_fees", "financing")) > 1e-6:
                raise ValueError("closed inventory retains unexplained costs or principal")
            continue
        mark = (final_marks or {}).get(lot["symbol"])
        unrealized = None
        if mark is not None:
            mark = _number(mark, positive=True)
            sign = 1 if lot["direction"] == "long" else -1
            unrealized = sign * (lot["quantity"] * mark - lot["entry_notional"]) - lot["entry_fees"] - lot["financing"]
        open_rows.append({"opening_order_id": oid, **lot, "mark_price": mark, "unrealized_after_paid_costs": unrealized})
    grouped = defaultdict(list)
    for match in matched:
        grouped[match["candidate_id"]].append(match)
    candidate_rows = []
    for cid, label in label_map.items():
        matches = grouped[cid]
        residual_quantity = sum(lot["quantity"] for lot in open_rows if lot["candidate_id"] == cid)
        closed_quantity = sum(m["quantity"] for m in matches)
        notional = sum(m["entry_notional"] for m in matches)
        net = sum(m["net_pnl"] for m in matches)
        fully_closed = bool(closed_quantity > 0 and residual_quantity <= 1e-9)
        status = ("closed" if fully_closed else "open_or_partially_closed" if entered[cid] > 0
                  else "submitted_without_fill" if cid in labels_with_orders else "not_submitted")
        actual_bps = net / notional * 10000 if fully_closed and complete_costs else None
        expected = label.get("net_return_bps")
        candidate_rows.append({"candidate_id": cid, "status": status, "filled_quantity": entered[cid],
            "closed_quantity": closed_quantity, "open_quantity": residual_quantity,
            "matched_lot_closes": len(matches), "closed_entry_notional": notional,
            "realized_net_pnl_observed_costs": net, "actual_net_bps": actual_bps,
            "label_net_bps": expected, "label_error_bps": actual_bps - expected
                if actual_bps is not None and expected is not None else None,
            "financing_complete": complete_costs,
            "comparison_scope": "actual strategy exits may differ from fixed label horizon"})
    realized = sum(m["net_pnl"] for m in matched)
    all_marks = all(row["unrealized_after_paid_costs"] is not None for row in open_rows)
    unrealized = sum(row["unrealized_after_paid_costs"] for row in open_rows) if all_marks else None
    residual = None
    if initial_capital is not None and final_equity is not None and unrealized is not None:
        residual = _number(final_equity) - (_number(initial_capital) + realized + unrealized - financing_unallocated)
    expected_fees = sum(m["fees"] for m in matched) + sum(row["entry_fees"] for row in open_rows)
    fee_residual = commission_total - expected_fees
    financing_residual = financing_total - (sum(m["financing_cost"] for m in matched)
        + sum(row["financing"] for row in open_rows) + financing_unallocated)
    money_ok = residual is not None and abs(residual) <= max(1e-7, abs(float(initial_capital)) * 1e-9)
    return {"schema": "label-account-reconciliation/v1", "source": source, "quote_currency": account_currency,
        "status": "complete" if not issues and complete_costs and money_ok and abs(fee_residual) < 1e-7 and abs(financing_residual) < 1e-7 else "incomplete",
        "candidates": candidate_rows, "lot_matches": matched, "open_inventory": open_rows,
        "financing_allocations": allocations, "issues": issues, "valuation_transfers": len(valuations),
        "coverage": dict(Counter(r["status"] for r in candidate_rows)),
        "accounting": {"ok": bool(money_ok and abs(fee_residual) < 1e-7 and abs(financing_residual) < 1e-7),
            "initial_capital": initial_capital, "final_equity": final_equity,
            "realized_after_observed_costs": realized, "unrealized_after_paid_costs": unrealized,
            "unallocated_financing": financing_unallocated, "commission_total": commission_total,
            "fee_allocation_residual": fee_residual, "financing_allocation_residual": financing_residual,
            "equity_bridge_residual": residual},
        "financing_complete": complete_costs, "financing_timing": financing_timing,
        "admission_eligible": False}


def reconcile_observation_account(payload, trades, *, initial_capital, final_equity,
                                  final_marks, account_mode="spot", valuation_only=False,
                                  quote_currency=None):
    """将普通 BacktestEngine 的 P0 观察结果接到账户对账。

    固定选用 5-bar 标签做比较；缺标签仍保留成交归属，但不声称标签误差。
    仅 spot 模式声明融资账本完整，其余账户模式保留成本不足状态。
    """
    selected = {row["candidate_id"]: row for row in payload.get("outcomes", []) if row.get("horizon_bars") == 5}
    labels = [{**candidate, **selected.get(candidate["candidate_id"], {}),
               "net_return_bps": selected.get(candidate["candidate_id"], {}).get("net_return_bps"),
               "label_available": candidate["candidate_id"] in selected}
              for candidate in payload.get("candidates", [])]
    result = reconcile_candidate_account(labels, payload.get("decisions", []), trades,
        payload.get("actual_financing", []), initial_capital=initial_capital,
        final_equity=final_equity, final_marks=final_marks, financing_complete=account_mode == "spot",
        financing_timing="unknown", valuation_only=valuation_only, source="simulation", quote_currency=quote_currency)
    result["label_comparison"] = {"horizon_bars": 5, "available": len(selected),
        "missing": len(labels) - len(selected), "policy": "missing labels retain cashflow attribution without a label-error claim"}
    return result
