"""Fixed-protocol revalidation on frozen Binance inputs. Never changes admission."""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import logging
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
import pandas as pd
from analysis.research_validation import walk_forward_splits
from backtest.engine import BacktestEngine
from backtest.reporting import ReportGenerator
from backtest.reporting.operating_periods import requested_period_curve, split_execution_records
from config.config import config
from core.data import DataHandler
from core.lots import CloseEvent
from core.metrics import calculate_equity_metrics
from core.strategy_health import classify_exit_controller
from core.reproducibility import canonical_json, deterministic_result_digest, sha256_file, sha256_frame
from scripts.prepare_revalidation_data import SYMBOLS, START, END


def save(path, value):
    path.write_text(json.dumps(json.loads(canonical_json(value)), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def event_row(event):
    return dict(event) if isinstance(event, dict) else asdict(event)


def load_inputs(root):
    inventory = json.loads((root / "data_manifest.json").read_text(encoding="utf-8"))
    if set(inventory["symbols"]) != set(SYMBOLS) or not inventory["complete"]:
        raise ValueError("Strict 60/60 validated input gate failed")
    lifecycle = json.loads((ROOT / "config/universe60_lifecycle.json").read_text(encoding="utf-8"))
    frames = {}
    for symbol in SYMBOLS:
        entry = inventory["symbols"][symbol]
        path = root / "data_inputs" / entry["file"]
        if sha256_file(path) != entry["sha256"]:
            raise ValueError(f"Data hash mismatch: {symbol}")
        frame = pd.read_csv(path, index_col="timestamp", parse_dates=True, float_precision="round_trip").loc[START:END]
        prices = frame[["open", "high", "low", "close", "volume"]]
        if frame.empty or frame.index.has_duplicates or not frame.index.is_monotonic_increasing or not np.isfinite(prices.to_numpy()).all():
            raise ValueError(f"Invalid time series: {symbol}")
        if ((prices[["open", "high", "low", "close"]] <= 0).any(axis=1) | (prices.volume < 0)
            | (prices.high < prices[["open", "close", "low"]].max(axis=1))
            | (prices.low > prices[["open", "close", "high"]].min(axis=1))).any():
            raise ValueError(f"Invalid OHLCV: {symbol}")
        if entry["truncated_before_end"] and symbol not in lifecycle:
            raise ValueError(f"Unexplained historical truncation: {symbol}")
        frame["entry_blocked"] = False
        frame["scheduled_exit"] = False
        if symbol in lifecycle:
            event = lifecycle[symbol]
            announced = pd.Timestamp(event["announced_at"]).tz_convert(None)
            cutoff = pd.Timestamp(event["margin_cutoff"]).tz_convert(None)
            # Last full daily close before the advertised margin deadline.
            exit_day = cutoff.normalize() - pd.Timedelta(days=1)
            if announced >= exit_day or exit_day not in frame.index:
                raise ValueError("No executable announced exit boundary")
            # Delisting information affects decisions only after publication.
            frame.loc[frame.index + pd.Timedelta(days=1) >= announced, "entry_blocked"] = True
            frame.loc[exit_day, "scheduled_exit"] = True
            frame = frame.loc[:exit_day]
        frames[symbol] = DataHandler.annotate_quality(frame)
    return frames, inventory, lifecycle


def source_hashes():
    paths = []
    for folder in ("core", "backtest", "strategies", "router", "composition", "config", "analysis", "live_trading", "scripts"):
        paths.extend(p for p in (ROOT / folder).rglob("*") if p.suffix in {".py", ".yaml", ".json"} and "__pycache__" not in p.parts)
    paths.extend(ROOT / p for p in ("run_live.py", "pyproject.toml", "requirements.lock.txt") if (ROOT / p).exists())
    return {p.relative_to(ROOT).as_posix(): sha256_file(p) for p in sorted(paths)}


def protocol(frames, lifecycle):
    timeline = pd.DatetimeIndex(sorted(set().union(*(set(df.index) for df in frames.values()))))
    a, b = int(len(timeline) * .6), int(len(timeline) * .8)
    segments = {"train60": [timeline[0], timeline[a - 1]],
                "validation20": [timeline[a], timeline[b - 1]], "final20": [timeline[b], timeline[-1]]}
    windows = walk_forward_splits(len(timeline), train_size=730, validation_size=180, test_size=180,
                                 purge_size=30, embargo_size=30)
    return {"capital": 10000, "requested_start": START, "requested_end_inclusive_utc": END,
            "market_start": timeline[0], "symbols": SYMBOLS, "parameters": deepcopy(config._config),
            "segments": segments, "rolling_windows": windows, "timeline": [str(t) for t in timeline],
            "cost_multipliers": [1, 1.5, 2, 3], "bootstrap_seed": 42, "bootstrap_samples": 2000,
            "bootstrap_block_length_cohorts": 5, "lifecycle": lifecycle,
            "source_hashes": source_hashes(), "engine_input_hashes": {s: sha256_frame(df) for s, df in frames.items()},
            "interpretation": "Retrospective fixed-parameter validation; no claim of never-observed history",
            "borrow_model": "8% annual default; historical eligibility, rates and capacity not fully evidenced",
            "static_selection_bias": True, "admission": "paused_revalidation"}


def run_one(name, root, all_frames, frozen, *, start=START, end=END, multiplier=1, forced=False):
    folder = root / "runs" / name
    if (folder / "summary.json").exists():
        summary = json.loads((folder / "summary.json").read_text())
        cached = {"close_events": [CloseEvent(**row) for row in json.loads((folder / "close_events.json").read_text())],
                  "_digest": json.loads((folder / "digest.json").read_text())}
        return cached, summary
    if folder.exists():
        sequence = 1
        while folder.with_name(f"{name}.incomplete.{sequence}").exists():
            sequence += 1
        folder.rename(folder.with_name(f"{name}.incomplete.{sequence}"))
    folder.mkdir(parents=True, exist_ok=False)
    start, end = pd.Timestamp(start).tz_localize(None), pd.Timestamp(end).tz_localize(None)
    frames = {}
    for symbol, frame in all_frames.items():
        eligible = frame.loc[:end]
        before = eligible.loc[eligible.index < start].tail(100)
        active = eligible.loc[eligible.index >= start]
        if not active.empty:
            frames[symbol] = pd.concat([before, active]).copy()
    prior = config._config
    current = deepcopy(frozen["parameters"])
    for field in ("commission_rate_taker", "commission_rate_maker", "slippage_bps", "spread_bps", "volatility_slippage_factor", "impact_coefficient"):
        current["execution"][field] *= multiplier
    current["account"]["default_borrow_rate_annual"] *= multiplier
    current["account"]["liquidation_penalty_bps"] *= multiplier
    current["backtest"]["end_of_backtest_mode"] = "forced_liquidation" if forced else "mark_to_market"
    save(folder / "resolved_config.json", current)
    config._config = current
    random.seed(42)
    np.random.seed(42)
    print(f"RUN {name} {start.date()}..{end.date()} cost={multiplier}", flush=True)
    try:
        engine = BacktestEngine(initial_capital=10000, timeframe="1d", alignment_mode="union",
                                benchmark_mode="fixed", run_id="fixed-revalidation60", trading_start=start)
        result = engine.run(frames, routing_log_enabled=False)
        # Legacy result['close_events'] is per-strategy observation counts.
        # The execution adapter owns the actual immutable close facts.
        result["close_event_records"] = list(engine.execution_adapter.broker.close_events)
        if not result["accounting_check"]["ok"]:
            raise ValueError(f"Accounting identity failed: {name}")
        if result["lifecycle"].get("unresolved_risk_positions"):
            raise ValueError("Risk liquidation is incomplete; no cash-tail result can be claimed")
        reporter = ReportGenerator(str(folder))
        metrics = reporter.generate(result["trades"], result["equity_curve"], metrics_only=True,
                                     benchmark_curve=result["benchmark"], close_events=result["close_events"],
                                     lifecycle=result["lifecycle"], strategy_health=result["strategy_health"],
                                     protective_stops=result["protective_stop_summary"])
        save(folder / "metrics.json", metrics)
        pd.DataFrame(result.get("strategy_activity", [])).to_csv(folder / "strategy_activity.csv", index=False)
        closed_legs = reporter._reconstruct_closed_trades(pd.DataFrame(result["trades"]))
        pd.DataFrame(reporter._aggregate_round_trips(closed_legs)).to_csv(folder / "closed_trades.csv", index=False)
        save(folder / "digest.json", deterministic_result_digest(result))
        for key in ("lifecycle", "accounting_check", "strategy_health", "breaker_state", "account_cost_contract"):
            save(folder / f"{key}.json", result[key])
        for key in ("financing_ledger", "execution_audit", "stop_order_audit", "allocation_audit", "breaker_audit",
                    "risk_budget_reconciliation", "correlated_risk_audit", "strategy_health_cohorts", "strategy_health_transitions",
                    "drawdown_budget_audit", "entry_observations"):
            pd.DataFrame(result.get(key, [])).to_csv(folder / f"{key}.csv", index=False)
        actual_trades, valuation_transfers = split_execution_records(result['trades'], mark_to_market=not forced)
        pd.DataFrame(result['trades']).to_csv(folder / 'engine_trades.csv', index=False)
        columns = pd.DataFrame(result['trades']).columns
        pd.DataFrame(actual_trades, columns=columns).to_csv(folder / 'trades.csv', index=False)
        pd.DataFrame(valuation_transfers, columns=columns).to_csv(folder / 'valuation_transfers.csv', index=False)
        pd.DataFrame([event_row(event) for event in result["close_event_records"]]).to_csv(folder / "close_events.csv", index=False)
        save(folder / "close_events.json", [event_row(event) for event in result["close_event_records"]])
        result["equity_curve"].to_csv(folder / "equity_engine.csv")
        full = requested_period_curve(result['equity_curve'], start, end, capital=10000,
            lifecycle=result['lifecycle'], activity=result.get('strategy_activity', []))
        full.to_csv(folder / "equity_requested_period.csv", index_label="timestamp")
        period_metrics = {}
        active_start = min((pd.Timestamp(t.get("fill_time", t.get("timestamp"))).tz_localize(None) for t in actual_trades), default=None)
        active_end = max((pd.Timestamp(t.get("fill_time", t.get("timestamp"))).tz_localize(None) for t in actual_trades), default=None)
        market_start = max(start, min(f.index.min() for f in all_frames.values()))
        for label, subset in {"requested": full, "market": full.loc[market_start:],
                              "active": full.loc[active_start:active_end] if active_start is not None else full.iloc[:0]}.items():
            if subset.empty:
                period_metrics[label] = {"status": "no_activity"}
                continue
            baseline_curve = pd.concat([pd.DataFrame({"equity": [10000]}, index=[subset.index[0] - pd.Timedelta(days=1)]), subset[["equity"]]])
            values = calculate_equity_metrics(baseline_curve)
            returns = baseline_curve.equity.pct_change().dropna()
            downside = np.sqrt(np.mean(np.minimum(returns, 0) ** 2))
            values.update(start=subset.index[0], end=subset.index[-1],
                          SortinoRatio=float(returns.mean() / downside * np.sqrt(365.25)) if downside else None,
                          MeanGrossExposure=float(subset.get("gross_exposure_pct_equity", pd.Series([0])).mean()),
                          Turnover=sum(abs(float(t["qty"]) * float(t["fill_price"])) for t in actual_trades
                              if subset.index[0] <= pd.Timestamp(t.get('fill_time', t.get('timestamp'))).tz_localize(None).normalize() <= subset.index[-1]) / float(subset.equity.mean()))
            period_metrics[label] = values
        save(folder / "period_metrics.json", period_metrics)
        yearly = []
        baseline = 10000
        for year, rows in full.groupby(full.index.year):
            final = float(rows.equity.iloc[-1])
            yearly.append({"year": year, "start_equity": baseline, "end_equity": final, "return_pct": (final / baseline - 1) * 100})
            baseline = final
        pd.DataFrame(yearly).to_csv(folder / "annual_results.csv", index=False)
        summary = {"name": name, "start": start, "end": end, "cost_multiplier": multiplier, "forced_exit": forced,
                   "initial_capital": 10000, "final_equity": float(full.equity.iloc[-1]),
                   "return_pct": (float(full.equity.iloc[-1]) / 10000 - 1) * 100,
                   "max_drawdown_pct": float((1 - full.equity / full.equity.cummax()).max()) * 100,
                   "fill_count": len(actual_trades), "valuation_transfer_count": len(valuation_transfers),
                   "closed_event_count": len(result["close_event_records"]),
                   "profit_factor": metrics.get("ProfitFactor"), "lifecycle": result["lifecycle"],
                   "accounting_ok": True}
        save(folder / "summary.json", summary)
        print(f"DONE {name}: equity={summary['final_equity']:.2f}", flush=True)
        result["_digest"] = deterministic_result_digest(result)
        return result, summary
    finally:
        config._config = prior


def concentration(result, *, mark_to_market=True):
    # Preserve all close facts rather than the health machine's retained tail.
    rows = [event_row(event) for event in result.get("close_event_records", result.get("close_events", []))]
    if mark_to_market:
        rows = [row for row in rows if row["exit_reason"] != "EndOfBacktest"]
    grouped = {}
    for row in rows:
        key = (row["opening_strategy_id"], str(pd.Timestamp(row["timestamp"]).date()),
               classify_exit_controller(row["exit_reason"]) + ":" + (row["risk_action_id"] or ""))
        grouped[key] = grouped.get(key, 0.0) + row["realized_pnl"]
    values = np.array([grouped[key] for key in sorted(grouped, key=lambda key: (key[1], key[0], key[2]))])
    output = {"cohort_count": len(values), "block_length": 5, "seed": 42, "iterations": 2000,
              "sample_status": "sufficient_for_estimation" if len(values) >= 30 else "insufficient",
              "tail_mark_events_excluded": mark_to_market, "scenarios": {}}
    for top in (0, 5, 10):
        retained = values.copy()
        positive = np.flatnonzero(values > 0)
        removed = positive[np.argsort(values[positive])[-top:]] if top and len(positive) else np.array([], dtype=int)
        retained[removed] = 0  # Keep chronological spacing of cohorts for block bootstrap.
        rng = np.random.default_rng(42)
        totals, pfs = [], []
        if len(values):
            for _ in range(2000):
                starts = rng.integers(0, len(values), size=int(np.ceil(len(values) / 5)))
                indices = np.concatenate([(np.arange(5) + origin) % len(values) for origin in starts])[:len(values)]
                sample = retained[indices]
                totals.append(float(sample.sum()))
                loss = -sample[sample < 0].sum()
                if loss:
                    pfs.append(float(sample[sample > 0].sum() / loss))
        output["scenarios"][str(top)] = {"removed_count": len(removed), "remaining_net_closed_pnl": float(retained.sum()),
            "profit_factor": float(retained[retained > 0].sum() / -retained[retained < 0].sum()) if (retained < 0).any() else None,
            "pnl_95pct_ci": np.quantile(totals, [.025, .975]).tolist() if totals else None,
            "pf_95pct_ci": np.quantile(pfs, [.025, .975]).tolist() if pfs else None,
            "interpretation": "cohort attribution sensitivity, not a counterfactual engine rerun"}
    return output


def write_research_outputs(root, frames, summaries):
    """Publish descriptive baselines and explicit existing admission gates."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from analysis.research_validation import AdmissionThresholds
    def benchmark(start, end):
        start, end = pd.Timestamp(start).tz_localize(None), pd.Timestamp(end).tz_localize(None)
        dates = pd.date_range(start, end)
        basket = pd.Series(0.0, index=dates)
        for symbol in ("BTC/USDT", "ETH/USDT"):
            data = frames[symbol].loc[start:end]
            sleeve = pd.Series(5000.0, index=dates)
            if not data.empty:
                entry = data.index[0]
                sleeve.loc[entry:] = (5000 / float(data.open.iloc[0]) * data.close).reindex(dates[dates >= entry]).ffill()
            basket += sleeve
        return basket
    benchmarks = pd.DataFrame({"cash": 10000.0, "btc_eth_equal_buy_hold_gross": benchmark(START, END)})
    benchmarks.to_csv(root / "benchmarks.csv", index_label="timestamp")
    save(root / "benchmark_metrics.json", {name: calculate_equity_metrics(series.to_frame("equity")) for name, series in benchmarks.items()})
    save(root / "benchmark_convention.json", {"entry": "First available daily open in each evaluation period; 50% per sleeve, unrebalanced",
         "capital": 10000, "costs": "Gross unlevered reference (no trading fees); not a financed strategy simulation", "pre_listing": "Cash"})
    lookup = {row["name"]: row for row in summaries}
    threshold = AdmissionThresholds()
    gates = {}
    for name in ("main_1", "final20"):
        events = [CloseEvent(**row) for row in json.loads((root / "runs" / name / "close_events.json").read_text())]
        cohorts = concentration({"close_events": events})
        save(root / f"{name}_cohort_concentration.json", cohorts)
        base = cohorts["scenarios"]["0"]
        enough = cohorts["sample_status"] == "sufficient_for_estimation"
        def assessed(value):
            return "insufficient" if not enough else ("pass" if value else "fail")
        row = lookup[name]
        bench = benchmark(pd.Timestamp(row["start"]), pd.Timestamp(row["end"]))
        gross_bench_return = float(bench.iloc[-1] / 10000 - 1)
        gates[name] = {
            "cohort_sample": cohorts["sample_status"],
            "PF_above_1_15_and_block_CI_lower_above_1": assessed(base["profit_factor"] is not None and base["profit_factor"] > threshold.minimum_pf
                and base["pf_95pct_ci"] is not None and base["pf_95pct_ci"][0] > threshold.minimum_pf_ci_lower),
            "drawdown_at_most_20pct": "pass" if row["max_drawdown_pct"] <= threshold.maximum_drawdown * 100 else "fail",
            "remove_top5_top10_remains_profitable": assessed(all(cohorts["scenarios"][str(n)]["remaining_net_closed_pnl"] > 0 for n in (5, 10))),
            "positive_edge_above_BTC_ETH_gross": "pass" if row["return_pct"] > max(0, gross_bench_return * 100) else "fail",
            "benchmark_return_pct": gross_bench_return * 100}
    gates["cost_1_5_positive_full_engine_rerun"] = "pass" if lookup["cost_1.5"]["return_pct"] > 0 else "fail"
    gates["determinism"] = "pass" if json.loads((root / "determinism.json").read_text())["passed"] else "fail"
    gates["not_performed"] = ["parameter_optimization", "factor_selection", "cross_exchange_validation"]
    save(root / "research_gates.json", gates)
    curve = pd.read_csv(root / "runs/main_1/equity_requested_period.csv", index_col="timestamp", parse_dates=True)
    fig, axes = plt.subplots(2, 1, figsize=(13, 8), gridspec_kw={"height_ratios": [2, 1]}, sharex=True)
    axes[0].plot(curve.index, curve.equity, label="TrendBreakout, modeled net equity")
    axes[0].plot(benchmarks.index, benchmarks.cash, label="Cash")
    axes[0].plot(benchmarks.index, benchmarks.btc_eth_equal_buy_hold_gross, label="BTC/ETH 50/50 gross buy & hold")
    axes[0].set_yscale("log")
    axes[0].set_ylabel("USDT (log scale)")
    axes[0].legend(loc="upper left")
    axes[1].fill_between(curve.index, (curve.equity / curve.equity.cummax() - 1) * 100, 0, alpha=.4)
    axes[1].set_ylabel("Strategy drawdown %")
    for ax in axes:
        ax.grid(alpha=.2)
    fig.suptitle("Frozen 60-coin Binance revalidation | 10,000 USDT | 2016-01-01 to 2026-06-30 UTC")
    fig.tight_layout()
    fig.savefig(root / "equity_drawdown.png", dpi=160)
    plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--freeze-only", action="store_true")
    args = parser.parse_args()
    root = args.root
    logging.disable(logging.CRITICAL)
    frames, inventory, lifecycle = load_inputs(root)
    frozen = protocol(frames, lifecycle)
    freeze_path = root / "frozen_protocol.json"
    if freeze_path.exists():
        if json.loads(freeze_path.read_text(encoding="utf-8")) != json.loads(canonical_json(frozen)):
            raise ValueError("Frozen code/config/data/protocol changed; create a new run package")
    else:
        save(freeze_path, frozen)
    if args.freeze_only:
        return
    checks = root / "engineering_gate.json"
    if not checks.exists() or json.loads(checks.read_text())["passed"] is not True:
        raise ValueError("Engineering acceptance evidence is required before research runs")
    if json.loads(checks.read_text())["source_hashes"] != frozen["source_hashes"]:
        raise ValueError("Engineering checks refer to different source files")
    summaries = []
    timeline = pd.to_datetime(frozen["timeline"])
    _, summary = run_one("integration_smoke", root, frames, frozen, start=timeline[0], end=timeline[min(180, len(timeline)-1)], forced=True)
    summaries.append(summary)
    for name, (start, end) in frozen["segments"].items():
        _, summary = run_one(name, root, frames, frozen, start=start, end=end, forced=True)
        summaries.append(summary)
    for index, window in enumerate(frozen["rolling_windows"]):
        _, summary = run_one(f"rolling_{index:02d}", root, frames, frozen,
                             start=timeline[window["test_start"]], end=timeline[window["test_end"] - 1], forced=True)
        summaries.append(summary)
    digests, principal = [], None
    for index in range(3):
        result, summary = run_one(f"main_{index + 1}", root, frames, frozen)
        digests.append(result["_digest"])
        summaries.append(summary)
        if index == 0:
            principal = result
    save(root / "determinism.json", {"passed": digests[0] == digests[1] == digests[2], "runs": digests})
    save(root / "cohort_concentration.json", concentration(principal))
    for multiplier in (1.5, 2, 3):
        _, summary = run_one(f"cost_{multiplier}", root, frames, frozen, multiplier=multiplier)
        summaries.append(summary)
    _, summary = run_one("end_exit_sensitivity", root, frames, frozen, forced=True)
    summaries.append(summary)
    save(root / "all_run_summaries.json", summaries)
    write_research_outputs(root, frames, summaries)
    save(root / "research_status.json", {"backtests_completed": True, "admission": "paused_revalidation",
                                         "interpretation": "Assess statistical gates separately; completion is not admission"})


if __name__ == "__main__":
    main()
