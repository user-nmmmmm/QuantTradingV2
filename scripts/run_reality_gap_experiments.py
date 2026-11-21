"""Controlled Backtest Experiments for Uncovering Actual Trading Problems.

Runs a structured controlled diagnostic experiment matrix (E1-E7 + E4b) on the
adopted 30-symbol Binance 1d dataset using QuantTradingV1's production
BacktestEngine, without mutating config/params.yaml or any baseline files.

Data is read through the standard local research entry (``cli.data``), so every
symbol is limited to its adopted research end (ADR 0041). The results are
retrospective diagnostics over dates that include validation20 and final20:
they may support process changes, never parameter or candidate selection.

Usage:
    python scripts/run_reality_gap_experiments.py                 # every group
    python scripts/run_reality_gap_experiments.py --groups E1 E3  # anchor + chosen groups
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
import math
import random
import subprocess
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from backtest.delayed_execution import replay_delayed_decisions
from backtest.engine import BacktestEngine
from backtest.reporting import ReportGenerator
from cli.data import _load_local_ohlcv
from config.config import config
from core.foundation.content_hash import sha256_lf_bytes
from core.market.universe import PointInTimeUniverse, UniverseMembership
from core.metrics import monte_carlo_trade_sequence

REPO_ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = REPO_ROOT / "data" / "binance" / "1d"
UNIVERSE_CSV = REPO_ROOT / "config" / "universe_binance_spot_1d.csv"
PARAMS_YAML = REPO_ROOT / "config" / "params.yaml"
OUTPUT_DIR = REPO_ROOT / "outputs" / "reality_gap_experiments"
DEFAULT_START = "2020-01-01"
DEFAULT_END = "2026-08-26"  # common adopted research end (ADR 0041)
SEED = 42
GROUPS = ("E1", "E2", "E3", "E4", "E4b", "E5", "E6", "E7")

# Calendar windows: contiguous and non-overlapping, so each label says what it covers.
# Every window starts from fresh capital and fresh indicator history, so its first
# weeks cannot trade (engine warm-up plus the 60-bar state-machine history).
CYCLE_WINDOWS = (
    ("E6_bull_2020_2021", "2020-01-01", "2021-12-31"),
    ("E6_bear_2022", "2022-01-01", "2022-12-31"),
    ("E6_chop_2023_2024", "2023-01-01", "2024-12-31"),
    ("E6_recent_2025_2026", "2025-01-01", DEFAULT_END),
)
IS_WINDOW = ("2020-01-01", "2023-06-30")
OOS_WINDOW = ("2023-08-01", DEFAULT_END)  # 31-day purge/embargo after the IS window
PARAM_GRID = ((10, 5), (15, 5), (15, 10), (20, 5), (20, 10), (20, 15), (30, 10), (30, 15), (40, 20))
# Requests the standard entry must refuse (symbol, end): beyond the common end,
# the raw BTC download end, and MATIC beyond its own adopted end.
GATE_PROBES = (("ADA-USDT", "2026-08-27"), ("BTC-USDT", "2026-09-19"), ("MATIC-USDT", "2026-08-26"))

SPOT_CFG: Dict[str, Any] = {
    "account": {"mode": "spot"},
    "execution": {
        "fee_schedule": {
            "venue": "binance",
            "market_type": "spot",
            "source": "Binance spot standard tier (taker 0.10%, maker 0.10%)",
        }
    },
}


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


def _git(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], cwd=REPO_ROOT, text=True).strip()
    except Exception:
        return "unknown"


def _research_policy(data_dir: Path) -> Dict[str, Any]:
    path = data_dir / "_research_policy.json"
    return json.loads(path.read_bytes()) if path.exists() else {}


def _dataset_fingerprint(data_dir: Path) -> Dict[str, Any]:
    """Newline-normalised digests, so Windows and Linux checkouts agree."""
    h = hashlib.sha256()
    files = sorted(data_dir.glob("*.csv"))
    for p in files:
        h.update(p.name.encode("utf-8"))
        h.update(sha256_lf_bytes(p.read_bytes()).encode("utf-8"))
    policy = _research_policy(data_dir)
    return {
        "csv_count": len(files),
        "combined_csv_sha256_lf": h.hexdigest(),
        "research_policy_receipt": policy.get("receipt"),
        "research_policy_receipt_sha256": policy.get("receipt_sha256"),
        "common_research_end_inclusive": policy.get("common_research_end_inclusive"),
    }


def run_identity(data_dir: Path = DATA_DIR) -> Dict[str, Any]:
    """What was run: source, configuration and data identities of this invocation."""
    status = _git("status", "--porcelain")
    return {
        "git_commit": _git("rev-parse", "--short", "HEAD"),
        "git_dirty": None if status == "unknown" else bool(status),
        "script_sha256_lf": sha256_lf_bytes(Path(__file__).read_bytes()),
        "params_sha256_lf": sha256_lf_bytes(PARAMS_YAML.read_bytes()),
        "universe_csv_sha256_lf": sha256_lf_bytes(UNIVERSE_CSV.read_bytes()),
        "dataset_fingerprint": _dataset_fingerprint(data_dir),
    }


def adopted_end(csv_path: Path, end: str) -> str:
    """Clamp ``end`` to the file's adopted research end; unregistered folders pass through."""
    entry = _research_policy(csv_path.parent).get("files", {}).get(csv_path.name) or {}
    cutoff = entry.get("research_end_inclusive")
    return min(end, cutoff) if isinstance(cutoff, str) else end


def adopted_universe(
    universe_csv: Path = UNIVERSE_CSV, data_dir: Path = DATA_DIR
) -> Tuple[PointInTimeUniverse, List[Dict[str, str]]]:
    """Point-in-time universe whose membership ends agree with the adopted data.

    The tracked universe file predates the data adoption: it ends MATIC's membership
    on 2025-03-25 while the adopted file stops at 2024-09-09 (delisted 2024-09-10), and
    the engine refuses a membership whose last legal exit bar is missing. For a symbol
    whose adopted end is before the common end, membership ends the day after its last
    adopted bar. The tracked file is not modified; every change is returned for the record.
    """
    policy = _research_policy(data_dir)
    common_end = policy.get("common_research_end_inclusive")
    memberships, overrides = [], []
    for row in pd.read_csv(universe_csv, dtype=str, keep_default_na=False).to_dict("records"):
        symbol, delisted = row["symbol"], row.get("delisted_at") or None
        entry = policy.get("files", {}).get(symbol.replace("-", "_") + ".csv") or {}
        cutoff = entry.get("research_end_inclusive")
        if isinstance(cutoff, str) and isinstance(common_end, str) and cutoff < common_end:
            adopted = (pd.Timestamp(cutoff) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            if delisted != adopted:
                overrides.append({"symbol": symbol, "tracked_delisted_at": delisted or "",
                                  "used_delisted_at": adopted, "adopted_research_end": cutoff})
                delisted = adopted
        memberships.append(UniverseMembership(
            symbol=symbol, listed_at=pd.Timestamp(row["listed_at"]),
            delisted_at=pd.Timestamp(delisted) if delisted else None,
            source=row.get("source") or universe_csv.name))
    return PointInTimeUniverse(memberships), overrides


def _load_universe_data(
    start: str, end: str, data_dir: Path = DATA_DIR
) -> Tuple[Dict[str, pd.DataFrame], List[str]]:
    data_map: Dict[str, pd.DataFrame] = {}
    for path in sorted(data_dir.glob("*.csv")):
        symbol = path.name.replace("_USDT.csv", "-USDT")
        symbol_end = adopted_end(path, end)
        if symbol_end < start:
            continue
        df = _load_local_ohlcv(symbol, start, symbol_end, str(data_dir))
        if not df.empty:
            data_map[symbol] = df
    return data_map, sorted(data_map.keys())


def verify_research_window_gate(data_dir: Path = DATA_DIR) -> Dict[str, Any]:
    """Confirm the standard entry refuses requests beyond the adopted research window.

    Replaces the former ``E5_raw_2017_2026_coverage_trap`` arm, which read the raw
    files past their adopted end to show how misaligned coverage inflated returns.
    """
    probes = []
    for symbol, end in GATE_PROBES:
        try:
            _load_local_ohlcv(symbol, DEFAULT_START, end, str(data_dir))
        except ValueError as exc:
            probes.append({"symbol": symbol, "end": end, "rejected": True, "error": str(exc)})
        else:
            probes.append({"symbol": symbol, "end": end, "rejected": False, "error": None})
    ok = all(p["rejected"] for p in probes)
    return {"status": "rejected_as_expected" if ok else "gate_missing", "probes": probes}


def _safe_float(val: Any) -> Optional[float]:
    if val is None or isinstance(val, bool):
        return None
    try:
        f = float(val)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def fmt_pct(v: Optional[float]) -> str:
    return "N/A" if v is None else f"{v * 100:+.2f}%"


def fmt_num(v: Optional[float]) -> str:
    return "N/A" if v is None else f"{v:.2f}"


def _exposure_stats(equity_curve: Any) -> Dict[str, Optional[float]]:
    out: Dict[str, Optional[float]] = {
        "avg_gross_exposure": None, "max_gross_exposure": None, "pct_bars_invested": None,
    }
    if not isinstance(equity_curve, pd.DataFrame) or equity_curve.empty or "equity" not in equity_curve.columns:
        return out
    equity = equity_curve["equity"].astype(float).replace(0, np.nan)
    if "gross_exposure" in equity_curve.columns:
        ratio = equity_curve["gross_exposure"].astype(float) / equity
    elif "cash" in equity_curve.columns:
        ratio = (equity_curve["equity"].astype(float) - equity_curve["cash"].astype(float)).abs() / equity
    else:
        return out
    return {
        "avg_gross_exposure": _safe_float(ratio.mean()),
        "max_gross_exposure": _safe_float(ratio.max()),
        "pct_bars_invested": _safe_float((ratio > 0.001).mean()),
    }


def _execution_stats(result: Mapping[str, Any], trades: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    exec_audit = result.get("execution_audit") or []
    risk_budget_audit = result.get("risk_budget_reconciliation") or []
    drawdown_budget_audit = result.get("drawdown_budget_audit") or []
    participations = [
        float(t.get("participation_rate") or 0.0) for t in trades if t.get("participation_rate") is not None
    ]
    rejected = [r for r in exec_audit if r.get("outcome") == "rejected"]
    return {
        "avg_participation_rate": round(sum(participations) / len(participations), 6) if participations else 0.0,
        "max_participation_rate_obs": round(max(participations), 6) if participations else 0.0,
        "partial_fills": sum(1 for r in exec_audit if r.get("outcome") == "partial_fill"),
        "rejected_orders": len(rejected),
        "expired_orders": sum(1 for r in exec_audit if r.get("outcome") == "expired"),
        "rejection_reasons": dict(sorted(Counter(str(r.get("reason", "unknown")) for r in rejected).items())),
        "protective_stop_fills": int((result.get("protective_stop_summary") or {}).get("triggered_stops") or 0),
        "gap_risk_resizes": sum(1 for r in risk_budget_audit if r.get("action") == "resize" or r.get("breached")),
        "drawdown_budget_reduces": sum(1 for r in drawdown_budget_audit if r.get("action") == "reduce"),
        "risk_budget_checked": len(risk_budget_audit),
    }


def _lifecycle_stats(result: Mapping[str, Any]) -> Dict[str, Any]:
    lifecycle = result.get("lifecycle") or {}
    health = (result.get("strategy_health") or {}).get("TrendBreakout") or {}
    transitions = lifecycle.get("health_transition_log") or []
    termination = lifecycle.get("termination_timestamp")
    return {
        "status": lifecycle.get("status"),
        "termination_timestamp": str(termination) if termination else None,
        "termination_reason": lifecycle.get("termination_reason"),
        "inactive_bars": int(lifecycle.get("inactive_bars") or 0),
        "inactive_days": _safe_float(lifecycle.get("inactive_days")),
        "resume_count": int(lifecycle.get("resume_count") or 0),
        "health_gated_days": _safe_float(lifecycle.get("health_gated_days")),
        "raw_setups": int(health.get("raw_setup_count") or 0),
        "suppressed_raw_setups": int(health.get("suppressed_raw_setups") or 0),
        "final_health_status": health.get("status"),
        "final_risk_multiplier": _safe_float(health.get("risk_multiplier")),
        "health_transitions": len(transitions),
        "health_transition_log": transitions[:12],
    }


def _diagnostic_stats(metrics: Mapping[str, Any]) -> Dict[str, Any]:
    diagnostics = metrics.get("Diagnostics") or {}
    exit_attr = diagnostics.get("exit_attribution") or {}
    pnl_conc = diagnostics.get("pnl_concentration") or {}
    streaks = diagnostics.get("streaks") or {}
    return {
        "own_exit_ratio": _safe_float(exit_attr.get("own_exit_ratio")),
        "exit_by_reason": dict(exit_attr.get("by_reason") or {}),
        "inert_exit_logic": list(exit_attr.get("inert_exit_logic") or []),
        "pnl_concentration_top_n": pnl_conc.get("top_n") or {},
        "profit_hhi": _safe_float(pnl_conc.get("profit_hhi")),
        "longest_losing_streak": (streaks.get("longest_losing") or {}).get("length"),
        "longest_winning_streak": (streaks.get("longest_winning") or {}).get("length"),
        "lifecycle_coverage": diagnostics.get("lifecycle_coverage") or {},
        "calendar_years": (diagnostics.get("calendar_returns") or {}).get("yearly") or {},
        "warnings": list(diagnostics.get("warnings") or []),
    }


def _summarize(arm_id: str, result: Mapping[str, Any], metrics: Mapping[str, Any], *,
               initial_capital: float, seed: int, elapsed: float) -> Dict[str, Any]:
    trades = result.get("trades") or []
    accounting = result.get("accounting_check") or {}
    equity_curve = result.get("equity_curve")
    has_curve = isinstance(equity_curve, pd.DataFrame) and not equity_curve.empty
    total_comm = sum(float(t.get("commission") or 0.0) for t in trades)
    total_slip = sum(float((t.get("costs") or {}).get("slippage") or 0.0) for t in trades)
    total_impact = sum(float((t.get("costs") or {}).get("impact") or 0.0) for t in trades)
    total_financing = sum(float(row.get("amount") or 0.0) for row in (result.get("financing_ledger") or []))
    buy_symbol_counts = Counter(str(t.get("symbol")) for t in trades if t.get("side") == "buy")
    return {
        "arm_id": arm_id,
        "elapsed_sec": round(elapsed, 2),
        "seed": seed,
        "account_mode": result.get("account_mode"),
        "alignment_mode": result.get("alignment_mode"),
        "accounting_ok": bool(accounting.get("ok", False)),
        "accounting_max_diff": _safe_float(accounting.get("max_abs_diff")),
        "terminal_valuation_status": (result.get("terminal_valuation") or {}).get("status"),
        "date_range": [str(equity_curve.index[0])[:10], str(equity_curve.index[-1])[:10]] if has_curve else [None, None],
        "bars": int(len(equity_curve)) if isinstance(equity_curve, pd.DataFrame) else 0,
        "initial_capital": float(initial_capital),
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
        "execution": _execution_stats(result, trades),
        "lifecycle": _lifecycle_stats(result),
        "exposure": _exposure_stats(equity_curve),
        "diagnostics": _diagnostic_stats(metrics),
        "top_buy_symbols": buy_symbol_counts.most_common(6),
    }


def run_arm(
    arm_id: str,
    data_map: Dict[str, pd.DataFrame],
    *,
    config_overrides: Optional[Mapping[str, Any]] = None,
    engine_kwargs: Optional[Mapping[str, Any]] = None,
    seed: int = SEED,
    return_raw: bool = False,
    output_dir: Optional[Path] = None,
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
    rg = ReportGenerator(str((output_dir or OUTPUT_DIR) / "_tmp"))
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
    summary = _summarize(arm_id, result, metrics, initial_capital=float(ekw["initial_capital"]),
                         seed=seed, elapsed=elapsed)
    print(
        f"  [{arm_id:32s}] {elapsed:5.1f}s | acct={summary['accounting_ok']} | "
        f"ret={fmt_pct(summary['total_return']):>8s} | mdd={fmt_pct(summary['max_drawdown']):>7s} | "
        f"sharpe={fmt_num(summary['sharpe']):>6s} | pf={fmt_num(summary['profit_factor']):>5s}"
        f"({summary['profit_factor_status']}) | "
        f"rt={summary['round_trip_trades']:3d} | life={summary['lifecycle']['status']}",
        flush=True,
    )
    if return_raw:
        return {"summary": summary, "result": result, "metrics": metrics}
    return summary


def _anchor_decisions(data_map: Mapping[str, pd.DataFrame], trades: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """Turn the anchor's spot-compatible fills into dated buy/sell decisions."""
    decisions = []
    for idx, t in enumerate(trades):
        side = str(t.get("side") or "").lower()
        sym = str(t.get("symbol") or "")
        if side not in {"buy", "sell"} or sym not in data_map:
            continue
        fill_ts = pd.Timestamp(t.get("fill_time") or t.get("timestamp"))
        qty = float(t.get("qty") or 0.0)
        ref_price = float(t.get("theoretical_price") or t.get("fill_price") or 0.0)
        if qty <= 0 or ref_price <= 0:
            continue
        decisions.append({
            "decision_id": f"dec-{idx:04d}-{sym}-{side}",
            "symbol": sym,
            "side": side,
            "quantity": qty,
            "reference_price": ref_price,
            "signal_time": fill_ts - pd.Timedelta(days=1),
            "available_at_base": fill_ts,
        })
    return decisions


def _adverse_drift_bps(replay: Mapping[str, Any]) -> Dict[str, Optional[float]]:
    """Mean fill-versus-reference drift per side; positive means a worse price.

    Each fill is matched to its own decision through the order id.
    """
    reference = {row["order_id"]: float(row["reference_price"]) for row in replay["orders"]}
    drifts: Dict[str, List[float]] = {"buy": [], "sell": []}
    for fill in replay["fills"]:
        ref = reference.get(fill.get("order_id"))
        side = str(fill.get("side") or "").lower()
        if not ref or side not in drifts:
            continue
        move = (float(fill["fill_price"]) - ref) / ref
        drifts[side].append(move if side == "buy" else -move)
    return {
        f"{side}_adverse_drift_bps": round(1e4 * sum(vals) / len(vals), 2) if vals else None
        for side, vals in drifts.items()
    }


def run_delayed_execution_diagnostic(
    data_map: Dict[str, pd.DataFrame],
    anchor_result: Mapping[str, Any],
    anchor_end_equity: Optional[float] = None,
) -> Dict[str, Any]:
    """Use backtest.delayed_execution to measure decision/polling latency impact.

    The replay is an independent spot account that executes the anchor's fills as
    IOC market orders (protective-stop fills included). Compare the delay rows with
    each other, not with the engine anchor's equity.
    """
    base_decisions = _anchor_decisions(data_map, anchor_result.get("trades") or [])
    if not base_decisions:
        return {"status": "insufficient", "decisions": 0}
    frames = {s: data_map[s] for s in sorted({d["symbol"] for d in base_decisions})}
    out: Dict[str, Any] = {
        "decision_count": len(base_decisions),
        "account_model": "independent spot replay of anchor fills; not comparable with engine anchor equity",
        "engine_anchor_end_equity": anchor_end_equity,
        "delay_days": {},
    }
    for delay_days in (0, 1, 2):
        decs = [{
            "decision_id": d["decision_id"],
            "symbol": d["symbol"],
            "side": d["side"],
            "quantity": d["quantity"],
            "reference_price": d["reference_price"],
            "signal_time": d["signal_time"].isoformat(),
            "signal_timeframe": "1d",
            "available_at": (d["available_at_base"] + pd.Timedelta(days=delay_days)).isoformat(),
        } for d in base_decisions]
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
        out["delay_days"][str(delay_days)] = {
            "final_equity": round(float(rep["final_equity"]), 2),
            "total_return": round(float(rep["final_equity"]) / 10000.0 - 1.0, 6),
            "fills": len(rep["fills"]),
            "unexecuted": len(rep["unexecuted_decisions"]),
            "rejected_or_unfilled": sum(1 for st in rep["order_statuses"].values() if str(st).lower() != "filled"),
            "no_fill_before_decision": rep["no_fill_before_decision"],
            **_adverse_drift_bps(rep),
        }
    return out


@dataclass
class Context:
    """Shared inputs of one invocation; ``cache`` holds arms reused across groups."""

    data_map: Dict[str, pd.DataFrame]
    universe: Any
    anchor: Dict[str, Any]
    anchor_raw: Dict[str, Any]
    output_dir: Path = OUTPUT_DIR
    data_dir: Path = DATA_DIR
    cache: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def arm(self, arm_id: str, *, data_map: Optional[Dict[str, pd.DataFrame]] = None,
            with_universe: bool = True, engine_kwargs: Optional[Mapping[str, Any]] = None,
            **kwargs: Any) -> Dict[str, Any]:
        ekw = dict(engine_kwargs or {})
        if with_universe:
            ekw.setdefault("universe", self.universe)
        return run_arm(arm_id, self.data_map if data_map is None else data_map,
                       engine_kwargs=ekw, output_dir=self.output_dir, **kwargs)

    def no_health_gate(self) -> Dict[str, Any]:
        if "E4_no_health_gate" not in self.cache:
            self.cache["E4_no_health_gate"] = self.arm(
                "E4_no_health_gate", config_overrides={"strategy_health": {"enabled": False}})
        return self.cache["E4_no_health_gate"]


def run_e1(ctx: Context) -> Dict[str, Any]:
    """E1: execution friction and cost decay stress test."""
    e1: Dict[str, Any] = {}
    e1["E1_ideal_zero_cost"] = ctx.arm(
        "E1_ideal_zero_cost",
        config_overrides={"execution": {
            "commission_rate_taker": 0.0,
            "commission_rate_maker": 0.0,
            "slippage_bps": 0.0,
            "spread_bps": 0.0,
            "volatility_slippage_factor": 0.0,
            "use_impact_cost": False,
        }},
        engine_kwargs={"slippage": 0.0},
    )
    e1["E1_baseline_1x"] = ctx.anchor
    for mult, label in [(1.5, "E1_stress_1.5x"), (2.0, "E1_stress_2.0x"), (3.0, "E1_stress_3.0x")]:
        e1[label] = ctx.arm(label, config_overrides={"execution": {
            "commission_rate_taker": 0.001 * mult,
            "commission_rate_maker": 0.001 * mult,
            "slippage_bps": 5.0 * mult,
            "spread_bps": 2.0 * mult,
            "volatility_slippage_factor": 0.02 * mult,
            "impact_coefficient": 0.10 * mult,
        }})
    e1["E1_random_slip_2x_s42"] = ctx.arm(
        "E1_random_slip_2x_s42",
        config_overrides={"execution": {
            "slippage_bps": 10.0,
            "spread_bps": 4.0,
            "volatility_slippage_factor": 0.04,
        }},
        engine_kwargs={"random_slip": True},
        seed=42,
    )
    return e1


def run_e2(ctx: Context) -> Dict[str, Any]:
    """E2: intrabar stops, gap-risk resize and execution delay."""
    e2: Dict[str, Any] = {"E2_resident_stop_anchor": ctx.anchor}
    e2["E2_legacy_close_stop"] = ctx.arm(
        "E2_legacy_close_stop", config_overrides={"protective_orders": {"backtest_resident": False}})
    e2["E2_no_gap_risk_resize"] = ctx.arm(
        "E2_no_gap_risk_resize", config_overrides={"entry_risk": {"enabled": False}})
    # Known confound: the trailing-stop inputs still have the F-10 defects
    # (docs/strategy_remediation_roadmap_20261110.md), so this arm cannot judge the design.
    e2["E2_atr_hybrid_trailing_stop"] = ctx.arm(
        "E2_atr_hybrid_trailing_stop",
        config_overrides={"stops": {"use_atr_initial_stop": True, "use_trailing_stop": True}})
    e2["E2_delayed_decision_replay"] = run_delayed_execution_diagnostic(
        ctx.data_map, ctx.anchor_raw, ctx.anchor.get("end_equity"))
    print(f"  [E2_delayed_decision_replay      ] {e2['E2_delayed_decision_replay']}", flush=True)
    return e2


def run_e3(ctx: Context) -> Dict[str, Any]:
    """E3: capital capacity and liquidity participation curve."""
    e3: Dict[str, Any] = {"E3_cap_10k": ctx.anchor}
    for cap, label in [
        (100_000.0, "E3_cap_100k"),
        (1_000_000.0, "E3_cap_1m"),
        (10_000_000.0, "E3_cap_10m"),
        (50_000_000.0, "E3_cap_50m"),
    ]:
        e3[label] = ctx.arm(label, engine_kwargs={"initial_capital": cap})
    return e3


def run_e4(ctx: Context) -> Dict[str, Any]:
    """E4: account mode, risk breakers and strategy-health lockout."""
    e4: Dict[str, Any] = {"E4_margin_anchor": ctx.anchor}
    e4["E4_spot_unlevered"] = ctx.arm(
        "E4_spot_unlevered", config_overrides=SPOT_CFG, engine_kwargs={"account_mode": "spot"})
    e4["E4_no_health_gate"] = ctx.no_health_gate()
    e4["E4_no_drawdown_budget"] = ctx.arm(
        "E4_no_drawdown_budget", config_overrides={"drawdown_budget": {"enabled": False}})
    e4["E4_breaker_timed_recovery"] = ctx.arm(
        "E4_breaker_timed_recovery",
        engine_kwargs={"breaker_policy": {
            "on_liquidate": "cooldown",
            "recovery": {
                "mode": "timed_rebase",
                "flat_bars_required": 30,
                "health_bars_required": 5,
                "rebase_high_water": True,
                "max_resumes": 5,
            },
        }},
    )
    e4["E4_raw_no_breaker_no_health"] = ctx.arm(
        "E4_raw_no_breaker_no_health",
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
    )
    return e4


def run_e4b(ctx: Context) -> Dict[str, Any]:
    """E4b: what the downstream controls do once strategy health no longer suppresses risk."""
    e4b: Dict[str, Any] = {"E4b_full_risk_base": ctx.no_health_gate()}
    e4b["E4b_full_risk_spot"] = ctx.arm(
        "E4b_full_risk_spot",
        config_overrides=_deep_merge(SPOT_CFG, {"strategy_health": {"enabled": False}}),
        engine_kwargs={"account_mode": "spot"},
    )
    for label, section in [
        ("E4b_full_risk_no_cluster", "portfolio_risk"),
        ("E4b_full_risk_no_scoring", "candidate_scoring"),
        ("E4b_full_risk_no_gap_resize", "entry_risk"),
    ]:
        e4b[label] = ctx.arm(label, config_overrides={
            "strategy_health": {"enabled": False}, section: {"enabled": False}})
    return e4b


def run_e5(ctx: Context) -> Dict[str, Any]:
    """E5: universe, alignment, cluster risk, scoring and the research-window gate."""
    e5: Dict[str, Any] = {"E5_pit_union_anchor": ctx.anchor}
    e5["E5_static_no_pit"] = ctx.arm("E5_static_no_pit", with_universe=False, engine_kwargs={"universe": None})
    e5["E5_intersection_align"] = ctx.arm(
        "E5_intersection_align", engine_kwargs={"alignment_mode": "intersection"})
    e5["E5_no_cluster_risk"] = ctx.arm(
        "E5_no_cluster_risk", config_overrides={"portfolio_risk": {"enabled": False}})
    e5["E5_no_scoring_alphabetical"] = ctx.arm(
        "E5_no_scoring_alphabetical", config_overrides={"candidate_scoring": {"enabled": False}})
    e5["E5_smart_capital_alloc"] = ctx.arm(
        "E5_smart_capital_alloc", engine_kwargs={"capital_allocation": {"enabled": True}})
    e5["E5_research_window_gate"] = verify_research_window_gate(ctx.data_dir)
    print(f"  [E5_research_window_gate         ] {e5['E5_research_window_gate']['status']}", flush=True)
    return e5


def _round_trip_pnls(result: Mapping[str, Any], output_dir: Path) -> List[float]:
    rg = ReportGenerator(str(output_dir / "_tmp"))
    closed_legs = [
        r for r in rg._reconstruct_closed_trades(pd.DataFrame(result.get("trades") or []))
        if r.get("status") != "invalid_input"
    ]
    return [float(r["net_pnl"]) for r in rg._aggregate_round_trips(closed_legs) if r.get("net_pnl") is not None]


def run_e6(ctx: Context) -> Dict[str, Any]:
    """E6: market-cycle breakdown and Monte Carlo trade-sequence risk."""
    e6: Dict[str, Any] = {}
    for label, w_start, w_end in CYCLE_WINDOWS:
        w_data, _ = _load_universe_data(w_start, w_end, ctx.data_dir)
        e6[label] = ctx.arm(label, data_map=w_data)
    rt_pnls = _round_trip_pnls(ctx.anchor_raw, ctx.output_dir)
    e6["monte_carlo_anchor"] = monte_carlo_trade_sequence(rt_pnls, n_simulations=2000, seed=SEED)
    print(f"  [E6_monte_carlo_anchor           ] n_trades={len(rt_pnls)} mc={e6['monte_carlo_anchor']}", flush=True)
    return e6


def run_e7(ctx: Context) -> Dict[str, Any]:
    """E7: IS versus OOS parameter sensitivity on a purged split."""
    is_data, _ = _load_universe_data(*IS_WINDOW, ctx.data_dir)
    oos_data, _ = _load_universe_data(*OOS_WINDOW, ctx.data_dir)
    rows = []
    for ew, xw in PARAM_GRID:
        p_cfg = {"research": {
            "experiment_id": f"e7_grid_{ew}_{xw}",
            "trend_breakout_parameters": {"entry_window": ew, "exit_window": xw},
        }}
        r_is = ctx.arm(f"E7_IS_{ew}_{xw}", data_map=is_data, config_overrides=p_cfg)
        r_oos = ctx.arm(f"E7_OOS_{ew}_{xw}", data_map=oos_data, config_overrides=p_cfg)
        row: Dict[str, Any] = {"entry_window": ew, "exit_window": xw}
        for prefix, r in (("is", r_is), ("oos", r_oos)):
            row.update({
                f"{prefix}_return": r["total_return"],
                f"{prefix}_sharpe": r["sharpe"],
                f"{prefix}_mdd": r["max_drawdown"],
                f"{prefix}_pf": r["profit_factor"],
                f"{prefix}_trades": r["round_trip_trades"],
                f"{prefix}_lifecycle": r["lifecycle"]["status"],
                f"{prefix}_accounting_ok": r["accounting_ok"],
            })
        rows.append(row)
    is_s = pd.Series([r["is_return"] for r in rows], dtype=float)
    oos_s = pd.Series([r["oos_return"] for r in rows], dtype=float)
    best_is = max(rows, key=lambda r: r["is_return"] if r["is_return"] is not None else -1e9)
    return {
        "is_window": list(IS_WINDOW),
        "oos_window": list(OOS_WINDOW),
        "purge_days": 31,
        "grid": rows,
        "spearman_is_vs_oos_return": _safe_float(is_s.corr(oos_s, method="spearman")),
        "best_is_param": {k: best_is[k] for k in (
            "entry_window", "exit_window", "is_return", "oos_return", "is_sharpe", "oos_sharpe")},
        "positive_is_count": sum(1 for r in rows if (r["is_return"] or 0) > 0),
        "positive_oos_count": sum(1 for r in rows if (r["oos_return"] or 0) > 0),
    }


GROUP_RUNNERS: Dict[str, Tuple[str, Callable[[Context], Dict[str, Any]]]] = {
    "E1": ("E1_cost_friction", run_e1),
    "E2": ("E2_stop_gap_latency", run_e2),
    "E3": ("E3_capital_capacity", run_e3),
    "E4": ("E4_account_risk_lifecycle", run_e4),
    "E4b": ("E4b_active_risk_controls", run_e4b),
    "E5": ("E5_universe_alignment_allocation", run_e5),
    "E6": ("E6_cycle_and_diagnostics", run_e6),
    "E7": ("E7_parameter_is_oos", run_e7),
}


def _symbol_coverage(data_map: Mapping[str, pd.DataFrame]) -> Dict[str, Any]:
    last = Counter(str(df.index[-1])[:10] for df in data_map.values())
    first = {s: str(df.index[0])[:10] for s, df in data_map.items()}
    return {
        "last_bar_counts": dict(sorted(last.items())),
        "first_bar_after_window_start": {s: d for s, d in sorted(first.items()) if d > DEFAULT_START},
    }


def run_suite(groups: Sequence[str], output_dir: Path = OUTPUT_DIR, data_dir: Path = DATA_DIR) -> Dict[str, Any]:
    started_at = datetime.now(timezone.utc).isoformat()
    t_all = time.perf_counter()
    universe, universe_overrides = adopted_universe(UNIVERSE_CSV, data_dir)
    data_map, symbols = _load_universe_data(DEFAULT_START, DEFAULT_END, data_dir)
    print(f"Loaded {len(symbols)} symbols ({DEFAULT_START} ~ {DEFAULT_END})", flush=True)
    for item in universe_overrides:
        print(f"Universe override: {item}", flush=True)

    print("\n--- Anchor Baseline (production config, PIT union) ---", flush=True)
    anchor_pack = run_arm(
        "ANCHOR_prod_baseline", data_map,
        engine_kwargs={"universe": universe, "calculate_benchmarks": True},
        return_raw=True, output_dir=output_dir,
    )
    ctx = Context(data_map=data_map, universe=universe, anchor=anchor_pack["summary"],
                  anchor_raw=anchor_pack["result"], output_dir=output_dir, data_dir=data_dir)
    report: Dict[str, Any] = {
        "meta": {
            "schema": "reality-gap-experiments/v2",
            "evidence_kind": "retrospective_diagnostic",
            "formal_admission": False,
            "started_at_utc": started_at,
            "seed": SEED,
            "window": [DEFAULT_START, DEFAULT_END],
            "groups": list(groups),
            "symbol_count": len(symbols),
            "symbol_coverage": _symbol_coverage(data_map),
            "universe_overrides": universe_overrides,
            "benchmark_fixed_return": _safe_float(anchor_pack["metrics"].get("BenchmarkTotalReturn")),
            **run_identity(data_dir),
        },
        "anchor": ctx.anchor,
        "experiments": {},
    }
    for group in groups:
        key, runner = GROUP_RUNNERS[group]
        print(f"\n--- {key} ---", flush=True)
        report["experiments"][key] = runner(ctx)
    report["meta"]["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    report["meta"]["total_elapsed_sec"] = round(time.perf_counter() - t_all, 2)
    return report


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--groups", nargs="+", choices=GROUPS, default=list(GROUPS),
                        help="experiment groups to run after the anchor (default: all)")
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    args = parser.parse_args(argv)

    logging.disable(logging.WARNING)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    print("=== QuantTradingV1 Reality-Gap Backtest Experiment Suite ===", flush=True)
    groups = [g for g in GROUPS if g in set(args.groups)]
    report = run_suite(groups, args.output_dir)
    # A partial run never overwrites the full-matrix summary.
    name = "summary.json" if groups == list(GROUPS) else "summary_partial.json"
    out_json = args.output_dir / name
    out_json.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\nSaved JSON report to {out_json} (total {report['meta']['total_elapsed_sec']}s)", flush=True)


if __name__ == "__main__":
    main()
