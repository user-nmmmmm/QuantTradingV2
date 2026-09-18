"""Broker-backed counterfactual streams; never share a live trading object."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import pandas as pd

from core.broker import Broker
from core.events import TradingEventPipeline
from core.orders import TERMINAL_STATUSES
from core.portfolio import Portfolio
from core.signal_observation_types import ObservationPolicy, close_time, iso


@dataclass
class _Track:
    broker: Broker
    active: dict = field(default_factory=dict)
    audit_cursor: int = 0
    trade_cursor: int = 0
    financing_cursor: int = 0
    halted: bool = False


def clone_research_broker(template, capital, *, name):
    portfolio = Portfolio(capital, account_mode=template.portfolio.account_mode,
        initial_margin_rate=template.portfolio.initial_margin_rate,
        maintenance_margin_rate=template.portfolio.maintenance_margin_rate)
    keys = ("commission_rate", "commission_rate_maker", "slippage", "use_impact_cost",
            "max_participation_rate", "spread_bps", "volatility_slippage_factor",
            "impact_coefficient", "impact_exponent", "funding_interval_hours",
            "funding_rate_required", "default_borrow_rate_annual", "borrow_availability_required",
            "default_borrow_limit_qty", "liquidation_penalty_bps", "opening_order_ttl_bars")
    return Broker(portfolio, **{key: getattr(template, key) for key in keys},
                  random_slip=False, account_id=name, timeframe=template.timeframe,
                  event_pipeline=TradingEventPipeline(run_id=name, retention_limit=1000))


def replay_ghosts(market_data, payload, template):
    """Fixed holding horizon, close decision -> next real open on BOTH legs.

    One isolated account per (decision group, strategy, symbol), plus one
    capital-constrained account per decision group. No strategy exit, health
    gate or alpha overlay is inferred. Pending/partial orders occupy the track.
    """
    policy = ObservationPolicy.from_mapping(payload["policy"])
    decisions = {d["candidate_id"]: d for d in payload["decisions"]}
    by_time = defaultdict(list)
    for candidate in payload["candidates"]:
        decision = decisions[candidate["candidate_id"]]
        if decision["accepted"] is not None and decision["veto_stage"] not in {"warmup", "data"}:
            by_time[pd.Timestamp(candidate["timestamp"]).tz_localize(None)].append(candidate)
    tracks, rows, errors = {}, [], []
    fill_log, financing_log, execution_log = [], [], []
    for event in market_data.stream():
        for key, track in list(tracks.items()):
            if track.halted:
                continue
            broker = track.broker
            bars = {symbol: bar for symbol, bar in event.bars.items()
                    if symbol in track.active}
            if not bars:
                continue
            try:
                flat_symbols = [s for s in bars if broker.portfolio.get_position(s)["qty"] == 0]
                account_was_flat = not any(p["qty"] for p in broker.portfolio.positions.values())
                broker.process_orders(bars)
                # Unlike the live account, ghost tracks skip inactive bars.
                # A newly borrowed position must not inherit the financing
                # clock from an earlier, already flat trade across that gap.
                for symbol in flat_symbols:
                    if broker.portfolio.get_position(symbol)["qty"] < 0:
                        broker._last_borrow_time.pop(symbol, None)
                if account_was_flat:
                    broker._last_borrow_time.pop(broker.QUOTE_BORROW_SYMBOL, None)
                broker.accrue_carry(bars)
                marks = {**broker.last_prices,
                         **{s: float(b.get("mark_price", b.close)) for s, b in bars.items()}}
                if broker.portfolio.margin_snapshot(marks, timestamp=event.timestamp).liquidation_required:
                    # P0 deliberately does not invent a liquidation path from
                    # OHLC or continue an insolvent fixed-horizon account.
                    raise ValueError("ghost margin breach: liquidation path outside P0 replay scope")
            except (ValueError, ArithmeticError) as exc:
                errors.append({"track": list(key), "timestamp": iso(event.timestamp), "reason": str(exc)})
                for row in track.active.values():
                    row["status"] = "unresolved_execution_error"
                # Stop this research account; it cannot manufacture a known
                # outcome or restart its capital when funding is missing.
                track.halted = True
            for fill in broker.trades[track.trade_cursor:]:
                row = track.active.get(fill["symbol"])
                if row is None:
                    continue
                fill_log.append({"mode": key[0], "group": key[1],
                                 "candidate_id": row["candidate_id"], **fill})
                if fill["side"] in {"buy", "short"}:
                    row.setdefault("first_fill_time", iso(fill["fill_time"]))
                    row["filled_quantity"] += fill["qty"]
                    row["entry_commission"] += fill["commission"]
                else:
                    row["last_exit_time"] = iso(fill["fill_time"])
                    row["exit_commission"] += fill["commission"]
            track.trade_cursor = len(broker.trades)
            for item in broker.portfolio.financing_ledger[track.financing_cursor:]:
                entry = item.to_dict()
                financing_log.append({"mode": key[0], "group": key[1],
                    "candidate_id": track.active.get(entry["symbol"], {}).get("candidate_id"), **entry})
                symbol = entry["symbol"]
                if symbol in track.active:
                    track.active[symbol]["carry"] += entry["amount"]
                elif symbol == "__QUOTE__":
                    weights = {s: max(broker.portfolio.get_position(s)["qty"], 0)
                               * broker.last_prices.get(s, 0) for s in track.active}
                    total = sum(weights.values())
                    if total:
                        for s, weight in weights.items():
                            track.active[s]["carry"] += entry["amount"] * weight / total
            track.financing_cursor = len(broker.portfolio.financing_ledger)
            if track.halted:
                for audit in broker.execution_audit[track.audit_cursor:]:
                    execution_log.append({"mode": key[0], "group": key[1], **audit})
                track.audit_cursor = len(broker.execution_audit)
                continue
            for symbol, row in list(track.active.items()):
                if symbol not in event.bars:
                    continue
                order = broker.opening_orders[row["opening_order_id"]]
                qty = float(broker.portfolio.get_position(symbol)["qty"])
                if row["filled_quantity"] == 0 and order.status in TERMINAL_STATUSES:
                    row["status"] = "unfilled_" + order.status.value
                    del track.active[symbol]
                    continue
                if row["filled_quantity"] > 0:
                    row["holding_bars"] += 1
                if qty == 0 and row["filled_quantity"] > 0:
                    row["status"] = "closed"
                    row["realized_pnl_ex_carry"] = sum(
                        e.realized_pnl for e in broker.close_events
                        if e.symbol == symbol and e.timestamp >= pd.Timestamp(row["first_fill_time"]).tz_localize(None)
                    )
                    row["net_pnl"] = row["realized_pnl_ex_carry"] - row["carry"]
                    # Costs use the execution bar's range/volume; the completed
                    # research label is only available once that bar closes.
                    row["available_at"] = close_time(event.timestamp, event.timeframe)
                    del track.active[symbol]
                    continue
                if row["holding_bars"] >= policy.ghost_horizon and not row.get("exit_submitted"):
                    broker.cancel_opening_orders([symbol])
                    if qty:
                        broker.submit_order(symbol, "sell" if qty > 0 else "cover", abs(qty),
                            price=float(event.bars[symbol]["close"]), timestamp=event.timestamp,
                            strategy_id=row["strategy"], exit_reason="GhostFixedHorizon")
                        row["exit_submitted"] = True
            for audit in broker.execution_audit[track.audit_cursor:]:
                execution_log.append({"mode": key[0], "group": key[1], **audit})
            track.audit_cursor = len(broker.execution_audit)
        for candidate in sorted(by_time.get(event.timestamp, []),
                                key=lambda c: (-c["native_score"], c["strategy"], c["symbol"], c["candidate_id"])):
            d = decisions[candidate["candidate_id"]]
            group = "accepted" if d["accepted"] else "veto:" + d["veto_stage"]
            for mode in ("isolated_track", "capital_constrained"):
                key = (mode, group, candidate["strategy"], candidate["symbol"]) if mode == "isolated_track" else (mode, group)
                if key not in tracks:
                    name = "ghost:" + ":".join(key)
                    tracks[key] = _Track(clone_research_broker(template, policy.ghost_capital, name=name))
                track, symbol = tracks[key], candidate["symbol"]
                row = {"candidate_id": candidate["candidate_id"], "mode": mode, "group": group,
                       "strategy": candidate["strategy"], "symbol": symbol,
                       "signal_time": candidate["timestamp"], "status": "queued",
                       "horizon_bars": policy.ghost_horizon, "holding_bars": 0,
                       "filled_quantity": 0.0, "entry_commission": 0.0,
                       "exit_commission": 0.0, "carry": 0.0, "net_pnl": None}
                rows.append(row)
                if track.halted:
                    row["status"] = "unresolved_track_error"
                    continue
                if symbol in track.active:
                    row["status"] = "busy_blocked"
                    continue
                broker = track.broker
                if mode == "capital_constrained":
                    marks = {**broker.last_prices, **{s: float(b.close) for s, b in event.bars.items()}}
                    equity = broker.portfolio.get_equity(marks)
                    used = sum(abs(p["qty"])*marks.get(s, p["avg_price"])
                               for s, p in broker.portfolio.positions.items())
                    reserved = sum(broker.pending_open_notional(marks).values())
                    if used + reserved + policy.reference_notional > max(equity, 0):
                        row["status"] = "capital_blocked"
                        continue
                signal = candidate["signal"]
                order = broker.submit_order(symbol, signal["action"],
                    policy.reference_notional/candidate["reference_price"],
                    price=candidate["reference_price"], order_type=signal.get("order_type", "market"),
                    timestamp=event.timestamp, strategy_id=candidate["strategy"], exit_reason="GhostEntry")
                row["opening_order_id"] = order.id
                track.active[symbol] = row
    for track in tracks.values():
        if not track.halted:
            for row in track.active.values():
                row["status"] = "censored_end_of_data"
    accounts = [{"track": list(key), "initial_capital": policy.ghost_capital,
                 "ending_marked_equity": track.broker.portfolio.get_equity(track.broker.last_prices),
                 "open_positions": sum(p["qty"] != 0 for p in track.broker.portfolio.positions.values()),
                 "active_candidates": len(track.active),
                 "status": "unresolved_execution_error" if track.halted else "completed"}
                for key, track in sorted(tracks.items())]
    return {"rows": rows, "accounts": accounts, "fills": fill_log, "financing": financing_log,
            "execution_audit": execution_log, "errors": errors,
            "protocol": {"holding": "count real bars from first fill; exit next real open after horizon close",
                         "partial_entries": "cancel unfilled remainder at exit decision",
                         "exits": "fixed_horizon_only_no_strategy_or_protective_stop",
                         "random_slippage": "deterministic_configured_rate_no_rng_draws",
                         "availability": "execution-bar costs and completed labels available after bar close",
                         "capital": "separate account per decision group; pre-trade one-times-gross reservation budget; post-gap fills follow broker margin rules",
                         "carry": "broker funding/borrow; quote interest attributed by current long notional",
                         "margin_breach": "halt_as_unresolved_without_inventing_liquidation_fills",
                         "interpretation": "descriptive policy replay, not causal gate uplift; isolated accounts are not one portfolio"}}
