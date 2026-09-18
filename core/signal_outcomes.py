"""Causal fixed-horizon outcomes for every observed candidate.

These are unlevered, fixed-notional signal diagnostics. Partial fills, capital
competition and busy-blocking belong to the separate broker-backed ghost replay.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

import pandas as pd

from core.broker.cost_model import market_slippage_components
from core.signal_observation_types import (
    ObservationPolicy, SignalCandidateEvent, close_time, finite, iso,
)
from core.timeframes import as_utc_timestamp, timeframe_delta


@dataclass(frozen=True)
class ObservationCosts:
    commission_rate: float = 0.0
    slippage: float = 0.0
    spread_bps: float = 0.0
    volatility_slippage_factor: float = 0.0
    use_impact_cost: bool = False
    impact_coefficient: float = 0.10
    impact_exponent: float = 1.5
    max_participation_rate: float = 1.0
    account_mode: str = "spot"
    default_borrow_rate_annual: float = 0.0
    funding_interval_hours: float = 8.0
    funding_rate_required: bool = True
    borrow_availability_required: bool = False
    default_borrow_limit_qty: float = 1e12

    @classmethod
    def from_broker(cls, broker):
        kwargs = {name: getattr(broker, name) for name in cls.__dataclass_fields__
                  if name != "account_mode"}
        kwargs["account_mode"] = broker.portfolio.account_mode.value
        return cls(**kwargs)

    def quote(self, price: float, qty: float, side: str, bar: Any):
        parts = market_slippage_components(
            price=price, quantity=qty, volume=float(bar.get("volume", 0)), bar=bar,
            spread_bps=self.spread_bps, volatility_factor=self.volatility_slippage_factor,
            use_impact=self.use_impact_cost, impact_coefficient=self.impact_coefficient,
            impact_exponent=self.impact_exponent,
        )
        rate = self.slippage + parts["spread"] + parts["volatility"] + parts["impact"]
        fill = price * (1 + rate if side in {"buy", "cover"} else 1 - rate)
        return {"price": fill, "commission": abs(fill * qty) * self.commission_rate,
                "slippage": price * qty * (self.slippage + parts["spread"] + parts["volatility"]),
                "impact": price * qty * parts["impact"]}

    def estimate_bps(self, price: float, qty: float, side: str, bar: Any):
        entry = self.quote(price, qty, side, bar)
        exit_ = self.quote(price, qty, "sell" if side == "buy" else "cover", bar)
        return sum(x[k] for x in (entry, exit_) for k in ("commission", "slippage", "impact")) / (qty * price) * 10000


@dataclass
class _Pending:
    candidate: SignalCandidateEvent
    horizons: set[int]
    quantity: float
    bars: int = 0
    entry: dict | None = None
    entry_time: str | None = None
    entry_reference: float | None = None
    last_bar: pd.Timestamp | None = None
    max_favorable: float = 0.0
    max_adverse: float = 0.0
    carry: float = 0.0
    funding_bucket: int | None = None
    flags: set[str] = field(default_factory=set)


class ForwardOutcomeTracker:
    def __init__(self, policy: ObservationPolicy, costs: ObservationCosts):
        self.policy, self.costs = policy, costs
        self.pending: dict[str, _Pending] = {}
        self.results: list[dict] = []
        self._seen: set[str] = set()

    def add(self, candidate: SignalCandidateEvent):
        if candidate.candidate_id in self._seen:
            return
        self._seen.add(candidate.candidate_id)
        self.pending[candidate.candidate_id] = _Pending(
            candidate, set(self.policy.horizons),
            self.policy.reference_notional / candidate.reference_price,
        )
        if json.loads(candidate.signal_json).get("order_type", "market") != "market":
            self._censor(self.pending.pop(candidate.candidate_id),
                         "censored_unsupported_order_type", candidate.context.available_at)

    def advance(self, event):
        moment = as_utc_timestamp(event.timestamp)
        for key, item in list(self.pending.items()):
            c = item.candidate
            delta = timeframe_delta(c.context.timeframe)
            expected = (item.last_bar + delta if item.last_bar is not None else
                        as_utc_timestamp(c.context.available_at))
            if moment < expected:
                continue  # same signal bar, duplicate delivery, or another symbol's earlier bar
            if moment > expected or c.symbol not in event.bars:
                self._censor(item, "censored_missing_bar", close_time(moment, c.context.timeframe))
                del self.pending[key]
                continue
            bar = event.bars[c.symbol]
            prices = [finite(bar.get(k)) for k in ("open", "high", "low", "close")]
            if (any(p is None or p <= 0 for p in prices)
                    or prices[1] < max(prices[0], prices[3])
                    or prices[2] > min(prices[0], prices[3])):
                self._censor(item, "censored_invalid_bar", close_time(moment, c.context.timeframe))
                del self.pending[key]
                continue
            open_, high, low, close = prices
            side = "buy" if c.direction == "long" else "short"
            sign = 1 if side == "buy" else -1
            if item.entry is None:
                item.entry = self.costs.quote(open_, item.quantity, side, bar)
                item.entry_time, item.entry_reference = iso(moment), open_
                if self.costs.account_mode == "spot" and side == "short":
                    item.flags.add("short_not_executable_in_spot")
                if item.quantity > float(bar.get("volume", 0)) * self.costs.max_participation_rate:
                    item.flags.add("entry_exceeds_participation_cap")
                if self.costs.account_mode == "spot_margin" and side == "short":
                    available = finite(bar.get("borrow_available_qty"))
                    if available is None and self.costs.borrow_availability_required:
                        item.flags.add("borrow_availability_missing")
                    elif item.quantity > (available if available is not None else self.costs.default_borrow_limit_qty):
                        item.flags.add("borrow_limit")
            mark = finite(bar.get("mark_price")) or close
            if self.costs.account_mode == "perpetual":
                bucket = int(moment.timestamp() // (self.costs.funding_interval_hours * 3600))
                if bucket != item.funding_bucket:
                    rate = finite(bar.get("funding_rate"))
                    if rate is None and self.costs.funding_rate_required:
                        self._censor(item, "censored_missing_funding", close_time(moment, c.context.timeframe))
                        del self.pending[key]
                        continue
                    item.carry += sign * item.quantity * mark * (rate or 0.0)
                    item.funding_bucket = bucket
            elif self.costs.account_mode == "spot_margin" and side == "short" and item.last_bar is not None:
                rate = finite(bar.get("borrow_rate_annual"))
                rate = self.costs.default_borrow_rate_annual if rate is None else rate
                item.carry += item.quantity * mark * rate * (moment-item.last_bar).total_seconds() / (365*86400)
            item.bars += 1
            item.last_bar = moment
            favorable, adverse = (high, low) if sign > 0 else (low, high)
            item.max_favorable = max(item.max_favorable, sign*(favorable/item.entry_reference-1))
            item.max_adverse = min(item.max_adverse, sign*(adverse/item.entry_reference-1))
            if item.bars in item.horizons:
                exit_ = self.costs.quote(close, item.quantity, "sell" if sign > 0 else "cover", bar)
                notional = item.quantity * item.entry_reference
                gross = sign * (close-item.entry_reference) * item.quantity
                net = sign*(exit_["price"]-item.entry["price"])*item.quantity
                net -= item.entry["commission"] + exit_["commission"] + item.carry
                flags = set(item.flags)
                if item.quantity > float(bar.get("volume", 0))*self.costs.max_participation_rate:
                    flags.add("exit_exceeds_participation_cap")
                self.results.append({
                    **self._identity(item, item.bars), "status": "matured",
                    "available_at": close_time(moment, c.context.timeframe),
                    "entry_time": item.entry_time, "exit_bar": iso(moment),
                    "entry_reference": item.entry_reference, "exit_reference": close,
                    "quantity": item.quantity, "entry_price": item.entry["price"],
                    "exit_price": exit_["price"], "gross_pnl": gross, "net_pnl": net,
                    "gross_return_bps": gross/notional*10000, "net_return_bps": net/notional*10000,
                    "commission": item.entry["commission"] + exit_["commission"],
                    "slippage": item.entry["slippage"] + exit_["slippage"],
                    "impact": item.entry["impact"] + exit_["impact"], "carry": item.carry,
                    "mae_bps": item.max_adverse*10000, "mfe_bps": item.max_favorable*10000,
                    "execution_flags": sorted(flags),
                })
                item.horizons.remove(item.bars)
            if not item.horizons:
                del self.pending[key]

    @staticmethod
    def _identity(item, horizon):
        c = item.candidate
        return {"candidate_id": c.candidate_id, "strategy": c.strategy, "symbol": c.symbol,
                "direction": c.direction, "horizon_bars": horizon,
                "scope": "independent_fixed_notional_signal_diagnostic"}

    def _censor(self, item, status, at):
        for horizon in sorted(item.horizons):
            self.results.append({**self._identity(item, horizon), "status": status,
                                 "available_at": at, "entry_time": item.entry_time,
                                 "net_pnl": None, "net_return_bps": None})

    def finish(self, at):
        for item in self.pending.values():
            self._censor(item, "censored_end_of_data", at)
        self.pending.clear()
