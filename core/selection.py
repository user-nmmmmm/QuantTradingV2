"""Opt-in S2 research selection and constrained order proposals; never submits orders.

Membership facts carry both effective and knowledge times. Data provenance is a
checked content identity, not proof that a vendor's supplied history is authentic.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_FLOOR
import hashlib
import json
import math
from typing import Iterable, Mapping

import pandas as pd

from core.universe import normalize_symbol


def _time(value) -> pd.Timestamp:
    result = pd.Timestamp(value)
    if pd.isna(result):
        raise ValueError("timestamp cannot be missing")
    return result.tz_localize("UTC") if result.tzinfo is None else result.tz_convert("UTC")


def _number(value, name: str, *, positive: bool = False) -> float:
    number = float(value)
    if not math.isfinite(number) or number < 0 or (positive and number == 0):
        raise ValueError(f"{name} must be finite and {'positive' if positive else 'nonnegative'}")
    return number


def _mapping(values: Mapping[str, float], name: str) -> dict[str, float]:
    result = {}
    for key, value in values.items():
        symbol = normalize_symbol(key)
        if symbol in result:
            raise ValueError(f"duplicate normalized symbol in {name}: {symbol}")
        result[symbol] = _number(value, name)
    return result


@dataclass(frozen=True)
class MembershipFact:
    symbol: str
    action: str
    effective_at: str
    available_at: str


@dataclass(frozen=True)
class FactorObservation:
    symbol: str
    observed_at: str
    available_at: str
    score: float | None
    quote_volume: float | None


def selection_input_digest(facts: Iterable[MembershipFact],
                           observations: Iterable[FactorObservation]) -> str:
    """Canonical JSON content identity; reject infinity, permit explicit nulls."""
    records = {"membership": [asdict(item) for item in facts],
               "observations": [asdict(item) for item in observations]}
    for items in records.values():
        items.sort(key=lambda item: json.dumps(item, sort_keys=True, allow_nan=False))
    return hashlib.sha256(json.dumps(records, sort_keys=True, allow_nan=False,
                                     separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class DataProvenance:
    source_uri: str
    source_kind: str
    dataset_sha256: str


@dataclass(frozen=True)
class SelectionPolicy:
    top_n: int = 3
    buffer_ranks: int = 0
    min_listing_days: float = 30
    min_quote_volume: float = 1_000_000
    max_observation_age_hours: float = 25
    gross_target: float = 0.9
    max_symbol_weight: float = 0.3

    def __post_init__(self):
        if type(self.top_n) is not int or self.top_n < 1:
            raise ValueError("top_n must be a positive integer")
        if type(self.buffer_ranks) is not int or self.buffer_ranks < 0:
            raise ValueError("buffer_ranks must be a nonnegative integer")
        for name in ("min_listing_days", "min_quote_volume", "max_observation_age_hours"):
            _number(getattr(self, name), name)
        for name in ("gross_target", "max_symbol_weight"):
            if not 0 < _number(getattr(self, name), name) <= 1:
                raise ValueError(f"{name} must be in (0, 1]")


def select_targets(*, facts: Iterable[MembershipFact],
                   observations: Iterable[FactorObservation], provenance: DataProvenance,
                   as_of, policy: SelectionPolicy, held_symbols: Iterable[str] = (),
                   explicit_symbols: Iterable[str] | None = None,
                   allow_synthetic: bool = False) -> dict:
    """Return causal rankings and long-only targets; null factors are excluded.

    Explicit symbols restrict the candidate universe, never bypass PIT/data rules.
    A visible delisting announcement immediately targets zero, without consulting
    a future final tradable bar. Unknown or already closed markets cannot exit.
    """
    facts, observations = tuple(facts), tuple(observations)
    if provenance.source_kind not in {"real", "synthetic"} or not provenance.source_uri.strip():
        raise ValueError("explicit source_uri and real/synthetic source_kind required")
    if provenance.source_kind == "synthetic" and not allow_synthetic:
        raise ValueError("synthetic data requires explicit research fixture opt-in")
    if selection_input_digest(facts, observations) != provenance.dataset_sha256:
        raise ValueError("selection input content hash mismatch")
    point = _time(as_of)
    held = {normalize_symbol(symbol) for symbol in held_symbols}
    override = None if explicit_symbols is None else {normalize_symbol(s) for s in explicit_symbols}
    visible_facts: dict[str, list] = {}
    seen = set()
    for fact in facts:
        symbol = normalize_symbol(fact.symbol)
        effective, available = _time(fact.effective_at), _time(fact.available_at)
        if fact.action not in {"listed", "delisted"}:
            raise ValueError("membership action must be listed or delisted")
        identity = (symbol, fact.action)
        if identity in seen:
            raise ValueError("duplicate membership action; relistings require a distinct market identity")
        seen.add(identity)
        if available <= point:
            visible_facts.setdefault(symbol, []).append((fact.action, effective, available))
    latest = {}
    seen = set()
    for item in observations:
        symbol = normalize_symbol(item.symbol)
        observed, available = _time(item.observed_at), _time(item.available_at)
        if available < observed:
            raise ValueError("factor availability cannot precede its observation")
        identity = (symbol, observed, available)
        if identity in seen:
            raise ValueError("duplicate factor observation")
        seen.add(identity)
        if observed <= point and available <= point:
            prior = latest.get(symbol)
            if prior is None or (observed, available) > prior[:2]:
                latest[symbol] = (observed, available, item)
    rows, eligible, forced, deadlines = {}, [], [], {}
    for symbol in sorted(set(visible_facts) | held):
        events = visible_facts.get(symbol, [])
        listing = next((time for action, time, _ in events if action == "listed"), None)
        delisting = next((time for action, time, _ in events if action == "delisted"), None)
        if listing is not None and delisting is not None and delisting <= listing:
            raise ValueError("delisting must follow listing")
        reason = None
        if listing is None or listing > point:
            reason = "membership_unknown_or_not_yet_listed"
        elif delisting is not None:
            reason = "delisted" if delisting <= point else "announced_delisting_exit"
        elif override is not None and symbol not in override:
            reason = "explicit_symbols_exclusion"
        elif point - listing < pd.Timedelta(days=policy.min_listing_days):
            reason = "listing_age"
        item = latest.get(symbol)
        if reason is None:
            if item is None:
                reason = "missing_factor"
            elif item[0] < listing:
                reason = "factor_precedes_listing"
            elif point - item[0] > pd.Timedelta(hours=policy.max_observation_age_hours):
                reason = "stale_factor"
            elif item[2].score is None or not math.isfinite(float(item[2].score)):
                reason = "missing_or_invalid_score"
            elif item[2].quote_volume is None or not math.isfinite(float(item[2].quote_volume)):
                reason = "missing_or_invalid_liquidity"
            elif float(item[2].quote_volume) < policy.min_quote_volume:
                reason = "liquidity_filter"
        row = {"eligible": reason is None, "reason": reason,
               "tradable": listing is not None and listing <= point and
                           (delisting is None or point < delisting),
               "score": None, "rank": None, "percentile_rank": None, "zscore": None}
        if item is not None:
            row["observed_at"], row["available_at"] = item[0].isoformat(), item[1].isoformat()
        if reason is None:
            row["score"] = float(item[2].score)
            eligible.append(symbol)
        if delisting is not None:
            deadlines[symbol] = delisting.isoformat()
            if symbol in held:
                forced.append(symbol)
        rows[symbol] = row
    eligible.sort(key=lambda symbol: (-rows[symbol]["score"], symbol))
    scores = pd.Series({symbol: rows[symbol]["score"] for symbol in eligible}, dtype=float)
    if len(scores):
        # Scaling leaves zscores unchanged and avoids overflow for finite extremes.
        scale = float(scores.abs().max()) or 1.
        scaled = scores / scale
        percentile, mean, std = scores.rank(method="average", pct=True), scaled.mean(), scaled.std(ddof=0)
        for rank, symbol in enumerate(eligible, 1):
            rows[symbol].update(rank=rank, percentile_rank=float(percentile[symbol]),
                                zscore=float((scaled[symbol] - mean) / std) if std > 0 else None,
                                zscore_status="ok" if std > 0 else "constant_cross_section")
    retained = [s for s in eligible[:policy.top_n + policy.buffer_ranks] if s in held]
    chosen = retained[:policy.top_n]
    chosen += [s for s in eligible if s not in chosen][:max(0, policy.top_n - len(chosen))]
    weight = min(policy.max_symbol_weight, policy.gross_target / len(chosen)) if chosen else 0.
    targets = {symbol: weight if symbol in chosen else 0. for symbol in sorted(set(rows) | held)}
    decision = {"as_of": point.isoformat(), "policy": asdict(policy), "rows": rows,
                "targets": targets, "forced_exits": sorted(forced), "deadlines": deadlines}
    decision_id = hashlib.sha256(json.dumps(decision, sort_keys=True, allow_nan=False).encode()).hexdigest()
    return {"schema_version": "s2-selection/v1", **decision, "decision_id": decision_id,
            "cash_target": 1. - sum(targets.values()), "eligible_count": len(eligible),
            "coverage": len(eligible) / len(rows) if rows else None,
            "provenance": asdict(provenance),
            "provenance_authenticity": "synthetic_fixture" if allow_synthetic and provenance.source_kind == "synthetic"
                                       else "requires_external_source_verification",
            "formal_routing_enabled": False}


@dataclass(frozen=True)
class ExecutionQuote:
    price: float
    base_volume: float
    observed_at: str
    available_at: str
    quantity_step: float
    min_notional: float


@dataclass(frozen=True)
class RebalancePolicy:
    participation: float = 0.01
    fee_bps: float = 10.
    slippage_bps: float = 10.
    max_turnover: float = 0.2
    max_gross_weight: float = 1.
    max_symbol_weight: float = 0.3
    min_rebalance_hours: float = 24.
    max_quote_age_hours: float = 1.

    def __post_init__(self):
        for name, value in asdict(self).items():
            _number(value, name)
        for name in ("participation", "max_turnover", "max_gross_weight", "max_symbol_weight"):
            if getattr(self, name) > 1:
                raise ValueError(f"{name} must be at most 1 for long-only cash planning")
        if self.slippage_bps >= 10000 or self.fee_bps >= 10000:
            raise ValueError("fee/slippage bps must be below 10000")


def _floor_quantity(quantity: float, step: float) -> float:
    return float((Decimal(str(quantity)) / Decimal(str(step))).to_integral_value(
        rounding=ROUND_FLOOR) * Decimal(str(step)))


def plan_rebalance(*, selection: Mapping, at, holdings: Mapping[str, float],
                   quotes: Mapping[str, ExecutionQuote], equity: float, settled_cash: float,
                   approved_buy_notional: Mapping[str, float], policy: RebalancePolicy,
                   pending_buy_qty: Mapping[str, float] | None = None,
                   pending_sell_qty: Mapping[str, float] | None = None,
                   pending_buy_cash: float = 0., reserved_participation_qty: Mapping[str, float] | None = None,
                   committed_turnover_notional: float = 0., last_rebalance_at=None,
                   facts_reconciled: bool = False) -> dict:
    """Plan proposals from authoritative positions/pending orders, without fills.

    approved_buy_notional is remaining authority supplied by the risk layer; this
    function cannot create it. New buys use settled cash only and never finance
    themselves from proposed sells. Callers must reconcile unknown orders first.
    """
    point = _time(at)
    if facts_reconciled is not True:
        raise ValueError("reconciled order/position facts required before planning")
    if selection.get("schema_version") != "s2-selection/v1":
        raise ValueError("unsupported selection schema")
    if point <= _time(selection["as_of"]):
        raise ValueError("execution must be after selection observation")
    equity = _number(equity, "equity", positive=True)
    cash = max(0., _number(settled_cash, "settled_cash") - _number(pending_buy_cash, "pending_buy_cash"))
    held = _mapping(holdings, "holdings")
    buys = _mapping(pending_buy_qty or {}, "pending_buy_qty")
    sells = _mapping(pending_sell_qty or {}, "pending_sell_qty")
    reserved = _mapping(reserved_participation_qty or {}, "reserved_participation_qty")
    budgets = _mapping(approved_buy_notional, "approved_buy_notional")
    targets = _mapping(selection["targets"], "target weights")
    if sum(targets.values()) > 1. + 1e-12 or any(value > 1 for value in targets.values()):
        raise ValueError("long-only target weights cannot exceed 1")
    market = {}
    for key, quote in quotes.items():
        symbol = normalize_symbol(key)
        if symbol in market:
            raise ValueError("duplicate quote symbol")
        _number(quote.price, "price", positive=True)
        _number(quote.base_volume, "base_volume")
        _number(quote.quantity_step, "quantity_step", positive=True)
        _number(quote.min_notional, "min_notional")
        observed, available = _time(quote.observed_at), _time(quote.available_at)
        if not observed <= available <= point or point - observed > pd.Timedelta(hours=policy.max_quote_age_hours):
            raise ValueError("execution quote is future, stale or has invalid availability")
        market[symbol] = quote
    symbols = sorted(set(targets) | set(held) | set(buys) | set(sells))
    if any(sells.get(s, 0) > held.get(s, 0) + 1e-12 for s in symbols):
        raise ValueError("pending sells exceed authoritative long holdings")
    if any((held.get(s, 0) > 0 or buys.get(s, 0) > 0) and s not in market for s in symbols):
        raise ValueError("all existing/pending exposures require fresh valuation quotes")
    fee, slip = policy.fee_bps / 10000, policy.slippage_bps / 10000
    pending_min_cash = sum(qty * market[s].price * (1 + slip) * (1 + fee)
                           for s, qty in buys.items() if qty > 0)
    if pending_buy_cash + 1e-9 < pending_min_cash:
        raise ValueError("pending buy cash reservation is incomplete")
    gross = sum((held.get(s, 0) + buys.get(s, 0)) * market[s].price
                for s in symbols if s in market)
    room = max(0., equity * policy.max_gross_weight - gross)
    turnover_left = max(0., equity * policy.max_turnover -
                        _number(committed_turnover_notional, "committed_turnover_notional"))
    due = last_rebalance_at is None
    if last_rebalance_at is not None:
        if _time(last_rebalance_at) > point:
            raise ValueError("last rebalance cannot be in the future")
        due = point - _time(last_rebalance_at) >= pd.Timedelta(hours=policy.min_rebalance_hours)
    forced = set(selection["forced_exits"])
    proposals, residuals, discretionary_notional, forced_notional = [], {}, 0., 0.
    for symbol in sorted(symbols, key=lambda s: (s not in forced, s)):
        quote = market.get(symbol)
        current = held.get(symbol, 0)
        if quote is None:
            residuals[symbol] = {"reason": "missing_execution_quote", "target_weight": targets.get(symbol, 0.)}
            continue
        target_qty = equity * targets.get(symbol, 0.) / quote.price
        projected = current + buys.get(symbol, 0.) - sells.get(symbol, 0.)
        delta = target_qty - projected
        if abs(delta) < 1e-12:
            continue
        side = "buy" if delta > 0 else "sell"
        reason = None
        row = selection["rows"].get(symbol, {})
        deadline = selection["deadlines"].get(symbol)
        if not row.get("tradable", False) or (deadline and point >= _time(deadline)):
            reason = "market_unavailable_pending_exit"
        elif symbol in forced and buys.get(symbol, 0.) > 0:
            reason = "cancel_pending_buys_before_forced_exit"
        elif not due and symbol not in forced:
            reason = "rebalance_interval"
        limit = max(0., quote.base_volume * policy.participation - reserved.get(symbol, 0.))
        qty = min(abs(delta), limit)
        if symbol not in forced:
            qty = min(qty, turnover_left / quote.price)
        if side == "sell":
            qty = min(qty, max(0., current - sells.get(symbol, 0.)))
        else:
            # Remaining approved budget is consumed at the adverse proposal price.
            adverse = quote.price * (1 + slip)
            symbol_room = max(0., equity * policy.max_symbol_weight -
                              (current + buys.get(symbol, 0.)) * quote.price)
            qty = min(qty, budgets.get(symbol, 0.) / adverse, room / adverse,
                      symbol_room / adverse, cash / (adverse * (1 + fee)))
        qty = _floor_quantity(qty, quote.quantity_step)
        if reason is None and qty <= 0:
            reason = "risk_cash_turnover_or_liquidity_limit"
        if reason is None and qty * quote.price < quote.min_notional:
            reason = "minimum_notional_pending_target"
        if reason is None:
            notional = qty * quote.price
            estimated_price = quote.price * (1 + slip if side == "buy" else 1 - slip)
            order = {"symbol": symbol, "side": side, "quantity": qty,
                     "reference_price": quote.price, "estimated_price": estimated_price,
                     "estimated_fee": qty * estimated_price * fee,
                     "reduce_only": side == "sell", "reason": "delisting_exit" if symbol in forced else "target_weight",
                     "target_weight": targets.get(symbol, 0.)}
            identity = {"decision": selection["decision_id"], "at": point.isoformat(),
                        "holdings": held, "pending_buys": buys, "pending_sells": sells, "order": order}
            order["proposal_id"] = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            proposals.append(order)
            if side == "buy":
                cash -= qty * estimated_price * (1 + fee)
                room -= qty * estimated_price
            if symbol in forced:
                forced_notional += notional
            else:
                turnover_left -= notional
                discretionary_notional += notional
            delta -= qty if side == "buy" else -qty
        if abs(delta) > 1e-12:
            residuals[symbol] = {"remaining_quantity": abs(delta), "side": side,
                                 "target_weight": targets.get(symbol, 0.),
                                 "reason": reason or "partial_proposal_pending_fill"}
    return {"schema_version": "s2-rebalance/v1", "selection_id": selection["decision_id"],
            "at": point.isoformat(), "proposals": proposals, "pending_targets": residuals,
            "discretionary_turnover": discretionary_notional / equity,
            "forced_exit_turnover": forced_notional / equity,
            "estimated_remaining_settled_cash": cash, "formal_routing_enabled": False,
            "status": "proposal_only_no_execution_claim"}
