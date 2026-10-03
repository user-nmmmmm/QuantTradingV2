"""Frozen, retrospective paper applications. No live policy or holdout writes."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
from typing import Mapping

import numpy as np
import pandas as pd

from core.reproducibility import canonical_json, sha256_bytes, sha256_file


ARMS = (
    "baseline", "regime_all", "range_only", "momentum60",
    "momentum_ensemble", "momentum_no_obv", "momentum_hard_gate",
)
WINDOWS = (
    ("2021_2022", "2021-01-01", "2022-12-31"),
    ("2023_2024", "2023-01-01", "2024-12-31"),
    ("2025_2026", "2025-01-01", "2026-09-19"),
)


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json.loads(canonical_json(value)), ensure_ascii=False,
                               indent=2) + "\n", encoding="utf-8")


def freeze_registration(path: Path, identity: Mapping) -> str:
    """An output directory belongs to one exact identity; never overwrite it."""
    normalized = json.loads(canonical_json(identity))
    if path.exists():
        if json.loads(path.read_text(encoding="utf-8")) != normalized:
            raise ValueError("Study identity changed; use a new output directory")
    else:
        write_json(path, normalized)
    return sha256_bytes(canonical_json(normalized).encode("utf-8"))


def load_verified_inputs(manifest_path: Path):
    """Only listed spot BTC/ETH inputs; reject path escape and identity drift."""
    manifest_path = manifest_path.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (manifest.get("market_type") != "spot" or manifest.get("timeframe") != "1d"
            or manifest.get("exchange") != "binance"):
        raise ValueError("Expected Binance spot daily manifest")
    symbols = ("BTC/USDT", "ETH/USDT")
    frames, identities = {}, {}
    for symbol in symbols:
        entry = manifest["symbols"][symbol]
        path = (manifest_path.parent / entry["file"]).resolve()
        if not path.is_relative_to(manifest_path.parent):
            raise ValueError("Input path escapes manifest directory")
        if sha256_file(path) != entry["sha256"]:
            raise ValueError(f"Input hash mismatch: {symbol}")
        frame = pd.read_csv(path, index_col="timestamp", parse_dates=True,
                            float_precision="round_trip")
        columns = ["open", "high", "low", "close", "volume"]
        if (frame.empty or not isinstance(frame.index, pd.DatetimeIndex)
                or not frame.index.is_monotonic_increasing or frame.index.has_duplicates
                or frame.index.hasnans or not np.isfinite(frame[columns].to_numpy()).all()
                or len(frame) != entry["rows"]):
            raise ValueError(f"Invalid input series: {symbol}")
        if (pd.Timestamp(entry["first"]) != frame.index[0]
                or pd.Timestamp(entry["last"]) != frame.index[-1]):
            raise ValueError(f"Input time bounds changed: {symbol}")
        if frame.index.tz is not None:
            frame.index = frame.index.tz_convert("UTC").tz_localize(None)
        frames[symbol] = frame
        identities[symbol] = {**entry, "path": str(path)}
    if not frames[symbols[0]].index.equals(frames[symbols[1]].index):
        raise ValueError("BTC/ETH common timeline required; no silent intersection")
    return frames, {"manifest_sha256": sha256_file(manifest_path),
                    "symbols": identities, "membership_evidence": "unknown",
                    "universe": "fixed two-asset retrospective subset; not full market"}


def arm_parameters(base: Mapping, arm: str, *, multiplier=1.0) -> dict:
    if arm not in ARMS or multiplier not in (1.0, 1.5):
        raise ValueError("Unregistered arm or cost multiplier")
    params = deepcopy(base)
    research = params.setdefault("research", {})
    research.update(experiment_id=f"paper-applications-v1:{arm}", entry_audit=True)
    params["backtest"]["end_of_backtest_mode"] = "forced_liquidation"
    for name in ("signal_observation", "signal_meta_layer", "signal_adaptive", "signal_meta_replay"):
        params.setdefault(name, {})["enabled"] = False
    if arm == "regime_all":
        states = ["TREND_UP", "TREND_DOWN", "SIDEWAYS", "VOLATILE"]
        params["routing"] = {state: "TrendBreakout" for state in states}
        research["regime_controls"] = {"TrendBreakout": {
            "entry_states": states, "exit_states": ["TREND_UP"]}}
    elif arm == "range_only":
        params["routing"] = {state: "RangeMeanReversion" if state == "SIDEWAYS" else "Cash"
                             for state in params["routing"]}
    elif arm.startswith("momentum"):
        params["routing"] = {state: "TrendPortfolioV2" for state in params["routing"]}
        options = {"use_obv": True, "exit_mode": "atr", "volatility_sizing": True,
                   "horizons": [20, 60, 120], "weights": [.5, .3, .2],
                   "market_state_mode": "risk_multiplier", "periods_per_year": 365.0}
        if arm == "momentum60":
            options.update(horizons=[60], weights=[1.0])
        if arm == "momentum_no_obv":
            options["use_obv"] = False
        if arm == "momentum_hard_gate":
            options["market_state_mode"] = "hard_gate"
        research["trend_portfolio_v2"] = options
    # The configured three-symbol recovery cannot be reached in two assets.
    # Freeze a common health-disabled research reference rather than weakening
    # its recovery threshold or pretending this is the production baseline.
    name = "TrendPortfolioV2" if arm.startswith("momentum") else "TrendBreakout"
    if arm != "range_only":
        research["strategy_health_overrides"] = {name: {"enabled": False}}
    for field in ("commission_rate_taker", "commission_rate_maker", "slippage_bps",
                  "spread_bps", "volatility_slippage_factor", "impact_coefficient"):
        params["execution"][field] *= multiplier
    params["account"]["default_borrow_rate_annual"] *= multiplier
    params["account"]["liquidation_penalty_bps"] *= multiplier
    return params


def fixed_benchmark(frames, start, end, *, capital=10000.0):
    """Two equally funded buy-and-hold legs at first open, before costs."""
    prices = pd.DataFrame({s: f.loc[start:end, "close"] for s, f in frames.items()})
    entries = pd.Series({s: f.loc[start:end, "open"].iloc[0] for s, f in frames.items()})
    if prices.empty or prices.isna().any().any():
        raise ValueError("Complete common benchmark timeline required")
    return capital * prices.div(entries).mean(axis=1)


def equity_returns(equity: pd.DataFrame, timeline: pd.DatetimeIndex, *, capital=10000.0):
    """Only a confirmed terminal flat book can be extended as a cash tail."""
    if equity.empty or equity.index.has_duplicates or not equity.index.is_monotonic_increasing:
        raise ValueError("Invalid equity timeline")
    normalized = equity.index.normalize()
    if not normalized.isin(timeline).all():
        raise ValueError("Equity outside declared study window")
    # The engine's forced terminal close has a synthetic sub-day timestamp;
    # use the day's last equity so its fees remain in the daily return panel.
    daily = equity["equity"].groupby(normalized).last()
    if not daily.index.equals(timeline[:len(daily)]):
        raise ValueError("Missing leading or internal equity bar; only a terminal cash tail is allowed")
    if not np.isfinite(equity["equity"].to_numpy()).all():
        raise ValueError("Nonfinite observed equity cannot be filled")
    curve = daily.reindex(timeline).ffill().fillna(capital)
    if not np.isfinite(curve.to_numpy()).all() or (curve <= 0).any():
        raise ValueError("Nonpositive or nonfinite equity")
    returns = curve.pct_change()
    returns.iloc[0] = curve.iloc[0] / capital - 1
    return curve, returns


def factor_proxies(frames):
    """Causal two-asset proxies; historical size and universe factors absent."""
    closes = pd.DataFrame({s: f["close"] for s, f in frames.items()})
    daily = closes.pct_change()
    lagged_momentum = closes.pct_change(60).shift(1)
    direction = np.sign(lagged_momentum.iloc[:, 0] - lagged_momentum.iloc[:, 1])
    turnover = direction.diff().abs().fillna(direction.abs())
    # A diagnostic long-short return spread, not an executable margin account.
    momentum = direction * (daily.iloc[:, 0] - daily.iloc[:, 1]) / 2
    return pd.DataFrame({"market": daily.mean(axis=1), "momentum_spread": momentum,
                         "momentum_proxy_turnover": turnover}, index=closes.index)


def factor_attribution(returns, factors, *, iterations=1000, block_length=20, seed=42):
    if (not returns.index.is_monotonic_increasing or returns.index.has_duplicates
            or not returns.index.isin(factors.index).all()):
        raise ValueError("Factor timestamps must cover the exact return timeline")
    design = factors.loc[returns.index, ["market", "momentum_spread"]]
    if design.isna().any().any() or not np.isfinite(design.to_numpy()).all():
        return {"status": "insufficient", "reason": "factor_history_incomplete"}
    y = returns.to_numpy(dtype=float)
    x = np.column_stack([np.ones(len(y)), design.to_numpy(dtype=float)])
    if len(y) < 60 or not np.isfinite(y).all() or np.linalg.matrix_rank(x) < 3:
        return {"status": "insufficient", "reason": "sample_size_or_rank"}
    beta = np.linalg.lstsq(x, y, rcond=None)[0]
    residual = y - x @ beta
    rng = np.random.default_rng(seed)
    samples = []
    for _ in range(iterations):
        starts = rng.integers(0, len(y), size=int(np.ceil(len(y) / block_length)))
        indices = np.concatenate([(s + np.arange(block_length)) % len(y) for s in starts])[:len(y)]
        if np.linalg.matrix_rank(x[indices]) == 3:
            samples.append(np.linalg.lstsq(x[indices], y[indices], rcond=None)[0][0])
    return {"status": "diagnostic", "sample_size": len(y), "alpha_daily": beta[0],
            "alpha_annual_linear": 365 * beta[0], "market_beta": beta[1],
            "momentum_proxy_beta": beta[2],
            "alpha_daily_block_ci95": np.quantile(samples, [.025, .975]).tolist(),
            "r_squared": None if np.var(y) == 0 else 1 - np.var(residual) / np.var(y),
            "block_length": block_length, "bootstrap_samples": len(samples),
            "interpretation": "retrospective exposure diagnostic; not causal alpha or three-factor replication",
            "unmodeled": ["historical market-cap size", "full-market momentum", "proxy financing"]}


def coverage_report(payload):
    predictions = (payload or {}).get("predictions", [])
    return {"status": (payload or {}).get("status", "not_run"), "count": len(predictions),
            "statuses": dict(Counter(p.get("status", "unknown") for p in predictions)),
            "reasons": dict(Counter(p.get("reason", "unspecified") for p in predictions)),
            "finite_estimates": sum(p.get("estimate_bps") is not None for p in predictions),
            "folds": len((payload or {}).get("folds", [])),
            "validation": (payload or {}).get("validation", {}),
            "errors": (payload or {}).get("errors", []),
            "admission": "diagnostic_only"}


def chronological_label_baseline(rows, *, minimum_train=30, train_days=365):
    """Constant net-EV baseline, trained only on labels already available."""
    mature = sorted((r for r in rows if r.get("training_eligible")),
                    key=lambda r: (pd.Timestamp(r["entry_time"]), r["candidate_id"]))
    output = []
    for row in rows:
        query_time = row.get("signal_available_at") or row.get("entry_time")
        if query_time is None:
            output.append({"candidate_id": row["candidate_id"], "status": "abstain_missing_query_time",
                           "predicted_net_bps": None, "scorable": False})
            continue
        at = pd.Timestamp(query_time)
        train = [r for r in mature if pd.Timestamp(r["available_at"]) < at
                 and pd.Timestamp(r["entry_time"]) >= at - pd.Timedelta(days=train_days)]
        predicted = float(np.mean([r["net_return_bps"] for r in train])) if len(train) >= minimum_train else None
        output.append({"candidate_id": row["candidate_id"], "available_at": at,
                       "training_count": len(train), "predicted_net_bps": predicted,
                       "realized_net_bps": row.get("net_return_bps"),
                       "scorable": bool(row.get("training_eligible")),
                       "latest_training_label_at": max((r["available_at"] for r in train), default=None),
                       "status": "predicted" if predicted is not None else "abstain"})
    predicted_rows = [r for r in output if r["predicted_net_bps"] is not None]
    scored = [r for r in predicted_rows if r["scorable"]]
    return {"schema": "paper-label-baseline/v1", "predictions": output,
            "query_count": len(output), "predicted": len(predicted_rows),
            "scored": len(scored), "abstain": len(output) - len(predicted_rows),
            "evaluation_excluded": sum(not r["scorable"] for r in output),
            "mae_bps": float(np.mean([abs(r["predicted_net_bps"] - r["realized_net_bps"]) for r in scored])) if scored else None,
            "interpretation": "all-candidate chronological constant net-EV forecasts; MAE conditional on mature unambiguous labels, not portfolio gains"}
