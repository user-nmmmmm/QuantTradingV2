"""Shared mark-to-stop budget. No trading, prices or time are invented here."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any

from core.entry_audit import note
from core.entry_risk import resolve_approved_risk


@dataclass(frozen=True)
class DrawdownBudgetPolicy:
    enabled: bool = False
    headroom_fraction: float = 0.5

    def __post_init__(self):
        if not math.isfinite(self.headroom_fraction) or not 0 < self.headroom_fraction <= 1:
            raise ValueError("drawdown budget headroom_fraction must be in (0, 1]")


@dataclass
class DrawdownBudgetSnapshot:
    equity: float
    high_water: float
    liquidation_floor: float
    budget: float
    open_risk: float = 0.0
    pending_risk: float = 0.0
    exit_cost: float = 0.0
    risks_by_symbol: dict[str, float] = field(default_factory=dict)
    issues: list[str] = field(default_factory=list)

    @property
    def occupied(self):
        return self.open_risk + self.pending_risk

    @property
    def available(self):
        return max(self.budget - self.occupied, 0.0) if not self.issues else 0.0

    @property
    def over_budget(self):
        return self.occupied > self.budget + max(1e-8, self.budget * 1e-10)

    def to_dict(self):
        return {**self.__dict__, "occupied": self.occupied, "available": self.available,
                "over_budget": self.over_budget, "verifiable": not self.issues}


class DrawdownBudget:
    def __init__(self, manager, policy=None, execution_costs=None):
        self.manager = manager
        self.policy = DrawdownBudgetPolicy(**(policy or {}))
        self.costs = dict(execution_costs or {})
        self.broker = None
        self.strategies = {}
        self.prices = {}
        self.bars = {}
        self.timestamp = None
        self.live = False
        self.audit: list[dict[str, Any]] = []

    def bind(self, execution, strategies):
        self.broker = getattr(execution, "broker", execution)
        self.strategies = strategies
        self.live = getattr(self.broker, "order_store", None) is not None
        if self.policy.enabled or self.manager.scale_minimum_with_risk:
            self.broker.opening_risk_guard = self

    def update(self, prices, bars, timestamp):
        self.prices = dict(prices)
        self.bars = dict(bars)
        self.timestamp = timestamp

    def cost(self, symbol, qty, price, *, legs=1):
        """Signal-time estimate using the same components as the fill model.

        No future fill bar is read. Unknown depth is covered by the headroom
        reserve, not claimed to be an exact execution-cost bound.
        """
        bar = self.bars.get(symbol, {})
        spread = float(bar.get("spread_bps", self.costs.get("spread_bps", 0)) or 0)
        vol = bar.get("volatility")
        if vol is None or not math.isfinite(float(vol)):
            vol = max(float(bar.get("high", price)) - float(bar.get("low", price)), 0) / price
        rate = (float(self.costs.get("commission_rate_taker", 0))
                + float(self.costs.get("slippage_bps", 0)) / 10000
                + max(spread, 0) / 20000
                + float(self.costs.get("volatility_slippage_factor", 0)) * max(float(vol), 0))
        volume = float(bar.get("volume", 0) or 0)
        if self.costs.get("use_impact_cost", False):
            participation = qty / volume if volume > 0 else float(self.costs.get("max_participation_rate", .05))
            rate += float(self.costs.get("impact_coefficient", 0)) * participation ** float(self.costs.get("impact_exponent", 1.5))
        return qty * price * rate * legs

    def snapshot(self, *, exclude_intent_id=None):
        if self.broker is None:
            raise ValueError('drawdown budget is not bound to an execution venue')
        portfolio = self.broker.portfolio
        equity = float(portfolio.get_equity(self.prices))
        high = float(self.manager.high_water_equity or equity)
        floor = high * (1 - self.manager.portfolio_drawdown_liquidate)
        snap = DrawdownBudgetSnapshot(equity, high, floor, self.policy.headroom_fraction * max(equity-floor, 0))
        if not all(math.isfinite(v) and v >= 0 for v in (equity, high, floor)):
            snap.issues.append("invalid_account_valuation")
        for symbol, pos in portfolio.positions.items():
            qty = abs(float(pos['qty']))
            if qty <= 1e-12:
                continue
            price = self.prices.get(symbol)
            if price is None or not math.isfinite(price) or price <= 0 or symbol not in self.bars:
                snap.issues.append(f"missing_current_mark:{symbol}")
                continue
            book = portfolio.lot_books.get(symbol)
            lots = list(book.open_lots) if book else []
            if abs(sum(lot.qty_open for lot in lots) - qty) > max(1e-8, qty*1e-8):
                snap.issues.append(f"unverifiable_lots:{symbol}")
                continue
            risk = 0.0
            for lot in lots:
                stop = lot.stop_price
                if stop is None or not math.isfinite(stop) or stop <= 0:
                    snap.issues.append(f"missing_original_stop:{symbol}:{lot.lot_id}")
                    continue
                distance = max(price-stop, 0) if lot.side == "long" else max(stop-price, 0)
                risk += lot.qty_open * distance
            cost = self.cost(symbol, qty, price)
            snap.exit_cost += cost
            snap.risks_by_symbol[symbol] = risk + cost
            snap.open_risk += risk + cost
        try:
            pending = self.broker.reservation_projection.pending_stop_risk(
                lambda symbol, qty, price: self.cost(symbol, qty, price, legs=2),
                exclude_intent_id=exclude_intent_id)
            snap.pending_risk = sum(pending.values())
            for symbol in pending:
                if symbol not in self.bars:
                    snap.issues.append(f"missing_pending_mark:{symbol}")
        except (AttributeError, ValueError) as exc:
            snap.issues.append(f"unverifiable_pending_risk:{exc}")
        return snap

    def clamp(self, symbol, qty, price, stop):
        if not self.policy.enabled:
            return qty
        snap = self.snapshot()
        if snap.issues or snap.available <= 0 or not stop or not math.isfinite(stop):
            note("drawdown_budget", drawdown_budget=snap.to_dict())
            return 0.0
        def needed(amount):
            return amount * abs(price-stop) + self.cost(symbol, amount, price, legs=2)
        if needed(qty) <= snap.available:
            return qty
        lo, hi = 0.0, qty
        for _ in range(48):
            mid = (lo+hi)/2
            if needed(mid) <= snap.available:
                lo = mid
            else:
                hi = mid
        note(drawdown_budget=snap.to_dict(), drawdown_clamped_qty=lo)
        return lo * (1-self.manager.CAP_SAFETY_MARGIN)

    def check_intent(self, intent):
        if intent.action not in {"buy", "short"} or intent.reduce_only:
            return None
        if not math.isfinite(intent.requested_qty) or intent.requested_qty <= 0:
            return "invalid_order_size"
        if self.broker is None:
            return "unbound_execution_venue"
        if self.manager._blocks_new_risk():
            return "portfolio_block"
        strategy = self.strategies.get(intent.strategy_id)
        health = strategy.health_risk_multiplier() if strategy is not None else 1.0
        if health <= 0:
            return "strategy_health_block"
        price = float(intent.reference_price or intent.price or 0)
        if price <= 0 or not math.isfinite(price):
            return "missing_sizing_reference"
        equity = self.broker.portfolio.get_equity(self.prices)
        minimum = self.manager.minimum_entry_notional(equity, health, live=self.live)
        if intent.requested_qty * price + 1e-9 < minimum:
            return "below_minimum_notional"
        if not self.policy.enabled:
            return None
        # An already approved, not-yet-submitted command competes with other
        # reservations, never with its own reservation a second time.
        snap = self.snapshot(exclude_intent_id=intent.intent_id)
        if intent.symbol not in self.bars:
            return "missing_current_mark"
        try:
            amount, _ = resolve_approved_risk(intent.__dict__)
        except ValueError:
            return "missing_approved_risk"
        stop = intent.initial_stop
        if stop is None or not math.isfinite(stop) or stop <= 0:
            return "missing_original_stop"
        needed = max(amount, intent.requested_qty * abs(price-stop)) + self.cost(intent.symbol, intent.requested_qty, price, legs=2)
        reason = None if not snap.issues and needed <= snap.available + 1e-8 else "drawdown_budget"
        self.audit.append({"timestamp": str(self.timestamp), "event": "entry_approval",
                           "symbol": intent.symbol, "intent_id": intent.intent_id,
                           "requested_risk_with_cost": needed, "approved": reason is None, **snap.to_dict()})
        return reason

    def reduction_fraction(self, snap):
        # Selling costs reduce equity and hence the budget. Reserve that
        # feedback before computing the common fraction left in each holding.
        fraction = self.policy.headroom_fraction
        numerator = snap.budget - fraction * snap.exit_cost
        denominator = snap.open_risk - fraction * snap.exit_cost
        return max(0.0, min(numerator / denominator, 1.0)) if denominator > 0 else 0.0
