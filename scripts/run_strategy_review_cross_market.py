"""Eight fixed cross-venue/timeframe runs using verified public spot candles.

Run --prepare-only before execution. Each job then runs in its own process; the
default driver is sequential and never takes more than one backtest slot.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import logging
from pathlib import Path
import random
import subprocess
import sys

import numpy as np
import pandas as pd


SYMBOLS = ("BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "ADA/USDT", "LTC/USDT")
VENUES = ("binance", "okx")
TIMEFRAMES = {"1d": pd.Timedelta(days=1), "4h": pd.Timedelta(hours=4)}
PERIODS = {"historical": ("2022-01-01", "2026-07-01"),
           "recent_drift": ("2026-07-01", "2026-09-19")}
ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = canonical(value) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") != content:
        raise ValueError(f"Immutable artifact already differs: {path}")
    path.write_text(content, encoding="utf-8")


def utc(value):
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def jobs():
    return [dict(name=f"{period}_{venue}_{timeframe}", period=period, venue=venue, timeframe=timeframe,
                 start=start, end_exclusive=end, symbols=list(SYMBOLS), warmup_bars=100)
            for period, (start, end) in PERIODS.items() for timeframe in TIMEFRAMES for venue in VENUES]


def select_closed_period(frame, *, timeframe, start, end_exclusive, asof, warmup_bars=100):
    """Validate actual closed UTC bars; never synthesize OHLCV or fill holes."""
    if timeframe not in TIMEFRAMES:
        raise ValueError("Unregistered timeframe")
    step = TIMEFRAMES[timeframe]
    start, end, asof = utc(start), utc(end_exclusive), utc(asof)
    source = frame.copy()
    source.index = pd.DatetimeIndex(pd.to_datetime(source.index, utc=True, errors="raise"))
    if source.index.has_duplicates or not source.index.is_monotonic_increasing:
        raise ValueError("Candles must be unique and ordered")
    if any(stamp.value % step.value for stamp in source.index):
        raise ValueError("Candle is off its UTC timeframe boundary")
    source = source.loc[source.index + step <= asof]
    active = source.loc[(source.index >= start) & (source.index < end)]
    expected = pd.date_range(start, end, freq=step, inclusive="left")
    missing = expected.difference(active.index)
    if len(missing):
        raise ValueError(f"Missing evaluation bars: {len(missing)}; first={missing[0].isoformat()}")
    before = source.loc[source.index < start].tail(warmup_bars)
    expected_before = pd.date_range(start - warmup_bars * step, start, freq=step, inclusive="left")
    if len(before) != warmup_bars or not before.index.equals(expected_before):
        raise ValueError("Selected 100 actual warmup bars do not cover the required contiguous interval")
    selected = pd.concat([before, active]).copy()
    values = selected[["open", "high", "low", "close", "volume"]].to_numpy(dtype=float)
    if not np.isfinite(values).all() or (values[:, :4] <= 0).any() or (values[:, 4] < 0).any():
        raise ValueError("Invalid OHLCV in selected support")
    if ((selected.high < selected[["open", "close", "low"]].max(axis=1)).any()
            or (selected.low > selected[["open", "close", "high"]].min(axis=1)).any()):
        raise ValueError("Inconsistent OHLC in selected support")
    return selected


def load_inputs(data_root, job, *, asof, protocol_hash):
    if tuple(job.get("symbols", SYMBOLS)) != SYMBOLS or job["venue"] not in VENUES:
        raise ValueError("Unregistered venue or fixed symbol set")
    frames, evidence = {}, {}
    for symbol in SYMBOLS:
        name = f"{job['venue']}_{symbol.replace('/', '')}_{job['timeframe']}"
        manifest_path = Path(data_root) / f"{name}.manifest.json"
        if not manifest_path.is_file():
            raise ValueError(f"Missing fixed stream manifest: {name}")
        record = json.loads(manifest_path.read_text(encoding="utf-8"))
        for key, expected in (("venue", job["venue"]), ("symbol", symbol),
                              ("timeframe", job["timeframe"]), ("market_type", "spot")):
            if record.get(key) != expected:
                raise ValueError(f"Stream identity mismatch: {name}:{key}")
        if record.get("substitution") is not None or record.get("timezone") != "UTC":
            raise ValueError(f"Unregistered substitution or timezone: {name}")
        if record["identity"]["protocol_sha256"] != protocol_hash:
            raise ValueError(f"Collector protocol identity mismatch: {name}")
        if record.get("csv_path") != f"{name}.csv":
            raise ValueError(f"Cross-venue candle path substitution: {name}")
        path = Path(data_root) / record["csv_path"]
        if digest(path) != record["csv_sha256"]:
            raise ValueError(f"Candle input checksum mismatch: {name}")
        frame = pd.read_csv(path, index_col="timestamp")
        selected = select_closed_period(frame, timeframe=job["timeframe"], start=job["start"],
                                        end_exclusive=job["end_exclusive"], asof=asof)
        frames[symbol] = selected
        # A rejected early warmup candle is disclosed even when outside the
        # selected 100-bar support and therefore irrelevant to this experiment.
        evidence[symbol] = dict(manifest_sha256=digest(manifest_path), csv_sha256=digest(path),
            source_status=record["status"], source_gaps=record.get("gaps", []),
            source_failures=record.get("failures", []), selected_status="complete",
            selected_rows=len(selected), warmup_rows=100, first=selected.index[0].isoformat(),
            last=selected.index[-1].isoformat(), endpoint=record["endpoint"],
            selected_sha256=hashlib.sha256(selected.to_csv(lineterminator="\n").encode()).hexdigest())
    if tuple(frames) != SYMBOLS:
        raise ValueError("Fixed six-symbol input set changed")
    return frames, evidence


def resolved_parameters(base, job):
    parameters = deepcopy(base)
    if parameters["state"]["stability_period"] != 5 or parameters["router"]["cooldown_bars"] != 2:
        raise ValueError("Official state/cooldown defaults changed")
    if parameters["strategy_governance"]["TrendBreakout"] != "paused_revalidation":
        raise ValueError("Admission policy changed")
    parameters["execution"].update(commission_rate_maker=0.001, commission_rate_taker=0.001,
        fee_schedule=dict(venue=job["venue"], market_type="spot",
            source="Registered common research assumption: maker/taker 0.10%; not historical venue-tier reconstruction"))
    parameters["account"].update(mode="spot", initial_margin_rate=1.0, funding_rate_required=False,
                                 default_borrow_rate_annual=0.0)
    parameters["risk"]["max_leverage"] = 1.0
    parameters["data"]["timeframe"] = job["timeframe"]
    parameters["backtest"]["end_of_backtest_mode"] = "mark_to_market"
    parameters.setdefault("research", {}).update(
        experiment_id=f"strategy-review-20260919:cross-market:{job['name']}", entry_audit=True,
        trend_breakout_parameters=dict(entry_window=20, exit_window=10))
    return parameters


def verify_source(batch, source_root):
    manifest = json.loads((batch / "revised_manifest.json").read_text(encoding="utf-8"))
    for relative, expected in manifest["source_hashes"].items():
        if digest(source_root / relative) != expected:
            raise ValueError("Frozen source changed: " + relative)
    return manifest["source_hashes"]


def prepare(batch, source_root, data_root):
    protocol_path = batch / "review_protocol.json"
    protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
    source = verify_source(batch, source_root)
    folder = batch / "cross_market"
    entries = []
    for job in jobs():
        parameters = resolved_parameters(protocol["parameters"], job)
        try:
            _, inputs = load_inputs(data_root, job, asof=protocol["registered_at"], protocol_hash=digest(protocol_path))
            status, error = "ready", None
        except (ValueError, FileNotFoundError) as exc:
            inputs, status, error = {}, "evidence_incomplete", str(exc)
        identity = dict(job=job, source=source, inputs=inputs, parameters=parameters,
                        protocol_sha256=digest(protocol_path), runner_sha256=digest(__file__))
        identity_hash = hashlib.sha256(canonical(identity).encode()).hexdigest()
        entry = dict(**job, input_status=status, input_error=error, identity_sha256=identity_hash,
                     identity=identity)
        entries.append(entry)
        save(folder / "jobs" / f"{job['name']}.json", entry)
    registry = dict(schema="cross_market_registry/v1", run_count=8, jobs=entries, initial_capital=10000,
        assumptions=dict(account="cash spot, no leverage or borrowing", warmup="100 complete actual bars per symbol",
            fees="Both venues modeled at maker/taker 0.10%; not historical fee-tier evidence",
            slippage="Unchanged frozen spread, slippage and participation-impact assumptions",
            durations="20/10 breakout, 5-bar state confirmation, 2-bar cooldown on both timeframes; unequal clock durations",
            hold_time="Calendar-based holding/recovery controls retain their official clock durations",
            lifecycle="Static six-symbol selection; retrospective test only; no historical pool reconstruction",
            missing_bars="Refuse missing selected support; never substitute or zero-fill", admission="paused_revalidation"))
    save(folder / "registry.json", registry)
    return registry


def run_job(batch, source_root, data_root, job_name):
    folder = batch / "cross_market" / "runs" / job_name
    spec = json.loads((batch / "cross_market" / "jobs" / f"{job_name}.json").read_text(encoding="utf-8"))
    if spec["input_status"] != "ready":
        save(folder / "input_failure.json", dict(status="evidence_incomplete", reason=spec["input_error"]))
        return 2
    verify_source(batch, source_root)
    if digest(__file__) != spec["identity"]["runner_sha256"]:
        raise ValueError("Runner changed after experiment registration")
    protocol = json.loads((batch / "review_protocol.json").read_text(encoding="utf-8"))
    frames, evidence = load_inputs(data_root, spec, asof=protocol["registered_at"],
                                  protocol_hash=digest(batch / "review_protocol.json"))
    if canonical(evidence) != canonical(spec["identity"]["inputs"]):
        raise ValueError("Selected input evidence changed after registration")
    if (folder / "completion.json").exists():
        completion = json.loads((folder / "completion.json").read_text(encoding="utf-8"))
        if completion["identity_sha256"] != spec["identity_sha256"]:
            raise ValueError("Cache identity mismatch")
        for path, expected in completion["artifacts"].items():
            if digest(folder / path) != expected:
                raise ValueError("Cached artifact changed: " + path)
        print("VERIFIED CACHE " + job_name, flush=True)
        return 0
    if folder.exists() and any(folder.iterdir()):
        raise ValueError("Incomplete run artifacts exist; preserve them and select a new output batch")
    folder.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(source_root.resolve()))
    from backtest.engine import BacktestEngine
    from config.config import config
    from core.state import MarketStateMachine
    from analysis.strategy_review import cohort_evidence, evaluate_gates
    from analysis.strategy_review_diagnostics import write_review_diagnostics
    if not Path(sys.modules["backtest.engine"].__file__).resolve().is_relative_to(source_root.resolve()):
        raise ValueError("Engine was imported outside the frozen snapshot")
    parameters = spec["identity"]["parameters"]
    config._config = deepcopy(parameters)
    random.seed(42)
    np.random.seed(42)
    logging.disable(logging.CRITICAL)
    for symbol, frame in frames.items():
        frame.to_csv(folder / f"input_{symbol.replace('/', '')}.csv", index_label="timestamp")
        frame.index = frame.index.tz_localize(None)
    save(folder / "resolved_config.json", parameters)
    save(folder / "input_evidence.json", evidence)
    engine = BacktestEngine(initial_capital=10000, warmup_period=100, timeframe=spec["timeframe"],
        alignment_mode="union", benchmark_mode="fixed", run_id=f"cross-market:{job_name}",
        trading_start=utc(spec["start"]).tz_localize(None), account_mode="spot")
    result = engine.run({symbol: frame.copy() for symbol, frame in frames.items()}, routing_log_enabled=False)
    if not result["accounting_check"]["ok"] or result["lifecycle"].get("unresolved_risk_positions"):
        save(folder / "execution_failure.json", dict(accounting=result["accounting_check"], lifecycle=result["lifecycle"]))
        raise ValueError("Accounting or unresolved liquidation failed")
    events = [asdict(event) for event in engine.execution_adapter.broker.close_events]
    cohort = cohort_evidence(events, mark_to_market=True)
    curve = result["equity_curve"]
    curve = curve.loc[(curve.index >= utc(spec["start"]).tz_localize(None)) &
                      (curve.index < utc(spec["end_exclusive"]).tz_localize(None))].copy()
    expected = pd.date_range(utc(spec["start"]).tz_localize(None), utc(spec["end_exclusive"]).tz_localize(None),
                             freq=TIMEFRAMES[spec["timeframe"]], inclusive="left")
    if not curve.index.equals(expected):
        raise ValueError("Engine equity does not cover every evaluation bar; no synthetic cash tail allowed")
    curve.to_csv(folder / "equity.csv", index_label="timestamp")
    peak = curve.equity.cummax().clip(lower=10000.0)
    summary = dict(name=job_name, venue=spec["venue"], timeframe=spec["timeframe"], period=spec["period"],
        start=spec["start"], end_exclusive=spec["end_exclusive"], initial_capital=10000,
        final_equity=float(curve.equity.iloc[-1]), return_pct=float((curve.equity.iloc[-1] / 10000 - 1) * 100),
        max_drawdown_pct=float((1 - curve.equity / peak).max() * 100), accounting_ok=True,
        cohort_count=cohort["cohort_count"], valuation_transfers_excluded_from_cohorts=True,
        fill_count=sum(trade.get("exit_reason") != "EndOfBacktest" for trade in result["trades"]))
    state_params = {key: value for key, value in parameters["state"].items()
                    if key in {"stability_period", "ma_fast", "ma_slow", "adx_period", "adx_threshold", "atr_period", "atr_pct_threshold"}}
    machine = MarketStateMachine(**state_params)
    regime_rows = []
    for symbol, frame in frames.items():
        states = machine.calculate_states(frame.copy())
        states = states.loc[states.index >= utc(spec["start"]).tz_localize(None)]
        regime_rows.extend(dict(symbol=symbol, timestamp=stamp, regime=state.name) for stamp, state in states.items())
    regime_frame = pd.DataFrame(regime_rows)
    regime_frame.to_csv(folder / "regime_bars.csv", index=False)
    summary["regime_bar_counts"] = regime_frame.regime.value_counts().to_dict()
    summary["distinct_regimes"] = len(summary["regime_bar_counts"])
    summary["gates"] = evaluate_gates(summary, cohort)
    summary["status"] = ("insufficient" if cohort["cohort_count"] < 30 or summary["distinct_regimes"] < 2 else
                         "pass" if all(value == "pass" for value in summary["gates"].values()) else "fail")
    for key in ("accounting_check", "lifecycle", "strategy_health", "account_cost_contract", "breaker_state"):
        save(folder / f"{key}.json", result[key])
    for key in ("strategy_health_cohorts", "strategy_health_transitions", "strategy_activity", "execution_audit", "entry_observations"):
        pd.DataFrame(result.get(key, [])).to_csv(folder / f"{key}.csv", index=False)
    trades = pd.DataFrame(result["trades"])
    trades.to_csv(folder / "engine_trades.csv", index=False)
    actual = [trade for trade in result["trades"] if trade.get("exit_reason") != "EndOfBacktest"]
    pd.DataFrame(actual, columns=trades.columns).to_csv(folder / "trades.csv", index=False)
    save(folder / "close_events.json", events)
    save(folder / "cohort_evidence.json", cohort)
    save(folder / "summary.json", summary)
    result["close_event_records"] = events
    result["review_period"] = dict(start=spec["start"], end=utc(spec["end_exclusive"]) - TIMEFRAMES[spec["timeframe"]])
    write_review_diagnostics(folder, frames, result, parameters)
    save(folder / "completion.json", dict(identity_sha256=spec["identity_sha256"],
        artifacts={path.name: digest(path) for path in folder.iterdir() if path.is_file()}))
    print(f"VERIFIED {job_name}: return={summary['return_pct']:.5f}% cohorts={cohort['cohort_count']}", flush=True)
    return 0


def summarize(batch):
    folder = batch / "cross_market"
    registry = json.loads((folder / "registry.json").read_text(encoding="utf-8"))
    summaries = {}
    for job in registry["jobs"]:
        path = folder / "runs" / job["name"] / "summary.json"
        receipt = path.parent / "completion.json"
        if receipt.exists():
            completed = json.loads(receipt.read_text(encoding="utf-8"))
            if completed["identity_sha256"] != job["identity_sha256"]:
                raise ValueError("Completed job identity mismatch")
            for relative, expected in completed["artifacts"].items():
                if digest(path.parent / relative) != expected:
                    raise ValueError("Completed job artifact changed: " + relative)
        summaries[job["name"]] = json.loads(path.read_text(encoding="utf-8")) if receipt.exists() else dict(
            name=job["name"], status="insufficient", reason=job["input_error"] or "run_not_completed")
    pairs = []
    for period in PERIODS:
        for timeframe in TIMEFRAMES:
            names = [f"{period}_{venue}_{timeframe}" for venue in VENUES]
            rows = [summaries[name] for name in names]
            ready = all(row.get("accounting_ok") for row in rows)
            expected_bars = int((utc(PERIODS[period][1]) - utc(PERIODS[period][0])) / TIMEFRAMES[timeframe])
            pair = dict(period=period, timeframe=timeframe, venues=list(VENUES), symbols=list(SYMBOLS),
                common_support_bars_per_symbol=expected_bars if ready else None,
                compared_runs=names, status="insufficient",
                interpretation="Paired identical complete evaluation support and fresh cash accounts; retrospective evidence")
            if ready:
                pair.update(returns_pct={row["venue"]: row["return_pct"] for row in rows},
                            cohorts={row["venue"]: row["cohort_count"] for row in rows},
                            regime_counts={row["venue"]: row["distinct_regimes"] for row in rows})
                if all(row["cohort_count"] >= 30 and row["distinct_regimes"] >= 2 for row in rows):
                    pair["status"] = "pass" if all(row["status"] == "pass" for row in rows) else "fail"
            pairs.append(pair)
    report = dict(schema="cross_market_evidence/v1", runs=summaries, paired_comparisons=pairs,
                  assumptions=registry["assumptions"], admission="paused_revalidation", retrospective_only=True)
    save(folder / "comparison.json", report)
    pd.DataFrame(summaries.values()).to_csv(folder / "summary.csv", index=False)
    text = ["# 公开行情跨市场与跨周期研究", "", "两市场均采用独立起跑、现金现货、无杠杆账户，初始资金 10,000 USDT。",
            "手续费统一假设为 maker/taker 0.10%；这不是交易所历史费率档位复刻。日线和四小时线使用相同 bar 参数，因此时钟期限不同。",
            "未替换币种、报价币或交易所，未填补缺失行情；缺少完整支持的运行标记证据不足。", "",
            "| 区间 | 周期 | Binance 收益 | OKX 收益 | 成对结论 |", "|---|---|---:|---:|---|"]
    for pair in pairs:
        values = pair.get("returns_pct", {})
        text.append(f"| {pair['period']} | {pair['timeframe']} | {values.get('binance', '缺失')} | {values.get('okx', '缺失')} | {pair['status']} |")
    text.extend(["", "每市场至少 30 个退出事件组且行情覆盖至少两种状态，才评价成对盈利门槛；否则结论为证据不足。",
                 "全部历史均为回顾性补充，静态六币选择偏差仍存在。没有选择参数或更改正式策略准入状态。"])
    (folder / "report.md").write_text("\n".join(text) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch", type=Path, default=ROOT / "reports/strategy_review_20260919")
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--public-data", type=Path)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--summarize-only", action="store_true")
    parser.add_argument("--job", choices=[job["name"] for job in jobs()])
    args = parser.parse_args()
    batch = args.batch.resolve()
    source_root = (args.source_root or batch / "revised_source").resolve()
    data_root = (args.public_data or batch / "public_data_validated").resolve()
    if args.job:
        return run_job(batch, source_root, data_root, args.job)
    if args.summarize_only:
        summarize(batch)
        return 0
    registry = prepare(batch, source_root, data_root)
    if args.prepare_only:
        print(canonical(dict(prepared=len(registry["jobs"]), ready=sum(job["input_status"] == "ready" for job in registry["jobs"]))))
        return 0
    for job in registry["jobs"]:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--batch", str(batch),
            "--source-root", str(source_root), "--public-data", str(data_root), "--job", job["name"]], check=False)
        if result.returncode not in {0, 2}:
            return result.returncode
    summarize(batch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
