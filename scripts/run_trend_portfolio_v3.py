"""Freeze and execute the finite, research-only TrendPortfolioV3 comparison.

This runner never changes the default configuration or opens a final holdout.
Incomplete market evidence remains explicitly insufficient in every result.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import asdict, is_dataclass, replace
from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
import platform
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd
import numpy as np

from analysis.trend_portfolio_v3_validation import (evaluate_primary, execution_diagnostics,
    health_reachability, performance_summary)
from backtest.engine import BacktestEngine
from composition.factory import build_strategy_registry
from config.config import config
from core.market_data import normalize_market_frame
from core.reproducibility import canonical_json, sha256_file
from core.universe import normalize_symbol
from scripts.run_trend_portfolio_v2 import _benchmark_diagnostic, _execute_suite_run, suite_profiles

SCHEMA = "trend_portfolio_v3_registered_daily/v1"
START, END = "2020-01-01", "2026-09-18"
CONTROL_DATA = ROOT / "reports/strategy_review_20260919/public_data_validated"
CONTROL_SYMBOLS = ("BTC", "ETH", "SOL", "XRP", "ADA", "LTC")
CONTROL_REFERENCES = {v: ROOT / f"reports/trend_portfolio_v2_20260920/runs/binance__{v}__main/summary.json"
                      for v in ("V2-C", "V2-D")}


def save(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(json.loads(canonical_json(payload)), ensure_ascii=False,
        indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp.replace(path)


def append_attempt(path, payload):
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(canonical_json({**payload, "recorded_at": datetime.now(timezone.utc).isoformat()}) + "\n")
        handle.flush()


def source_hashes():
    paths = {ROOT / "pyproject.toml", ROOT / "requirements.lock.txt"}
    for name in ("backtest", "core", "strategies", "router", "composition", "analysis", "config", "scripts", "tests"):
        paths.update(p for p in (ROOT / name).rglob("*") if p.is_file() and p.suffix in {".py", ".yaml", ".yml"})
    return {p.relative_to(ROOT).as_posix(): sha256_file(p) for p in sorted(paths)}


def load_bundle(folder):
    from core.trend_portfolio_data import load_data_bundle
    folder = Path(folder)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("completed") is False:
        raise ValueError("data collection is still running; cannot freeze partial mutable inputs")
    loaded, metadata, manifest = load_data_bundle(folder)
    metadata = {normalize_symbol(s): row for s, row in metadata.get("symbols", metadata).items()}
    frames = {}
    for raw_symbol, frame in sorted(loaded.items()):
        if not {"open", "high", "low", "close", "volume", "quote_volume"}.issubset(frame):
            raise ValueError(f"{raw_symbol}: true quote_volume and OHLCV required")
        if "scheduled_exit" in frame and frame.scheduled_exit.fillna(False).any():
            raise ValueError("legacy last-bar scheduled exits prohibited")
        symbol = normalize_symbol(raw_symbol)
        if symbol in frames:
            raise ValueError(f"duplicate symbol: {symbol}")
        frames[symbol] = normalize_market_frame(frame)
    if not frames:
        raise ValueError("bundle has no market frames")
    missing = sorted(set(frames) - set(metadata))
    if missing:
        raise ValueError(f"market metadata missing for {len(missing)} symbols")
    identity = {str(p.relative_to(folder)): sha256_file(p) for p in sorted(folder.glob("data/*.csv"))}
    identity.update({name: sha256_file(folder / name) for name in ("metadata.json", "manifest.json")})
    if (folder / "quote_borrow_facts.json").exists():
        identity["quote_borrow_facts.json"] = sha256_file(folder / "quote_borrow_facts.json")
    if manifest.get("completed") is False:
        raise ValueError("data collection is still running; cannot freeze partial mutable inputs")
    evidence = {"full_market_verified": manifest.get("full_market_verified", manifest.get("historical_full_universe_verified")) is True,
        "manifest_status": manifest.get("status"), "market_count": len(frames),
        "coverage": manifest.get("coverage", {}), "gaps": manifest.get("gaps", []),
        "limitations": manifest.get("limitations", manifest.get("blockers", [])), "source_kind": manifest.get("source_kind", "real")}
    return frames, metadata, evidence, identity


def suite_specs():
    specs = []
    for variant in ("momentum", "breakout"):
        for financing in ("assumed", "verified_only"):
            def add(role, suffix, start=START, end=END, **updates):
                specs.append({"run_id": f"{variant}__{financing}__{suffix}", "variant": variant,
                    "financing_mode": financing, "role": role, "start": start, "end": end,
                    "cost_multiplier": 1., "borrow_rate": .08, "horizons": [60, 120],
                    "min_quote_volume": 5_000_000., **updates})
            add("primary", "main")
            for year in range(2020, 2027):
                for month in (1, 7):
                    start = pd.Timestamp(year=year, month=month, day=1)
                    end = start + pd.DateOffset(months=6) - pd.Timedelta(days=1)
                    if end <= pd.Timestamp(END):
                        add("rolling", f"{year}_{month:02d}", start.date().isoformat(), end.date().isoformat())
            add("recent", "recent", "2026-07-01")
            for multiplier in (1.5, 2.):
                add("cost", f"cost_{multiplier:g}", cost_multiplier=multiplier)
            for horizons in ([40, 80], [80, 160]):
                add("neighbor", f"horizons_{horizons[0]}_{horizons[1]}", horizons=horizons)
            for volume in (3_000_000., 10_000_000.):
                add("neighbor", f"liquidity_{int(volume)}", min_quote_volume=volume)
            if financing == "assumed":
                for rate in (.16, .24):
                    add("financing_stress", f"interest_{int(rate*100)}", borrow_rate=rate)
    return specs


def effective_config(baseline, spec):
    settings = deepcopy(baseline)
    for state in ("TREND_UP", "TREND_DOWN", "SIDEWAYS", "VOLATILE"):
        settings["routing"][state] = "TrendPortfolioV3"
    settings["router"].update(cooldown_bars=0, max_holding_days=365)
    settings["risk"].update(max_leverage=3., max_pos_size_pct=.30, risk_per_trade=.01)
    settings["portfolio_risk"].update(enabled=True, max_cluster_exposure_pct=1.5,
        max_crypto_beta_exposure=2., max_same_session_entry_risk=.02,
        max_correlated_stop_risk=.03, max_crypto_beta_stop_risk=.03)
    settings["account"].update(mode="spot_margin", default_borrow_rate_annual=spec["borrow_rate"])
    settings["backtest"]["end_of_backtest_mode"] = "valuation_only"
    settings["backtest"]["v3_benchmark"] = "BTC_ETH_cost_net_risk_match; skip unused generic engine benchmarks"
    settings["backtest"].setdefault("breaker_policy", {})["shadow_diagnostics"] = False
    settings.setdefault("strategy_governance", {})["TrendPortfolioV3"] = "isolated_research"
    settings.setdefault("research", {}).update(experiment_id=f"{SCHEMA}:{spec['run_id']}", entry_audit=True,
        trend_portfolio_v3={"variant": spec["variant"], "horizons": spec["horizons"],
                            "min_quote_volume": spec["min_quote_volume"]})
    for section in ("signal_observation", "signal_meta_layer", "signal_adaptive", "signal_meta_replay"):
        settings.setdefault(section, {})["enabled"] = False
    for field in ("commission_rate_taker", "commission_rate_maker", "slippage_bps", "spread_bps",
                  "volatility_slippage_factor", "impact_coefficient"):
        settings["execution"][field] *= spec["cost_multiplier"]
    return settings


def attainable_market_symbols(frames, metadata):
    """Necessary input reachability, based on actual observable eligible data.

    This is a research preflight diagnostic, never a symbol-selection input.
    It does not promise positive momentum or future profitable recovery cohorts.
    """
    from core.selection_v2 import SelectionPolicyV2, _metadata_reasons
    policy = SelectionPolicyV2()
    possible = []
    for symbol, frame in frames.items():
        if metadata.get(symbol, {}).get("source_status") != "verified":
            continue
        if len(frame) < policy.required_closes or "quote_volume" not in frame:
            continue
        median = frame.quote_volume.rolling(20, min_periods=20).median()
        lengths = 0
        previous = None
        for timestamp, volume in median.items():
            lengths = lengths + 1 if previous is not None and timestamp - previous == pd.Timedelta(days=1) else 1
            previous = timestamp
            if lengths < policy.required_closes or pd.isna(volume) or volume < policy.min_quote_volume:
                continue
            if not pd.Timestamp(START) <= timestamp <= pd.Timestamp(END):
                continue
            point = timestamp.tz_localize("UTC") + pd.Timedelta(days=1)
            if not _metadata_reasons(metadata.get(symbol, {}), point, policy):
                possible.append(symbol)
                break
    return possible


def make_controller(strategy, metadata, spec, quote_borrow_facts=()):
    from core.portfolio_target_controller import PortfolioTargetController
    from core.quote_borrow import QuoteBorrowFact, QuoteBorrowPolicy
    from core.risk.portfolio_governor import CorrelationClusterPolicy
    from core.selection_v2 import SelectionPolicyV2, select_all_qualified, size_portfolio_targets

    policy = replace(getattr(strategy, "selection_policy", SelectionPolicyV2()),
        variant=spec["variant"], horizons=tuple(spec["horizons"]),
        min_quote_volume=spec["min_quote_volume"])
    cluster_policy = CorrelationClusterPolicy.from_mapping(config._config.get("portfolio_risk", {}))
    frozen = {"week": None, "weights": {}, "sizing": None}

    def targets(*, event, as_of, held_symbols, existing_weights):
        point = pd.Timestamp(as_of)
        week = (point.normalize() - pd.Timedelta(days=point.weekday())).isoformat()
        if frozen["week"] is None and controller._state.get("week") is not None:
            # A fresh provider after process restart must honor the persisted
            # weekly cohort, including targets with no fill yet.
            frozen.update(week=controller._state["week"], weights={
                s: row["weight"] for s, row in controller._state["targets"].items()})
        weekly = point.weekday() == 0 and frozen["week"] != week
        # Full cross-section is evaluated only for a new Monday allocation.
        # Daily permissions and exits cover the frozen cohort and all holdings.
        relevant = set(frozen["weights"]) | set(held_symbols)
        histories = event.histories if weekly else {s: f for s, f in event.histories.items() if s in relevant}
        selected = select_all_qualified(histories, metadata, as_of=as_of,
            held_symbols=held_symbols, policy=policy)
        if weekly:
            sized = size_portfolio_targets(selected, existing_weights=existing_weights,
                clusters={s: cluster_policy.cluster_for(s) for s in selected.selected_symbols})
            frozen.update(week=week, weights=dict(sized.target_weights),
                sizing=asdict(sized) if is_dataclass(sized) else sized)
        rows = {s: r.to_dict() for s, r in selected.rows.items()}
        return {"target_weights": dict(frozen["weights"]),
            "add_allowed": {s: bool(r.get("add_allowed", False)) for s, r in rows.items()},
            "stop_prices": {s: r.get("stop_price") for s, r in rows.items() if r.get("stop_price") is not None},
            "forced_exits": [s for s, r in rows.items() if r.get("force_exit")],
            "as_of": as_of, "selection_rows": rows,
            "sizing": frozen["sizing"]}

    controller = PortfolioTargetController(strategy=strategy, target_provider=targets,
        quote_borrow_policy=QuoteBorrowPolicy([QuoteBorrowFact(**row) for row in quote_borrow_facts],
            mode=spec["financing_mode"], assumed_annual_rate=spec["borrow_rate"]),
        metadata=metadata)
    return controller


def run_one(spec, frames, metadata, baseline, output, quote_borrow_facts=()):
    folder = Path(output) / "runs" / spec["run_id"]
    folder.mkdir(parents=True, exist_ok=False)
    save(folder / "attempt_started.json", {"run_id": spec["run_id"], "state": "started",
        "recorded_at": datetime.now(timezone.utc).isoformat(), "spec": spec})
    settings = effective_config(baseline, spec)
    save(folder / "resolved_config.json", settings)
    market = {s: frame.loc[frame.index <= pd.Timestamp(spec["end"])].copy() for s, frame in frames.items()
              if (frame.index <= pd.Timestamp(spec["end"])).any()}
    prior = config._config
    try:
        config._config = settings
        registry = build_strategy_registry(config)
        controller = make_controller(registry["TrendPortfolioV3"], metadata, spec, quote_borrow_facts)
        # Candle labels are UTC opens, while decisions use the next midnight.
        # A Monday window must admit Sunday's completed-bar decision so its
        # first executable order can fill on Monday. There is no inherited
        # position/order that could generate a pre-window cashflow.
        decision_start = pd.Timestamp(spec["start"]) - pd.Timedelta(days=1)
        engine = BacktestEngine(initial_capital=10000., warmup_period=0, alignment_mode="union",
            timeframe="1d", trading_start=decision_start, portfolio_controller=controller,
            terminal_policy="valuation_only", calculate_benchmarks=False, run_id=f"v3:{spec['run_id']}")
        result = engine.run(market, strategies=registry, routing_log_enabled=False)
        curve = result["equity_curve"].loc[pd.Timestamp(spec["start"]):pd.Timestamp(spec["end"])]
        if any(pd.Timestamp(row["fill_time"]).tz_localize(None) < pd.Timestamp(spec["start"])
               for row in result["trades"]):
            raise ValueError("pre-window execution is prohibited")
        if curve.empty:
            raise ValueError("no account equity observations")
        curve.to_csv(folder / "equity.csv", index_label="timestamp")
        events = [asdict(event) for event in engine.execution_adapter.broker.close_events]
        for name in ("trades", "financing_ledger", "margin_ledger", "stop_order_audit", "risk_budget_reconciliation",
                     "valuation_quality", "strategy_activity"):
            pd.DataFrame(result.get(name, [])).to_csv(folder / f"{name}.csv", index=False)
        for name in ("portfolio_controller", "terminal_valuation", "strategy_health", "accounting_check",
                     "lifecycle", "protective_stop_summary", "account_cost_contract"):
            save(folder / f"{name}.json", result.get(name, {}))
        save(folder / "close_events.json", events)
        diagnostic = execution_diagnostics(result)
        save(folder / "execution_diagnostics.json", diagnostic)
        summary = {**spec, **performance_summary(curve["equity"], 10000.),
            "status": "completed", "fill_count": len(result["trades"]), "market_count": len(market),
            "close_event_records": events, "trades": result["trades"],
            "financing_total": sum(float(row.get("amount", 0)) for row in result["financing_ledger"]),
            "financing_gross": sum(abs(float(row.get("amount", 0))) for row in result["financing_ledger"]),
            "accounting_check": result["accounting_check"], "terminal_valuation": result["terminal_valuation"],
            "execution_diagnostics": diagnostic,
            "stale_valuation_count": len(result["valuation_quality"]), "unseen_oos": False,
            "sample_classification": ("synthetic_engineering_fixture" if any(
                str(meta.get("source_kind", "")).startswith("synthetic") for meta in metadata.values())
                else "retrospective_research"), "live_admission": False}
        benchmarks = {s: f for s, f in market.items() if s in {"BTC-USDT", "ETH-USDT"}}
        summary["benchmark_diagnostic"] = (_benchmark_diagnostic(benchmarks, spec, summary["daily_returns"], settings)
            if len(benchmarks) == 2 else {"status": "insufficient", "reason": "BTC_ETH_pair_required"})
        save(folder / "summary.json", summary)
        save(folder / "artifacts.json", {p.name: sha256_file(p) for p in sorted(folder.iterdir()) if p.is_file()})
        return json.loads(canonical_json(summary))
    finally:
        config._config = prior


def register(bundle, output):
    output = Path(output)
    if (output / "preregistration.json").exists():
        raise ValueError("research identity already exists; use --resume")
    frames, metadata, evidence, data_hashes = load_bundle(bundle)
    baseline = deepcopy(config._config)
    protocol = {"schema": SCHEMA, "registered_at": datetime.now(timezone.utc).isoformat(),
        "environment": {"python": sys.version, "platform": platform.platform(),
            "pandas": pd.__version__, "numpy": np.__version__, "execution": "CPU deterministic; random_slip=False"},
        "quote_borrow_facts": (json.loads((Path(bundle) / "quote_borrow_facts.json").read_text(encoding="utf-8"))
            if (Path(bundle) / "quote_borrow_facts.json").exists() else []),
        "bundle_path": str(Path(bundle).resolve()), "data_hashes": data_hashes, "data_evidence": evidence,
        "source_hashes": source_hashes(), "baseline_config": baseline, "specs": suite_specs(),
        "engineering_evidence_hashes": {p.name: sha256_file(p)
            for p in sorted(output.parent.glob("engineering_*")) if p.is_file()},
        "control_data_hashes": {f"binance_{s}USDT_1d.csv": sha256_file(CONTROL_DATA / f"binance_{s}USDT_1d.csv")
                                for s in CONTROL_SYMBOLS},
        "control_reference_hashes": {v: sha256_file(p) for v, p in CONTROL_REFERENCES.items()},
        "controls": ["V2-C_original", "V2-C_health_2", "V2-D_original", "V2-D_health_3"],
        "initial_capital": 10000., "maximum_drawdown_pct": 15,
        "unseen_oos": False, "live_admission": False,
        "selection_policy": "all_qualified_no_topn", "risk_target_annualized": .10,
        "financing_assumptions": {"annual_rates": [.08, .16, .24],
            "assumed_availability": "eligible spot pairs may use account-approved quote funding up to configured account limits",
            "verified_only": "missing contemporaneous eligibility, quote limit or rate prohibits new debt"},
        "execution_filter_assumptions": {"quantity_step_if_unverified": 1e-8,
            "minimum_notional_if_unverified": 5.,
            "status": "model assumptions unless contemporaneous per-market metadata provides verified filters",
            "historical_filter_coverage_verified": False},
        "health_reachability": health_reachability(baseline, attainable_market_symbols(frames, metadata)),
        "evaluation": {"minimum_cohorts": 30, "profitable_windows": "at least 2/3 of 13 half-years",
            "bootstrap": {"iterations": 2000, "block_length": 5, "seed": 42},
            "remove_best_cohorts": [1, 3, 5, 10], "cost_stress_positive_net": True,
            "neighbor_rule": "3 of 4 positive; median at least half of positive primary return"}}
    output.mkdir(parents=True, exist_ok=True)
    for relative in protocol["source_hashes"]:
        target = output / "frozen_source" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    save(output / "preregistration.json", protocol)
    return protocol


_WORKER_INPUTS = None


def _initialize_worker(bundle, baseline, output, quote_borrow_facts):
    global _WORKER_INPUTS
    logging.disable(logging.CRITICAL)
    frames, metadata, _, _ = load_bundle(bundle)
    _WORKER_INPUTS = (frames, metadata, baseline, output, quote_borrow_facts)


def _execute_worker(spec):
    frames, metadata, baseline, output, facts = _WORKER_INPUTS
    return run_one(spec, frames, metadata, baseline, output, facts)


def resume(bundle, output, only=None, workers=1):
    output = Path(output)
    protocol = json.loads((output / "preregistration.json").read_text(encoding="utf-8"))
    frames, metadata, evidence, data_hashes = load_bundle(bundle)
    if data_hashes != protocol["data_hashes"] or source_hashes() != protocol["source_hashes"]:
        raise ValueError("source or market identity changed after registration; new research identity required")
    if any(sha256_file(CONTROL_DATA / name) != digest for name, digest in protocol["control_data_hashes"].items()):
        raise ValueError("frozen V2 control inputs changed")
    if any(sha256_file(CONTROL_REFERENCES[v]) != digest for v, digest in protocol["control_reference_hashes"].items()):
        raise ValueError("old V2 reference artifacts changed")
    if only is None:
        run_controls(protocol, output)
    specs = [s for s in protocol["specs"] if only is None or s["run_id"] == only]
    if only and not specs:
        raise ValueError("unknown registered run")
    if not 1 <= workers <= 16:
        raise ValueError("workers must be in [1,16]")
    results, pending = [], []
    for spec in specs:
        folder = output / "runs" / spec["run_id"]
        completed = folder / "summary.json"
        if completed.exists():
            hashes = json.loads((folder / "artifacts.json").read_text())
            if any(sha256_file(folder / name) != digest for name, digest in hashes.items()):
                raise ValueError("completed artifacts changed")
            results.append(json.loads(completed.read_text(encoding="utf-8")))
            continue
        if folder.exists():
            raise ValueError(f"incomplete prior attempt retained at {folder}; explicit new identity required")
        pending.append(spec)
    if workers > 1 and len(pending) > 1:
        failures = []
        with ProcessPoolExecutor(max_workers=workers, initializer=_initialize_worker,
                initargs=(str(bundle), protocol["baseline_config"], str(output), protocol.get("quote_borrow_facts", []))) as pool:
            tasks = {}
            for spec in pending:
                append_attempt(output / "attempts.jsonl", {"run_id": spec["run_id"], "state": "queued"})
                tasks[pool.submit(_execute_worker, spec)] = spec
            for future in as_completed(tasks):
                spec = tasks[future]
                try:
                    row = future.result()
                except Exception as exc:
                    record = {"run_id": spec["run_id"], "state": "failed", "error": repr(exc)}
                    append_attempt(output / "attempts.jsonl", record)
                    failures.append(record)
                    continue
                append_attempt(output / "attempts.jsonl", {"run_id": spec["run_id"], "state": "completed"})
                results.append(row)
                print(json.dumps({"completed": spec["run_id"], "return_pct": row["return_pct"],
                    "fills": row["fill_count"]}), flush=True)
        if failures:
            save(output / "failures.json", failures)
            raise ValueError(f"{len(failures)} fixed attempts failed; artifacts retained")
        pending = []
    for spec in pending:
        append_attempt(output / "attempts.jsonl", {"run_id": spec["run_id"], "state": "started"})
        try:
            row = run_one(spec, frames, metadata, protocol["baseline_config"], output, protocol.get("quote_borrow_facts", []))
        except Exception as exc:
            append_attempt(output / "attempts.jsonl", {"run_id": spec["run_id"], "state": "failed", "error": repr(exc)})
            raise
        append_attempt(output / "attempts.jsonl", {"run_id": spec["run_id"], "state": "completed"})
        results.append(row)
        print(json.dumps({"completed": spec["run_id"], "return_pct": row["return_pct"],
            "fills": row["fill_count"]}), flush=True)
    if only is None:
        ordering = {s["run_id"]: i for i, s in enumerate(protocol["specs"])}
        results.sort(key=lambda row: ordering[row["run_id"]])
        write_report(output, results, evidence)
    return results


def run_controls(protocol, output):
    """Old V2 original and explicitly named health-policy controls, same data."""
    from core.universe import PointInTimeUniverse, UniverseMembership
    frames = {f"{s}-USDT": normalize_market_frame(pd.read_csv(
        CONTROL_DATA / f"binance_{s}USDT_1d.csv", parse_dates=["timestamp"]).set_index("timestamp"))
        for s in CONTROL_SYMBOLS}
    # No delisted_at and hence no future scheduled exits in this six-coin control.
    universe = PointInTimeUniverse(UniverseMembership(s, f.index[0],
        source="frozen_V2_first_observed_availability_not_verified_listing") for s, f in frames.items())
    summaries = []
    for name in protocol["controls"]:
        folder = Path(output) / "controls" / name
        if (folder / "summary.json").exists():
            hashes = json.loads((folder / "artifacts.json").read_text())
            if any(sha256_file(folder / key) != value for key, value in hashes.items()):
                raise ValueError("control artifacts changed")
            summaries.append(json.loads((folder / "summary.json").read_text(encoding="utf-8")))
            continue
        if folder.exists():
            raise ValueError("incomplete control attempt requires an explicit new identity")
        variant = name.split("_")[0]
        profiles = suite_profiles(protocol["baseline_config"])
        symbols = list(frames) if variant == "V2-D" else ["BTC-USDT", "ETH-USDT"]
        if "health_" in name:
            profiles[variant]["strategy_health"]["probation_min_distinct_symbols"] = 2 if variant == "V2-C" else 3
        spec = {"run_id": name, "variant": variant, "venue": "binance", "timeframe": "1d",
            "role": "primary", "start": "2022-01-01", "end": END,
            "recent_start": "2026-07-01", "cost_multiplier": 1.}
        append_attempt(Path(output) / "attempts.jsonl", {"run_id": name, "state": "started", "control": True})
        try:
            summary = _execute_suite_run(spec, profiles, frames, universe, folder, 10000.)
            summary["health_reachability"] = health_reachability(profiles[variant], symbols)
            summary["control_note"] = ("C has two symbols; original three-symbol recovery is unreachable; health_2 explicitly repairs that input gate"
                if variant == "V2-C" else "D already satisfies three-symbol input reachability; health_3 is an identity control")
            if name.endswith("_original"):
                previous = json.loads(CONTROL_REFERENCES[variant].read_text(encoding="utf-8"))
                fields = ("return_pct", "max_drawdown_pct", "fill_count", "financing_gross")
                summary["original_reproduction"] = {"reference": str(CONTROL_REFERENCES[variant]),
                    "checks": {key: {"previous": previous.get(key), "current": summary.get(key),
                        "matches": math.isclose(float(previous[key]), float(summary[key]), rel_tol=1e-10, abs_tol=1e-8)}
                        for key in fields}}
                summary["original_reproduction"]["ok"] = all(item["matches"]
                    for item in summary["original_reproduction"]["checks"].values())
            save(folder / "summary.json", summary)
            save(folder / "artifacts.json", {p.name: sha256_file(p) for p in folder.iterdir()
                if p.is_file() and p.name != "artifacts.json"})
            summaries.append(summary)
        except Exception as exc:
            append_attempt(Path(output) / "attempts.jsonl", {"run_id": name, "state": "failed", "error": repr(exc)})
            raise
        append_attempt(Path(output) / "attempts.jsonl", {"run_id": name, "state": "completed", "control": True})
    save(Path(output) / "control_comparison.json", summaries)
    return summaries


def write_report(output, rows, evidence):
    output = Path(output)
    primaries = [r for r in rows if r["role"] == "primary"]
    acceptance = [evaluate_primary(row, rows, evidence) for row in primaries]
    save(output / "acceptance.json", acceptance)
    save(output / "suite.json", rows)
    columns = ["run_id", "variant", "financing_mode", "role", "start", "end", "return_pct", "max_drawdown_pct",
        "annualized_volatility", "fill_count", "financing_total", "stale_valuation_count"]
    pd.DataFrame(rows)[columns].to_csv(output / "results.csv", index=False)
    controls_file = output / "control_comparison.json"
    controls = json.loads(controls_file.read_text(encoding="utf-8")) if controls_file.exists() else []
    engineering_file = output.parent / "engineering_validation.json"
    engineering = json.loads(engineering_file.read_text(encoding="utf-8")) if engineering_file.exists() else {}
    lines = ["# TrendPortfolioV3 日线研究报告", "", "所有结果为已见历史的回顾性研究；不构成实盘准入。", "",
        f"完成 {len(rows)} 个已登记 V3 运行及 {len(controls)} 个 V2 对照；历史全市场证据："
        f"{'已核验' if evidence.get('full_market_verified') else '不足，不能宣称历史全市场通过'}。", "",
        "主评价为2020-01-01至2026-09-18，初始权益10,000 USDT；最大回撤按扣手续费、价格滑移及融资后的每日权益计算。",
        "两套规则及两种融资口径独立评价，未合并平仓样本。", "",
        "|方案|融资口径|收益|最大回撤|成交数|判定|", "|---|---|---:|---:|---:|---|"]
    if str(evidence.get("source_kind", "")).startswith("synthetic"):
        lines.insert(2, "**仅为合成工程测试样本，任何显示收益均不可作为策略绩效证据。**")
    for row, verdict in zip(primaries, acceptance):
        lines.append(f"|{row['variant']}|{row['financing_mode']}|{row['return_pct']:.4f}%|"
            f"{row['max_drawdown_pct']:.4f}%|{row['fill_count']}|{verdict['status']}|")
    lines += ["", "## 数据与工程证据", "", f"已加载真实行情市场 {evidence.get('market_count', 0)} 个。",
        "归档发现、成功下载和具有当时可得上市/分类证据是三个不同口径。已退市档案保留，未知分类明确排除。",
        "", "```json", json.dumps(evidence.get("coverage", {}), ensure_ascii=False, indent=2), "```", "",
        "残留证据缺口：" + "；".join(map(str, evidence.get("limitations", []))), "",
        f"保存JUnit的唯一测试数：{engineering.get('unique_tests', '未汇总')}；"
        f"失败：{engineering.get('failures', '未汇总')}；工程状态：{engineering.get('status', '未汇总')}。",
        "完整验证明细见 ../engineering_validation.json 与对应XML。", "",
        "源码、完整参数矩阵、行情哈希及恢复条件见 preregistration.json；所有已启动尝试见 attempts.jsonl。",
        "工程测试验证执行约束，历史收益判定验证策略；二者分别报告。", "",
        "## 旧V2与健康恢复对照", "", "旧对照沿用其2022-01-01起的冻结六币行情口径，不能和V3全期收益直接作同窗排名。",
        "|对照|收益|最大回撤|恢复门槛可达性|成交数|原始复现|", "|---|---:|---:|---|---:|---|"]
    for row in controls:
        lines.append(f"|{row['run_id']}|{row.get('return_pct', 0):.4f}%|{row.get('max_drawdown_pct', 0):.4f}%|"
            f"{row.get('health_reachability', {}).get('status', 'unknown')}|{row.get('fill_count', 0)}|"
            f"{row.get('original_reproduction', {}).get('ok', '政策对照')}|")
    lines += ["", "V2-C原三币恢复门槛在两币池不可达；health_2仅修正这条独立登记的恢复条件。"
        "V2-D保留三币门槛，health_3作为身份对照。", "", "## 主方案持仓、杠杆与成本", "",
        "|方案 / 融资|最大持仓数|最大实际杠杆|手续费USDT|价格滑移USDT|融资USDT|普通成交额USDT|风险退出额USDT|",
        "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for row in primaries:
        d = row.get("execution_diagnostics", {})
        lines.append(f"|{row['variant']} / {row['financing_mode']}|{d.get('maximum_position_count', 0)}|"
            f"{d.get('maximum_gross_leverage', 0):.4f}|{d.get('commission', 0):.2f}|"
            f"{d.get('modeled_price_slippage', 0):.2f}|{row.get('financing_gross', 0):.2f}|"
            f"{d.get('ordinary_filled_notional', 0):.2f}|{d.get('forced_exit_filled_notional', 0):.2f}|")
    lines += ["", "价格滑移为逐笔成交价相对理论价格的绝对差乘成交数量，已体现在净权益中，不再次扣除。",
        "实际杠杆为持仓名义总额/净权益，借款与现金单独记账；3倍账户上限并非目标杠杆。", "",
        "## 分窗口、成本与固定邻域", "", f"本目录完成的{len(rows)}次结果在 results.csv。以下逐项列出主运行以外的固定尝试，没有按结果追加参数。", "",
        "|方案|融资|运行|收益|最大回撤|成交数|", "|---|---|---|---:|---:|---:|"]
    for row in rows:
        if row["role"] != "primary":
            lines.append(f"|{row['variant']}|{row['financing_mode']}|{row['run_id'].split('__', 2)[-1]}|"
                f"{row['return_pct']:.4f}%|{row['max_drawdown_pct']:.4f}%|{row['fill_count']}|")
    lines += ["", "## 逐方案裁决与阻塞原因", ""]
    for row, verdict in zip(primaries, acceptance):
        lines += [f"### {row['variant']} / {row['financing_mode']}", "",
            f"裁决：**{verdict['status']}**；退出日cohort数：{verdict['cohorts']['cohort_count']}。", "",
            "|门槛|状态|", "|---|---|"]
        for name, detail in verdict["gates"].items():
            lines.append(f"|{name}|{detail['status']}|")
        d = row.get("execution_diagnostics", {})
        lines += ["", "目标未完成、实际限制规则、健康状态和融资证据：", "", "```json",
            json.dumps({key: d.get(key, {}) for key in ("selection_rejection_counts", "limiting_rules",
                "unfilled_reasons", "financing_status_days", "financing_verified_day_fraction", "strategy_health")},
                ensure_ascii=False, indent=2), "```", "", "期末未平仓与归零损失压力：", "", "```json",
            json.dumps(row.get("terminal_valuation", {}), ensure_ascii=False, indent=2), "```", ""]
    lines += ["", "## 证据边界", "", "- 币池、上市/退市及分类缺口见冻结数据清单；未知分类不参与新开仓。",
        "- 假设融资与可验证融资分别评价；3倍只是账户上限，更严格的簇及风险预算继续生效。",
        "- 账户权益已计融资成本；未分配到平仓cohort的融资不得伪称PF已完整扣除融资。",
        "- 未平仓期末仅估值；陈旧估值及无法退出持仓独立披露，不制造成交。",
        "- 缺历史交易规则时，数量步长1e-8和最低订单5 USDT属于已登记执行假设；未声称逐日交易所过滤器已核验。",
        "- 没有打开旧前瞻观察窗口，也没有根据本轮收益追加搜索。", "",
        "详细门槛见 acceptance.json；逐笔事实、仓位目标、成本和估值见 runs/。", "",
        "![净值与回撤](equity_drawdown.png)", "", "![持仓、杠杆、换手、健康与成本](execution_diagnostics.png)"]
    (output / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    plot_results(output, primaries)


def plot_results(output, primaries):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 1, figsize=(12, 8), sharex=True)
    for row in primaries:
        curve = pd.read_csv(Path(output) / "runs" / row["run_id"] / "equity.csv", parse_dates=["timestamp"]).set_index("timestamp")
        label = row["variant"] + " / " + row["financing_mode"]
        axes[0].plot(curve.index, curve.equity / 10000., label=label)
        seeded_peak = curve.equity.cummax().clip(lower=10000.)
        axes[1].plot(curve.index, (curve.equity / seeded_peak - 1) * 100, label=label)
    axes[0].set_ylabel("Net equity / initial capital")
    axes[1].set_ylabel("Drawdown (%)")
    axes[1].axhline(-15, color="red", linestyle="--", label="15% research limit")
    for ax in axes:
        ax.legend(); ax.grid(alpha=.2)
    fig.tight_layout(); fig.savefig(Path(output) / "equity_drawdown.png", dpi=160); plt.close(fig)
    fig, axes = plt.subplots(3, 2, figsize=(15, 11), sharex=True)
    metrics = [("position_count", "Held markets"), ("gross_weight", "Gross exposure / net equity"),
               ("turnover_committed", "Weekly ordinary committed notional (USDT)"),
               ("health_multiplier", "Health risk multiplier")]
    for row in primaries:
        folder = Path(output) / "runs" / row["run_id"]
        label = row["variant"] + " / " + row["financing_mode"]
        payload = json.loads((folder / "portfolio_controller.json").read_text(encoding="utf-8"))
        audit = pd.DataFrame(payload.get("audit", []))
        if not audit.empty:
            dates = pd.to_datetime(audit.as_of, utc=True)
            for ax, (key, title) in zip(axes.flat, metrics):
                if key in audit:
                    ax.plot(dates, audit[key], label=label, alpha=.8)
                ax.set_ylabel(title)
        fills = row.get("trades", [])
        if fills:
            trades = pd.DataFrame(fills)
            dates = pd.to_datetime(trades.fill_time, utc=True)
            costs = trades.commission.fillna(0) + trades.slip.fillna(0).abs() * trades.qty.abs()
            axes[2, 0].plot(dates, costs.cumsum(), label=label)
        financing_file = folder / "financing_ledger.csv"
        if financing_file.stat().st_size > 5:
            ledger = pd.read_csv(financing_file)
            if not ledger.empty:
                axes[2, 1].plot(pd.to_datetime(ledger.timestamp, utc=True), ledger.amount.cumsum(), label=label)
    axes[2, 0].set_ylabel("Cumulative fee + modeled price slippage (USDT)")
    axes[2, 1].set_ylabel("Cumulative financing cost (USDT)")
    for ax in axes.flat:
        handles, labels = ax.get_legend_handles_labels()
        if handles:
            ax.legend(fontsize=7)
        ax.grid(alpha=.2)
    fig.tight_layout(); fig.savefig(Path(output) / "execution_diagnostics.png", dpi=150); plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--register-only", action="store_true")
    modes.add_argument("--resume", action="store_true")
    parser.add_argument("--only", help="Run one pre-registered identity in an isolated process")
    parser.add_argument("--workers", type=int, default=1, help="Isolated research processes; never shared config threads")
    args = parser.parse_args()
    logging.disable(logging.CRITICAL)
    if args.register_only:
        protocol = register(args.bundle, args.output)
        print(json.dumps({"registered_runs": len(protocol["specs"]), "output": str(args.output)}))
    else:
        resume(args.bundle, args.output, args.only, args.workers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
