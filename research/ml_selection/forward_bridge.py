"""Frozen RL decisions from complete original-engine hook state.

This is a decision bridge, not an account restorer or a second execution
engine. Its output does not assert portfolio performance. Actual simulated
fills and equity must be appended separately from an original-engine replay.
"""
from dataclasses import dataclass, field
import hashlib
import math
from types import SimpleNamespace

import pandas as pd

from core.reproducibility import canonical_json
from research.ml_selection.selector import ResearchSelector, utc


def _identity(payload):
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def _finite(value, name, positive=False):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite")
    number = float(value)
    if not math.isfinite(number) or positive and number <= 0:
        raise ValueError(f"{name} must be finite" + (" and positive" if positive else ""))
    return number


@dataclass(frozen=True)
class BridgeCandidate:
    symbol: str
    strategy_name: str
    score: float
    signal: dict = field(default_factory=dict)
    audit: dict | None = None


def capture_hook_state(candidates, *, event, portfolio, broker, risk_manager,
                       current_prices, protocol_id, policy_id, parent_model_id):
    """Capture causal facts at the existing hook, after strategy collection."""
    candidates = list(candidates)
    venue = getattr(broker, "broker", broker)
    orders = {}
    for name in ("pending_orders", "active_orders"):
        book = getattr(venue, name, ()) or ()
        for order in book.values() if isinstance(book, dict) else book:
            identifier = getattr(order, "id", None)
            if not identifier:
                raise ValueError("state bridge requires stable order identities")
            orders[identifier] = {"id": identifier, "symbol": order.symbol,
                "side": order.side,
                "status": getattr(order.status, "value", order.status),
                "remaining_qty": order.remaining_qty, "filled_qty": order.filled_qty,
                "qty": order.qty, "timestamp": order.timestamp,
                "order_type": getattr(order.order_type, "value", order.order_type),
                "price": order.price, "strategy_id": order.strategy_id,
                "stop_loss": order.stop_loss, "take_profit": order.take_profit,
                "submitted_date": order.submitted_date, "expire_time": order.expire_time,
                "avg_fill_price": order.avg_fill_price,
                "match_not_before": order.match_not_before}
    budget = getattr(risk_manager, "drawdown_budget", None)
    if budget is None:
        raise ValueError("state bridge requires original risk budget state")
    budget_state = budget.snapshot().to_dict()
    health, rows = {}, []
    for candidate in candidates:
        name = candidate.strategy_name
        strategy = candidate.strategy
        health[name] = (strategy.health_snapshot() if hasattr(strategy, "health_snapshot") else {})
        if not health[name]:
            health[name] = {"status": "not_health_managed", "allows_new_entries": True}
        rows.append({"symbol": candidate.symbol, "strategy_name": name, "score": candidate.score,
                     "signal": dict(candidate.signal), "audit": dict(candidate.audit or {})})
    equity = portfolio.get_total_value(dict(current_prices))
    payload = {"schema": "ml-rl-hook-state/v1", "source": "original_engine_candidate_hook",
        "protocol_id": protocol_id, "policy_id": policy_id, "parent_model_id": parent_model_id,
        "bar_time": utc(event.timestamp), "as_of": utc(event.timestamp) + pd.Timedelta(days=1),
        "available_at": utc(event.timestamp) + pd.Timedelta(days=1), "timeframe": event.timeframe,
        "candidates_collected_after_strategy_health": True,
        "portfolio": {"cash": portfolio.cash, "initial_capital": portfolio.initial_capital,
                      "equity": equity, "positions": portfolio.positions},
        "prices": dict(current_prices), "orders": list(orders.values()),
        "risk": {"high_water_equity": risk_manager.high_water_equity,
                 "last_drawdown": risk_manager.last_drawdown,
                 "breaker_action": getattr(risk_manager.breaker_action, "value", risk_manager.breaker_action),
                 "circuit_breaker_triggered": risk_manager.circuit_breaker_triggered,
                 "daily_loss_triggered": risk_manager.daily_loss_triggered,
                 "system_health_allows_new_risk": (getattr(risk_manager.health_assessment, "allows_new_risk", False)
                    if risk_manager.health_assessment is not None else True),
                 "risk_per_trade": risk_manager.risk_per_trade,
                 "budget": budget_state},
        "strategy_health": health, "candidates": rows}
    payload = __import__("json").loads(canonical_json(payload))
    payload["snapshot_id"] = _identity(payload)
    return payload


def validate_hook_state(snapshot, *, protocol_id, policy_id, parent_model_id, information_cutoff):
    if not isinstance(snapshot, dict):
        raise ValueError("complete RL state snapshot required")
    payload = dict(snapshot)
    identifier = payload.pop("snapshot_id", None)
    if identifier != _identity(payload):
        raise ValueError("RL state snapshot identity mismatch")
    required = {"portfolio", "prices", "orders", "risk", "strategy_health", "candidates", "available_at", "as_of", "bar_time"}
    if not required <= set(payload) or payload.get("schema") != "ml-rl-hook-state/v1":
        raise ValueError("incomplete RL account and strategy state")
    if payload.get("source") != "original_engine_candidate_hook" or payload.get("candidates_collected_after_strategy_health") is not True:
        raise ValueError("RL candidates require original strategy and health collection")
    for key, value in (("protocol_id", protocol_id), ("policy_id", policy_id), ("parent_model_id", parent_model_id)):
        if payload.get(key) != value:
            raise ValueError(f"RL bridge {key} mismatch")
    cutoff = utc(information_cutoff)
    as_of = utc(payload["as_of"])
    if payload.get("timeframe") != "1d" or utc(payload["bar_time"]) + pd.Timedelta(days=1) != as_of:
        raise ValueError("RL bridge requires a daily closed-bar state")
    if (utc(payload["available_at"]) < as_of or utc(payload["available_at"]) > cutoff
            or as_of > cutoff or as_of < cutoff.normalize()):
        raise ValueError("RL state is stale or unavailable at the information cutoff")
    account = payload["portfolio"]
    for key in ("cash", "equity", "initial_capital", "positions"):
        if key not in account:
            raise ValueError("incomplete RL portfolio state")
    equity = _finite(account["equity"], "equity", True)
    cash = _finite(account["cash"], "cash")
    _finite(account["initial_capital"], "initial capital", True)
    if not isinstance(account["positions"], dict) or not isinstance(payload["prices"], dict):
        raise ValueError("positions and causal prices must be mappings")
    mark = cash
    for symbol, position in account["positions"].items():
        qty = _finite(position["qty"], "position quantity")
        if qty < 0:
            raise ValueError("RL bridge only supports cash spot accounts")
        if qty:
            if symbol not in payload["prices"]:
                raise ValueError("held asset is missing a causal mark")
            mark += qty * _finite(payload["prices"][symbol], "held asset price", True)
    if not math.isclose(mark, equity, rel_tol=1e-10, abs_tol=1e-6):
        raise ValueError("RL state equity does not reconcile with cash and positions")
    risk = payload["risk"]
    if not isinstance(risk, dict) or not {"breaker_action", "circuit_breaker_triggered",
            "daily_loss_triggered", "system_health_allows_new_risk", "risk_per_trade"} <= set(risk):
        raise ValueError("incomplete RL risk capability state")
    if risk["breaker_action"] not in {"normal", "reduce", "block_new", "liquidate", "locked"}:
        raise ValueError("unknown RL risk breaker action")
    for flag in ("circuit_breaker_triggered", "daily_loss_triggered", "system_health_allows_new_risk"):
        if type(risk[flag]) is not bool:
            raise ValueError("risk capability flags must be explicit booleans")
    if not 0 < _finite(risk["risk_per_trade"], "risk per trade") <= 1:
        raise ValueError("invalid RL risk per trade")
    high = _finite(risk.get("high_water_equity"), "risk high-water", True)
    drawdown = _finite(risk.get("last_drawdown"), "risk drawdown")
    if not 0 <= drawdown <= 1 or high < equity - 1e-6:
        raise ValueError("invalid RL risk state")
    if not math.isclose(drawdown, max(0., 1 - equity / high), rel_tol=1e-8, abs_tol=1e-8):
        raise ValueError("RL risk drawdown disagrees with its high-water")
    budget = risk.get("budget")
    if not isinstance(budget, dict) or budget.get("verifiable") is not True:
        raise ValueError("RL risk budget is missing or unverifiable")
    for key in ("budget", "open_risk", "pending_risk", "available"):
        if _finite(budget.get(key), f"risk {key}") < 0:
            raise ValueError("risk budget values cannot be negative")
    if not math.isclose(budget["available"], max(0., budget["budget"] - budget["open_risk"] - budget["pending_risk"]),
                        rel_tol=1e-8, abs_tol=1e-6):
        raise ValueError("RL risk budget does not reconcile")
    if not isinstance(payload["orders"], list):
        raise ValueError("complete RL matching-book state required")
    ids = set()
    for order in payload["orders"]:
        if not order.get("id") or order["id"] in ids or not order.get("status"):
            raise ValueError("RL matching orders require distinct IDs and statuses")
        ids.add(order["id"])
        if _finite(order.get("remaining_qty"), "remaining quantity") < 0:
            raise ValueError("remaining order quantity cannot be negative")
        qty = _finite(order.get("qty"), "order quantity", True)
        filled = _finite(order.get("filled_qty"), "filled quantity")
        if (filled < 0 or filled + order["remaining_qty"] > qty + max(1e-9, qty * 1e-9)
                or not order.get("symbol") or order.get("side") not in {"buy", "sell"}
                or order.get("timestamp") is None or utc(order["timestamp"]) > as_of):
            raise ValueError("incomplete or inconsistent RL matching order state")
    keys = set()
    for candidate in payload["candidates"]:
        key = (candidate["symbol"], candidate["strategy_name"])
        if key in keys:
            raise ValueError("duplicate bridge candidate")
        keys.add(key)
        _finite(candidate["score"], "native candidate score")
        if not isinstance(candidate.get("signal"), dict) or candidate["signal"].get("action") != "buy":
            raise ValueError("RL bridge requires original spot entry signals")
        health = payload["strategy_health"].get(candidate["strategy_name"])
        if not isinstance(health, dict) or health.get("allows_new_entries") is not True:
            raise ValueError("candidate lacks permitting strategy health state")
    return payload


def bridge_decision(snapshot, dataset, model, policy, *, protocol_id, information_cutoff, selection_options=None):
    state = validate_hook_state(snapshot, protocol_id=protocol_id, policy_id=policy.model_id,
        parent_model_id=model.model_id, information_cutoff=information_cutoff)
    account = state["portfolio"]
    portfolio = SimpleNamespace(cash=account["cash"], initial_capital=account["initial_capital"],
        positions=account["positions"], get_total_value=lambda prices: account["equity"])
    broker = SimpleNamespace(pending_orders=state["orders"], active_orders=[])
    selector = ResearchSelector(dataset, mode="policy", model=model, policy=policy,
        deterministic=True, initial_capital=account["initial_capital"], **dict(selection_options or {}))
    candidates = [BridgeCandidate(row["symbol"], row["strategy_name"], row["score"],
                                 dict(row["signal"]), dict(row.get("audit") or {}))
                  for row in state["candidates"]]
    selected = selector.select(candidates,
        event=SimpleNamespace(timestamp=utc(state["bar_time"]), timeframe="1d"), portfolio=portfolio,
        broker=broker, risk_manager=SimpleNamespace(**state["risk"]), current_prices=state["prices"])
    risk = state["risk"]
    blocked = (risk["breaker_action"] in {"block_new", "liquidate", "locked"}
               or risk["circuit_breaker_triggered"] or risk["daily_loss_triggered"]
               or not risk["system_health_allows_new_risk"] or risk["budget"]["available"] <= 0)
    if blocked:
        selected = []
        for row in selector.audit:
            if row["selected"]:
                row["policy_selected_before_risk"] = True
                row["selected"], row["reason"] = False, "original_engine_risk_blocks_new_entries"
    return {"snapshot_id": snapshot["snapshot_id"], "policy_id": policy.model_id,
        "model_id": model.model_id, "decisions": selector.audit,
        "selected": [{"symbol": row.symbol, "strategy": row.strategy_name, "score": row.score} for row in selected],
        "risk_blocks_new_entries": blocked, "execution_approval_available": False,
        "decision_only": True, "simulated_account_performance_available": False, "real_orders": False}


class RecordingSelector:
    """Observe an original selector without changing its decisions or RNG."""
    def __init__(self, selector, protocol_id):
        self.selector, self.protocol_id = selector, protocol_id
        self.snapshots, self.replay_comparisons = [], []
        self.verification_errors = []

    def __getattr__(self, name):
        return getattr(self.selector, name)

    def select(self, candidates, **context):
        candidates = list(candidates)
        if not candidates:
            return self.selector.select(candidates, **context)
        state = None
        try:
            state = capture_hook_state(candidates, protocol_id=self.protocol_id,
                policy_id=self.selector.policy.model_id, parent_model_id=self.selector.model.model_id, **context)
            self.snapshots.append(state)
        except Exception as error:
            self.verification_errors.append({"stage": "capture", "bar_time": str(context["event"].timestamp),
                                             "error": f"{type(error).__name__}: {error}"})
        audit_start = len(self.selector.audit)
        selected = self.selector.select(candidates, **context)
        if self.selector.deterministic and state is not None:
            try:
                replay = bridge_decision(state, self.selector.table.reset_index(), self.selector.model, self.selector.policy,
                    protocol_id=self.protocol_id, information_cutoff=state["available_at"],
                    selection_options={"min_expected_return": self.selector.threshold,
                        "score_scale": self.selector.score_scale, "score_cap": self.selector.score_cap,
                        "policy_threshold": self.selector.policy_threshold})
                actual = [{"symbol": row.symbol, "strategy": row.strategy_name, "score": row.score} for row in selected]
                # Compare complete causal decisions, including probabilities, scores,
                # signals and account features rather than selected IDs alone.
                decision_equal = canonical_json(replay["decisions"]) == canonical_json(self.selector.audit[audit_start:])
                selected_equal = canonical_json(replay["selected"]) == canonical_json(actual)
                self.replay_comparisons.append({"snapshot_id": state["snapshot_id"],
                    "selected_identical": selected_equal, "decision_facts_identical": decision_equal,
                    "identical": selected_equal and decision_equal})
            except Exception as error:
                self.verification_errors.append({"stage": "replay", "snapshot_id": state["snapshot_id"],
                                                 "error": f"{type(error).__name__}: {error}"})
        return selected
