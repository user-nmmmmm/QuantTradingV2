"""Passive baselines beside the default strategy, under the simulated broker's costs.

Covers the "passive" row of the R1 draft (docs/research/backtest_r1_null_baseline_20261116.md):
cash, BTC buy-and-hold and a monthly equal-weight rebalance, each on train60 and
validation20 of the adopted 30-symbol data. final20 is never loaded.

The production engine sizes every entry from a stop distance, so it cannot hold a
fixed share of equity. The passive accounts therefore drive the same ``Broker``
and ``Portfolio`` directly: market orders decided on a close fill at the next open
with the configured commission, spread, slippage, impact and participation cap.
The strategy row is a normal engine run (the reality-gap anchor on that window).

This is a retrospective diagnostic. The R1 protocol is a draft and is not frozen;
nothing here is registered, selects a parameter or counts as admission evidence.

Usage:
    python scripts/run_passive_baseline_diagnostic.py
"""
from __future__ import annotations

import argparse
import json
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import pandas as pd

from config.config import config
from core.broker import Broker
from core.foundation.content_hash import sha256_lf_bytes
from core.metrics.performance import calculate_drawdown, calculate_sharpe, equity_period_returns
from core.trading.portfolio import Portfolio
from scripts.run_reality_gap_experiments import (
    DATA_DIR,
    DEFAULT_END,
    DEFAULT_START,
    _load_universe_data,
    adopted_universe,
    run_arm,
    run_identity,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "outputs" / "passive_baseline_diagnostic"
INITIAL_CAPITAL = 10000.0
BTC = "BTC-USDT"
TRAIN_FRACTION, VALIDATION_FRACTION = 0.60, 0.20
PURGE_BARS, EMBARGO_BARS = 30, 30
# Cash kept back at each rebalance so that costs do not make the last buy the only one resized.
CASH_BUFFER = 0.005
MIN_ORDER_NOTIONAL = 5.0  # venue minimum; smaller rebalance legs are skipped
DAILY_PERIODS_PER_YEAR = 365.25  # what the report generator infers for a regular daily clock


def research_segments(start: str = DEFAULT_START, end: str = DEFAULT_END) -> Dict[str, Tuple[str, str]]:
    """Time-ordered 60/20/20 split of the daily calendar; purge and embargo precede each later segment.

    Computed from the calendar alone, so the final20 boundary is known without reading its bars.
    """
    days = pd.date_range(start, end, freq="D")
    a, b = int(len(days) * TRAIN_FRACTION), int(len(days) * (TRAIN_FRACTION + VALIDATION_FRACTION))
    gap = PURGE_BARS + EMBARGO_BARS

    def span(first: int, last: int) -> Tuple[str, str]:
        return days[first].strftime("%Y-%m-%d"), days[last].strftime("%Y-%m-%d")

    return {"train60": span(0, a - 1), "validation20": span(a + gap, b - 1), "final20": span(b + gap, len(days) - 1)}


def evaluated_segments() -> Dict[str, Tuple[str, str]]:
    return {name: window for name, window in research_segments().items() if name != "final20"}


@dataclass(frozen=True)
class PassivePolicy:
    """Target weights of a passive account. ``symbols=None`` means every tradable symbol, equally."""

    name: str
    gross: float = 1.0
    symbols: Optional[Tuple[str, ...]] = None
    monthly: bool = False

    def __post_init__(self) -> None:
        if not 0.0 <= self.gross <= 1.0:
            raise ValueError("a passive spot account holds between 0% and 100% of equity")

    def weights(self, tradable: Sequence[str]) -> Dict[str, float]:
        chosen = [s for s in tradable if self.symbols is None or s in self.symbols]
        return {s: self.gross / len(chosen) for s in chosen} if chosen else {}


def _passive_account(initial_capital: float) -> Tuple[Portfolio, Broker]:
    execution = config.require("execution")
    portfolio = Portfolio(initial_capital, account_mode="spot")
    broker = Broker(
        portfolio,
        commission_rate=execution["commission_rate_taker"],
        commission_rate_maker=execution["commission_rate_maker"],
        slippage=execution["slippage_bps"] / 10000.0,
        random_slip=False,
        use_impact_cost=execution["use_impact_cost"],
        max_participation_rate=execution["max_participation_rate"],
        spread_bps=execution.get("spread_bps", 0.0),
        volatility_slippage_factor=execution.get("volatility_slippage_factor", 0.0),
        impact_coefficient=execution.get("impact_coefficient", 0.10),
        impact_exponent=execution.get("impact_exponent", 1.5),
        exchange_id="binance", account_id="passive-baseline", timeframe="1d",
    )
    return portfolio, broker


def _held(portfolio: Portfolio) -> Dict[str, float]:
    return {s: float(p["qty"]) for s, p in portfolio.positions.items() if p["qty"]}


def _rebalance(broker: Broker, portfolio: Portfolio, targets: Mapping[str, float], marks: Mapping[str, float],
               at: pd.Timestamp, frozen: Sequence[str]) -> None:
    """Submit the orders that move the account to ``targets``; sells go first so their cash funds the buys.

    ``frozen`` symbols cannot trade on the next bar: their holdings stay and are not part of the target.
    """
    held = _held(portfolio)
    equity = float(portfolio.get_equity(dict(marks)))
    investable = (equity - sum(held[s] * marks[s] for s in frozen if s in held)) * (1.0 - CASH_BUFFER)
    deltas = {}
    for symbol in sorted(set(held) | set(targets)):
        if symbol in frozen:
            continue
        target_qty = investable * targets.get(symbol, 0.0) / marks[symbol]
        deltas[symbol] = target_qty - held.get(symbol, 0.0)
    for side, sign in (("sell", -1.0), ("buy", 1.0)):
        for symbol, delta in deltas.items():
            leaving = side == "sell" and symbol not in targets
            if delta * sign <= 0 or (abs(delta) * marks[symbol] < MIN_ORDER_NOTIONAL and not leaving):
                continue
            qty = held[symbol] if leaving else abs(delta)
            broker.submit_order(symbol, side, qty, price=marks[symbol], timestamp=at,
                                strategy_id="PassiveBaseline", exit_reason="passive_rebalance",
                                reference_price=marks[symbol])


def simulate_passive(policy: PassivePolicy, data_map: Mapping[str, pd.DataFrame],
                     initial_capital: float = INITIAL_CAPITAL) -> Dict[str, Any]:
    """Run one passive account over ``data_map`` and return its equity curve and fills.

    A symbol is tradable on a close when it has a bar on the next timeline step. A symbol whose
    data stops before the window does is sold on its last bar's open and not bought again.
    The account is marked to market on the last close (``backtest.end_of_backtest_mode``).
    """
    if config.require("backtest", "end_of_backtest_mode") != "mark_to_market":
        raise ValueError("the passive accounts only model the mark_to_market terminal policy")
    portfolio, broker = _passive_account(initial_capital)
    timeline = sorted(set().union(*(set(frame.index) for frame in data_map.values())))
    last_bar = {symbol: frame.index[-1] for symbol, frame in data_map.items()}
    marks: Dict[str, float] = {}
    rows: List[Dict[str, Any]] = []
    invested = False
    for step, at in enumerate(timeline):
        bars = {symbol: frame.loc[at] for symbol, frame in data_map.items() if at in frame.index}
        broker.process_orders(bars)
        marks.update({symbol: float(bar["close"]) for symbol, bar in bars.items()})
        equity = float(portfolio.get_equity(dict(marks)))
        gross = sum(qty * marks[symbol] for symbol, qty in _held(portfolio).items())
        rows.append({"timestamp": at, "equity": equity, "cash": float(portfolio.cash), "gross_exposure": gross})
        if step == len(timeline) - 1:
            break
        upcoming = timeline[step + 1]
        on_next_bar = {s for s, frame in data_map.items() if upcoming in frame.index}
        # Tradable now and still alive after the next bar, or running to the end of the window.
        tradable = sorted(s for s in on_next_bar if s in bars and (last_bar[s] > upcoming or last_bar[s] == timeline[-1]))
        frozen = sorted(s for s in _held(portfolio) if s not in on_next_bar)
        targets = policy.weights(tradable)
        ending = [s for s in _held(portfolio) if s in on_next_bar and s not in tradable]
        due = (not invested and bool(targets)) or (invested and policy.monthly and upcoming.month != at.month)
        if due:
            _rebalance(broker, portfolio, targets, marks, at, frozen)
            invested = True
        else:
            for symbol in ending:
                broker.submit_order(symbol, "sell", _held(portfolio)[symbol], price=marks[symbol], timestamp=at,
                                    strategy_id="PassiveBaseline", exit_reason="passive_data_end",
                                    reference_price=marks[symbol])
    curve = pd.DataFrame(rows).set_index("timestamp")
    return {"equity_curve": curve, "trades": list(broker.trades), "execution_audit": list(broker.execution_audit),
            "cash": float(portfolio.cash), "open_positions": _held(portfolio), "initial_capital": float(initial_capital)}


def cash_reconciles(run: Mapping[str, Any], tolerance: float = 1e-6) -> bool:
    """Rebuild cash from the fills alone and compare it with the account's cash."""
    cash = float(run["initial_capital"])
    for trade in run["trades"]:
        notional = float(trade["qty"]) * float(trade["fill_price"])
        fee = float(trade.get("commission") or 0.0)
        cash += -(notional + fee) if trade["side"] == "buy" else notional - fee
    return math.isclose(cash, float(run["cash"]), rel_tol=0.0, abs_tol=tolerance * float(run["initial_capital"]))


def return_drawdown_ratio(return_pct: float, max_drawdown_pct: float) -> Optional[float]:
    """R1 section 5: return over drawdown; zero for a flat account, undefined for a gain without drawdown."""
    if max_drawdown_pct > 0:
        return return_pct / max_drawdown_pct
    return 0.0 if return_pct == 0 else None


def curve_metrics(equity: pd.Series, initial_capital: float) -> Dict[str, Optional[float]]:
    """Return, drawdown and Sharpe from one equity value per calendar day.

    The engine appends a terminal row a microsecond after the last bar when it closes open
    positions; the report generator then declines to annualise. Keeping the last value of each
    day gives the strategy and the passive accounts the same regular clock.
    """
    series = pd.Series(equity, copy=True).astype(float)
    daily = series.groupby(pd.DatetimeIndex(series.index).normalize()).last()
    drawdown = calculate_drawdown(pd.concat([pd.Series([float(initial_capital)]), daily], ignore_index=True))
    sharpe = calculate_sharpe(equity_period_returns(daily), DAILY_PERIODS_PER_YEAR)
    return {"return_pct": 100.0 * (float(daily.iloc[-1]) / float(initial_capital) - 1.0),
            "max_drawdown_pct": 100.0 * abs(float(drawdown["max_pct"] or 0.0)),
            "sharpe": sharpe["value"]}


def _row(name: str, kind: str, curve: pd.DataFrame, initial_capital: float, *, fills: int, costs: float,
         accounting_ok: bool, extra: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    metrics = curve_metrics(curve["equity"], initial_capital)
    exposure = (curve["gross_exposure"].astype(float) / curve["equity"].astype(float)).mean()
    return {"object": name, "kind": kind, "return_pct": round(float(metrics["return_pct"] or 0.0), 4),
            "max_drawdown_pct": round(float(metrics["max_drawdown_pct"] or 0.0), 4),
            "ratio": return_drawdown_ratio(float(metrics["return_pct"] or 0.0), float(metrics["max_drawdown_pct"] or 0.0)),
            "sharpe": metrics["sharpe"], "avg_gross_exposure": float(exposure), "fills": fills,
            "costs": round(costs, 4), "accounting_ok": accounting_ok, **dict(extra or {})}


def summarize_passive(policy: PassivePolicy, run: Mapping[str, Any]) -> Dict[str, Any]:
    trades, audit = run["trades"], run["execution_audit"]
    costs = sum(float(t.get("commission") or 0.0) + float((t.get("costs") or {}).get("slippage") or 0.0)
                + float((t.get("costs") or {}).get("impact") or 0.0) for t in trades)
    return _row(policy.name, "passive", run["equity_curve"], run["initial_capital"], fills=len(trades), costs=costs,
                accounting_ok=cash_reconciles(run),
                extra={"turnover_notional": round(sum(float(t["qty"]) * float(t["fill_price"]) for t in trades), 2),
                       "resized_orders": sum(1 for r in audit if r.get("outcome") == "resized"),
                       "rejected_orders": sum(1 for r in audit if r.get("outcome") == "rejected")})


def _cash_row(timeline: pd.Index, initial_capital: float) -> Dict[str, Any]:
    curve = pd.DataFrame({"equity": initial_capital, "gross_exposure": 0.0}, index=timeline)
    return _row("cash", "passive", curve, initial_capital, fills=0, costs=0.0, accounting_ok=True)


def run_segment(name: str, window: Tuple[str, str], universe: Any, output_dir: Path = OUTPUT_DIR,
                data_dir: Path = DATA_DIR) -> Dict[str, Any]:
    if name == "final20" or window[1] > evaluated_segments()["validation20"][1]:
        raise ValueError("final20 must not be read")
    data_map, symbols = _load_universe_data(window[0], window[1], data_dir)
    pack = run_arm(f"S1_default_{name}", data_map, engine_kwargs={"universe": universe},
                   return_raw=True, output_dir=output_dir)
    strategy, strategy_curve = pack["summary"], pack["result"]["equity_curve"]
    exposure = float(strategy["exposure"]["avg_gross_exposure"] or 0.0)
    rows = [
        _row("S1 TrendBreakout (default)", "strategy", strategy_curve, INITIAL_CAPITAL,
             fills=strategy["total_fills"], costs=strategy["costs"]["total"],
             accounting_ok=strategy["accounting_ok"],
             extra={"round_trip_trades": strategy["round_trip_trades"]}),
        _cash_row(strategy_curve.index, INITIAL_CAPITAL),
    ]
    policies = [
        PassivePolicy("BTC buy-and-hold", symbols=(BTC,)),
        PassivePolicy("equal weight, monthly rebalance", monthly=True),
        # Not in the R1 draft: the same passive holdings scaled to the strategy's own average exposure.
        PassivePolicy("BTC at S1 average exposure, monthly", gross=min(exposure, 1.0), symbols=(BTC,), monthly=True),
        PassivePolicy("equal weight at S1 average exposure, monthly", gross=min(exposure, 1.0), monthly=True),
    ]
    for policy in policies:
        rows.append(summarize_passive(policy, simulate_passive(policy, data_map)))
    r1_passive = [r for r in rows if r["object"] in {"cash", "BTC buy-and-hold", "equal weight, monthly rebalance"}]
    best = max(r1_passive, key=lambda r: r["ratio"] if r["ratio"] is not None else float("-inf"))
    strategy_ratio = rows[0]["ratio"]
    return {"window": list(window), "symbols": len(symbols), "rows": rows,
            "best_r1_passive": {"object": best["object"], "ratio": best["ratio"]},
            "strategy_ratio_above_best_passive": (strategy_ratio is not None and best["ratio"] is not None
                                                  and strategy_ratio > best["ratio"])}


def format_table(segment: str, result: Mapping[str, Any]) -> str:
    lines = [f"{segment}  {result['window'][0]} ~ {result['window'][1]}  ({result['symbols']} symbols)",
             f"  {'object':46s} {'return':>9s} {'max DD':>8s} {'ratio':>7s} {'Sharpe':>7s} {'avg exp':>8s} {'costs':>9s}"]
    for row in result["rows"]:
        ratio = "n/a" if row["ratio"] is None else f"{row['ratio']:.2f}"
        sharpe = "n/a" if row["sharpe"] is None else f"{row['sharpe']:.2f}"
        lines.append(f"  {row['object']:46s} {row['return_pct']:+8.2f}% {row['max_drawdown_pct']:7.2f}% {ratio:>7s} "
                     f"{sharpe:>7s} {100 * (row['avg_gross_exposure'] or 0.0):7.2f}% {row['costs']:9.2f}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args(argv)
    logging.disable(logging.WARNING)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    universe, universe_overrides = adopted_universe()
    report: Dict[str, Any] = {
        "meta": {"schema": "passive-baseline-diagnostic/v1", "evidence_kind": "retrospective_diagnostic",
                 "formal_admission": False, "r1_protocol_frozen": False,
                 "started_at_utc": datetime.now(timezone.utc).isoformat(),
                 "segments": {k: list(v) for k, v in research_segments().items()},
                 "final20_read": False, "initial_capital": INITIAL_CAPITAL,
                 "cash_buffer": CASH_BUFFER, "min_order_notional": MIN_ORDER_NOTIONAL,
                 "universe_overrides": universe_overrides, **run_identity()},
        "segments": {},
    }
    # run_identity() digests the script it lives in, which supplies the engine arm; add this one.
    report["meta"]["engine_arm_script_sha256_lf"] = report["meta"].pop("script_sha256_lf")
    report["meta"]["script_sha256_lf"] = sha256_lf_bytes(Path(__file__).read_bytes())
    for name, window in evaluated_segments().items():
        print(f"\n--- {name} ---", flush=True)
        report["segments"][name] = run_segment(name, window, universe, args.output_dir)
        print(format_table(name, report["segments"][name]), flush=True)
    out = args.output_dir / "summary.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nSaved {out}", flush=True)


if __name__ == "__main__":
    main()
