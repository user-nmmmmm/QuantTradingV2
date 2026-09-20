"""Run a deterministic S2/S3 example through the real offline Broker.

This is a synthetic accounting acceptance fixture, never strategy evidence.
It has no network, credential, live broker or formal configuration dependency.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd

from core.broker import Broker
from core.portfolio import Portfolio
from core.selection import (DataProvenance, ExecutionQuote, FactorObservation, MembershipFact,
    RebalancePolicy, SelectionPolicy, plan_rebalance, select_targets, selection_input_digest)
from core.target_position import VolatilityPolicy, volatility_target_weight


def run_example():
    point = pd.Timestamp("2024-02-01T00:00:00Z")
    facts = [MembershipFact(s, "listed", "2023-01-01T00:00:00Z", "2023-01-01T00:00:00Z")
             for s in ("AAA/USDT", "BBB/USDT")]
    observations = [FactorObservation(s, point.isoformat(), point.isoformat(), score, 1000000.)
                    for s, score in (("AAA/USDT", 2.), ("BBB/USDT", 1.))]
    selected = select_targets(facts=facts, observations=observations,
        provenance=DataProvenance("fixture://section56", "synthetic", selection_input_digest(facts, observations)),
        as_of=point, policy=SelectionPolicy(top_n=1, gross_target=.3), allow_synthetic=True)
    # Closed daily returns only; no price from the execution bars enters sizing.
    returns = [(t.to_pydatetime(), .02 if i % 2 else -.02)
               for i, t in enumerate(pd.date_range(end=point-pd.Timedelta(days=1), periods=20))]
    adjusted = dict(selected)
    sizing = {symbol: asdict(volatility_target_weight(weight, returns,
        as_of=point.to_pydatetime(), policy=VolatilityPolicy(enabled=True)))
        for symbol, weight in selected["targets"].items()}
    adjusted["targets"] = {symbol: row["weight"] for symbol, row in sizing.items()}
    adjusted["cash_target"] = 1 - sum(adjusted["targets"].values())
    adjusted["parent_selection_id"] = selected["decision_id"]
    adjusted["decision_id"] = hashlib.sha256(json.dumps({"selection": selected["decision_id"],
        "sizing": sizing}, sort_keys=True).encode()).hexdigest()
    policy = RebalancePolicy(participation=.01, fee_bps=10, slippage_bps=0,
                             max_turnover=.2, max_quote_age_hours=1)
    quotes = {symbol: ExecutionQuote(100., 10000., point.isoformat(), point.isoformat(), .001, 1.)
              for symbol in adjusted["targets"]}
    decision_at = point + pd.Timedelta(seconds=1)
    plan = plan_rebalance(selection=adjusted, at=decision_at, holdings={}, quotes=quotes,
        equity=10000., settled_cash=10000., approved_buy_notional={s: 3000. for s in quotes}, policy=policy,
        facts_reconciled=True)
    portfolio = Portfolio(10000., account_mode="spot")
    broker = Broker(portfolio, commission_rate=.001, slippage=0,
        max_participation_rate=.01, account_id="isolated_s2_s3_fixture")
    orders = [broker.submit_order(p["symbol"], p["side"], p["quantity"],
        price=p["reference_price"], timestamp=decision_at, strategy_id="S2S3Fixture",
        signal_id=p["proposal_id"], stop_loss=90., approved_risk_amount=p["quantity"]*10.)
        for p in plan["proposals"]]
    fills = []
    # Each real execution bar can supply just one unit. Orders remain partial.
    for day in (1, 2):
        bar_time = point + pd.Timedelta(days=day)
        bars = {symbol: pd.Series(dict(open=100., high=101., low=99., close=100., volume=100.),
                                  name=bar_time) for symbol in quotes}
        fills.extend(broker.process_orders(bars))
    spent = sum(float(t["qty"])*float(t["fill_price"])+float(t["commission"]) for t in fills)
    qty = sum(float(t["qty"]) for t in fills)
    cash_error = portfolio.cash - (10000. - spent)
    position_error = sum(p["qty"] for p in portfolio.positions.values()) - qty
    if abs(cash_error) > 1e-8 or abs(position_error) > 1e-8:
        raise AssertionError("Broker facts do not reconcile")
    # Report quantities from the order facts, not a new desired-weight assumption.
    return {"schema": "strategy_branch_example/v1", "source_kind": "synthetic",
        "selection": selected, "volatility_sizing": sizing, "plan": plan,
        "orders": [{"id": o.id, "status": o.status.value, "qty": o.qty,
                    "filled_qty": o.filled_qty} for o in orders],
        "fills": [{k: t[k] for k in ("symbol", "side", "qty", "fill_price", "commission")} for t in fills],
        "cash": portfolio.cash, "positions": portfolio.positions,
        "reconciliation": {"cash_error": cash_error, "position_error": position_error},
        "engineering_status": "pass", "research_status": "not_evaluated",
        "formal_routing_enabled": False, "admission": "paused_revalidation",
        "limitations": ["Synthetic unit-based spot example; no PIT vendor authenticity or alpha evidence.",
                        "Unfilled targets remain pending; no completion or liquidity is invented.",
                        "Execution proposals still require normal production admission and risk approval."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run_example()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)+"\n")
    print(json.dumps(result["reconciliation"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
