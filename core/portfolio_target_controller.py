"""Opt-in weekly target execution through the existing risk and Broker paths.

Targets are intentions. Only Broker order/fill/lot facts consume quantities and
budgets. The controller never treats a proposal or a missing order as a fill.
"""
from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
import math

import pandas as pd

from core.accounts import AccountMode
from core.domain import OrderIntent
from core.entry_audit import capture
from core.events import OrderEvent
from core.margin_rebalance import MarginBudgetSnapshot, MarginQuote, plan_margin_targets
from core.quote_borrow import utc
from core.risk.portfolio_governor import (PortfolioRiskGovernor, CorrelationClusterPolicy,
                                         crypto_beta_open_stop_risk, open_risk_by_cluster,
                                         exposure_by_cluster)


_TERMINAL = {"filled", "canceled", "rejected", "expired", "no_position", "expired_unsubmitted"}
# The historical Broker aliases SUBMITTED to the canonical "submitting"
# enum, and retains it on accepted resting protective orders. UNKNOWN and
# CANCEL_PENDING are the genuinely unresolved states in this adapter.
_AMBIGUOUS = {"unknown", "cancel_pending"}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False,
                                     separators=(",", ":")).encode()).hexdigest()


def _status(order):
    return str(getattr(getattr(order, "status", None), "value", getattr(order, "status", "unknown")))


def _week(value):
    stamp = utc(value).normalize()
    return (stamp-pd.Timedelta(days=stamp.weekday())).isoformat()


class PortfolioTargetController:
    """Daily reconciliation of a Monday target; disabled unless explicitly wired.

    Provider signature: ``provider(event=, as_of=, held_symbols=,
    existing_weights=)``. It returns a dict or ``.to_dict()`` object containing
    target_weights, stop_prices, add_allowed, as_of and optional forced_exits.
    Health scaling is performed here once on newly approved quantity. Providers
    must leave health/account multipliers at one.
    """

    def __init__(self, *, strategy, target_provider, quote_borrow_policy=None,
                 max_weekly_turnover=.5, relative_tolerance=.1, participation=.01,
                 state_store=None, checkpoint_key="portfolio_target_v3", metadata=None):
        if not 0 < max_weekly_turnover <= 1 or not 0 <= relative_tolerance < 1:
            raise ValueError("invalid turnover or tolerance")
        if not 0 < participation <= 1:
            raise ValueError("invalid participation")
        self.strategy, self.target_provider = strategy, target_provider
        self.quote_borrow_policy = quote_borrow_policy
        self.max_weekly_turnover, self.relative_tolerance = max_weekly_turnover, relative_tolerance
        self.participation = participation
        self.state_store, self.checkpoint_key = state_store, checkpoint_key
        self.metadata = metadata or {}
        self.policy_identity = _digest({
            "strategy": self.strategy.name,
            "strategy_parameters": getattr(self.strategy, "trend_parameters_identity", None),
            "max_weekly_turnover": max_weekly_turnover, "relative_tolerance": relative_tolerance,
            "participation": participation,
            "borrow_mode": getattr(quote_borrow_policy, "mode", None),
            "assumed_rate": getattr(quote_borrow_policy, "assumed_annual_rate", None),
            "assumed_limit": getattr(quote_borrow_policy, "assumed_limit", None)})
        self.audit = []
        self._orders = {}
        self._prepared = None
        self._state = {"version": 1, "policy_identity": self.policy_identity,
                       "account": None, "week": None, "target_id": None,
                       "targets": {}, "suppressed": [], "orders": {}, "seen_closes": [],
                       "sequence": 0, "last_processed": None, "session": None, "session_risk": 0.}
        if state_store is not None:
            saved = state_store.get(checkpoint_key)
            if saved is not None:
                self.restore(saved)

    def bind(self, broker):
        broker = getattr(broker, "broker", broker)
        account = getattr(broker, "account_id", "backtest")
        if self._state["account"] not in (None, account):
            raise ValueError("portfolio target checkpoint belongs to another account")
        self._state["account"] = account
        if self.quote_borrow_policy is not None:
            broker.quote_borrow_policy = self.quote_borrow_policy
            broker.quote_borrow_market_metadata = self.metadata
        broker.portfolio_target_controller = self

    @staticmethod
    def _filled_notionals(broker):
        totals = {}
        for trade in getattr(broker, "trades", ()):
            order_id = trade.get("order_id")
            totals[order_id] = totals.get(order_id, 0.)+float(trade["qty"])*float(trade["fill_price"])
        return totals

    def fill_quantity_cap(self, *, broker, order, price, requested, timestamp):
        """Recheck immutable turnover approval at the actual execution price.

        Each accepted discretionary order owns a disjoint part of the weekly
        budget. A gap can reduce its fill quantity, never expand that budget.
        Forced exits have no discretionary turnover cap.
        """
        if order.exit_reason != "v3_rebalance":
            return requested
        record = self._state["orders"].get(order.id)
        if record is None:
            return 0.
        notionals = self._filled_notionals(broker)
        remaining = max(0., record["turnover_budget"]-notionals.get(order.id, 0.))
        week = _week(timestamp)
        actual = sum(float(trade["qty"])*float(trade["fill_price"])
                     for trade in getattr(broker, "trades", ())
                     if trade.get("exit_reason") == "v3_rebalance" and _week(trade["fill_time"]) == week)
        remaining = min(remaining, max(0., self._state.get("weekly_equity", 0.)*self.max_weekly_turnover-actual))
        if price <= 0:
            return 0.
        if requested*price <= remaining:
            return requested
        return max(0., math.nextafter(remaining/price, 0.))

    def export(self):
        return {"schema_version": "portfolio-target-controller/v1", "checkpoint": self.checkpoint(),
                "audit": list(self.audit), "formal_routing_enabled": False,
                "quote_borrow_mode": getattr(self.quote_borrow_policy, "mode", "cash_only"),
                "max_weekly_turnover": self.max_weekly_turnover,
                "relative_tolerance": self.relative_tolerance}

    def prepare(self, *, event, portfolio, broker, current_prices):
        """Select once, then tell the runtime which symbols require state work.

        All prices still enter account valuation. Closed symbols remain in the
        runtime pass so their authoritative close events reach strategy health.
        """
        as_of = utc(event.timestamp)+pd.Timedelta(days=1)
        equity = float(portfolio.get_equity(dict(current_prices)))
        held = tuple(symbol for symbol, position in portfolio.positions.items() if position["qty"] != 0)
        existing = {symbol: portfolio.get_position(symbol)["qty"]*current_prices.get(symbol, 0.)/equity
                    for symbol in held} if equity > 0 else {}
        result = self.target_provider(event=event, as_of=as_of, held_symbols=held, existing_weights=existing)
        snapshot = result.to_dict() if hasattr(result, "to_dict") else dict(result)
        if utc(snapshot.get("as_of", as_of)) > as_of:
            raise ValueError("target snapshot is not yet available")
        self._prepared = (event, snapshot)
        seen = set(self._state["seen_closes"])
        self.management_symbols = set(held) | {
            close.symbol for close in getattr(broker, "close_events", ()) if close.close_event_id not in seen}
        return (self.management_symbols | set(self._state["targets"])
                | {order.symbol for order in self._working(broker)}
                | {symbol for symbol, allowed in snapshot.get("add_allowed", {}).items() if allowed})

    def checkpoint(self):
        payload = json.loads(json.dumps(self._state, allow_nan=False))
        return {"schema_version": "portfolio-target-controller/v1", "state": payload,
                "sha256": _digest(payload)}

    def restore(self, checkpoint):
        if (checkpoint.get("schema_version") != "portfolio-target-controller/v1"
                or checkpoint.get("sha256") != _digest(checkpoint.get("state"))):
            raise ValueError("invalid portfolio target checkpoint")
        if checkpoint["state"].get("policy_identity") != self.policy_identity:
            raise ValueError("portfolio target checkpoint policy changed")
        self._state = json.loads(json.dumps(checkpoint["state"], allow_nan=False))
        self._orders = {}

    def _save(self):
        if self.state_store is not None:
            self.state_store.set(self.checkpoint_key, self.checkpoint())

    @staticmethod
    def _positions(portfolio, symbol):
        qty = float(portfolio.get_position(symbol).get("qty", 0.))
        if qty < 0:
            raise ValueError("portfolio target controller only supports long positions")
        book = getattr(portfolio, "lot_books", {}).get(symbol)
        lots = list(getattr(book, "open_lots", ()))
        if qty and (not lots or not math.isclose(sum(float(lot.qty_open) for lot in lots), qty,
                                                rel_tol=1e-8, abs_tol=1e-8)):
            raise ValueError("position quantities are not reconciled with authoritative lots")
        return qty, sorted({lot.position_id for lot in lots})

    @staticmethod
    def _working(broker):
        found = {}
        for order in list(getattr(broker, "pending_orders", ())) + list(getattr(broker, "active_orders", ())):
            if _status(order) not in _TERMINAL:
                found[order.id] = order
        return list(found.values())

    def _reconcile(self, broker):
        found = {**getattr(broker, "opening_orders", {}), **self._orders}
        found.update({order.id: order for order in self._working(broker)})
        events = {}
        for envelope in getattr(getattr(broker, "event_pipeline", None), "events", ()):
            if isinstance(envelope.payload, OrderEvent):
                events[envelope.payload.client_order_id] = envelope.payload
        reconciled = True
        for order_id, record in self._state["orders"].items():
            fact = found.get(order_id) or events.get(order_id)
            if fact is None:
                if record.get("status") not in _TERMINAL:
                    reconciled = False
                continue
            status = _status(fact)
            filled = float(getattr(fact, "filled_qty", 0.))
            if filled + 1e-9 < float(record.get("filled_qty", 0.)):
                raise ValueError("order filled quantity regressed")
            record.update(status=status, filled_qty=filled,
                          remaining_qty=float(getattr(fact, "remaining_qty", 0.)))
            if status in _AMBIGUOUS:
                reconciled = False
        if any(_status(order) in _AMBIGUOUS for order in self._working(broker)):
            reconciled = False
        return reconciled

    def _suppress_exits(self, broker, as_of, working):
        suppressed = set(self._state["suppressed"])
        seen = set(self._state["seen_closes"])
        for event in getattr(broker, "close_events", ()):
            if event.close_event_id in seen:
                continue
            seen.add(event.close_event_id)
            if (event.exit_reason != "v3_rebalance" and _week(event.timestamp) == _week(as_of)):
                suppressed.add(event.symbol)
        for order in working:
            if (order.side == "sell" and order.exit_reason not in {"v3_rebalance", "protective_stop"}):
                suppressed.add(order.symbol)
        self._state["suppressed"] = sorted(suppressed)
        self._state["seen_closes"] = sorted(seen)
        if suppressed:
            broker.cancel_opening_orders(suppressed, timestamp=as_of)

    def _freeze(self, snapshot, *, event, as_of, portfolio, broker, prices, equity, account_multiplier=1.):
        # An old week's unfinished orders retain capacity until cancellation is
        # confirmed. Protective/reducing orders are never canceled here.
        broker.cancel_opening_orders(set(self._state["targets"]), timestamp=as_of)
        weights = snapshot.get("target_weights", {})
        targets = {}
        health = float(self.strategy.health_risk_multiplier())
        if not math.isfinite(health) or not 0 <= health <= 1:
            raise ValueError("health multiplier must be in [0,1]")
        for symbol in sorted(set(weights) | set(portfolio.positions)):
            weight = float(weights.get(symbol, 0.))
            if not math.isfinite(weight) or weight < 0:
                raise ValueError("target weights must be finite and nonnegative")
            qty, identities = self._positions(portfolio, symbol)
            price = float(prices.get(symbol, 0.))
            if price <= 0:
                continue
            stop = float(snapshot.get("stop_prices", {}).get(symbol, 0.) or 0.)
            target_qty = equity*weight/price
            if target_qty > qty:
                target_qty = qty+(target_qty-qty)*health*account_multiplier
            targets[symbol] = {"weight": weight, "quantity": target_qty,
                               "initial_qty": qty, "position_ids": identities,
                               "opening_capacity": max(0., target_qty-qty),
                               "reference_price": price, "stop_price": stop,
                               "health_multiplier": health,
                               "account_multiplier": account_multiplier,
                               "sizing_multiplier": health*account_multiplier,
                               "approved_capacity_risk": max(0., target_qty-qty)*max(0., price-stop)}
        frozen = {"as_of": as_of.isoformat(), "targets": targets, "account": self._state["account"],
                  "policy_identity": self.policy_identity}
        self._state.update(week=_week(as_of), target_id=_digest(frozen), targets=targets,
                           suppressed=[], decision_as_of=as_of.isoformat(),
                           weekly_equity=equity)

    def process(self, *, event, portfolio, broker, risk_manager, current_prices,
                risk_decision, risk_governor=None, market_states=None):
        self.bind(broker)
        if portfolio.account_mode not in {AccountMode.SPOT, AccountMode.SPOT_MARGIN}:
            raise ValueError("V3 target execution only supports spot or spot-margin accounts")
        if event.timeframe not in {"1d", "daily", "D", "unknown"}:
            raise ValueError("V3 portfolio controller requires daily bars")
        as_of = utc(event.timestamp)+pd.Timedelta(days=1)
        if self._state["last_processed"] is not None and as_of < utc(self._state["last_processed"]):
            raise ValueError("portfolio event time regressed")
        equity = float(portfolio.get_equity(dict(current_prices)))
        if equity <= 0:
            return []
        health_check = getattr(self.strategy, "check_health", None)
        health_allows = health_check(as_of) if callable(health_check) else True
        held = tuple(symbol for symbol, position in portfolio.positions.items() if position["qty"] != 0)
        existing = {symbol: portfolio.get_position(symbol)["qty"]*current_prices.get(symbol, 0.)/equity
                    for symbol in held}
        if self._prepared is None or self._prepared[0] is not event:
            self.prepare(event=event, portfolio=portfolio, broker=broker, current_prices=current_prices)
        snapshot = self._prepared[1]
        self._prepared = None
        reconciled = self._reconcile(broker)
        if as_of.weekday() == 0 and self._state["week"] != _week(as_of):
            self._freeze(snapshot, event=event, as_of=as_of, portfolio=portfolio,
                         broker=broker, prices=current_prices, equity=equity,
                         account_multiplier=float(risk_manager.risk_multiplier))
        working = self._working(broker)
        self._suppress_exits(broker, as_of, working)
        forced = set(snapshot.get("forced_exits", ()))
        for symbol, metadata in self.metadata.items():
            for fact in metadata.get("events", ()):
                if (fact.get("kind", fact.get("action")) in {"spot_delisted", "delisted"}
                        and fact.get("source_status", "unavailable") == "verified"
                        and fact.get("available_at") is not None and utc(fact["available_at"]) <= as_of):
                    forced.add(symbol)
        self._state["suppressed"] = sorted(set(self._state["suppressed"]) | forced)
        if forced:
            broker.cancel_opening_orders(forced, timestamp=as_of)
        working = self._working(broker)
        reconciled = self._reconcile(broker) and reconciled
        if not reconciled:
            self.audit.append({"as_of": as_of.isoformat(), "reason": "unreconciled_orders"})
            self._save()
            return []
        governor = risk_governor or PortfolioRiskGovernor(CorrelationClusterPolicy(
            max_same_session_entry_risk=.02, max_correlated_stop_risk=.03,
            max_crypto_beta_stop_risk=.03))
        governor.begin_session(event.timestamp)
        session = as_of.isoformat()
        if self._state["session"] != session:
            self._state.update(session=session, session_risk=0.)
        governor._session_risk = max(governor._session_risk, self._state["session_risk"])
        projection = getattr(broker, "reservation_projection", None)
        pending_notional = broker.pending_open_notional(dict(current_prices))
        pending_risk = projection.pending_stop_risk() if projection is not None else {}
        pending_buys, pending_sells = {}, {}
        for order in working:
            mapping = pending_buys if order.side == "buy" else pending_sells if order.side == "sell" else None
            if mapping is not None and order.exit_reason != "protective_stop":
                mapping[order.symbol] = mapping.get(order.symbol, 0.)+float(order.remaining_qty)
        targets = self._state["targets"]
        buy, sell, quotes, distances, approvals = {}, {}, {}, {}, {}
        symbol_audit = {}
        health = float(self.strategy.health_risk_multiplier())
        if not math.isfinite(health) or not 0 <= health <= 1:
            raise ValueError("health multiplier must be in [0,1]")
        account_multiplier = float(risk_manager.risk_multiplier)
        if not math.isfinite(account_multiplier) or not 0 <= account_multiplier <= 1:
            raise ValueError("account multiplier must be in [0,1]")
        for symbol in sorted(set(targets) | forced):
            target = targets.get(symbol, {})
            detail = {"target_weight": target.get("weight", 0.),
                      "target_quantity": target.get("quantity", 0.), "reasons": []}
            symbol_audit[symbol] = detail
            if symbol not in event.bars:
                detail["reasons"].append("missing_real_bar")
                continue
            bar = event.bars[symbol]
            price = float(current_prices[symbol])
            metadata = self.metadata.get(symbol, {})
            quote = MarginQuote(price, max(0., float(bar.get("volume", 0.))),
                                float(metadata.get("quantity_step", 1e-8)),
                                float(metadata.get("min_notional", 5.)))
            quotes[symbol] = quote
            current, identities = self._positions(portfolio, symbol)
            target = targets.get(symbol, {"quantity": 0., "weight": 0., "position_ids": identities})
            detail.update(current_quantity=current, current_weight=current*price/equity,
                          pending_buy_quantity=pending_buys.get(symbol, 0.),
                          pending_sell_quantity=pending_sells.get(symbol, 0.))
            if symbol in forced:
                sell[symbol] = max(0., current-pending_sells.get(symbol, 0.))
                detail["reasons"].append("forced_exit_bypasses_turnover")
                continue
            if target["position_ids"]:
                if current and not set(identities).intersection(target["position_ids"]):
                    self._state["suppressed"] = sorted(set(self._state["suppressed"]) | {symbol})
                    detail["reasons"].append("position_lifecycle_changed")
            elif current:
                own_fills = any(record["symbol"] == symbol and record["target_id"] == self._state["target_id"]
                                and record["side"] == "buy" and record["filled_qty"] > 0
                                for record in self._state["orders"].values())
                if own_fills:
                    target["position_ids"] = identities
                else:
                    self._state["suppressed"] = sorted(set(self._state["suppressed"]) | {symbol})
                    detail["reasons"].append("unowned_new_position")
            delta = target["quantity"]-current-pending_buys.get(symbol, 0.)+pending_sells.get(symbol, 0.)
            detail["remaining_target_quantity"] = delta
            if target["quantity"] > 0 and abs(delta) < target["quantity"]*self.relative_tolerance:
                detail["reasons"].append("within_relative_tolerance_or_reserved")
                continue
            if delta < 0:
                sell[symbol] = min(-delta, max(0., current-pending_sells.get(symbol, 0.)))
                detail["reasons"].append("ordinary_target_reduction")
                continue
            gates = {
                "target_satisfied": delta <= 0,
                "same_week_buy_suppressed": symbol in self._state["suppressed"],
                "account_blocks_new_risk": not risk_decision.allow_new_entries,
                "strategy_health_blocks_new_risk": not health_allows,
                "daily_add_signal_unavailable": not snapshot.get("add_allowed", {}).get(symbol, False),
                "market_membership_blocks_entry": bool(bar.get("entry_blocked", False)),
                "pending_reduction": pending_sells.get(symbol, 0.) > 0}
            blocked_reasons = [reason for reason, blocked in gates.items() if blocked]
            if blocked_reasons:
                detail["reasons"].extend(blocked_reasons)
                continue
            market_gate = getattr(self.strategy, "entry_risk_multiplier", None)
            if callable(market_gate) and market_states is not None:
                if market_gate(market_states.get(symbol)) <= 0:
                    detail["reasons"].append("abnormal_or_unknown_market_state")
                    continue
            context = self.strategy.get_context(symbol)
            stop = max(float(target.get("stop_price", 0.)),
                       float(snapshot.get("stop_prices", {}).get(symbol, 0.) or 0.),
                       float(context.get("stop_loss", 0.) or 0.),
                       float(context.get("effective_stop", 0.) or 0.),
                       float(context.get("trailing_stop", 0.) or 0.))
            if not 0 < stop < price:
                detail["reasons"].append("invalid_or_crossed_protection")
                continue
            stop_planner = getattr(self.strategy, "_plan_stop", None)
            frame, index = event.histories.get(symbol), event.positions.get(symbol)
            if callable(stop_planner) and frame is not None and index is not None:
                stop_history = frame.iloc[:index+1].copy()
                ensure_atr = getattr(self.strategy, "_ensure_atr", None)
                if callable(ensure_atr):
                    ensure_atr(stop_history)
                stop_plan = stop_planner(side="buy", reference_price=price,
                    structural_stop=stop, df=stop_history, i=index)
                if not stop_plan.accepted:
                    detail["reasons"].append("initial_stop_policy_rejected")
                    continue
                stop = max(stop, float(stop_plan.stop_price))
                if stop >= price:
                    detail["reasons"].append("price_crossed_protection")
                    continue
            consumed = sum(record["filled_qty"] for record in self._state["orders"].values()
                           if record["symbol"] == symbol and record["side"] == "buy"
                           and record["target_id"] == self._state["target_id"])
            capacity = max(0., target["opening_capacity"]-consumed-pending_buys.get(symbol, 0.))
            # A probation target cannot gradually regain full size by applying
            # the same multiplier to each remaining tranche. Recovery waits for
            # the next weekly approval; deterioration tightens this target.
            original_multiplier = target.get("sizing_multiplier", target.get("health_multiplier", 1.))
            multiplier = health*account_multiplier
            if multiplier < original_multiplier:
                ratio = multiplier/original_multiplier if original_multiplier else 0.
                capacity *= ratio
                target["opening_capacity"] = consumed+pending_buys.get(symbol, 0.)+capacity
                target["quantity"] = min(target["quantity"], current+pending_buys.get(symbol, 0.)+capacity)
                target["health_multiplier"] = health
                target["account_multiplier"] = account_multiplier
                target["sizing_multiplier"] = multiplier
            quantity = min(delta, capacity)
            detail.update(remaining_original_capacity=capacity, consumed_quantity=consumed,
                          health_multiplier=target.get("health_multiplier", 1.),
                          account_multiplier=target.get("account_multiplier", 1.), stop_price=stop)
            # Freeze the original monetary approval too: increasing prices or
            # replacing a stop cannot recycle consumed risk after a partial exit.
            consumed_risk = sum(record["approved_risk_amount"]*record["filled_qty"]/record["qty"]
                                for record in self._state["orders"].values()
                                if record["symbol"] == symbol and record["side"] == "buy"
                                and record["target_id"] == self._state["target_id"])
            risk_left = max(0., target["approved_capacity_risk"]-consumed_risk-pending_risk.get(symbol, 0.))
            quantity = min(quantity, risk_left/(price-stop), equity*.01/(price-stop))
            detail["remaining_original_approved_risk"] = risk_left
            if quantity <= 0:
                detail["reasons"].append("original_target_capacity_or_approval_exhausted")
            risk_audit = {}
            with capture(risk_audit):
                quantity = risk_manager.clamp_entry_qty(portfolio, symbol, quantity, price,
                    current_prices=dict(current_prices), pending_open_notional=pending_notional,
                    reservation_projection=projection, action="buy", health_multiplier=health)
            detail["account_risk"] = risk_audit
            budget = getattr(risk_manager, "drawdown_budget", None)
            if budget is not None and quantity > 0:
                quantity = budget.clamp(symbol, quantity, price, stop)
            if quantity > 0:
                buy[symbol], distances[symbol], approvals[symbol] = quantity, price-stop, quantity*price
                detail["requested_buy_quantity"] = quantity
            else:
                detail["reasons"].append("account_or_drawdown_budget_exhausted")
        # Shared cluster budgets are applied to each cluster as a batch so that
        # deterministic symbol sorting never allocates scarce risk alphabetically.
        cluster_policy = governor.policy
        open_cluster = open_risk_by_cluster(cluster_policy, portfolio)
        cluster_exposure = exposure_by_cluster(cluster_policy, portfolio, dict(current_prices), pending_notional)
        for cluster in {cluster_policy.cluster_for(symbol) for symbol in buy}:
            members = [symbol for symbol in buy if cluster_policy.cluster_for(symbol) == cluster]
            planned = sum(buy[symbol]*distances[symbol] for symbol in members)
            cap = cluster_policy.max_correlated_stop_risk
            scale = 1.
            if cap is not None and planned:
                reserved = sum(value for symbol, value in pending_risk.items()
                               if cluster_policy.cluster_for(symbol) == cluster)
                scale = min(1., max(0., equity*cap-open_cluster.get(cluster, 0.)-reserved)/planned)
            cluster_cap = cluster_policy.max_cluster_exposure_pct
            planned_notional = sum(buy[symbol]*quotes[symbol].price for symbol in members)
            if cluster_cap is not None and planned_notional:
                scale = min(scale, max(0., equity*cluster_cap-cluster_exposure.get(cluster, 0.))/planned_notional)
            for symbol in members:
                buy[symbol] *= scale
                if scale < 1:
                    symbol_audit[symbol]["reasons"].append("shared_cluster_budget")
                    symbol_audit[symbol]["cluster_scale"] = scale
        parent_limit = min(.03, cluster_policy.max_crypto_beta_stop_risk or .03)
        daily_limit = min(.02, cluster_policy.max_same_session_entry_risk or .02)
        risk_remaining = max(0., min(equity*parent_limit-crypto_beta_open_stop_risk(portfolio)-sum(pending_risk.values()),
                                    equity*daily_limit-governor._session_risk))
        week_start = utc(self._state["week"]) if self._state["week"] else utc(_week(as_of))
        turnover = sum(float(trade["qty"])*float(trade["fill_price"])
                       for trade in getattr(broker, "trades", ())
                       if trade.get("exit_reason") == "v3_rebalance" and utc(trade["fill_time"]) >= week_start)
        forced_turnover = sum(float(trade["qty"])*float(trade["fill_price"])
                              for trade in getattr(broker, "trades", ())
                              if trade.get("side") == "sell" and trade.get("exit_reason") != "v3_rebalance"
                              and utc(trade["fill_time"]) >= week_start)
        filled_notionals = self._filled_notionals(broker)
        turnover += sum(max(0., self._state["orders"][order.id]["turnover_budget"]-filled_notionals.get(order.id, 0.))
                        for order in working if order.exit_reason == "v3_rebalance" and order.id in self._state["orders"])
        available_margin = max(0., portfolio.margin_snapshot(dict(current_prices)).available_margin)
        margin_rate = portfolio.initial_margin_rate if portfolio.account_mode.uses_margin else 1.
        borrow_limit = 0.
        if portfolio.account_mode is AccountMode.SPOT_MARGIN and self.quote_borrow_policy is not None:
            borrow_limit = self.quote_borrow_policy.resolve(account=broker.account_id, as_of=as_of).limit
        gross = portfolio.get_total_exposure(dict(current_prices))+sum(pending_notional.values())
        gross_cap = min(3., float(getattr(risk_manager, "max_leverage", 3.)),
                        cluster_policy.max_crypto_beta_exposure or 3.)
        long_notional = sum(max(0., position["qty"])*current_prices.get(symbol, position["avg_price"])
                            for symbol, position in portfolio.positions.items())
        net_quote_cash = equity-long_notional if portfolio.account_mode.uses_margin else portfolio.cash
        financing_allowed = {}
        for symbol in buy:
            eligibility = (self.quote_borrow_policy.market_eligibility(symbol=symbol, as_of=as_of, metadata=self.metadata)
                           if self.quote_borrow_policy is not None else None)
            financing_allowed[symbol] = bool(eligibility and eligibility.allowed)
            symbol_audit[symbol]["margin_eligibility"] = asdict(eligibility) if eligibility is not None else {
                "allowed": False, "status": "unavailable", "reason": "cash_only_account"}
            if not financing_allowed[symbol]:
                symbol_audit[symbol]["reasons"].append("cash_only_no_market_financing_permission")
        budget_snapshot = MarginBudgetSnapshot(account=self._state["account"], as_of=as_of.isoformat(),
            equity=equity, net_quote_cash=net_quote_cash,
            current_gross_exposure=portfolio.get_total_exposure(dict(current_prices)),
            approved_buy_notional=approvals, quote_borrow_limit=borrow_limit,
            available_margin=available_margin, initial_margin_rate=margin_rate,
            pending_buy_notional=sum(pending_notional.values()),
            gross_headroom=max(0., equity*gross_cap-gross),
            remaining_turnover_notional=max(0., self._state.get("weekly_equity", equity)*self.max_weekly_turnover-turnover),
            remaining_stop_risk=risk_remaining, facts_reconciled=True)
        plan = plan_margin_targets(budget=budget_snapshot, requested_buys=buy, requested_sells=sell,
            quotes=quotes, stop_distances=distances,
            target_gross_exposure=sum(target["quantity"]*current_prices.get(symbol, target["reference_price"])
                                      for symbol, target in targets.items()),
            fee_rate=float(getattr(broker, "commission_rate", .001)),
            slippage_rate=float(getattr(broker, "slippage", .001)), participation=self.participation,
            forced_exits=forced, financing_allowed=financing_allowed)
        planned_symbols = {item["symbol"] for item in plan["orders"]}
        for symbol in set(buy) | set(sell):
            if symbol not in planned_symbols:
                symbol_audit[symbol]["reasons"].append("no_quantity_after_budget_participation_or_venue_minimum")
            if plan["turnover_scale"] < 1 and symbol not in forced:
                symbol_audit[symbol]["reasons"].append("weekly_turnover_budget")
            if plan["buy_scale"] < 1 and symbol in buy:
                symbol_audit[symbol]["reasons"].append("shared_cash_margin_gross_or_stop_budget")
        submitted = []
        for proposal in plan["orders"]:
            symbol, quantity = proposal["symbol"], proposal["qty"]
            price, side = proposal["reference_price"], proposal["side"]
            decision = None
            if side == "buy":
                minimum = risk_manager.minimum_entry_notional(equity, health,
                    live=getattr(getattr(risk_manager, "drawdown_budget", None), "live", False))
                if quantity*price < minimum:
                    symbol_audit[symbol]["reasons"].append("below_minimum_entry_notional")
                    continue
                if not risk_manager.check_entry_risk(
                        portfolio, symbol, quantity, price, current_prices=dict(current_prices),
                        pending_open_notional=broker.pending_open_notional(dict(current_prices)), action="buy"):
                    symbol_audit[symbol]["reasons"].append("final_shared_account_risk_rejected")
                    continue
                decision = governor.evaluate(symbol=symbol, planned_risk=quantity*distances[symbol],
                    equity=equity, portfolio=portfolio,
                    pending_stop_risk=projection.pending_stop_risk() if projection else {})
                if not decision.allowed:
                    symbol_audit[symbol]["reasons"].append(decision.reason)
                    continue
                symbol_audit[symbol]["governor"] = decision.to_dict()
                quantity *= decision.scale
            sequence = self._state["sequence"]
            self._state["sequence"] += 1
            intent = OrderIntent(exchange=getattr(broker, "exchange_id", "backtest"),
                account=self._state["account"], symbol=symbol, timeframe="1d",
                bar_time=pd.Timestamp(event.timestamp).isoformat(), strategy_id=self.strategy.name,
                action=side, sequence=sequence, requested_qty=quantity,
                reference_price=price, price=price, reduce_only=side == "sell",
                initial_stop=price-distances[symbol] if side == "buy" else None,
                approved_risk_amount=quantity*distances[symbol] if side == "buy" else None,
                signal_id=self._state["target_id"], causation_id=self._state["target_id"],
                exit_reason="v3_forced_exit" if proposal["forced"] else "v3_rebalance")
            record = {"symbol": symbol, "side": side, "qty": quantity, "filled_qty": 0.,
                      "remaining_qty": quantity, "status": "created", "target_id": self._state["target_id"],
                      "turnover_budget": quantity*price,
                      "approved_risk_amount": intent.approved_risk_amount or 0.,
                      "intent": asdict(intent)}
            self._state["orders"][intent.client_order_id] = record
            self._save()  # persist deterministic identity before sending
            order = broker.submit_intent(intent)
            self._orders[intent.client_order_id] = order
            record["status"] = _status(order)
            symbol_audit[symbol].update(order_id=intent.client_order_id, order_status=_status(order),
                                       submitted_quantity=quantity,
                                       approved_risk_amount=intent.approved_risk_amount)
            if order.accepted:
                submitted.append(order)
                if side == "buy":
                    context = self.strategy.get_context(symbol)
                    if not portfolio.get_position(symbol)["qty"]:
                        context.update(entry_pending=True, entry_price=price,
                                       entry_bar=event.positions.get(symbol, -1))
                    context["stop_loss"] = max(float(context.get("stop_loss", 0.) or 0.), intent.initial_stop)
                    context["effective_stop"] = max(float(context.get("effective_stop", 0.) or 0.), intent.initial_stop)
                    if decision is not None:
                        governor.commit(replace(decision, allowed_risk=intent.approved_risk_amount), symbol=symbol)
                        self._state["session_risk"] = governor._session_risk
            else:
                symbol_audit[symbol]["reasons"].append("broker_rejected_submission")
            self._save()
        self._state["last_processed"] = as_of.isoformat()
        self._save()
        self.audit.append({"as_of": as_of.isoformat(), "target_id": self._state["target_id"],
                           "orders": [order.id for order in submitted], "turnover_committed": turnover,
                           "forced_exit_turnover_notional": forced_turnover,
                           "suppressed": list(self._state["suppressed"]),
                           "buy_scale": plan["buy_scale"], "turnover_scale": plan["turnover_scale"],
                           "cash_only_scale": plan["cash_only_scale"], "buy_budget_caps": plan["buy_budget_caps"],
                           "equity": equity, "actual_weights": existing,
                           "symbol_decisions": symbol_audit,
                           "health_state": str(getattr(getattr(getattr(self.strategy, "health", None), "status", None), "value", "unmodelled")),
                           "health_multiplier": health, "account_multiplier": account_multiplier,
                           "borrow_limit": borrow_limit, "net_quote_cash": net_quote_cash,
                           "cash_asset": plan["cash_asset"], "borrow_liability": plan["borrow_liability"],
                           "target_gross_exposure": plan["target_gross_exposure"],
                           "proposed_financing_requirement": plan["proposed_financing_requirement"],
                           "quote_debt": max(0., -net_quote_cash), "remaining_stop_risk": risk_remaining,
                           "gross_headroom": max(0., equity*gross_cap-gross),
                           "position_count": len(held), "gross_weight": portfolio.get_total_exposure(dict(current_prices))/equity,
                           "borrow_status": (self.quote_borrow_policy.resolve(account=self._state["account"], as_of=as_of).status
                                             if self.quote_borrow_policy is not None else "cash_only"),
                           "weekly_snapshot": snapshot if as_of.weekday() == 0 else None})
        return submitted
