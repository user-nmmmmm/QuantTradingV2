"""Controlled Backtest Experiments for Uncovering Actual Trading Problems.

Runs a structured 7-group controlled diagnostic experiment matrix (E1-E7) on the
real 30-symbol Binance 1d dataset using QuantTradingV1's production BacktestEngine,
without mutating config/params.yaml or any baseline files.

Usage:
    python scripts/run_reality_gap_experiments.py
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
import math
import os
import random
import subprocess
import sys
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Suppress verbose INFO logs during batch runs; preserve warnings/errors in audit.
logging.disable(logging.WARNING)

from backtest.delayed_execution import replay_delayed_decisions
from backtest.engine import BacktestEngine
from backtest.reporting import ReportGenerator
from config.config import config
from core.metrics import monte_carlo_trade_sequence
from core.universe import PointInTimeUniverse
from main import _load_local_ohlcv

DATA_DIR = REPO_ROOT / "data" / "binance" / "1d"
UNIVERSE_CSV = REPO_ROOT / "config" / "universe_binance_spot_1d.csv"
OUTPUT_DIR = REPO_ROOT / "outputs" / "reality_gap_experiments"
DEFAULT_START = "2020-01-01"
DEFAULT_END = "2026-08-27"
SEED = 42


def _deep_merge(base: dict, overrides: Mapping[str, Any]) -> dict:
    out = copy.deepcopy(base)
    for k, v in overrides.items():
        if isinstance(v, Mapping) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


@contextmanager
def override_config(overrides: Optional[Mapping[str, Any]] = None):
    """Temporarily override config._config in memory without touching params.yaml."""
    original = copy.deepcopy(config._config)
    try:
        if overrides:
            config._config = _deep_merge(original, overrides)
        yield
    finally:
        config._config = original


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, text=True
        ).strip()
    except Exception:
        return "unknown"


def _dataset_fingerprint(data_dir: Path) -> Dict[str, Any]:
    h = hashlib.sha256()
    files = sorted(data_dir.glob("*.csv"))
    for p in files:
        h.update(p.name.encode("utf-8"))
        h.update(hashlib.sha256(p.read_bytes()).hexdigest().encode("utf-8"))
    manifest_path = data_dir / "_manifest.json"
    manifest_sha = (
        hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        if manifest_path.exists()
        else None
    )
    return {
        "csv_count": len(files),
        "combined_csv_sha256": h.hexdigest(),
        "manifest_sha256": manifest_sha,
    }


def _load_universe_data(start: str, end: str) -> Tuple[Dict[str, pd.DataFrame], List[str]]:
    syms = [
        p.name.replace("_USDT.csv", "-USDT")
        for p in sorted(DATA_DIR.glob("*.csv"))
    ]
    data_map: Dict[str, pd.DataFrame] = {}
    for s in syms:
        df = _load_local_ohlcv(s, start, end, str(DATA_DIR))
        if not df.empty:
            data_map[s] = df
    return data_map, sorted(data_map.keys())


def _safe_float(val: Any) -> Optional[float]:
    if val is None or isinstance(val, bool):
        return None
    try:
        f = float(val)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def run_arm(
    arm_id: str,
    data_map: Dict[str, pd.DataFrame],
    *,
    config_overrides: Optional[Mapping[str, Any]] = None,
    engine_kwargs: Optional[Mapping[str, Any]] = None,
    seed: int = SEED,
    return_raw: bool = False,
) -> Dict[str, Any]:
    """Run a single controlled backtest arm and extract standardized diagnostics."""
    random.seed(seed)
    np.random.seed(seed)
    t0 = time.perf_counter()
    ekw = dict(engine_kwargs or {})
    ekw.setdefault("initial_capital", 10000.0)
    ekw.setdefault("timeframe", "1d")
    ekw.setdefault("calculate_benchmarks", False)

    with override_config(config_overrides):
        engine = BacktestEngine(**ekw)
        result = engine.run(data_map, routing_log_enabled=False)

    elapsed = time.perf_counter() - t0
    rg = ReportGenerator(str(OUTPUT_DIR / "_tmp"))
    metrics = rg.generate(
        trades=result.get("trades") or [],
        equity_curve=result.get("equity_curve"),
        benchmark_curve=result.get("benchmark"),
        metrics_only=True,
        close_events=result.get("close_events"),
        lifecycle=result.get("lifecycle"),
        strategy_health=result.get("strategy_health"),
        protective_stops=result.get("protective_stops"),
        data_quality=result.get("data_quality"),
        event_log=result.get("event_log"),
        max_holding_days=result.get("effective_max_holding_days"),
    )

    trades = result.get("trades") or []
    exec_audit = result.get("execution_audit") or []
    risk_budget_audit = result.get("risk_budget_reconciliation") or []
    drawdown_budget_audit = result.get("drawdown_budget_audit") or []
    stop_summary = result.get("protective_stop_summary") or {}
    lifecycle = result.get("lifecycle") or {}
    accounting = result.get("accounting_check") or {}
    diagnostics = metrics.get("Diagnostics") or {}
    health = (result.get("strategy_health") or {}).get("TrendBreakout") or {}
    equity_curve = result.get("equity_curve")

    total_comm = sum(float(t.get("commission") or 0.0) for t in trades)
    total_slip = sum(float((t.get("costs") or {}).get("slippage") or 0.0) for t in trades)
    total_impact = sum(float((t.get("costs") or {}).get("impact") or 0.0) for t in trades)
    total_financing = sum(
        float(row.get("amount") or 0.0) for row in (result.get("financing_ledger") or [])
    )
    participations = [
        float(t.get("participation_rate") or 0.0)
        for t in trades
        if t.get("participation_rate") is not None
    ]

    rejected = [r for r in exec_audit if r.get("outcome") == "rejected"]
    partials = [r for r in exec_audit if r.get("outcome") == "partial_fill"]
    expired = [r for r in exec_audit if r.get("outcome") == "expired"]
    rejection_reasons = dict(sorted(Counter(str(r.get("reason", "unknown")) for r in rejected).items()))

    gap_resizes = [r for r in risk_budget_audit if r.get("action") == "resize" or r.get("breached")]
    drawdown_reduces = sum(1 for r in drawdown_budget_audit if r.get("action") == "reduce")
    protective_triggers = int(stop_summary.get("triggered_stops") or 0)

    exit_attr = diagnostics.get("exit_attribution") or {}
    pnl_conc = diagnostics.get("pnl_concentration") or {}
    streaks = diagnostics.get("streaks") or {}
    cal_ret = diagnostics.get("calendar_returns") or {}
    life_cov = diagnostics.get("lifecycle_coverage") or {}

    eq_start = str(equity_curve.index[0])[:10] if isinstance(equity_curve, pd.DataFrame) and not equity_curve.empty else None
    eq_end = str(equity_curve.index[-1])[:10] if isinstance(equity_curve, pd.DataFrame) and not equity_curve.empty else None
    eq_bars = int(len(equity_curve)) if isinstance(equity_curve, pd.DataFrame) else 0

    avg_gross_exp = None
    max_gross_exp = None
    pct_invested = None
    if isinstance(equity_curve, pd.DataFrame) and not equity_curve.empty:
        if "gross_exposure" in equity_curve.columns and "equity" in equity_curve.columns:
            ratio = equity_curve["gross_exposure"].astype(float) / equity_curve["equity"].astype(float).replace(0, np.nan)
            avg_gross_exp = _safe_float(ratio.mean())
            max_gross_exp = _safe_float(ratio.max())
            pct_invested = _safe_float((ratio > 0.001).mean())
        elif "cash" in equity_curve.columns and "equity" in equity_curve.columns:
            invested = (equity_curve["equity"].astype(float) - equity_curve["cash"].astype(float)).abs() / equity_curve["equity"].astype(float).replace(0, np.nan)
            avg_gross_exp = _safe_float(invested.mean())
            max_gross_exp = _safe_float(invested.max())
            pct_invested = _safe_float((invested > 0.001).mean())

    buy_symbol_counts = Counter(str(t.get("symbol")) for t in trades if t.get("side") == "buy")

    summary = {
        "arm_id": arm_id,
        "elapsed_sec": round(elapsed, 2),
        "seed": seed,
        "account_mode": result.get("account_mode"),
        "alignment_mode": result.get("alignment_mode"),
        "accounting_ok": bool(accounting.get("ok", False)),
        "accounting_max_diff": _safe_float(accounting.get("max_abs_diff")),
        "date_range": [eq_start, eq_end],
        "bars": eq_bars,
        "initial_capital": float(ekw["initial_capital"]),
        "end_equity": _safe_float(metrics.get("EndEquity")),
        "total_return": _safe_float(metrics.get("TotalReturn")),
        "cagr": _safe_float(metrics.get("CAGR")),
        "cagr_status": metrics.get("CAGRStatus"),
        "sharpe": _safe_float(metrics.get("SharpeRatio")),
        "sharpe_status": metrics.get("SharpeStatus"),
        "max_drawdown": _safe_float(metrics.get("MaxDrawdownPct")),
        "profit_factor": _safe_float(metrics.get("ProfitFactor")),
        "profit_factor_status": metrics.get("ProfitFactorStatus"),
        "profit_factor_ci95": [
            _safe_float(metrics.get("ProfitFactorLower95")),
            _safe_float(metrics.get("ProfitFactorUpper95")),
        ],
        "win_rate": _safe_float(metrics.get("WinRate")),
        "round_trip_trades": int(metrics.get("TotalTrades") or 0),
        "closed_legs": int(metrics.get("ClosedTradeLegs") or 0),
        "total_fills": len(trades),
        "net_pnl": _safe_float(metrics.get("NetPnL")),
        "gross_pnl": _safe_float(metrics.get("GrossPnL")),
        "costs": {
            "commission": round(total_comm, 4),
            "slippage": round(total_slip, 4),
            "impact": round(total_impact, 4),
            "financing": round(total_financing, 4),
            "total": round(total_comm + total_slip + total_impact + total_financing, 4),
        },
        "execution": {
            "avg_participation_rate": round(sum(participations) / len(participations), 6) if participations else 0.0,
            "max_participation_rate_obs": round(max(participations), 6) if participations else 0.0,
            "partial_fills": len(partials),
            "rejected_orders": len(rejected),
            "expired_orders": len(expired),
            "rejection_reasons": rejection_reasons,
            "protective_stop_fills": protective_triggers,
            "gap_risk_resizes": len(gap_resizes),
            "drawdown_budget_reduces": drawdown_reduces,
            "risk_budget_checked": len(risk_budget_audit),
        },
        "lifecycle": {
            "status": lifecycle.get("status"),
            "termination_timestamp": str(lifecycle.get("termination_timestamp")) if lifecycle.get("termination_timestamp") else None,
            "termination_reason": lifecycle.get("termination_reason"),
            "inactive_bars": int(lifecycle.get("inactive_bars") or 0),
            "inactive_days": _safe_float(lifecycle.get("inactive_days")),
            "resume_count": int(lifecycle.get("resume_count") or 0),
            "health_gated_days": _safe_float(lifecycle.get("health_gated_days")),
            "raw_setups": int(health.get("raw_setup_count") or 0),
            "suppressed_raw_setups": int(health.get("suppressed_raw_setups") or 0),
            "final_health_status": health.get("status"),
            "final_risk_multiplier": _safe_float(health.get("risk_multiplier")),
            "health_transitions": len(lifecycle.get("health_transition_log") or []),
            "health_transition_log": (lifecycle.get("health_transition_log") or [])[:12],
        },
        "exposure": {
            "avg_gross_exposure": avg_gross_exp,
            "max_gross_exposure": max_gross_exp,
            "pct_bars_invested": pct_invested,
        },
        "diagnostics": {
            "own_exit_ratio": _safe_float(exit_attr.get("own_exit_ratio")),
            "exit_by_reason": dict(exit_attr.get("by_reason") or {}),
            "inert_exit_logic": list(exit_attr.get("inert_exit_logic") or []),
            "pnl_concentration_top_n": pnl_conc.get("top_n") or {},
            "profit_hhi": _safe_float(pnl_conc.get("profit_hhi")),
            "longest_losing_streak": (streaks.get("longest_losing") or {}).get("length"),
            "longest_winning_streak": (streaks.get("longest_winning") or {}).get("length"),
            "lifecycle_coverage": life_cov,
            "calendar_years": cal_ret.get("yearly") or {},
            "warnings": list(diagnostics.get("warnings") or []),
        },
        "top_buy_symbols": buy_symbol_counts.most_common(6),
    }

    print(
        f"  [{arm_id:32s}] {elapsed:5.1f}s | acct={summary['accounting_ok']} | "
        f"ret={fmt_pct(summary['total_return']):>8s} | mdd={fmt_pct(summary['max_drawdown']):>7s} | "
        f"sharpe={fmt_num(summary['sharpe']):>6s} | pf={fmt_num(summary['profit_factor']):>5s}({summary['profit_factor_status']}) | "
        f"rt={summary['round_trip_trades']:3d} | life={summary['lifecycle']['status']}",
        flush=True,
    )
    if return_raw:
        return {"summary": summary, "result": result, "metrics": metrics}
    return summary


def fmt_pct(v: Optional[float]) -> str:
    return "N/A" if v is None else f"{v * 100:+.2f}%"


def fmt_num(v: Optional[float]) -> str:
    return "N/A" if v is None else f"{v:.2f}"


def run_delayed_execution_diagnostic(
    data_map: Dict[str, pd.DataFrame], anchor_result: Dict[str, Any]
) -> Dict[str, Any]:
    """Use backtest.delayed_execution to measure real-world decision/polling latency impact."""
    trades = anchor_result.get("trades") or []
    # Extract all spot-compatible entry/exit signals (buy/sell) from anchor fills
    # and replay them under 0-bar delay (next open) vs +1-day (polling/outage delay) vs +2-day delay.
    base_decisions = []
    for idx, t in enumerate(trades):
        side = str(t.get("side") or "").lower()
        sym = str(t.get("symbol") or "")
        if side not in {"buy", "sell"} or sym not in data_map:
            continue
        fill_ts = pd.Timestamp(t.get("fill_time") or t.get("timestamp"))
        sig_ts = fill_ts - pd.Timedelta(days=1)
        qty = float(t.get("qty") or 0.0)
        ref_price = float(t.get("theoretical_price") or t.get("fill_price") or 0.0)
        if qty <= 0 or ref_price <= 0:
            continue
        base_decisions.append({
            "decision_id": f"dec-{idx:04d}-{sym}-{side}",
            "symbol": sym,
            "side": side,
            "quantity": qty,
            "reference_price": ref_price,
            "signal_time": sig_ts,
            "signal_timeframe": "1d",
            "available_at_base": fill_ts,
        })

    if not base_decisions:
        return {"status": "insufficient", "decisions": 0}

    # Filter frames to symbols present in decisions
    used_syms = sorted({d["symbol"] for d in base_decisions})
    frames = {s: data_map[s] for s in used_syms}

    out = {"decision_count": len(base_decisions), "delay_days": {}}
    for delay_days in (0, 1, 2):
        decs = []
        for d in base_decisions:
            avail = d["available_at_base"] + pd.Timedelta(days=delay_days)
            decs.append({
                "decision_id": d["decision_id"],
                "symbol": d["symbol"],
                "side": d["side"],
                "quantity": d["quantity"],
                "reference_price": d["reference_price"],
                "signal_time": d["signal_time"].isoformat(),
                "signal_timeframe": "1d",
                "available_at": avail.isoformat(),
            })
        rep = replay_delayed_decisions(
            frames,
            decs,
            initial_capital=10000.0,
            execution_timeframe="1d",
            commission_rate=0.001,
            slippage=0.0005,
            spread_bps=2.0,
            max_participation_rate=0.05,
        )
        # Measure price drift from reference_price across filled entries/exits
        buy_drifts = []
        sell_drifts = []
        for f in rep["fills"]:
            ref = float(f.get("theoretical_price") or 0.0)
            # Match original decision reference price
            dec_match = next((x for x in base_decisions if x["symbol"] == f["symbol"] and x["side"] == f["side"]), None)
            if dec_match and dec_match["reference_price"] > 0:
                drift = (float(f["fill_price"]) - dec_match["reference_price"]) / dec_match["reference_price"]
                if f["side"] == "buy":
                    buy_drifts.append(drift)
                else:
                    sell_drifts.append(drift)
        out["delay_days"][str(delay_days)] = {
            "final_equity": round(float(rep["final_equity"]), 2),
            "total_return": round(float(rep["final_equity"]) / 10000.0 - 1.0, 6),
            "fills": len(rep["fills"]),
            "unexecuted": len(rep["unexecuted_decisions"]),
            "rejected_or_unfilled": sum(1 for st in rep["order_statuses"].values() if str(st).lower() != "filled"),
            "no_fill_before_decision": rep["no_fill_before_decision"],
        }
    return out


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now(timezone.utc).isoformat()
    t_all = time.perf_counter()

    print("=== QuantTradingV1 Reality-Gap Backtest Experiment Suite ===", flush=True)
    universe = PointInTimeUniverse.from_csv(UNIVERSE_CSV)
    data_map, symbols = _load_universe_data(DEFAULT_START, DEFAULT_END)
    print(f"Loaded {len(symbols)} symbols ({DEFAULT_START} ~ {DEFAULT_END})", flush=True)

    # ------------------------------------------------------------------
    # Anchor Baseline (Production config on 30-symbol PIT universe)
    # ------------------------------------------------------------------
    print("\n--- Anchor Baseline (Production Config: spot_margin, 1x cost, PIT union) ---", flush=True)
    anchor_pack = run_arm(
        "ANCHOR_prod_baseline",
        data_map,
        engine_kwargs={"universe": universe, "calculate_benchmarks": True},
        return_raw=True,
    )
    anchor = anchor_pack["summary"]
    anchor_raw = anchor_pack["result"]
    anchor_metrics = anchor_pack["metrics"]

    # Also run a clean SPOT anchor (for pure spot comparisons without margin leverage)
    spot_cfg = {
        "account": {"mode": "spot"},
        "execution": {
            "fee_schedule": {
                "venue": "binance",
                "market_type": "spot",
                "source": "Binance spot standard tier (taker 0.10%, maker 0.10%)",
            }
        },
    }

    report: Dict[str, Any] = {
        "meta": {
            "started_at_utc": started_at,
            "git_commit": _git_commit(),
            "seed": SEED,
            "window": [DEFAULT_START, DEFAULT_END],
            "symbol_count": len(symbols),
            "dataset_fingerprint": _dataset_fingerprint(DATA_DIR),
            "benchmark_fixed_return": _safe_float(anchor_metrics.get("BenchmarkTotalReturn")),
        },
        "anchor": anchor,
        "experiments": {},
    }

    # ------------------------------------------------------------------
    # E1: Execution Friction & Cost Decay Stress Test
    # ------------------------------------------------------------------
    print("\n--- E1: Execution Friction & Cost Decay Stress Test ---", flush=True)
    e1 = {}
    e1["E1_ideal_zero_cost"] = run_arm(
        "E1_ideal_zero_cost",
        data_map,
        config_overrides={
            "execution": {
                "commission_rate_taker": 0.0,
                "commission_rate_maker": 0.0,
                "slippage_bps": 0.0,
                "spread_bps": 0.0,
                "volatility_slippage_factor": 0.0,
                "use_impact_cost": False,
            }
        },
        engine_kwargs={"universe": universe, "slippage": 0.0},
    )
    e1["E1_baseline_1x"] = anchor
    for mult, label in [(1.5, "E1_stress_1.5x"), (2.0, "E1_stress_2.0x"), (3.0, "E1_stress_3.0x")]:
        e1[label] = run_arm(
            label,
            data_map,
            config_overrides={
                "execution": {
                    "commission_rate_taker": 0.001 * mult,
                    "commission_rate_maker": 0.001 * mult,
                    "slippage_bps": 5.0 * mult,
                    "spread_bps": 2.0 * mult,
                    "volatility_slippage_factor": 0.02 * mult,
                    "impact_coefficient": 0.10 * mult,
                }
            },
            engine_kwargs={"universe": universe},
        )
    e1["E1_random_slip_2x_s42"] = run_arm(
        "E1_random_slip_2x_s42",
        data_map,
        config_overrides={
            "execution": {
                "slippage_bps": 10.0,
                "spread_bps": 4.0,
                "volatility_slippage_factor": 0.04,
            }
        },
        engine_kwargs={"universe": universe, "random_slip": True},
        seed=42,
    )
    report["experiments"]["E1_cost_friction"] = e1

    # ------------------------------------------------------------------
    # E2: Stop Execution, Gap Risk & Delayed Execution Reality Gap
    # ------------------------------------------------------------------
    print("\n--- E2: Intrabar Stops, Gap Risk Resize & Execution Delay ---", flush=True)
    e2 = {}
    e2["E2_resident_stop_anchor"] = anchor
    e2["E2_legacy_close_stop"] = run_arm(
        "E2_legacy_close_stop",
        data_map,
        config_overrides={"protective_orders": {"backtest_resident": False}},
        engine_kwargs={"universe": universe},
    )
    e2["E2_no_gap_risk_resize"] = run_arm(
        "E2_no_gap_risk_resize",
        data_map,
        config_overrides={"entry_risk": {"enabled": False}},
        engine_kwargs={"universe": universe},
    )
    e2["E2_atr_hybrid_trailing_stop"] = run_arm(
        "E2_atr_hybrid_trailing_stop",
        data_map,
        config_overrides={
            "stops": {
                "use_atr_initial_stop": True,
                "use_trailing_stop": True,
            }
        },
        engine_kwargs={"universe": universe},
    )
    e2["E2_delayed_decision_replay"] = run_delayed_execution_diagnostic(data_map, anchor_raw)
    print(f"  [E2_delayed_decision_replay      ] {e2['E2_delayed_decision_replay']}", flush=True)
    report["experiments"]["E2_stop_gap_latency"] = e2

    # ------------------------------------------------------------------
    # E3: Capital Capacity & Liquidity Participation Trap
    # ------------------------------------------------------------------
    print("\n--- E3: Capital Capacity & Liquidity Participation Curve ---", flush=True)
    e3 = {}
    e3["E3_cap_10k"] = anchor
    for cap, label in [
        (100_000.0, "E3_cap_100k"),
        (1_000_000.0, "E3_cap_1m"),
        (10_000_000.0, "E3_cap_10m"),
        (50_000_000.0, "E3_cap_50m"),
    ]:
        e3[label] = run_arm(
            label,
            data_map,
            engine_kwargs={"initial_capital": cap, "universe": universe},
        )
    report["experiments"]["E3_capital_capacity"] = e3

    # ------------------------------------------------------------------
    # E4: Account Semantics, Leverage, Drawdown Breaker & Health Lockout
    # ------------------------------------------------------------------
    print("\n--- E4: Account Mode, Risk Breakers & Strategy Health Lockout ---", flush=True)
    e4 = {}
    e4["E4_margin_anchor"] = anchor
    e4["E4_spot_unlevered"] = run_arm(
        "E4_spot_unlevered",
        data_map,
        config_overrides=spot_cfg,
        engine_kwargs={"account_mode": "spot", "universe": universe},
    )
    e4["E4_no_health_gate"] = run_arm(
        "E4_no_health_gate",
        data_map,
        config_overrides={"strategy_health": {"enabled": False}},
        engine_kwargs={"universe": universe},
    )
    e4["E4_no_drawdown_budget"] = run_arm(
        "E4_no_drawdown_budget",
        data_map,
        config_overrides={"drawdown_budget": {"enabled": False}},
        engine_kwargs={"universe": universe},
    )
    e4["E4_breaker_timed_recovery"] = run_arm(
        "E4_breaker_timed_recovery",
        data_map,
        engine_kwargs={
            "universe": universe,
            "breaker_policy": {
                "on_liquidate": "cooldown",
                "recovery": {
                    "mode": "timed_rebase",
                    "flat_bars_required": 30,
                    "health_bars_required": 5,
                    "rebase_high_water": True,
                    "max_resumes": 5,
                },
            },
        },
    )
    e4["E4_raw_no_breaker_no_health"] = run_arm(
        "E4_raw_no_breaker_no_health",
        data_map,
        config_overrides={
            "strategy_health": {"enabled": False},
            "drawdown_budget": {"enabled": False},
            "drawdown": {
                "daily_loss_limit": 0.99,
                "reduce_threshold": 0.96,
                "block_threshold": 0.97,
                "liquidate_threshold": 0.98,
                "lock_threshold": 0.99,
            },
        },
        engine_kwargs={"universe": universe},
    )
    report["experiments"]["E4_account_risk_lifecycle"] = e4

    # ------------------------------------------------------------------
    # E4b: Downstream Risk Controls Under Active Full-Size Deployment
    # (Exposes what cluster caps, scoring, gap-resize, and margin actually do
    #  when strategy_health isn't suppressing risk to 0.10x)
    # ------------------------------------------------------------------
    print("\n--- E4b: Active-Risk Controls (With Health Gate Disabled) ---", flush=True)
    e4b = {}
    e4b["E4b_full_risk_base"] = e4["E4_no_health_gate"]
    e4b["E4b_full_risk_spot"] = run_arm(
        "E4b_full_risk_spot",
        data_map,
        config_overrides=_deep_merge(spot_cfg, {"strategy_health": {"enabled": False}}),
        engine_kwargs={"account_mode": "spot", "universe": universe},
    )
    e4b["E4b_full_risk_no_cluster"] = run_arm(
        "E4b_full_risk_no_cluster",
        data_map,
        config_overrides={
            "strategy_health": {"enabled": False},
            "portfolio_risk": {"enabled": False},
        },
        engine_kwargs={"universe": universe},
    )
    e4b["E4b_full_risk_no_scoring"] = run_arm(
        "E4b_full_risk_no_scoring",
        data_map,
        config_overrides={
            "strategy_health": {"enabled": False},
            "candidate_scoring": {"enabled": False},
        },
        engine_kwargs={"universe": universe},
    )
    e4b["E4b_full_risk_no_gap_resize"] = run_arm(
        "E4b_full_risk_no_gap_resize",
        data_map,
        config_overrides={
            "strategy_health": {"enabled": False},
            "entry_risk": {"enabled": False},
        },
        engine_kwargs={"universe": universe},
    )
    report["experiments"]["E4b_active_risk_controls"] = e4b

    # ------------------------------------------------------------------
    # E5: Survivorship, Delisting, Alignment & Capital Contention
    # ------------------------------------------------------------------
    print("\n--- E5: Universe, Alignment, Cluster Risk & Scoring Biases ---", flush=True)
    e5 = {}
    e5["E5_pit_union_anchor"] = anchor
    e5["E5_static_no_pit"] = run_arm(
        "E5_static_no_pit",
        data_map,
        engine_kwargs={"universe": None},
    )
    e5["E5_intersection_align"] = run_arm(
        "E5_intersection_align",
        data_map,
        engine_kwargs={"universe": universe, "alignment_mode": "intersection"},
    )
    e5["E5_no_cluster_risk"] = run_arm(
        "E5_no_cluster_risk",
        data_map,
        config_overrides={"portfolio_risk": {"enabled": False}},
        engine_kwargs={"universe": universe},
    )
    e5["E5_no_scoring_alphabetical"] = run_arm(
        "E5_no_scoring_alphabetical",
        data_map,
        config_overrides={"candidate_scoring": {"enabled": False}},
        engine_kwargs={"universe": universe},
    )
    e5["E5_smart_capital_alloc"] = run_arm(
        "E5_smart_capital_alloc",
        data_map,
        engine_kwargs={
            "universe": universe,
            "capital_allocation": {"enabled": True},
        },
    )
    # Full raw history 2017-08-17 ~ 2026-09-19 to expose actual repo data mismatch
    full_data_map, _ = _load_universe_data("2017-08-17", "2026-09-19")
    e5["E5_raw_2017_2026_coverage_trap"] = run_arm(
        "E5_raw_2017_2026_coverage_trap",
        full_data_map,
        engine_kwargs={"universe": universe},
    )
    report["experiments"]["E5_universe_alignment_allocation"] = e5

    # ------------------------------------------------------------------
    # E6: Market Regime / Cycle Breakdown & Monte Carlo Path Risk
    # ------------------------------------------------------------------
    print("\n--- E6: Market Cycle Breakdown & Monte Carlo Trade Sequence ---", flush=True)
    e6 = {}
    cycle_windows = [
        ("E6_bull_2020_2021", "2020-01-01", "2021-11-30"),
        ("E6_bear_2022", "2021-11-01", "2022-12-31"),
        ("E6_chop_2023_2024", "2023-01-01", "2024-12-31"),
        ("E6_recent_2025_2026", "2025-01-01", "2026-08-27"),
    ]
    for label, w_start, w_end in cycle_windows:
        w_data, _ = _load_universe_data(w_start, w_end)
        e6[label] = run_arm(label, w_data, engine_kwargs={"universe": universe})

    # Monte Carlo trade-order permutation on anchor closed round-trip PnLs
    rg_tmp = ReportGenerator(str(OUTPUT_DIR / "_tmp"))
    trades_df = pd.DataFrame(anchor_raw.get("trades") or [])
    closed_legs = [
        r for r in rg_tmp._reconstruct_closed_trades(trades_df)
        if r.get("status") != "invalid_input"
    ]
    round_trips = rg_tmp._aggregate_round_trips(closed_legs)
    rt_pnls = [float(r["net_pnl"]) for r in round_trips if r.get("net_pnl") is not None]
    e6["monte_carlo_anchor"] = monte_carlo_trade_sequence(
        rt_pnls, n_simulations=2000, seed=SEED
    )
    print(f"  [E6_monte_carlo_anchor           ] n_trades={len(rt_pnls)} mc={e6['monte_carlo_anchor']}", flush=True)
    report["experiments"]["E6_cycle_and_diagnostics"] = e6

    # ------------------------------------------------------------------
    # E7: IS vs OOS Parameter Fragility & Overfitting Surface (Purged Split)
    # ------------------------------------------------------------------
    print("\n--- E7: IS vs OOS Parameter Sensitivity & Overfitting Surface ---", flush=True)
    is_data, _ = _load_universe_data("2020-01-01", "2023-06-30")
    oos_data, _ = _load_universe_data("2023-08-01", "2026-08-27")  # 31-day purge/embargo
    param_grid = [
        (10, 5), (15, 5), (15, 10),
        (20, 5), (20, 10), (20, 15),
        (30, 10), (30, 15), (40, 20),
    ]
    e7_rows = []
    for ew, xw in param_grid:
        p_cfg = {
            "research": {
                "experiment_id": f"e7_grid_{ew}_{xw}",
                "trend_breakout_parameters": {"entry_window": ew, "exit_window": xw},
            }
        }
        r_is = run_arm(f"E7_IS_{ew}_{xw}", is_data, config_overrides=p_cfg, engine_kwargs={"universe": universe})
        r_oos = run_arm(f"E7_OOS_{ew}_{xw}", oos_data, config_overrides=p_cfg, engine_kwargs={"universe": universe})
        e7_rows.append({
            "entry_window": ew,
            "exit_window": xw,
            "is_return": r_is["total_return"],
            "is_sharpe": r_is["sharpe"],
            "is_mdd": r_is["max_drawdown"],
            "is_pf": r_is["profit_factor"],
            "is_trades": r_is["round_trip_trades"],
            "is_lifecycle": r_is["lifecycle"]["status"],
            "oos_return": r_oos["total_return"],
            "oos_sharpe": r_oos["sharpe"],
            "oos_mdd": r_oos["max_drawdown"],
            "oos_pf": r_oos["profit_factor"],
            "oos_trades": r_oos["round_trip_trades"],
            "oos_lifecycle": r_oos["lifecycle"]["status"],
        })
    # Spearman rank correlation between IS return and OOS return
    is_s = pd.Series([r["is_return"] for r in e7_rows], dtype=float)
    oos_s = pd.Series([r["oos_return"] for r in e7_rows], dtype=float)
    spearman = _safe_float(is_s.corr(oos_s, method="spearman"))
    best_is = max(e7_rows, key=lambda r: r["is_return"] if r["is_return"] is not None else -1e9)
    report["experiments"]["E7_parameter_is_oos"] = {
        "is_window": ["2020-01-01", "2023-06-30"],
        "oos_window": ["2023-08-01", "2026-08-27"],
        "purge_days": 31,
        "grid": e7_rows,
        "spearman_is_vs_oos_return": spearman,
        "best_is_param": {
            "entry_window": best_is["entry_window"],
            "exit_window": best_is["exit_window"],
            "is_return": best_is["is_return"],
            "oos_return": best_is["oos_return"],
            "is_sharpe": best_is["is_sharpe"],
            "oos_sharpe": best_is["oos_sharpe"],
        },
        "positive_is_count": sum(1 for r in e7_rows if (r["is_return"] or 0) > 0),
        "positive_oos_count": sum(1 for r in e7_rows if (r["oos_return"] or 0) > 0),
    }

    report["meta"]["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    report["meta"]["total_elapsed_sec"] = round(time.perf_counter() - t_all, 2)

    out_json = OUTPUT_DIR / "summary.json"
    out_json.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nSaved complete JSON report to {out_json} (total {report['meta']['total_elapsed_sec']}s)", flush=True)


if __name__ == "__main__":
    main()
