"""P3 independent broker replays of preregistered research execution policies.

Every arm owns its broker and starts with the same finite capital. Predictions
choose orders before matching; no arm filters already-realised fills or labels.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from backtest.signal_ghost import clone_research_broker
from core.orders import TERMINAL_STATUSES
from core.reproducibility import canonical_json, sha256_bytes
from core.runtime import MarketDataSlice
from core.signal_adaptive_types import MetaReplayPolicy
from core.signal_meta_layer import EXCLUDED_GATES
from core.signal_observation_types import finite, fingerprint, iso
from core.timeframes import as_utc_timestamp, timeframe_delta


ARMS = ("baseline", "gate", "sizing")


@dataclass
class _Account:
    broker: object
    active: dict = field(default_factory=dict)
    audit_cursor: int = 0
    trade_cursor: int = 0
    financing_cursor: int = 0
    close_cursor: int = 0
    halted: bool = False
    halt_time: str | None = None


def _identity(policy):
    root = Path(__file__).resolve().parents[1]
    names = ("backtest/signal_meta_replay.py", "backtest/signal_ghost.py",
             "core/signal_adaptive_types.py", "core/broker/__init__.py",
             "core/broker/matching.py", "core/broker/fill_service.py",
             "core/broker/financing.py", "core/portfolio.py")
    return fingerprint({"policy": policy.to_dict(), "sources": {
        name: (root/name).read_text(encoding="utf-8") for name in names}})


def _validate_inputs(p0, p2, policy, timeframe):
    # Outcome rows are deliberately not inspected here: a label or an
    # execution flag learned after entry cannot change an earlier order.
    if p0.get("schema") != "signal_observation/v1" or p0.get("status") != "complete":
        raise ValueError("P3 requires a complete P0 observation payload")
    horizons = tuple(p0["policy"]["horizons"])
    if (not horizons or any(type(h) is not int or h < 1 for h in horizons)
            or len(horizons) != len(set(horizons))):
        raise ValueError("invalid P0 horizon policy")
    candidates = {c["candidate_id"]: c for c in p0["candidates"]}
    decisions = {d["candidate_id"]: d for d in p0["decisions"]}
    if (len(candidates) != len(p0["candidates"]) or len(decisions) != len(p0["decisions"])
            or set(candidates) != set(decisions)):
        raise ValueError("P0 candidate/decision partition is incomplete or duplicated")
    for candidate in candidates.values():
        context = candidate["context"]
        if (context["snapshot_version"] != p0["snapshot_version"]
                or candidate["signal_version"] != p0["strategy_versions"].get(candidate["strategy"])):
            raise ValueError("candidate differs from P0 version identity")
        available = as_utc_timestamp(context["available_at"])
        if (pd.isna(available) or available != as_utc_timestamp(candidate["timestamp"])
                + timeframe_delta(context["timeframe"])):
            raise ValueError("candidate availability differs from signal-bar close")
        if candidate["direction"] not in {"long", "short"}:
            raise ValueError("invalid candidate direction")
        if not isinstance(decisions[candidate["candidate_id"]].get("veto_stage"), str):
            raise ValueError("candidate requires an explicit P0 gate stage")
    if policy.horizon_bars not in horizons:
        raise ValueError("replay horizon must be a preregistered P0/P2 horizon")
    if p2.get("schema") != "signal_adaptive_ev/v1" or p2.get("status") != "complete":
        raise ValueError("P3 requires a complete signal_adaptive_ev/v1 payload")
    if not isinstance(p2.get("model_version"), str) or not p2["model_version"]:
        raise ValueError("P2 implementation version is required")
    identity = p2.get("input_identity", {})
    if identity.get("p0_snapshot_version") != p0["snapshot_version"]:
        raise ValueError("P2 snapshot differs from the supplied P0")
    expected = fingerprint([candidates[cid] for cid in sorted(candidates)])
    if identity.get("candidates_sha256") != expected:
        raise ValueError("P2 candidate digest differs from the supplied P0")
    folds = {fold["fold_id"]: fold for fold in p2["folds"]}
    if len(folds) != len(p2["folds"]):
        raise ValueError("duplicate P2 fold identity")
    predictions = {}
    for prediction in p2["predictions"]:
        cid, horizon = prediction["candidate_id"], prediction["horizon_bars"]
        key = cid, horizon
        if cid not in candidates or type(horizon) is not int or horizon not in horizons or key in predictions:
            raise ValueError("invalid or duplicate P2 prediction identity")
        candidate = candidates[cid]
        if candidate["context"]["timeframe"] != timeframe:
            raise ValueError("market-data timeframe differs from candidate timeframe")
        for name in ("strategy", "symbol", "direction"):
            if prediction.get(name) != candidate[name]:
                raise ValueError("P2 prediction identity differs from candidate")
        for name, expected_value in (("signal_version", candidate["signal_version"]),
                ("snapshot_version", candidate["context"]["snapshot_version"]),
                ("timeframe", candidate["context"]["timeframe"])):
            if prediction.get(name) != expected_value:
                raise ValueError("P2 prediction version or timeframe differs from candidate")
        available = as_utc_timestamp(prediction["available_at"])
        if pd.isna(available) or available != as_utc_timestamp(candidate["context"]["available_at"]):
            raise ValueError("P2 prediction must be available at candidate decision time")
        if prediction["status"] not in {"allow", "veto", "abstain"}:
            raise ValueError("unknown P2 prediction status")
        if prediction["status"] != "abstain" and any(
                isinstance(prediction.get(name), bool) or finite(prediction.get(name)) is None
                for name in ("estimate_bps", "lower_bound_bps")):
            raise ValueError("non-abstaining prediction requires finite EV and lower bound")
        fold_id = prediction.get("fold_id")
        if fold_id is None:
            if (prediction["status"] != "abstain" or prediction.get("training_cutoff") is not None
                    or prediction["model_version"] != p2["model_version"]):
                raise ValueError("untrained P2 predictions must abstain with the implementation version")
        else:
            fold = folds[fold_id]
            cutoff = as_utc_timestamp(prediction["training_cutoff"])
            if (pd.isna(cutoff) or cutoff > available
                    or cutoff != as_utc_timestamp(fold["training_cutoff"])
                    or prediction["model_version"] != fold["model_version"]
                    or not as_utc_timestamp(fold["start"]) <= available < as_utc_timestamp(fold["end"])):
                raise ValueError("P2 fold, model version or training cutoff is inconsistent")
            latest = prediction.get("max_label_available_at")
            if latest is not None and not as_utc_timestamp(latest) < cutoff:
                raise ValueError("P2 prediction includes an unavailable training label")
        predictions[key] = prediction
    if len(predictions) != len(candidates)*len(horizons):
        raise ValueError("P2 prediction partition is incomplete")
    for candidate in candidates.values():
        if (isinstance(candidate.get("native_score"), bool) or finite(candidate.get("native_score")) is None
                or isinstance(candidate.get("reference_price"), bool)
                or finite(candidate.get("reference_price")) is None or candidate["reference_price"] <= 0):
            raise ValueError("candidate requires finite native score and positive reference price")
        expected_side = "buy" if candidate["direction"] == "long" else "short"
        if candidate["signal"].get("action") != expected_side:
            raise ValueError("candidate signal action conflicts with direction")
        if candidate["signal"].get("order_type", "market") not in {"market", "limit", "stop"}:
            raise ValueError("unsupported candidate order type")
    return candidates, decisions, predictions


def _drain(account, arm, result):
    broker = account.broker
    for fill in broker.trades[account.trade_cursor:]:
        row = account.active.get(fill["symbol"])
        result["fills"].append({"arm": arm, "candidate_id": row["candidate_id"] if row else None, **fill})
        if row is None:
            continue
        opening = fill["side"] in {"buy", "short"}
        if opening:
            row.setdefault("first_fill_time", iso(fill["fill_time"]))
            row["filled_quantity"] += fill["qty"]
            row["entry_notional"] += fill["fill_price"]*fill["qty"]
        else:
            row["last_exit_time"] = iso(fill["fill_time"])
            row["closed_quantity"] += fill["qty"]
        row["entry_commission" if opening else "exit_commission"] += fill["commission"]
        row["slippage_cost"] += fill["slip"]*fill["qty"]
    account.trade_cursor = len(broker.trades)
    for close in broker.close_events[account.close_cursor:]:
        if close.symbol in account.active:
            account.active[close.symbol]["realized_pnl_ex_carry"] += close.realized_pnl
    account.close_cursor = len(broker.close_events)
    for item in broker.portfolio.financing_ledger[account.financing_cursor:]:
        entry = item.to_dict()
        row = account.active.get(entry["symbol"])
        result["financing"].append({"arm": arm, "candidate_id": row["candidate_id"] if row else None, **entry})
        if row is not None:
            row["carry"] += entry["amount"]
        elif entry["symbol"] == broker.QUOTE_BORROW_SYMBOL:
            weights = {s: max(broker.portfolio.get_position(s)["qty"], 0)*broker.last_prices.get(s, 0)
                       for s in account.active}
            total = sum(weights.values())
            if total:
                for symbol, weight in weights.items():
                    account.active[symbol]["carry"] += entry["amount"]*weight/total
    account.financing_cursor = len(broker.portfolio.financing_ledger)
    for audit in broker.execution_audit[account.audit_cursor:]:
        result["execution_audit"].append({"arm": arm, **audit})
    account.audit_cursor = len(broker.execution_audit)


def _halt(account, arm, result, timestamp, exc):
    account.halted = True
    account.halt_time = iso(timestamp)
    result["errors"].append({"arm": arm, "timestamp": iso(timestamp), "reason": str(exc)})
    for row in account.active.values():
        row["status"], row["reason"] = "unresolved_execution_error", str(exc)


def _advance(account, arm, event, result, policy):
    broker = account.broker
    if account.halted:
        # Keep a diagnostic mark of the preserved position, but never process
        # another order or imply that missing financing became known again.
        for symbol, bar in event.bars.items():
            mark = finite(bar.get("mark_price", bar.get("close")))
            if mark is not None and mark > 0:
                broker.last_prices[symbol] = mark
        return
    try:
        flat_symbols = {s for s in event.bars if broker.portfolio.get_position(s)["qty"] == 0}
        flat_account = not any(p["qty"] for p in broker.portfolio.positions.values())
        broker.process_orders(event.bars)
        for symbol in flat_symbols:
            if broker.portfolio.get_position(symbol)["qty"] < 0:
                broker._last_borrow_time.pop(symbol, None)
        if flat_account:
            broker._last_borrow_time.pop(broker.QUOTE_BORROW_SYMBOL, None)
        broker.accrue_carry(event.bars)
        margin = broker.portfolio.margin_snapshot(broker.last_prices, timestamp=event.timestamp)
        if margin.liquidation_required:
            raise ValueError("research margin breach; liquidation path is unresolved")
    except (ValueError, ArithmeticError) as exc:
        _halt(account, arm, result, event.timestamp, exc)
    _drain(account, arm, result)
    if account.halted:
        return
    for symbol, row in list(account.active.items()):
        if symbol not in event.bars:
            continue
        order = broker.opening_orders[row["opening_order_id"]]
        qty = float(broker.portfolio.get_position(symbol)["qty"])
        if row["filled_quantity"] == 0 and order.status in TERMINAL_STATUSES:
            row["status"], row["reason"] = "unfilled_" + order.status.value, "broker_terminal_order"
            del account.active[symbol]
            continue
        if qty == 0 and row["filled_quantity"] > 0:
            row["status"], row["reason"] = "closed", "fixed_horizon_exit_completed"
            row["net_pnl"] = row["realized_pnl_ex_carry"] - row["carry"]
            row["outcome_available_at"] = iso(as_utc_timestamp(event.timestamp)
                + (as_utc_timestamp(row["decision_available_at"])-as_utc_timestamp(row["signal_time"])))
            del account.active[symbol]
            continue
        if row["filled_quantity"] > 0:
            row["holding_bars"] += 1
        if row["holding_bars"] >= policy.horizon_bars and not row.get("exit_submitted"):
            broker.cancel_opening_orders([symbol], timestamp=event.timestamp)
            if qty:
                order = broker.submit_order(symbol, "sell" if qty > 0 else "cover", abs(qty),
                    price=float(event.bars[symbol]["close"]), timestamp=event.timestamp,
                    strategy_id=row["strategy"], exit_reason="MetaReplayFixedHorizon")
                row["exit_submitted"] = True
                row["exit_order_id"] = order.id
    _drain(account, arm, result)


def _allocation(arm, prediction, policy):
    if arm == "baseline":
        return 1.0, "fixed_notional_baseline"
    if prediction["status"] == "veto":
        return 0.0, "meta_veto"
    if prediction["status"] == "abstain":
        return (policy.min_size_multiplier, "meta_abstain_minimum_size") if arm == "sizing" else (0.0, "meta_abstain")
    if arm == "gate":
        return 1.0, "meta_allow_fixed_notional"
    multiplier = min(policy.max_size_multiplier,
                     max(policy.min_size_multiplier, prediction["lower_bound_bps"]/policy.full_size_ev_bps))
    return multiplier, "meta_allow_lower_bound_size"


def _budget(account, policy):
    broker = account.broker
    equity = broker.portfolio.get_equity(broker.last_prices)
    used = broker.portfolio.get_total_exposure(broker.last_prices)
    reserved = sum(broker.pending_open_notional(broker.last_prices).values())
    budget = max(equity, 0)*policy.max_gross_fraction
    return {"marked_equity": equity, "gross_used": used, "pending_reserved": reserved,
            "gross_budget": budget, "remaining_budget": max(budget-used-reserved, 0)}


def _submit(account, arm, candidate, prediction, decision, event, result, policy):
    multiplier, reason = _allocation(arm, prediction, policy)
    row = {"arm": arm, "candidate_id": candidate["candidate_id"], "strategy": candidate["strategy"],
        "symbol": candidate["symbol"], "direction": candidate["direction"], "signal_time": candidate["timestamp"],
        "decision_available_at": candidate["context"]["available_at"], "model_version": prediction["model_version"],
        "fold_id": prediction["fold_id"], "prediction_status": prediction["status"],
        "lower_bound_bps": prediction.get("lower_bound_bps"), "estimate_bps": prediction.get("estimate_bps"),
        "official_gate": decision["veto_stage"], "horizon_bars": policy.horizon_bars,
        "multiplier": multiplier, "base_notional": policy.reference_notional,
        "research_notional": multiplier*policy.reference_notional, "status": "queued", "reason": reason,
        "decision_reason": reason,
        "holding_bars": 0, "filled_quantity": 0.0, "closed_quantity": 0.0, "entry_notional": 0.0,
        "entry_commission": 0.0, "exit_commission": 0.0, "slippage_cost": 0.0,
        "carry": 0.0, "realized_pnl_ex_carry": 0.0, "net_pnl": None}
    result["rows"].append(row)
    if decision["veto_stage"] in EXCLUDED_GATES:
        row["status"], row["reason"] = "p0_ineligible", "p0_ineligible:" + decision["veto_stage"]
        return
    if account.halted:
        row["status"], row["reason"] = "unresolved_account_error", "account_previously_halted"
        return
    symbol = candidate["symbol"]
    if symbol not in event.bars:
        row["status"], row["reason"] = "unresolved_missing_bar", "candidate_bar_missing"
        _halt(account, arm, result, event.timestamp, row["reason"])
        result["errors"][-1]["candidate_id"] = candidate["candidate_id"]
        return
    if multiplier == 0:
        row["status"] = "meta_blocked"
        return
    if symbol in account.active:
        row["status"], row["reason"] = "busy_blocked", "symbol_has_position_or_pending_order"
        return
    before = _budget(account, policy)
    row.update({"decision_" + name: value for name, value in before.items()})
    quantity = row["research_notional"]/candidate["reference_price"]
    # Match RiskReservationProjection.pending_notional: a limit order below
    # the current mark still occupies its quantity valued at the higher mark.
    row["decision_required_reservation"] = quantity*max(
        candidate["reference_price"], account.broker.last_prices.get(symbol, 0))
    if row["decision_required_reservation"] > before["remaining_budget"] + 1e-9:
        row["status"], row["reason"] = "capital_blocked", "gross_plus_pending_budget"
        return
    try:
        order = account.broker.submit_order(symbol, candidate["signal"]["action"],
            quantity, price=candidate["reference_price"],
            order_type=candidate["signal"].get("order_type", "market"), timestamp=event.timestamp,
            strategy_id=candidate["strategy"], exit_reason="MetaReplayEntry")
        row["opening_order_id"] = order.id
        if order.status in TERMINAL_STATUSES:
            row["status"], row["reason"] = "unfilled_" + order.status.value, "broker_submission_rejected"
        else:
            account.active[symbol] = row
    except (ValueError, ArithmeticError) as exc:
        row["status"], row["reason"] = "unresolved_execution_error", str(exc)
        _halt(account, arm, result, event.timestamp, exc)
    _drain(account, arm, result)


def _events_with_candidate_times(market_data, candidate_times):
    """Insert absent decision events in order, before a later fill can occur."""
    times = iter(sorted(candidate_times))
    pending = next(times, None)
    for event in market_data.stream():
        while pending is not None and pending < event.timestamp:
            yield MarketDataSlice(pending, {}, {}, timeframe=market_data.timeframe)
            pending = next(times, None)
        if pending == event.timestamp:
            pending = next(times, None)
        yield event
    while pending is not None:
        yield MarketDataSlice(pending, {}, {}, timeframe=market_data.timeframe)
        pending = next(times, None)


def replay_signal_meta(market_data, p0_payload, p2_payload, template_broker, policy=None):
    """Replay baseline, allow-only gate and reduced-size-abstention accounts."""
    policy = MetaReplayPolicy.from_mapping(policy)
    if not policy.enabled:
        return None
    result = {"schema": policy.schema, "policy": policy.to_dict(), "status": "complete",
        "rows": [], "accounts": [], "equity": [], "fills": [], "financing": [],
        "execution_audit": [], "errors": [], "validation": {}, "input_identity": {},
        "implementation_version": _identity(policy),
        "protocol": {"arms": list(ARMS), "sizing_abstain": "predeclared minimum size, not positive-EV evidence",
            "baseline": "all P0 raw candidates excluding warmup/data/universe; ignore official later gate decisions",
            "entry": "signal-bar close decision; order timestamp is signal bar index; next actual bar match",
            "exit": "count real bars from first fill; cancel entry remainder at horizon and exit next actual bar",
            "selection": "consume only frozen P2 predictions; no realised labels or post-hoc fill filtering",
            "capital": "independent finite accounts; one candidate per symbol; gross plus pending budget; new reservation = quantity * max(reference price, current mark)",
            "ordering": "descending native_score, strategy, symbol, candidate_id within each signal bar",
            "carry": "broker financing; account quote-borrow cost attributed by current long notional",
            "marks": "every market event; last observed marks for missing symbols; tail positions are not liquidated",
            "halt": "preserve fills, positions and diagnostic marks; missing candidate bar, missing carry or maintenance breach makes return unknown",
            "random_slippage": "configured deterministic rate; no production broker or random-generator state shared",
            "interpretation": "research policy comparison, not causal uplift, new out-of-sample evidence or live admission"}}
    try:
        candidates, decisions, predictions = _validate_inputs(
            p0_payload, p2_payload, policy, market_data.timeframe)
        result["input_identity"] = {"p0_snapshot_version": p0_payload["snapshot_version"],
            "p2_model_version": p2_payload["model_version"],
            "candidates_sha256": fingerprint([candidates[cid] for cid in sorted(candidates)]),
            "predictions_sha256": fingerprint([predictions[key] for key in sorted(predictions)]),
            "decisions_sha256": sha256_bytes(canonical_json(
                [decisions[cid] for cid in sorted(decisions)]).encode("utf-8"))}
    except (KeyError, TypeError, ValueError) as exc:
        result["status"] = "incomplete"
        result["errors"].append({"reason": "invalid_replay_input", "message": str(exc)})
        return result
    accounts = {arm: _Account(clone_research_broker(template_broker, policy.initial_capital,
                name="meta_replay:" + arm)) for arm in ARMS}
    by_time = defaultdict(list)
    for candidate in candidates.values():
        by_time[as_utc_timestamp(candidate["timestamp"]).tz_localize(None)].append(candidate)
    seen, missing_bars = set(), set()
    for event in _events_with_candidate_times(market_data, by_time):
        for arm, account in accounts.items():
            _advance(account, arm, event, result, policy)
        for candidate in sorted(by_time.get(event.timestamp, []), key=lambda c:
                                (-c["native_score"], c["strategy"], c["symbol"], c["candidate_id"])):
            cid = candidate["candidate_id"]
            seen.add(cid)
            if candidate["symbol"] not in event.bars and decisions[cid]["veto_stage"] not in EXCLUDED_GATES:
                missing_bars.add(cid)
            for arm, account in accounts.items():
                _submit(account, arm, candidate, predictions[cid, policy.horizon_bars],
                        decisions[cid], event, result, policy)
        for arm, account in accounts.items():
            result["equity"].append({"arm": arm, "timestamp": iso(event.timestamp),
                "available_at": iso(as_utc_timestamp(event.timestamp)
                    + timeframe_delta(event.timeframe)),
                **_budget(account, policy), "cash": account.broker.portfolio.cash,
                "status": "diagnostic_after_halt" if account.halted else "valid",
                "stale_mark_symbols": sorted(s for s in account.broker.portfolio.positions if s not in event.bars)})
    if seen != set(candidates):
        result["errors"].append({"reason": "candidate_events_missing_from_market_data",
                                 "candidate_ids": sorted(set(candidates)-seen)})
    for arm, account in accounts.items():
        broker = account.broker
        for row in account.active.values():
            if not account.halted:
                row["status"], row["reason"] = "censored_end_of_data", "position_or_order_unfinished"
        rows = [row for row in result["rows"] if row["arm"] == arm]
        marks = broker.last_prices
        ending = broker.portfolio.get_equity(marks)
        curve = [policy.initial_capital] + [row["marked_equity"] for row in result["equity"] if row["arm"] == arm]
        peak, max_drawdown = curve[0], 0.0
        for value in curve:
            peak = max(peak, value)
            if peak > 0:
                max_drawdown = max(max_drawdown, (peak-value)/peak)
        positions = [{"symbol": symbol, "quantity": position["qty"], "entry_price": position["avg_price"],
                      "mark_price": marks.get(symbol, position["avg_price"]),
                      "unrealized_pnl_ex_costs": position["qty"]*(marks.get(symbol, position["avg_price"])-position["avg_price"])}
                     for symbol, position in sorted(broker.portfolio.positions.items()) if position["qty"]]
        result["accounts"].append({"arm": arm,
            "status": "unresolved_execution_error" if account.halted else "completed",
            "activity": "active" if broker.trades else "inactive", "halt_time": account.halt_time,
            "initial_capital": policy.initial_capital, "ending_marked_equity": ending,
            "net_pnl": None if account.halted else ending-policy.initial_capital,
            "return_fraction": None if account.halted else ending/policy.initial_capital-1,
            "max_drawdown_fraction": None if account.halted else max_drawdown,
            "fills": len(broker.trades), "entered_candidates": sum(row["filled_quantity"] > 0 for row in rows),
            "closed_candidates": sum(row["status"] == "closed" for row in rows),
            "active_candidates": len(account.active), "open_positions": len(positions), "ending_positions": positions,
            "commission": sum(fill["commission"] for fill in broker.trades),
            "slippage_cost": sum(fill["slip"]*fill["qty"] for fill in broker.trades),
            "financing_cost": broker.portfolio.cumulative_financing_cost, **_budget(account, policy)})
    result["validation"] = {"prediction_partition_ok": True, "candidate_event_partition_ok": seen == set(candidates),
        "candidate_bars_complete": not missing_bars,
        "arm_row_partition_ok": len(result["rows"]) == len(candidates)*len(ARMS),
        "independent_accounts": len({id(account.broker.portfolio) for account in accounts.values()}) == len(ARMS),
        "pretrade_budget_ok": all(row["decision_required_reservation"] <= row["decision_remaining_budget"]+1e-9
            for row in result["rows"] if "opening_order_id" in row), "no_official_actuation": True}
    if result["errors"] or not all(result["validation"].values()):
        result["status"] = "incomplete"
    result["input_identity"]["replay_facts_sha256"] = sha256_bytes(canonical_json({
        "fills": result["fills"], "financing": result["financing"], "equity": result["equity"]}).encode("utf-8"))
    return result
