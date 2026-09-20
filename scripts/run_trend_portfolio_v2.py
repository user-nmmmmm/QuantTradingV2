"""Compare the baseline and TrendPortfolioV2 on identical local market facts.

This is a sequential research helper: the shared configuration singleton is
temporarily replaced per arm and restored even on failure. Never call it from
concurrent threads in the same process. No data is fetched or live orders sent.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd
import numpy as np

from backtest.engine import BacktestEngine, DEFAULT_INITIAL_CAPITAL
from composition.factory import build_strategy_registry
from config.config import config
from core.data import DataHandler
from core.market_data import normalize_market_frame
from core.reproducibility import canonical_json, sha256_file, sha256_frame
from core.state import MarketState
from core.universe import PointInTimeUniverse, UniverseMembership, normalize_symbol

WARMUP_BARS = 120
V2_NAME = "TrendPortfolioV2"
REGIMES = ("TREND_UP", "TREND_DOWN", "SIDEWAYS", "VOLATILE")
EXPERIMENT_ID = "trend_portfolio_v2_phase1"
SUITE_ID = "trend_portfolio_v2_registered_daily_v1"
SUITE_SYMBOLS = ("BTC-USDT", "ETH-USDT", "SOL-USDT", "XRP-USDT", "ADA-USDT", "LTC-USDT")
SUITE_VARIANTS = ("V1", "V2-A", "V2-B", "V2-C", "V2-D")


def paired_configs(baseline):
    """Keep every non-strategy setting byte-for-byte equivalent in both arms."""
    original = deepcopy(baseline)
    if V2_NAME in original["routing"].values():
        raise ValueError("baseline routing already selects TrendPortfolioV2")
    if (original.get("research") or {}).get("strategy_ablation"):
        raise ValueError("strategy ablations cannot be combined with the phase-one comparison")
    revised = deepcopy(original)
    revised["routing"].update({state: V2_NAME for state in REGIMES})
    revised.setdefault("research", {})["experiment_id"] = EXPERIMENT_ID
    revised.setdefault("strategy_governance", {})[V2_NAME] = "isolated_research"
    assert_config_invariants(original, revised)
    return {"baseline": original, "trend_portfolio_v2": revised}


def assert_config_invariants(baseline, revised):
    """Fail before execution if anything beyond the authorized profile differs."""
    expected = deepcopy(baseline)
    expected["routing"].update({state: V2_NAME for state in REGIMES})
    expected.setdefault("research", {})["experiment_id"] = EXPERIMENT_ID
    expected.setdefault("strategy_governance", {})[V2_NAME] = "isolated_research"
    if canonical_json(revised) != canonical_json(expected):
        raise ValueError("paired configuration changed a setting outside the V2 strategy profile")


def _save(path, value):
    path.write_text(json.dumps(json.loads(canonical_json(value)), ensure_ascii=False,
                               indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _utc_naive(value):
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        raise ValueError("invalid trading boundary")
    return stamp.tz_convert("UTC").tz_localize(None) if stamp.tzinfo else stamp


def _prepare_inputs(data_map, trading_start, end):
    if not data_map:
        raise ValueError("at least one market frame is required")
    frames = {}
    end_stamp = None if end is None else _utc_naive(end)
    for symbol, source in sorted(data_map.items()):
        key = normalize_symbol(symbol)
        if key in frames:
            raise ValueError(f"duplicate normalized symbol: {key}")
        frame = normalize_market_frame(source)
        if end_stamp is not None:
            frame = frame.loc[frame.index <= end_stamp].copy()
        if not {"open", "high", "low", "close", "volume"}.issubset(frame.columns):
            raise ValueError(f"{symbol}: required OHLCV columns are missing")
        if len(frame) <= WARMUP_BARS:
            raise ValueError(f"{symbol}: need at least {WARMUP_BARS + 1} bars")
        frames[key] = frame
    start = (_utc_naive(trading_start) if trading_start is not None
             else max(frame.index[WARMUP_BARS] for frame in frames.values()))
    for symbol, frame in frames.items():
        if (frame.index < start).sum() < WARMUP_BARS or not (frame.index >= start).any():
            raise ValueError(f"{symbol}: trading_start needs 120 prior bars and an active bar")
    return frames, start


def _strategy_settings(strategies):
    result = {}
    for name, strategy in strategies.items():
        row = {"class": f"{type(strategy).__module__}.{type(strategy).__qualname__}",
               "allowed_states": sorted(state.name for state in strategy.allowed_states)}
        for field in ("entry_window", "exit_window", "use_obv", "horizons", "weights",
                      "market_state_mode", "exit_mode", "volatility_sizing", "asset_base_weight",
                      "target_annual_volatility", "max_asset_weight", "max_initial_risk",
                      "volatility_window", "periods_per_year", "medium_exit_window",
                      "sideways_breakout_atr", "long_score_threshold"):
            if hasattr(strategy, field):
                row[field] = getattr(strategy, field)
        for field in ("stop_policy", "score_policy"):
            policy = getattr(strategy, field, None)
            if is_dataclass(policy):
                row[field] = asdict(policy)
        health = getattr(strategy, "health", None)
        if health is not None:
            row["health_policy"] = asdict(health.policy)
        row["market_risk_multipliers"] = {
            state.name: strategy.entry_risk_multiplier(state) for state in MarketState}
        if name == V2_NAME:
            row["active_score"] = "0.50 sign(return20) + 0.30 sign(return60) + 0.20 sign(return120)"
            row["inherited_candidate_score_policy_applied"] = False
        result[name] = row
    return result


def _arm_summary(result, initial_capital):
    curve = result["equity_curve"]
    if curve.empty or "equity" not in curve:
        raise ValueError("engine returned no equity curve")
    values = pd.to_numeric(curve["equity"], errors="raise")
    if not all(math.isfinite(value) for value in values):
        raise ValueError("engine returned non-finite equity")
    seeded = pd.concat([pd.Series([initial_capital]), values.reset_index(drop=True)], ignore_index=True)
    final = float(values.iloc[-1])
    return {"initial_capital": initial_capital, "final_equity": final,
            "return_pct": (final / initial_capital - 1) * 100,
            "max_drawdown_pct": float((1 - seeded / seeded.cummax()).max() * 100),
            "fill_count": len(result["trades"]), "equity_rows": len(curve),
            "account_mode": result.get("account_mode"),
            "accounting_check": result.get("accounting_check"),
            "lifecycle": result.get("lifecycle"),
            "protective_stop_summary": result.get("protective_stop_summary")}


def run_paired_comparison(data_map, output_dir, *, trading_start=None, end=None,
                          initial_capital=DEFAULT_INITIAL_CAPITAL, timeframe=None,
                          input_facts=None):
    """Write an auditable paired comparison; preserve inputs and global config.

    ``end`` is an inclusive timestamp. Both arms retain the same prior history
    and start trading after at least 120 observed bars for every supplied asset.
    The helper changes neither capital/cost/risk defaults nor health controls.
    """
    if not math.isfinite(initial_capital) or initial_capital <= 0:
        raise ValueError("initial_capital must be finite and positive")
    prior = config._config
    profiles = paired_configs(prior)
    if (timeframe or profiles["baseline"]["data"]["timeframe"]) != "1d":
        raise ValueError("TrendPortfolioV2 comparison is daily only; other timeframes require a separately registered protocol")
    frames, start = _prepare_inputs(data_map, trading_start, end)
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=False)
    options = {"initial_capital": initial_capital, "warmup_period": WARMUP_BARS,
               "trading_start": start, "random_slip": False,
               "timeframe": timeframe or profiles["baseline"]["data"]["timeframe"]}
    source_paths = [Path(__file__), ROOT / "composition/factory.py",
                    ROOT / "strategies/base.py", ROOT / "strategies/trend_breakout.py",
                    ROOT / "strategies/trend_portfolio_v2.py", ROOT / "backtest/engine.py"]
    inputs = {"schema": "trend_portfolio_v2_comparison/v1", "engine_options": options,
              "input_facts": input_facts or {"source_kind": "caller_supplied_frames"},
              "frames": {symbol: {"rows": len(frame), "first_bar": frame.index[0],
                         "last_bar": frame.index[-1], "sha256": sha256_frame(frame),
                         "history_bars": int((frame.index < start).sum())}
                         for symbol, frame in frames.items()},
              "strategy_source_sha256": {path.relative_to(ROOT).as_posix(): sha256_file(path)
                                          for path in source_paths},
              "config_invariants": {"passed": True, "unchanged_sections": sorted(
                  set(profiles["baseline"]) - {"routing", "research", "strategy_governance"})}}
    _save(root / "inputs.json", inputs)
    summaries = {}
    try:
        for arm, settings in profiles.items():
            folder = root / arm
            folder.mkdir()
            config._config = deepcopy(settings)
            try:
                strategies = build_strategy_registry(config)
                _save(folder / "resolved_config.json", config._config)
                _save(folder / "effective_strategy_config.json", _strategy_settings(strategies))
                engine = BacktestEngine(**options, run_id=f"{EXPERIMENT_ID}:{arm}")
                result = engine.run({symbol: frame.copy(deep=True) for symbol, frame in frames.items()},
                                    strategies=strategies, routing_log_enabled=False)
                summaries[arm] = _arm_summary(result, initial_capital)
                fills = pd.DataFrame(result["trades"])
                if fills.empty:
                    fills = pd.DataFrame(columns=["symbol", "side", "qty", "fill_price",
                                                  "commission", "fill_time", "strategy_id"])
                fills.to_csv(folder / "trades.csv", index=False)
                result["equity_curve"].to_csv(folder / "equity.csv", index_label="timestamp")
                _save(folder / "summary.json", summaries[arm])
                _save(folder / "strategy_health.json", result.get("strategy_health", {}))
                _save(folder / "account_cost_contract.json", result.get("account_cost_contract", {}))
                if not (result.get("accounting_check") or {}).get("ok"):
                    raise ValueError(f"{arm}: accounting reconciliation failed")
                if (result.get("lifecycle") or {}).get("unresolved_risk_positions"):
                    raise ValueError(f"{arm}: unresolved risk positions")
            finally:
                config._config = prior
    except Exception as exc:
        _save(root / "comparison_failure.json", {"status": "incomplete", "error": str(exc),
                                                  "completed_arms": list(summaries)})
        raise
    finally:
        config._config = prior
    baseline, revised = summaries["baseline"], summaries["trend_portfolio_v2"]
    comparison = {"schema": inputs["schema"], "engineering_status": "completed",
                  "research_status": "descriptive_comparison_not_admission_evidence",
                  "scope": "three strategy structures; identical shared infrastructure and data",
                  "limitations": ["No holdout validation or profitability claim is implied.",
                                  "All three changes are combined; individual effects need separate ablations.",
                                  "Existing risk and health gates may stop either arm early; lifecycle is retained."],
                  "engine_options": options, "arms": summaries,
                  "v2_minus_baseline": {key: revised[key] - baseline[key]
                                         for key in ("final_equity", "return_pct", "max_drawdown_pct", "fill_count")},
                  "inputs_sha256": sha256_file(root / "inputs.json")}
    _save(root / "comparison.json", comparison)
    _save(root / "artifacts.json", {path.relative_to(root).as_posix(): sha256_file(path)
                                    for path in sorted(root.rglob("*")) if path.is_file()})
    return comparison


def load_local_frames(data_dir, symbols):
    """Use the repository CSV loader while retaining financing and venue facts."""
    directory = Path(data_dir).resolve()
    frames, facts = {}, {}
    for symbol in symbols:
        key = normalize_symbol(symbol)
        if key in frames:
            raise ValueError(f"duplicate normalized symbol: {key}")
        safe = key.replace("-", "_").replace(":", "_")
        candidates = [directory / f"{safe}.csv", directory / f"{key}.csv"]
        path = next((item for item in candidates if item.is_file()), None)
        if path is None:
            raise FileNotFoundError(f"No local CSV for {symbol} in {directory}")
        resolved = path.resolve()
        if directory not in resolved.parents:
            raise ValueError("symbol must identify a CSV within data_dir")
        frames[key] = DataHandler.load_csv(str(resolved))
        facts[key] = {"path": str(resolved), "sha256": sha256_file(resolved),
                      "size_bytes": resolved.stat().st_size}
    return frames, {"source_kind": "local_csv", "files": facts}


def suite_profiles(baseline):
    """The five named hypotheses; the unchanged V1 remains the real baseline.

    Every V2 arm uses the same volatility sizing. A -> B isolates the regime
    policy, B -> C the exit policy, and C -> D adds assets plus the explicitly
    tighter aggregate risk budget. This is not a pure sizing ablation.
    """
    paired_configs(baseline)  # Reject a contaminated/ablated starting profile.
    profiles = {"V1": deepcopy(baseline)}
    for variant in SUITE_VARIANTS[1:]:
        current = deepcopy(baseline)
        current["routing"].update({state: V2_NAME for state in REGIMES})
        current["state"]["stability_period"] = 3
        current["state"]["stability_candidates"] = [2, 3, 5]
        current["router"]["cooldown_bars"] = 0
        current.setdefault("research", {})["trend_portfolio_v2"] = {
            "market_state_mode": "hard_gate" if variant == "V2-A" else "risk_multiplier",
            "exit_mode": "baseline" if variant in ("V2-A", "V2-B") else "atr",
            "volatility_sizing": True,
            "asset_base_weight": 1 / 6 if variant == "V2-D" else .5,
            "target_annual_volatility": .10, "max_asset_weight": .25,
            "max_initial_risk": .01, "periods_per_year": 365,
            "initial_atr_multiple": 2., "trailing_atr_multiple": 2.5,
        }
        current.setdefault("strategy_governance", {})[V2_NAME] = "isolated_research"
        if variant == "V2-D":
            risk = current.setdefault("portfolio_risk", {})
            risk["enabled"] = True
            risk.setdefault("clusters", {}).update({"default": "crypto_beta", "BTC": "major", "ETH": "major"})
            for name, ceiling in (("max_same_session_entry_risk", .02),
                                  ("max_correlated_stop_risk", .03),
                                  ("max_crypto_beta_stop_risk", .03)):
                previous = risk.get(name)
                risk[name] = min(float(previous), ceiling) if previous is not None else ceiling
        profiles[variant] = current
    for variant, current in profiles.items():
        current.setdefault("research", {}).update(experiment_id=f"{SUITE_ID}:{variant}", entry_audit=True)
    return profiles


def suite_specs(venues, start, end, *, recent_start="2026-07-01", rolling_days=180):
    """Declare the entire finite plan before invoking any backtest.

    Rolling windows are disjoint complete 180-day periods; the incomplete tail
    is reported in the separately registered recent window. Neighbors are exactly
    the six user-specified router pairs; no returns determine the plan.
    """
    if rolling_days != 180:
        raise ValueError("registered rolling windows are fixed at 180 days")
    start, end, recent = _utc_naive(start), _utc_naive(end), _utc_naive(recent_start)
    if start > end:
        raise ValueError("start must not follow end")
    windows, cursor = [], start
    while cursor <= end:
        last = min(end, cursor + pd.Timedelta(days=rolling_days - 1))
        if (last - cursor).days + 1 == rolling_days:
            windows.append((cursor, last))
        cursor = last + pd.Timedelta(days=1)
    specs = []
    def add(venue, variant, role, label, first=start, last=end, **extra):
        specs.append(dict(run_id=f"{venue}__{variant}__{label}", variant=variant,
                          venue=venue, timeframe="1d", role=role, start=first, end=last,
                          recent_start=recent, cost_multiplier=1., **extra))
    for venue in sorted(venues):
        for variant in SUITE_VARIANTS:
            add(venue, variant, "primary", "main")
            for index, (first, last) in enumerate(windows):
                add(venue, variant, "rolling", f"rolling_{index:02d}", first, last)
            if start < recent <= end:
                add(venue, variant, "recent", "recent", recent, end)
        for variant in ("V2-C", "V2-D"):
            for multiplier in (1.5, 2.):
                add(venue, variant, "cost", f"cost_{multiplier:g}")
                specs[-1]["cost_multiplier"] = multiplier
        for stability in (2, 3, 5):
            for cooldown in (0, 2):
                if (stability, cooldown) != (3, 0):
                    add(venue, "V2-C", "neighbor", f"router_{stability}_{cooldown}",
                        stability=stability, cooldown=cooldown)
    return specs


def _suite_settings(profiles, spec):
    current = deepcopy(profiles[spec["variant"]])
    if "stability" in spec:
        current["state"]["stability_period"] = spec["stability"]
        current["router"]["cooldown_bars"] = spec["cooldown"]
    multiplier = spec["cost_multiplier"]
    for field in ("commission_rate_taker", "commission_rate_maker", "slippage_bps", "spread_bps",
                  "volatility_slippage_factor", "impact_coefficient"):
        current["execution"][field] *= multiplier
    for field in ("default_borrow_rate_annual", "liquidation_penalty_bps"):
        current["account"][field] *= multiplier
    return current


def _suite_market_frames(data_map, spec):
    symbols = SUITE_SYMBOLS if spec["variant"] == "V2-D" else SUITE_SYMBOLS[:2]
    frames = {}
    start, end = _utc_naive(spec["start"]), _utc_naive(spec["end"])
    for symbol in symbols:
        source = data_map[symbol]
        eligible = source.loc[source.index <= end]
        before = eligible.loc[eligible.index < start].tail(WARMUP_BARS)
        active = eligible.loc[eligible.index >= start]
        if active.empty:
            continue
        # A later-listed asset can enter only after its own real-bar history
        # exists; missing early prices are never backfilled into market data.
        frame = pd.concat([before, active]).copy()
        for field in ("funding_rate", "borrow_rate_annual", "spread_bps"):
            if field in frame:
                frame[field] *= spec["cost_multiplier"]
        frames[symbol] = frame
    if not frames:
        raise ValueError("no contemporaneously available market bars")
    if max(len(frame.loc[frame.index < start]) for frame in frames.values()) < WARMUP_BARS:
        raise ValueError("suite start requires 120 real prior bars for at least one asset")
    return frames


def _source_hashes():
    paths = {Path(__file__), ROOT / "config/params.yaml"}
    for folder in ("backtest", "core", "strategies", "composition", "analysis", "config"):
        paths.update((ROOT / folder).rglob("*.py"))
    return {path.relative_to(ROOT).as_posix(): sha256_file(path) for path in sorted(paths)}


def _append_attempt(path, record):
    row = dict(record, recorded_at=datetime.now(timezone.utc).isoformat())
    with Path(path).open("a", encoding="utf-8") as handle:
        handle.write(canonical_json(row) + "\n")
        handle.flush()


def _benchmark_diagnostic(frames, spec, strategy_returns, settings):
    """Net equal-capital buy-and-hold, with an ex-post risk-match diagnostic.

    The scaling uses observed strategy and benchmark volatility and therefore
    is an evaluation normalization, not a tradable allocation signal. Original
    unscaled results and this limitation are always exported alongside it.
    """
    start, end = _utc_naive(spec["start"]), _utc_naive(spec["end"])
    dates = pd.date_range(start.normalize(), end.normalize(), freq="D")
    sleeves = []
    one_way_cost = (settings["execution"]["commission_rate_taker"]
                    + settings["execution"]["slippage_bps"] / 10000
                    + settings["execution"]["spread_bps"] / 20000)
    for symbol, frame in frames.items():
        active = frame.loc[(frame.index >= start) & (frame.index <= end)]
        if active.empty:
            continue
        # Equal sleeves reserve cash for later listings rather than allocating
        # an asset before its first contemporaneously observed trading bar.
        series = active["close"] / float(active["open"].iloc[0])
        series.index = series.index.normalize()
        series = series.groupby(level=0).last().reindex(dates).ffill()
        series = series / (1 + one_way_cost)
        series = series.fillna(1.)
        series.iloc[-1] *= 1 - one_way_cost
        sleeves.append(series.rename(symbol))
    if not sleeves:
        return {"status": "insufficient"}
    curve = pd.concat(sleeves, axis=1).mean(axis=1)
    returns = pd.concat([pd.Series([1.]), curve.reset_index(drop=True)]).pct_change().dropna()
    bvol = float(returns.std(ddof=1))
    svol = float(pd.Series(strategy_returns, dtype=float).std(ddof=1))
    # Mixing initial cash with a buy-and-hold sleeve changes the denominator
    # of subsequent returns as its weight drifts. Solve on the mixed equity
    # curve rather than multiplying total PnL by a volatility ratio.
    def mixed_vol(weight):
        mixed = 1 + weight * (curve - 1)
        seeded = pd.concat([pd.Series([1.]), mixed.reset_index(drop=True)], ignore_index=True)
        return float(seeded.pct_change().dropna().std(ddof=1))
    verified = math.isfinite(svol) and bvol > 0 and svol <= bvol + 1e-12
    scale = 1. if math.isfinite(svol) and svol > bvol else 0.
    if verified:
        lower, upper = 0., 1.
        for _ in range(60):
            middle = (lower + upper) / 2
            if mixed_vol(middle) < svol:
                lower = middle
            else:
                upper = middle
        scale = (lower + upper) / 2
    achieved = mixed_vol(scale)
    verified = verified and abs(achieved - svol) <= max(1e-10, abs(svol) * 1e-8)
    total = float(curve.iloc[-1] - 1)
    return {"status": "descriptive", "buyhold_return_pct": total * 100,
            "buyhold_risk_matched_return_pct": total * scale * 100 if verified else None,
            "buyhold_cash_scaled_return_pct": total * scale * 100,
            "risk_match_verified": verified,
            "risk_match_scale": scale, "strategy_annualized_volatility": svol * math.sqrt(365),
            "buyhold_annualized_volatility": bvol * math.sqrt(365),
            "matched_buyhold_annualized_volatility": achieved * math.sqrt(365),
            "risk_match_annualized_volatility_gap": (achieved - svol) * math.sqrt(365),
            "method": "equal initial sleeves; net entry/exit fee, fixed slippage and spread; ex-post initial cash weight solved by bisection on realized volatility",
            "limitations": ["Risk matching uses the evaluation period and is not an executable signal.",
                            "Passive benchmark costs omit dynamic range slippage and participation impact; this favors the benchmark.",
                            "Missing marks are carried only for benchmark valuation, never as tradable bars."]}


def _execute_suite_run(spec, profiles, frames, universe, folder, initial_capital):
    from analysis.strategy_review import cohort_evidence
    from analysis.trend_portfolio_validation import exit_day_cohorts
    from backtest.reporting import ReportGenerator
    from backtest.reporting.operating_periods import requested_period_curve, split_execution_records

    prior = config._config
    settings = _suite_settings(profiles, spec)
    market = _suite_market_frames(frames, spec)
    folder.mkdir(parents=True, exist_ok=False)
    _save(folder / "resolved_config.json", settings)
    try:
        config._config = settings
        strategies = build_strategy_registry(config)
        _save(folder / "effective_strategy_config.json", _strategy_settings(strategies))
        engine = BacktestEngine(initial_capital=initial_capital, warmup_period=WARMUP_BARS,
            timeframe="1d", universe=universe, trading_start=_utc_naive(spec["start"]),
            random_slip=False, run_id=f"{SUITE_ID}:{spec['run_id']}")
        result = engine.run(market, strategies=strategies, routing_log_enabled=False)
        if not (result.get("accounting_check") or {}).get("ok"):
            raise ValueError("accounting reconciliation failed")
        if (result.get("lifecycle") or {}).get("unresolved_risk_positions"):
            raise ValueError("unresolved risk positions")
        mark = settings["backtest"]["end_of_backtest_mode"] == "mark_to_market"
        events = [asdict(row) if is_dataclass(row) else dict(row)
                  for row in engine.execution_adapter.broker.close_events]
        actual, valuations = split_execution_records(result["trades"], mark_to_market=mark)
        all_fills = pd.DataFrame(result["trades"])
        if all_fills.empty:
            all_fills = pd.DataFrame(columns=["symbol", "side", "qty", "fill_price", "commission", "fill_time", "strategy_id"])
        all_fills.to_csv(folder / "engine_trades.csv", index=False)
        pd.DataFrame(actual, columns=all_fills.columns).to_csv(folder / "trades.csv", index=False)
        pd.DataFrame(valuations, columns=all_fills.columns).to_csv(folder / "valuation_transfers.csv", index=False)
        full = requested_period_curve(result["equity_curve"], _utc_naive(spec["start"]), _utc_naive(spec["end"]),
            capital=initial_capital, lifecycle=result["lifecycle"], activity=result.get("strategy_activity", []))
        full.to_csv(folder / "equity.csv", index_label="timestamp")
        reporter = ReportGenerator(str(folder))
        legs = reporter._reconstruct_closed_trades(all_fills)
        round_trips = reporter._aggregate_round_trips(legs)
        pd.DataFrame(round_trips).to_csv(folder / "round_trips.csv", index=False)
        _save(folder / "close_events.json", events)
        pd.DataFrame(events).to_csv(folder / "close_events.csv", index=False)
        evidence = cohort_evidence(events, mark_to_market=mark)
        _save(folder / "cohort_evidence.json", evidence)
        pd.DataFrame(evidence["groups"]).to_csv(folder / "cohorts.csv", index=False)
        exit_days = exit_day_cohorts(events)
        _save(folder / "exit_day_cohorts.json", exit_days)
        pd.DataFrame(exit_days["groups"]).to_csv(folder / "exit_day_cohorts.csv", index=False)
        for name in ("strategy_health", "account_cost_contract", "accounting_check", "lifecycle", "protective_stop_summary"):
            _save(folder / f"{name}.json", result.get(name, {}))
        for name in ("financing_ledger", "stop_order_audit", "risk_budget_reconciliation", "correlated_risk_audit", "entry_observations"):
            pd.DataFrame(result.get(name, [])).to_csv(folder / f"{name}.csv", index=False)
        seeded = pd.concat([pd.Series([initial_capital]), full.equity.reset_index(drop=True)])
        daily = seeded.pct_change().dropna().tolist()
        benchmark = _benchmark_diagnostic(market, spec, daily, settings)
        _save(folder / "benchmark_diagnostic.json", benchmark)
        attributable = [row for row in events if not (mark and row["exit_reason"] == "EndOfBacktest")]
        exits = {}
        for row in attributable:
            reason = row["exit_reason"]
            item = exits.setdefault(reason, {"event_count": 0, "net_pnl": 0.})
            item["event_count"] += 1
            item["net_pnl"] += float(row["realized_pnl"])
        time_pnl = sum(float(row["realized_pnl"]) for row in attributable if row["exit_reason"] == "MaxHoldingPeriod")
        closed_pnl = sum(float(row["realized_pnl"]) for row in attributable)
        summary = {**spec, **_arm_summary(result, initial_capital),
            "status": "completed", "stability": settings["state"]["stability_period"],
            "symbols": list(SUITE_SYMBOLS if spec["variant"] == "V2-D" else SUITE_SYMBOLS[:2]),
            "cooldown": settings["router"]["cooldown_bars"],
            "fill_count": len(actual), "valuation_transfer_count": len(valuations),
            "daily_returns": daily, "trades": actual, "close_event_records": events,
            "round_trip_count": len(round_trips), "cohort_evidence": evidence,
            "financing_total": sum(float(row.get("amount", 0.)) for row in result.get("financing_ledger", [])),
            "financing_gross": sum(abs(float(row.get("amount", 0.))) for row in result.get("financing_ledger", [])),
            "cohort_financing_allocated": False,
            "exit_attribution": exits, "time_exit_net_pnl": time_pnl,
            "net_closed_pnl_without_time_exits": closed_pnl - time_pnl,
            "buyhold_risk_matched_return_pct": benchmark.get("buyhold_risk_matched_return_pct"),
            "benchmark_diagnostic": benchmark,
            "sample_classification": "previously_viewed_retrospective_research",
            "unseen_oos": False,
            "artifact_path": str(folder.resolve())}
        _save(folder / "summary.json", summary)
        _save(folder / "artifacts.json", {path.name: sha256_file(path) for path in sorted(folder.iterdir()) if path.is_file()})
        return json.loads(canonical_json(summary))
    finally:
        config._config = prior


def run_research_suite(data_by_venue, output_dir, *, trading_start="2022-01-01", end="2026-09-18",
                       recent_start="2026-07-01", initial_capital=DEFAULT_INITIAL_CAPITAL,
                       input_facts=None, universes=None, prior_trial_count=52,
                       register_only=False, resume=False):
    """Freeze and run the finite daily suite, logging every started/failed try.

    ``universes`` may contain independently sourced PIT membership facts. In
    their absence the first real bar supplies a causal availability boundary,
    which does not resolve survivorship of the deliberately selected six coins.
    Resume verifies protocol, config, code and data identities and never silently
    reuses or overwrites a failed attempt. Historical data is never called unseen.
    """
    from analysis.trend_portfolio_validation import AcceptanceConfig, evaluate_trend_portfolio

    if not math.isfinite(initial_capital) or initial_capital <= 0 or prior_trial_count < 0:
        raise ValueError("positive finite capital and nonnegative prior trial count are required")
    frames, actual_universes, universe_facts = {}, {}, {}
    for venue, source in sorted(data_by_venue.items()):
        if not venue.replace("_", "").isalnum():
            raise ValueError("venue must be a safe alphanumeric identifier")
        normalized = {}
        for symbol, frame in source.items():
            key = normalize_symbol(symbol)
            if key in normalized:
                raise ValueError(f"duplicate normalized symbol: {key}")
            clean = normalize_market_frame(frame)
            if clean.empty or not {"open", "high", "low", "close", "volume"}.issubset(clean):
                raise ValueError(f"{venue}/{key}: nonempty OHLCV frame required")
            normalized[key] = clean
        if set(normalized) != set(SUITE_SYMBOLS):
            raise ValueError("the registered suite requires exactly BTC, ETH, SOL, XRP, ADA and LTC USDT pairs")
        frames[venue] = normalized
        supplied = (universes or {}).get(venue)
        universe = supplied or PointInTimeUniverse(UniverseMembership(symbol, frame.index[0],
            source="first_observed_local_bar_not_verified_listing_date") for symbol, frame in normalized.items())
        actual_universes[venue] = universe
        universe_facts[venue] = {**universe.to_manifest(), "provided_membership_facts": supplied is not None,
            "survivorship_bias_controlled": False,
            "scope": "six preselected liquid coins; causal bar availability does not recover omitted delisted coins"}
    if not frames:
        raise ValueError("at least one venue is required")
    profiles = suite_profiles(config._config)
    specs = suite_specs(frames, trading_start, end, recent_start=recent_start)
    distinct_configurations = len({canonical_json(_suite_settings(profiles, spec)) for spec in specs})
    history_path = ROOT / "reports/strategy_review_20260919/review_protocol.json"
    history = {}
    if history_path.is_file():
        previous = json.loads(history_path.read_text(encoding="utf-8"))
        history = {"path": str(history_path), "sha256": sha256_file(history_path),
                   "registered_configuration_count": len(previous.get("arms", [])),
                   "registered_matrix_evaluations": previous.get("matrix_runs"),
                   "executed_attempt_count": None,
                   "scope": "prior registration is not proof that every historical attempt was recorded or executed"}
    facts = {"frames": {venue: {symbol: {"sha256": sha256_frame(frame), "rows": len(frame),
                            "first_bar": frame.index[0], "last_bar": frame.index[-1]}
                         for symbol, frame in current.items()} for venue, current in frames.items()},
             "input_facts": input_facts or {}, "universes": universe_facts}
    protocol = {"schema": SUITE_ID, "profiles": profiles, "specs": specs,
        "acceptance_thresholds": asdict(AcceptanceConfig()),
        "current_distinct_configuration_count": distinct_configurations,
        "prior_registration_evidence": history,
        "initial_capital": initial_capital, "source_sha256": _source_hashes(), "input_identity": facts,
        "prior_trial_count_lower_bound": prior_trial_count, "historical_trial_count_complete": False,
        "selection_rule": "V2-C router3/0 remains the first candidate regardless of returns; no post-result search expansion",
        "research_status": "retrospective_descriptive_not_final_unseen_oos",
        "cost_stress": "full engine reruns; fee, fixed/range slip, spread, impact, borrowing and liquidation penalty scaled together",
        "limitations": ["All V2 arms use volatility sizing; V1 to A also changes timing and is not a one-factor comparison.",
                        "V2-D adds both six assets and tighter shared crypto risk budgets.",
                        "Historical experiment count is a lower bound, so any DSR is descriptive and cannot prove admission.",
                        "The six selected coins do not constitute a survivorship-complete exchange universe.",
                        "Both venues use the unchanged configured account and fee model for controlled price robustness; this is not venue-specific deployable cost evidence.",
                        "Daily and 4h evidence are separate; this protocol executes only daily data."]}
    root = Path(output_dir).resolve()
    path = root / "preregistration.json"
    if resume:
        frozen = json.loads(path.read_text(encoding="utf-8"))
        frozen.pop("registered_at", None)
        if canonical_json(frozen) != canonical_json(protocol):
            raise ValueError("registered protocol, source, configuration or market facts changed; refusing resume")
    else:
        root.mkdir(parents=True, exist_ok=False)
        _save(path, dict(protocol, registered_at=datetime.now(timezone.utc).isoformat()))
        _save(root / "preregistration.sha256.json", {"sha256": sha256_file(path)})
        for spec in specs:
            _append_attempt(root / "attempts.jsonl", {"event": "registered", "run_id": spec["run_id"], "spec": spec})
    expected = json.loads((root / "preregistration.sha256.json").read_text(encoding="utf-8"))["sha256"]
    if sha256_file(path) != expected:
        raise ValueError("preregistration checksum mismatch")
    if register_only:
        return {"status": "registered", "planned_runs": len(specs), "output_dir": str(root)}
    ledger_path = root / "attempts.jsonl"
    rows = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
    attempted = {row["run_id"] for row in rows if row["event"] == "started"}
    results, failures = [], []
    for number, spec in enumerate(specs, 1):
        folder = root / "runs" / spec["run_id"]
        if spec["run_id"] in attempted:
            if (folder / "summary.json").is_file() and (folder / "artifacts.json").is_file():
                for name, digest in json.loads((folder / "artifacts.json").read_text(encoding="utf-8")).items():
                    if sha256_file(folder / name) != digest:
                        raise ValueError(f"cached artifact changed: {spec['run_id']}/{name}")
                results.append(json.loads((folder / "summary.json").read_text(encoding="utf-8")))
                continue
            failures.append({"run_id": spec["run_id"], "error": "previous failed or interrupted attempt; retained without silent retry"})
            continue
        _append_attempt(ledger_path, {"event": "started", "run_id": spec["run_id"], "sequence": number})
        print(f"[{number}/{len(specs)}] {spec['run_id']}", flush=True)
        try:
            result = _execute_suite_run(spec, profiles, frames[spec["venue"]], actual_universes[spec["venue"]], folder, initial_capital)
            results.append(result)
            _append_attempt(ledger_path, {"event": "completed", "run_id": spec["run_id"],
                "return_pct": result["return_pct"], "summary_sha256": sha256_file(folder / "summary.json")})
        except Exception as exc:
            failure = {"run_id": spec["run_id"], "error": f"{type(exc).__name__}: {exc}"}
            failures.append(failure)
            _append_attempt(ledger_path, {"event": "failed", **failure})
            folder.mkdir(parents=True, exist_ok=True)
            _save(folder / "failure.json", failure)
            print(f"FAILED {failure['run_id']}: {failure['error']}", flush=True)
    ledger = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
    attempts = sum(row["event"] == "started" for row in ledger)
    metadata = {"registered_run_count": len(specs), "attempted_run_count": attempts,
                "completed_run_count": len(results), "failed_run_count": len(failures),
                "distinct_variant_count": len(SUITE_VARIANTS), "prior_trial_count_lower_bound": prior_trial_count,
                "current_distinct_configuration_count": distinct_configurations,
                "total_trial_count_lower_bound": prior_trial_count + distinct_configurations,
                "prior_registration_evidence": history,
                "historical_trial_count_complete": False,
                "historical_trials_complete": False, "registered_before_results": True,
                "current_run_count": attempts, "total_trials": prior_trial_count + distinct_configurations,
                "trial_sharpe_scope": "observed full-period configurations at the alphabetically first preregistered venue; historical dispersion unknown",
                "trial_sharpes": [float(np.mean(row["daily_returns"]) / np.std(row["daily_returns"], ddof=1))
                                  for row in results if len(row.get("daily_returns", [])) > 2
                                  and row["venue"] == sorted(frames)[0]
                                  and row["role"] in {"primary", "cost", "neighbor"}
                                  and np.std(row["daily_returns"], ddof=1) > 0]}
    report = {"schema": SUITE_ID, "status": "completed" if not failures else "incomplete",
              "research_status": protocol["research_status"], "trial_metadata": metadata,
              "preregistration_sha256": expected, "runs": results, "failures": failures,
              "limitations": protocol["limitations"]}
    _save(root / "suite.json", report)
    acceptance = evaluate_trend_portfolio(results, config=AcceptanceConfig(), trial_metadata=metadata)
    if failures:
        acceptance["engineering_status"] = "incomplete"
        acceptance["failed_registered_runs"] = failures
    _save(root / "acceptance.json", acceptance)
    columns = ("run_id", "variant", "venue", "timeframe", "role", "start", "end", "stability", "cooldown",
               "cost_multiplier", "return_pct", "max_drawdown_pct", "fill_count", "round_trip_count",
               "time_exit_net_pnl", "net_closed_pnl_without_time_exits", "buyhold_risk_matched_return_pct")
    pd.DataFrame([{key: row.get(key) for key in columns} for row in results], columns=columns).to_csv(root / "results.csv", index=False)
    return report


def load_public_frames(data_dir, venues, symbols=SUITE_SYMBOLS):
    directory = Path(data_dir).resolve()
    output, facts = {}, {}
    for venue in venues:
        current, files = {}, {}
        if not venue.replace("_", "").isalnum():
            raise ValueError("venue must be a safe alphanumeric identifier")
        for symbol in symbols:
            key = normalize_symbol(symbol)
            path = directory / f"{venue}_{key.replace('-', '')}_1d.csv"
            if directory not in path.resolve().parents:
                raise ValueError("market CSV must stay within data directory")
            if key in current:
                raise ValueError(f"duplicate normalized symbol: {key}")
            current[key] = DataHandler.load_csv(str(path))
            files[key] = {"path": str(path), "sha256": sha256_file(path)}
            manifest = path.with_suffix(".manifest.json")
            if manifest.exists():
                fact = json.loads(manifest.read_text(encoding="utf-8"))
                if fact.get("csv_sha256") != files[key]["sha256"]:
                    raise ValueError(f"public data manifest checksum mismatch: {path.name}")
                files[key]["manifest_sha256"] = sha256_file(manifest)
        output[venue], facts[venue] = current, files
    return output, {"source_kind": "frozen_local_public_daily_csv", "files": facts}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--symbols", nargs="+", help="Required for paired mode; suite uses the six registered coins")
    parser.add_argument("--output-dir", type=Path, required=True, help="New exclusive output directory")
    parser.add_argument("--start", help="First eligible trading timestamp; requires 120 prior bars")
    parser.add_argument("--end", help="Inclusive final timestamp (date alone means that date at 00:00 UTC)")
    parser.add_argument("--capital", type=float, default=DEFAULT_INITIAL_CAPITAL)
    parser.add_argument("--timeframe", help="Bar timeframe; defaults to existing config")
    parser.add_argument("--suite", action="store_true", help="Run the pre-registered V1/V2-A/B/C/D daily research suite")
    parser.add_argument("--venues", nargs="+", default=["binance", "okx"])
    parser.add_argument("--universe-file", action="append", default=[], metavar="VENUE=CSV",
                        help="PIT membership CSV per venue; defaults to the repository Binance lifecycle facts when available")
    parser.add_argument("--recent-start", default="2026-07-01")
    parser.add_argument("--prior-trials", type=int, default=52, help="Known historical trial count lower bound; not a complete search history")
    parser.add_argument("--register-only", action="store_true", help="Write the frozen protocol before running any arm")
    parser.add_argument("--resume", action="store_true", help="Verify identity and continue unattempted registered runs")
    args = parser.parse_args(argv)
    if args.suite:
        if args.timeframe not in (None, "1d"):
            parser.error("the suite is daily only; 4h needs an independent protocol")
        logging.disable(logging.CRITICAL)
        frames, facts = load_public_frames(args.data_dir, args.venues, args.symbols or SUITE_SYMBOLS)
        universes = {}
        repository_universe = ROOT / "config/universe_binance_spot_1d.csv"
        if "binance" in frames and repository_universe.is_file():
            universes["binance"] = PointInTimeUniverse.from_csv(repository_universe)
            facts["binance_membership_file"] = {"path": str(repository_universe), "sha256": sha256_file(repository_universe)}
        for argument in args.universe_file:
            venue, separator, path = argument.partition("=")
            if not separator or venue not in frames:
                parser.error("--universe-file must name a selected venue as VENUE=CSV")
            universes[venue] = PointInTimeUniverse.from_csv(path)
            facts[f"{venue}_membership_file"] = {"path": str(Path(path).resolve()), "sha256": sha256_file(path)}
        report = run_research_suite(frames, args.output_dir, trading_start=args.start or "2022-01-01",
            end=args.end or "2026-09-18", recent_start=args.recent_start, initial_capital=args.capital,
            input_facts=facts, universes=universes, prior_trial_count=args.prior_trials,
            register_only=args.register_only, resume=args.resume)
        print(canonical_json({"output_dir": str(args.output_dir.resolve()), "status": report["status"],
                              "trial_metadata": report.get("trial_metadata"), "planned_runs": report.get("planned_runs")}))
        return 0 if report["status"] in ("registered", "completed") else 1
    if not args.symbols:
        parser.error("paired mode requires --symbols")
    if args.register_only or args.resume:
        parser.error("--register-only and --resume require --suite")
    frames, facts = load_local_frames(args.data_dir, args.symbols)
    comparison = run_paired_comparison(frames, args.output_dir, trading_start=args.start,
        end=args.end, initial_capital=args.capital, timeframe=args.timeframe, input_facts=facts)
    print(canonical_json({"output_dir": str(args.output_dir.resolve()),
                          "v2_minus_baseline": comparison["v2_minus_baseline"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
