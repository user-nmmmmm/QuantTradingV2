"""Pure pro-rata margin proposal sizing; no permission to borrow or send orders.

All capacities are supplied by the authoritative account/risk layers. Proposed
sells never release cash, margin, risk or exposure for proposed buys.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
import math
from typing import Mapping


@dataclass(frozen=True)
class MarginQuote:
    price: float
    volume: float
    step: float = 1e-8
    min_notional: float = 5.

    def __post_init__(self):
        for name in ("price", "volume", "step", "min_notional"):
            value = getattr(self, name)
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and nonnegative")
        if self.price == 0 or self.step == 0:
            raise ValueError("quote price and quantity step must be positive")


@dataclass(frozen=True)
class MarginBudgetSnapshot:
    """Account/risk-approved capacities at one reconciled decision time.

    Monetary fields are quote units (USDT). ``net_quote_cash`` means equity
    minus funded long notional, not the collateral-only ``Portfolio.cash``.
    Negative net funding is represented as a positive borrow liability in
    outputs; a proposal is never a new authoritative cash/debt ledger.
    """
    account: str
    as_of: str
    equity: float
    net_quote_cash: float
    current_gross_exposure: float
    quote_borrow_limit: float
    available_margin: float
    initial_margin_rate: float
    approved_buy_notional: Mapping[str, float]
    pending_buy_notional: float
    gross_headroom: float
    remaining_turnover_notional: float
    remaining_stop_risk: float
    facts_reconciled: bool

    def __post_init__(self):
        if not self.account or not self.as_of or not math.isfinite(self.net_quote_cash):
            raise ValueError("account, decision time and finite net funding are required")
        for name in ("equity", "current_gross_exposure", "quote_borrow_limit", "available_margin",
                     "pending_buy_notional", "gross_headroom", "remaining_turnover_notional", "remaining_stop_risk"):
            _nonnegative(getattr(self, name), name)
        if self.equity <= 0 or not 0 < self.initial_margin_rate <= 1:
            raise ValueError("positive equity and valid initial margin are required")
        for value in self.approved_buy_notional.values():
            _nonnegative(value, "approved buy notional")
        if self.facts_reconciled is not True:
            raise ValueError("budget requires reconciled account facts")


def plan_margin_targets(*, budget: MarginBudgetSnapshot, requested_buys, requested_sells,
                        quotes, stop_distances, target_gross_exposure=None, **policies):
    """Budget-snapshot interface; target and proposal notionals use USDT units."""
    result = plan_margin_rebalance(requested_buys=requested_buys, requested_sells=requested_sells,
        quotes=quotes, approved_buy_notional=budget.approved_buy_notional,
        cash=budget.net_quote_cash, quote_borrow_limit=budget.quote_borrow_limit,
        available_margin=budget.available_margin, initial_margin_rate=budget.initial_margin_rate,
        pending_buy_notional=budget.pending_buy_notional, gross_headroom=budget.gross_headroom,
        remaining_turnover_notional=budget.remaining_turnover_notional,
        remaining_stop_risk=budget.remaining_stop_risk, stop_distances=stop_distances,
        facts_reconciled=budget.facts_reconciled, **policies)
    if target_gross_exposure is None:
        target_gross_exposure = max(0., budget.current_gross_exposure
            + sum(qty*quotes[symbol].price for symbol, qty in requested_buys.items())
            - sum(qty*quotes[symbol].price for symbol, qty in requested_sells.items()))
    target_gross_exposure = _nonnegative(target_gross_exposure, "target gross exposure")
    result.update(account=budget.account, as_of=budget.as_of, target_gross_exposure=target_gross_exposure,
                  target_gross_weight=target_gross_exposure/budget.equity,
                  monetary_unit="USDT", budget_source="reconciled_account_risk_snapshot")
    return result


def _nonnegative(value, name):
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and nonnegative")
    return value


def floor_quantity(qty, step):
    if step <= 0 or not math.isfinite(step):
        raise ValueError("quantity step must be positive")
    return float((Decimal(str(qty))/Decimal(str(step))).to_integral_value(rounding=ROUND_FLOOR)
                 * Decimal(str(step)))


def plan_margin_rebalance(*, requested_buys: Mapping[str, float],
                          requested_sells: Mapping[str, float],
                          quotes: Mapping[str, MarginQuote], approved_buy_notional: Mapping[str, float],
                          cash: float, quote_borrow_limit: float, available_margin: float,
                          initial_margin_rate: float, pending_buy_notional: float = 0.,
                          gross_headroom: float, remaining_turnover_notional: float,
                          remaining_stop_risk: float, stop_distances: Mapping[str, float],
                          fee_rate: float = .001, slippage_rate: float = .001,
                          participation: float = .01, forced_exits=(), facts_reconciled=False,
                          financing_allowed: Mapping[str, bool] | None = None):
    if facts_reconciled is not True:
        raise ValueError("authoritative order and position facts must be reconciled")
    if not math.isfinite(float(cash)) or not 0 < initial_margin_rate <= 1:
        raise ValueError("invalid cash or initial margin rate")
    if not 0 < participation <= 1:
        raise ValueError("invalid participation")
    values = {name: _nonnegative(value, name) for name, value in {
        "quote_borrow_limit": quote_borrow_limit, "available_margin": available_margin,
        "pending_buy_notional": pending_buy_notional, "gross_headroom": gross_headroom,
        "turnover": remaining_turnover_notional, "risk": remaining_stop_risk,
        "fee": fee_rate, "slippage": slippage_rate}.items()}
    forced = set(forced_exits)
    candidates = []
    for side, mapping in (("sell", requested_sells), ("buy", requested_buys)):
        for symbol, requested in sorted(mapping.items()):
            quote = quotes[symbol]
            if not math.isfinite(quote.price) or quote.price <= 0:
                raise ValueError("quote price must be positive")
            quantity = min(_nonnegative(requested, "quantity"),
                           _nonnegative(quote.volume, "volume") * participation)
            if side == "buy":
                distance = _nonnegative(stop_distances.get(symbol, 0.), "stop distance")
                if distance <= 0:
                    continue
                quantity = min(quantity, _nonnegative(approved_buy_notional.get(symbol, 0.),
                                                      "approved notional") / quote.price)
            if quantity:
                candidates.append({"symbol": symbol, "side": side, "qty": quantity,
                                   "forced": side == "sell" and symbol in forced})
    regular = [order for order in candidates if not order["forced"]]
    turnover = sum(order["qty"]*quotes[order["symbol"]].price for order in regular)
    turnover_scale = min(1., values["turnover"]/turnover) if turnover else 1.
    for order in regular:
        order["qty"] *= turnover_scale
    buys = [order for order in candidates if order["side"] == "buy"]
    cost_factor = (1 + slippage_rate)*(1 + fee_rate)
    cash_only = [order for order in buys if not (financing_allowed or {}).get(order["symbol"], True)]
    cash_only_notional = sum(order["qty"]*quotes[order["symbol"]].price for order in cash_only)
    own_cash_capacity = max(0., cash-pending_buy_notional*cost_factor)/cost_factor
    cash_only_scale = min(1., own_cash_capacity/cash_only_notional) if cash_only_notional else 1.
    for order in cash_only:
        order["qty"] *= cash_only_scale
    notional = sum(order["qty"]*quotes[order["symbol"]].price for order in buys)
    risk = sum(order["qty"]*stop_distances[order["symbol"]] for order in buys)
    cash_capacity = max(0., cash + quote_borrow_limit - pending_buy_notional*cost_factor) / cost_factor
    margin_capacity = max(0., available_margin-pending_buy_notional*(initial_margin_rate+fee_rate)) \
        / (initial_margin_rate + fee_rate) / (1+slippage_rate)
    capacity = min(cash_capacity, margin_capacity, values["gross_headroom"])
    buy_scale = min(1., capacity/notional if notional else 1.,
                    values["risk"]/risk if risk else 1.)
    result = []
    for order in candidates:
        quote = quotes[order["symbol"]]
        quantity = floor_quantity(order["qty"]*(buy_scale if order["side"] == "buy" else 1.), quote.step)
        if quantity <= 0 or quantity*quote.price < quote.min_notional:
            continue
        result.append({**order, "qty": quantity, "reference_price": quote.price,
                       "approved_risk_amount": (quantity*stop_distances[order["symbol"]]
                                                if order["side"] == "buy" else None)})
    # Cash-restricted buys consume their reserved own-funding share before
    # financeable buys; no ordering preference changes the pro-rata allocation.
    result.sort(key=lambda order: (order["side"] == "buy",
                bool((financing_allowed or {}).get(order["symbol"], True)), order["symbol"]))
    current_debt = max(0., -cash)
    debt_after_pending = max(0., pending_buy_notional*cost_factor-cash)
    proposed_buy_cost = sum(order["qty"]*quotes[order["symbol"]].price*cost_factor
                            for order in result if order["side"] == "buy")
    debt_after_proposals = max(0., pending_buy_notional*cost_factor+proposed_buy_cost-cash)
    return {"orders": result, "buy_scale": buy_scale, "turnover_scale": turnover_scale,
            "cash_only_scale": cash_only_scale,
            "cash_asset": max(0., cash), "borrow_liability": current_debt,
            "pending_financing_requirement": max(0., debt_after_pending-current_debt),
            "proposed_financing_requirement": max(0., debt_after_proposals-debt_after_pending),
            "proposed_borrow_liability_if_buys_fill": debt_after_proposals,
            "buy_budget_caps": {"own_cash": own_cash_capacity, "cash_and_quote_credit": cash_capacity,
                                "initial_margin": margin_capacity, "gross_exposure": values["gross_headroom"],
                                "initial_stop_risk": values["risk"]},
            "proposal_only": True, "formal_routing_enabled": False}
