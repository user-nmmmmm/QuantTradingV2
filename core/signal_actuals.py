"""Associate authoritative fills/partial closes with frozen raw candidates."""
from __future__ import annotations

from collections import defaultdict, deque

from core.signal_observation_types import close_time, iso
from core.strategy_health import classify_exit_controller


def reconcile_actuals(payload, trades, orders, execution_audit, *, mark_to_market=True):
    candidates = {c["candidate_id"]: c for c in payload["candidates"]}
    by_order = {str(d["order_id"]): d["candidate_id"] for d in payload["decisions"] if d.get("order_id")}
    if len(by_order) != sum(bool(d.get("order_id")) for d in payload["decisions"]):
        raise ValueError("an actual opening order cannot belong to two candidates")
    summaries = {c["candidate_id"]: {"candidate_id": c["candidate_id"],
        "actual_status": "not_submitted", "filled_quantity": 0.0, "closed_quantity": 0.0,
        "realized_net_pnl_ex_carry": 0.0, "realized_R": None,
        "retired_initial_risk": 0.0, "opening_order_id": None,
        "carry_status": "account_financing_ledger_separate_not_assumed_zero"}
        for c in payload["candidates"]}
    for order_id, candidate_id in by_order.items():
        order = orders.get(order_id)
        summaries[candidate_id].update(opening_order_id=order_id,
            actual_status="submitted_without_fill", order_status=getattr(getattr(order, "status", None), "value", "unknown"))
    stacks = defaultdict(deque)
    fill_rows, close_rows, valuations, unmatched = [], [], [], []
    for trade in trades:
        if mark_to_market and trade.get("exit_reason") == "EndOfBacktest":
            valuations.append(dict(trade))
            continue
        qty, price = float(trade["qty"]), float(trade["fill_price"])
        if qty <= 0:
            continue
        symbol, side = trade["symbol"], trade["side"]
        if side in {"buy", "short"}:
            order_id = str(trade["order_id"])
            cid = by_order.get(order_id)
            if cid is None:
                unmatched.append({"order_id": order_id, "symbol": symbol, "reason": "opening_fill_without_raw_candidate"})
            order = orders.get(order_id)
            stop = getattr(order, "stop_loss", None)
            stacks[symbol, "long" if side == "buy" else "short"].append({
                "candidate_id": cid, "order_id": order_id, "qty": qty, "price": price,
                "commission_unit": float(trade.get("commission", 0))/qty,
                "time": iso(trade["fill_time"]), "stop": stop,
            })
            fill_rows.append({**trade, "candidate_id": cid, "fact_type": "opening_fill"})
            if cid:
                summaries[cid]["filled_quantity"] += qty
                summaries[cid]["actual_status"] = "open_or_partially_closed"
            continue
        if side not in {"sell", "cover"}:
            unmatched.append({"order_id": trade["order_id"], "reason": "unsupported_fill_side"})
            continue
        direction = "long" if side == "sell" else "short"
        queue = stacks[symbol, direction]
        remaining = qty
        while queue and remaining > 1e-10:
            entry = queue[0]
            matched = min(entry["qty"], remaining)
            commission = (entry["commission_unit"] + float(trade.get("commission", 0))/qty)*matched
            net = (price-entry["price"])*matched*(1 if direction == "long" else -1)-commission
            risk = abs(entry["price"]-entry["stop"])*matched if entry["stop"] else None
            cid = entry["candidate_id"]
            reason = trade.get("exit_reason", "unknown")
            close_rows.append({"candidate_id": cid, "opening_order_id": entry["order_id"],
                "exit_order_id": trade["order_id"], "symbol": symbol, "direction": direction,
                "quantity": matched, "entry_time": entry["time"], "exit_time": iso(trade["fill_time"]),
                "entry_price": entry["price"], "exit_price": price,
                "realized_net_pnl_ex_carry": net, "commission_both_sides": commission,
                "initial_risk_share": risk, "realized_R": net/risk if risk and risk > 0 else None,
                "exit_reason": reason, "exit_controller": classify_exit_controller(reason),
                "close_event_ids": list(trade.get("close_event_ids") or []),
                # Backtest fills use execution-bar range/volume and may be
                # intrabar stops. Retain fill_time, but never advertise the
                # completed cost/PnL label as known before that bar closes.
                "available_at": close_time(trade["fill_time"], candidates[cid]["context"]["timeframe"])
                if cid else None})
            if cid:
                row = summaries[cid]
                row["closed_quantity"] += matched
                row["realized_net_pnl_ex_carry"] += net
                row["retired_initial_risk"] += risk or 0
                row["actual_status"] = ("closed" if row["closed_quantity"] >= row["filled_quantity"]-1e-9
                                        else "open_or_partially_closed")
                row["realized_R"] = (row["realized_net_pnl_ex_carry"]/row["retired_initial_risk"]
                                      if row["retired_initial_risk"] > 0 else None)
            entry["qty"] -= matched
            remaining -= matched
            if entry["qty"] <= 1e-10:
                queue.popleft()
        if remaining > 1e-9:
            unmatched.append({"order_id": trade["order_id"], "reason": "unmatched_closing_quantity", "quantity": remaining})
        fill_rows.append({**trade, "fact_type": "closing_fill"})
    audit = [{**item, "candidate_id": by_order.get(str(item.get("order_id")))} for item in execution_audit]
    return {"summaries": list(summaries.values()), "fills": fill_rows, "closes": close_rows,
            "valuation_transfers": valuations, "execution_audit": audit, "unmatched": unmatched,
            "pnl_convention": "fill-price PnL less both commissions; slippage/impact already in fills; funding/borrow reported separately"}
