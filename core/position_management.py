"""Causal, cash-funded planning for a batch of independent entry signals.

This module is a planner, not another position or order ledger. Portfolio and
the broker's reservation projection remain authoritative. Approved quantities
are upper bounds; existing execution-time risk checks may still reduce them.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from math import isfinite
from numbers import Real
from typing import Any, Mapping

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class CapitalAllocationPolicy:
    enabled: bool = False
    max_positions: int = 8
    max_gross_exposure: float = .9
    max_position_pct: float = .2
    cash_reserve_pct: float = .1
    volatility_lookback: int = 60
    min_history: int = 20
    volatility_floor: float = .005
    correlation_penalty: float = 1.
    score_cap: float = 5.
    cost_buffer_bps: float = 50.

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ValueError("enabled must be boolean")
        for name in ("max_positions", "volatility_lookback", "min_history"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not 2 <= self.min_history <= self.volatility_lookback:
            raise ValueError("min_history must be between 2 and volatility_lookback")
        for name in ("max_gross_exposure", "max_position_pct"):
            value = getattr(self, name)
            if not isinstance(value, Real) or not _finite(value) or not 0 < float(value) <= 1:
                raise ValueError(f"{name} must be in (0, 1]; allocation is cash-funded")
        if self.max_position_pct > self.max_gross_exposure:
            raise ValueError("max_position_pct cannot exceed max_gross_exposure")
        if (not isinstance(self.cash_reserve_pct, Real) or not _finite(self.cash_reserve_pct)
                or not 0 <= self.cash_reserve_pct < 1):
            raise ValueError("cash_reserve_pct must be in [0, 1)")
        for name in ("volatility_floor", "score_cap"):
            if (not isinstance(getattr(self, name), Real) or not _finite(getattr(self, name))
                    or getattr(self, name) <= 0):
                raise ValueError(f"{name} must be positive and finite")
        for name in ("correlation_penalty", "cost_buffer_bps"):
            if (not isinstance(getattr(self, name), Real) or not _finite(getattr(self, name))
                    or getattr(self, name) < 0):
                raise ValueError(f"{name} must be non-negative and finite")
        if self.cost_buffer_bps > 10000:
            raise ValueError("cost_buffer_bps cannot exceed 10000")

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any] | None) -> CapitalAllocationPolicy:
        if mapping is not None and not isinstance(mapping, Mapping):
            raise ValueError("capital allocation policy must be a mapping")
        values = dict(mapping or {})
        unknown = set(values) - cls.__dataclass_fields__.keys()
        if unknown:
            raise ValueError(f"unknown capital allocation settings: {sorted(unknown)}")
        return cls(**values)


def _finite(value: Any) -> bool:
    try:
        return not isinstance(value, bool) and isfinite(float(value))
    except (ValueError, TypeError, OverflowError):
        return False


def _nonnegative_map(values: Any) -> dict[str, float]:
    if not isinstance(values, Mapping):
        raise ValueError("unverifiable reservation projection")
    if any(not _finite(v) or float(v) < 0 for v in values.values()):
        raise ValueError("invalid reservation amount")
    return {str(k): float(v) for k, v in values.items() if float(v) > 0}


def _fill(weights: np.ndarray, caps: np.ndarray,
          constraints: list[tuple[str, float, np.ndarray]]) -> np.ndarray:
    """Weighted progressive filling under intersecting shared linear budgets."""
    allocated = np.zeros(len(caps))
    active = set(range(len(caps)))
    while active:
        direction = np.array([weights[i] if i in active else 0. for i in range(len(caps))])
        steps = [(max(caps[i] - allocated[i], 0.) / weights[i], {i}) for i in active]
        for _, budget, coefficients in constraints:
            velocity = float(coefficients @ direction)
            if velocity > 0:
                headroom = max(budget - float(coefficients @ allocated), 0.)
                steps.append((headroom / velocity, {i for i in active if coefficients[i] > 0}))
        step = min(value for value, _ in steps)
        allocated += step * direction
        frozen = set().union(*(indices for value, indices in steps
                               if value <= step + max(1e-12, abs(step) * 1e-12)))
        active -= frozen
    return np.minimum(allocated, caps)


class SmartCapitalPlanner:
    """Plan one batch with bounded scores, inverse volatility and correlation.

    Only long entries are supported by this cash-funded policy. Short and
    borrowed buying power are intentionally excluded from the budget. Signals
    with insufficient causal history are rejected, rather than being assigned
    a misleading zero volatility. No orders or reservations are created here.
    """

    def __init__(self, policy: CapitalAllocationPolicy | None = None):
        self.policy = policy or CapitalAllocationPolicy()

    def plan(self, candidates, *, portfolio, broker, risk_manager,
             current_prices: Mapping[str, float], risk_governor=None
             ) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        items = list(candidates)
        decisions = {c.symbol: {"symbol": c.symbol, "approved_qty": 0.,
                     "approved_notional": 0., "approved_risk": 0., "weight": 0.,
                     "requested_qty": 0., "reason": "not_planned"} for c in items}
        summary: dict[str, Any] = {"enabled": self.policy.enabled,
                                  "candidate_count": len(items), "approved_count": 0,
                                  "budget_basis": "cash_funded_long_only",
                                  "approved_notional": 0., "approved_risk": 0.}

        def reject_all(reason):
            for decision in decisions.values():
                decision["reason"] = reason
            summary["reason"] = reason
            return decisions, summary

        if not self.policy.enabled:
            return reject_all("disabled")
        if not items:
            summary["reason"] = "empty_batch"
            return decisions, summary
        try:
            sessions = {str(c.frame.index[c.bar_index]) for c in items}
            if len(sessions) != 1:
                return reject_all("mixed_batch_timestamps")
        except (AttributeError, IndexError, TypeError, ValueError):
            return reject_all("invalid_batch_timestamp")
        try:
            held = {}
            for symbol, position in portfolio.positions.items():
                qty = float(position.get("qty", 0.))
                if not isfinite(qty):
                    raise ValueError("invalid held quantity")
                if qty:
                    price = current_prices.get(symbol)
                    if price is None or not _finite(price) or float(price) <= 0:
                        raise ValueError("missing current held mark")
                    held[symbol] = abs(qty) * float(price)
            equity = float(portfolio.get_equity(dict(current_prices)))
            cash = float(portfolio.cash)
            if not isfinite(equity) or equity <= 0 or not isfinite(cash):
                raise ValueError("invalid account valuation")
        except (ValueError, TypeError, AttributeError, OverflowError):
            return reject_all("invalid_account_valuation")

        budget_model = getattr(risk_manager, "drawdown_budget", None)
        try:
            cost_fn, known_cost_rate = self._execution_cost(broker, budget_model)
        except (ValueError, TypeError, ArithmeticError):
            return reject_all("invalid_execution_costs")
        summary["known_execution_cost_floor_rate"] = known_cost_rate
        cost_buffer = self.policy.cost_buffer_bps / 10000.
        try:
            projection = getattr(broker, "reservation_projection", None)
            if projection is None:
                raise ValueError("reservation projection unavailable")
            pending = _nonnegative_map(projection.pending_notional(dict(current_prices)))
            pending_risk = _nonnegative_map(projection.pending_stop_risk())
            pending_cash = float(projection.pending_cash(dict(current_prices), cost=cost_fn))
            if not _finite(pending_cash) or pending_cash < 0:
                raise ValueError("invalid reserved cash")
            if set(pending) != set(pending_risk):
                raise ValueError("incomplete reserved risk")
            if not all(_finite(sum(values.values())) for values in (pending, pending_risk)):
                raise ValueError("invalid total reservation amount")
            # Shorts also occupy capital under the conservative cash-funded
            # envelope; an unfilled sell never increases cash availability.
            pending_cash = max(pending_cash, sum(pending.values()) * (1 + cost_buffer))
        except (ValueError, TypeError, AttributeError, ArithmeticError):
            return reject_all("unverifiable_pending_reservations")

        occupied = set(held) | set(pending)
        gross = sum(held.values())
        reserved = sum(pending.values())
        cash_budget = max(min(cash, equity - gross) - pending_cash
                          - equity * self.policy.cash_reserve_pct, 0.)
        gross_budget = max(equity * self.policy.max_gross_exposure - gross - reserved, 0.)
        summary.update(equity=equity, held_notional=gross, pending_notional=reserved,
                       pending_cash=pending_cash, cash_reserve=equity*self.policy.cash_reserve_pct,
                       cash_budget=cash_budget, gross_budget=gross_budget,
                       occupied_positions=len(occupied))
        duplicates = {s for s, count in Counter(c.symbol for c in items).items() if count > 1}
        prepared = []
        for item in sorted(items, key=lambda c: c.symbol):
            decision = decisions[item.symbol]
            if item.symbol in duplicates:
                decision["reason"] = "duplicate_symbol"
                continue
            if item.symbol in pending:
                decision["reason"] = "entry_already_pending"
                continue
            try:
                prepared.append(self._prepare(item, decision, equity, held, pending,
                                              portfolio, risk_manager, current_prices, projection))
            except (ValueError, TypeError, AttributeError, KeyError, IndexError,
                    ArithmeticError) as exc:
                decision["reason"] = str(exc) if isinstance(exc, ValueError) else "invalid_candidate"
        if not prepared:
            summary["reason"] = "no_eligible_candidates"
            return decisions, summary

        try:
            self._weights(prepared, held, equity, risk_governor)
        except (ValueError, TypeError, ArithmeticError):
            return reject_all("invalid_allocation_weights")
        explicit_ranking = [p["selection_rank_score"] is not None for p in prepared]
        if any(explicit_ranking) and not all(explicit_ranking):
            return reject_all("mixed_selection_ranking_contract")
        use_selection_ranking = all(explicit_ranking)

        def priority(item):
            return item["selection_rank_score"] if use_selection_ranking else item["raw_weight"]

        remaining_slots = max(self.policy.max_positions - len(occupied), 0)
        ranked_new = sorted((p for p in prepared if p["symbol"] not in occupied),
                            key=lambda p: (-priority(p), p["symbol"]))
        selected_new = {p["symbol"] for p in ranked_new[:remaining_slots]}
        selected = []
        for item in prepared:
            if item["symbol"] in occupied or item["symbol"] in selected_new:
                selected.append(item)
            else:
                decisions[item["symbol"]]["reason"] = "position_slots_exhausted"
        if not selected:
            summary["reason"] = "position_slots_exhausted"
            return decisions, summary
        try:
            constraints = self._constraints(selected, equity, held, pending, pending_risk,
                                            cash_budget, gross_budget, portfolio,
                                            risk_manager, risk_governor, cost_fn)
        except (ValueError, TypeError, AttributeError, ArithmeticError):
            return reject_all("unverifiable_shared_risk_budget")

        # Reallocate dust to the remaining eligible signals. Removing one weak
        # signal at a time allows a feasible minimum order instead of dropping
        # a whole batch whose first proportional shares are individually tiny.
        live = list(range(len(selected)))
        allocation = np.zeros(len(selected))
        while live:
            weights = np.array([selected[i]["raw_weight"] for i in live])
            caps = np.array([selected[i]["cap"] for i in live])
            subset = [(name, budget, coefficients[live]) for name, budget, coefficients in constraints]
            result = _fill(weights, caps, subset)
            dust = [j for j, i in enumerate(live)
                    if result[j] + 1e-8 < selected[i]["minimum"] or result[j] <= 1e-10]
            if not dust:
                allocation[live] = result
                break
            remove = min(dust, key=lambda j: (priority(selected[live[j]]),
                                              selected[live[j]]["symbol"]))
            decisions[selected[live[remove]]["symbol"]]["reason"] = "below_minimum_allocation"
            del live[remove]
        total_weight = sum(selected[i]["raw_weight"] for i in live)
        for i, item in enumerate(selected):
            notional = float(allocation[i])
            decision = decisions[item["symbol"]]
            if notional <= 0:
                continue
            decision.update(approved_qty=notional/item["price"], approved_notional=notional,
                            approved_risk=notional*item["risk_rate"],
                            weight=item["raw_weight"]/total_weight, target_weight=notional/equity,
                            reason="allocated", correlation_penalty=item["correlation_factor"])
        summary.update(approved_count=sum(d["approved_qty"] > 0 for d in decisions.values()),
                       approved_notional=sum(d["approved_notional"] for d in decisions.values()),
                       approved_risk=sum(d["approved_risk"] for d in decisions.values()),
                       constraints={name: {"budget": budget, "used": float(coefficients @ allocation)}
                                    for name, budget, coefficients in constraints},
                       reason="planned")
        return decisions, summary

    @staticmethod
    def _execution_cost(broker, budget_model):
        """Keep known venue fees and base friction even without a risk model.

        This is a planning estimate at the supplied reference price. It does
        not bound future gaps, changing depth, or unobserved live fee tiers.
        Commission is charged on the slipped purchase price, as in Broker.
        """
        known = {}
        for name in ("commission_rate", "commission_rate_maker", "slippage", "spread_bps"):
            value = getattr(broker, name, None)
            if value is None:
                known[name] = 0.
            elif not _finite(value) or (name in {"slippage", "spread_bps"} and float(value) < 0):
                raise ValueError("invalid known execution cost")
            else:
                known[name] = max(float(value), 0.)
        commission = max(known["commission_rate"], known["commission_rate_maker"])
        friction = known["slippage"] + known["spread_bps"] / 20000.
        minimum_rate = friction + commission * (1 + friction)
        if not isfinite(minimum_rate):
            raise ValueError("invalid known execution cost")
        model = getattr(budget_model, "cost", None)

        def estimate(symbol, qty, price):
            value = float(model(symbol, qty, price)) if callable(model) else 0.
            if not isfinite(value) or value < 0:
                raise ValueError("invalid execution cost estimate")
            result = max(value, qty * price * minimum_rate)
            if not isfinite(result):
                raise ValueError("invalid execution cost estimate")
            return result

        return estimate, minimum_rate

    def _prepare(self, candidate, decision, equity, held, pending,
                 portfolio, risk_manager, prices, projection):
        signal = candidate.signal
        if signal.get("action") != "buy":
            raise ValueError("unsupported_action")
        index = candidate.bar_index
        if isinstance(index, bool) or not isinstance(index, (int, np.integer)) or index < 0:
            raise ValueError("invalid_bar_index")
        frame = candidate.frame
        current = float(frame["close"].iat[index])
        mark = prices.get(candidate.symbol)
        order_price = float(signal.get("price", current))
        price = max(current, order_price)
        stop = float(signal.get("stop_loss", 0.))
        if not all(_finite(v) and float(v) > 0 for v in (current, order_price, price, mark, stop)):
            raise ValueError("invalid_price_or_stop")
        if stop >= min(current, price) or not _finite(candidate.score):
            raise ValueError("invalid_stop_or_score")
        rank_score = getattr(candidate, "selection_rank_score", None)
        if rank_score is not None and not _finite(rank_score):
            raise ValueError("invalid_selection_rank_score")
        closes = pd.to_numeric(frame["close"].iloc[max(0, index-self.policy.volatility_lookback):index+1],
                               errors="coerce")
        if len(closes) < self.policy.min_history + 1:
            raise ValueError("insufficient_history")
        if not closes.index.is_unique or not closes.index.is_monotonic_increasing:
            raise ValueError("invalid_history_order")
        if not np.isfinite(closes.to_numpy()).all() or (closes <= 0).any():
            raise ValueError("invalid_history")
        returns = np.log(closes).diff().dropna()
        volatility = max(float(returns.std(ddof=1)), self.policy.volatility_floor)
        health = float(candidate.strategy.health_risk_multiplier())
        regime = float(candidate.strategy.entry_risk_multiplier(candidate.state))
        if not all(_finite(v) and v >= 0 for v in (health, regime, volatility)):
            raise ValueError("invalid_sizing_multiplier")
        qty = float(candidate.strategy.initial_entry_quantity(
            signal=signal, equity=equity, current_price=current, stop_loss=stop,
            risk_manager=risk_manager)) * health * regime
        if not _finite(qty) or qty <= 0:
            raise ValueError("strategy_risk_blocked")
        max_entry = risk_manager.max_entry_notional(
            portfolio, candidate.symbol, price, current_prices=dict(prices),
            pending_open_notional=pending, reservation_projection=projection, action="buy")
        cap = min(qty*price, equity*self.policy.max_position_pct
                  - held.get(candidate.symbol, 0.) - pending.get(candidate.symbol, 0.), float(max_entry))
        minimum = float(risk_manager.minimum_entry_notional(
            equity, health, live=bool(getattr(getattr(risk_manager, "drawdown_budget", None), "live", False))))
        if not all(_finite(v) for v in (cap, minimum)) or minimum < 0:
            raise ValueError("invalid_notional_budget")
        decision.update(requested_qty=qty, requested_notional=qty*price,
                        maximum_notional=max(cap, 0.), minimum_notional=minimum,
                        volatility=volatility, history_observations=len(returns),
                        history_end=str(frame.index[index]), reference_price=price)
        if cap <= 0 or cap + 1e-8 < minimum:
            raise ValueError("below_minimum_capacity")
        if rank_score is not None:
            decision.update(selection_rank_score=float(rank_score),
                            ranking_source="selector_score", capital_score=float(candidate.score))
        return {"symbol": candidate.symbol, "price": price, "risk_rate": abs(price-stop)/price,
                "score": float(candidate.score), "volatility": volatility, "returns": returns,
                "selection_rank_score": float(rank_score) if rank_score is not None else None,
                "cap": cap, "minimum": minimum}

    def _weights(self, prepared, held, equity, governor):
        policy = getattr(governor, "policy", None)
        for item in prepared:
            correlations = []
            for other in prepared:
                if other is item:
                    continue
                paired = pd.concat([item["returns"], other["returns"]], axis=1, join="inner")
                value = (float(paired.iloc[:, 0].corr(paired.iloc[:, 1]))
                         if len(paired) >= self.policy.min_history
                         and (paired.std() > 1e-12).all() else 1.)
                correlations.append(max(value, 0.) if isfinite(value) else 1.)
            cluster = policy.cluster_for(item["symbol"]) if policy else "crypto_beta"
            existing = sum(v for s, v in held.items()
                           if not policy or policy.cluster_for(s) == cluster) / equity
            penalty = 1 + self.policy.correlation_penalty * (sum(correlations)/max(len(correlations), 1)+existing)
            item["correlation_factor"] = penalty
            item["raw_weight"] = (1 + min(max(item["score"], 0.), self.policy.score_cap)) / item["volatility"] / penalty
            if not isfinite(item["raw_weight"]) or item["raw_weight"] <= 0:
                raise ValueError("invalid allocation weight")

    def _constraints(self, selected, equity, held, pending, pending_risk,
                     cash_budget, gross_budget, portfolio, manager, governor, cost_fn):
        size = len(selected)
        risk = np.array([p["risk_rate"] for p in selected])
        rates = []
        for item in selected:
            cost = (float(cost_fn(item["symbol"], item["cap"]/item["price"], item["price"]))
                    if callable(cost_fn) else 0.)
            if not isfinite(cost) or cost < 0:
                raise ValueError("invalid cost estimate")
            # Current cost model's participation impact is nondecreasing in
            # size, so the estimate at the cap safely bounds smaller orders.
            rates.append(max(self.policy.cost_buffer_bps/10000., cost/item["cap"]))
        constraints = [("cash", cash_budget, 1+np.array(rates)),
                       ("gross", gross_budget, np.ones(size))]
        leverage = float(manager.max_leverage)
        if not _finite(leverage) or leverage <= 0:
            raise ValueError("invalid account leverage")
        constraints.append(("account_gross", max(equity*leverage
                            - sum(held.values())-sum(pending.values()), 0.), np.ones(size)))
        cluster_policy = getattr(governor, "policy", None)
        if cluster_policy is not None and cluster_policy.enabled:
            policy = cluster_policy
            occupied = {s: held.get(s, 0.)+pending.get(s, 0.) for s in set(held)|set(pending)}
            clusters = {policy.cluster_for(p["symbol"]) for p in selected}
            if policy.max_crypto_beta_exposure is not None:
                constraints.append(("crypto_beta_exposure", max(equity*policy.max_crypto_beta_exposure
                                    - sum(occupied.values()), 0.), np.ones(size)))
            for cluster in sorted(clusters):
                mask = np.array([float(policy.cluster_for(p["symbol"]) == cluster) for p in selected])
                if policy.max_cluster_exposure_pct is not None:
                    used = sum(v for s, v in occupied.items() if policy.cluster_for(s) == cluster)
                    constraints.append((f"cluster_exposure:{cluster}",
                                        max(equity*policy.max_cluster_exposure_pct-used, 0.), mask))
            if policy.has_risk_caps:
                open_risk = self._open_risk(portfolio, held)
                if policy.max_same_session_entry_risk is not None:
                    used = float(governor.session_risk_used)
                    if not _finite(used) or used < 0:
                        raise ValueError("invalid session risk")
                    constraints.append(("session_risk", max(equity*policy.max_same_session_entry_risk
                                        - max(used, sum(pending_risk.values())), 0.), risk))
                if policy.max_crypto_beta_stop_risk is not None:
                    constraints.append(("crypto_beta_stop_risk", max(equity*policy.max_crypto_beta_stop_risk
                                        - sum(open_risk.values())-sum(pending_risk.values()), 0.), risk))
                if policy.max_correlated_stop_risk is not None:
                    for cluster in sorted(clusters):
                        mask = np.array([float(policy.cluster_for(p["symbol"]) == cluster) for p in selected])
                        used = sum(v for s, v in open_risk.items() if policy.cluster_for(s) == cluster)
                        used += sum(v for s, v in pending_risk.items() if policy.cluster_for(s) == cluster)
                        constraints.append((f"cluster_stop_risk:{cluster}",
                                            max(equity*policy.max_correlated_stop_risk-used, 0.), mask*risk))
        drawdown = getattr(manager, "drawdown_budget", None)
        if drawdown is not None and drawdown.policy.enabled:
            snapshot = drawdown.snapshot()
            if snapshot.issues or not _finite(snapshot.available):
                raise ValueError("unverifiable drawdown budget")
            constraints.append(("drawdown_risk_with_cost", max(snapshot.available, 0.), risk+2*np.array(rates)))
        return constraints

    @staticmethod
    def _open_risk(portfolio, held):
        result: dict[str, float] = {}
        for symbol in held:
            book = portfolio.lot_books.get(symbol)
            lots = list(book.open_lots) if book else []
            qty = abs(float(portfolio.positions[symbol]["qty"]))
            if abs(sum(abs(float(lot.qty_open)) for lot in lots)-qty) > max(1e-8, qty*1e-8):
                raise ValueError("unverifiable open lots")
            for lot in lots:
                entry, stop = lot.entry_price, lot.stop_price
                if not all(_finite(v) and float(v) > 0 for v in (entry, stop, lot.qty_original)):
                    raise ValueError("unverifiable open stop risk")
                approved = float(lot.initial_risk or 0.) * abs(float(lot.qty_open))/float(lot.qty_original)
                actual = abs(float(entry)-float(stop))*abs(float(lot.qty_open))
                if not _finite(approved) or approved < 0:
                    raise ValueError("invalid open risk")
                result[symbol] = result.get(symbol, 0.)+max(approved, actual)
        return result


PositionManager = SmartCapitalPlanner
