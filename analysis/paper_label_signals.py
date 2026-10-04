"""Causal constant-EV label comparison targets for a shared cash account.

This is a preregistered research baseline, not the project's P1/P2 model. Only
long candidates are eligible for the long-only spot replay. Every prediction
uses labels whose availability strictly precedes the candidate decision.
"""
from __future__ import annotations

from dataclasses import asdict
import math

import numpy as np
import pandas as pd

from analysis.paper_labels import LabelCosts, _time


def fixed_horizon_cost_labels(outcomes, *, costs: LabelCosts, horizon=5):
    """Use observed raw reference prices with exactly the same label cost model."""
    rows = []
    for source in outcomes:
        if source.get("horizon_bars") != horizon:
            continue
        fields = ("candidate_id", "symbol", "direction", "strategy", "status", "reason",
            "available_at", "horizon_bars", "entry_time", "exit_bar", "entry_reference", "exit_reference")
        row = {key: source[key] for key in fields if key in source}
        row["source_outcome"] = dict(source)
        row["training_eligible"] = source.get("status") == "matured"
        row["cost_model"] = {"schema": "fixed_scenario_bps/v1", **asdict(costs)}
        row["scope"] = "fixed_horizon_reference_label_shared_cost_scenario"
        if row["training_eligible"]:
            entry, exit_ = float(row["entry_reference"]), float(row["exit_reference"])
            if not np.isfinite([entry, exit_]).all() or min(entry, exit_) <= 0:
                raise ValueError("valid fixed label reference prices required")
            ratio = exit_ / entry
            sign = 1 if row["direction"] == "long" else -1
            components = {k.removesuffix("_bps_per_side"): v * (1 + ratio)
                for k, v in asdict(costs).items() if k.endswith("_bps_per_side")}
            components["carry"] = costs.carry_bps
            row["gross_return_bps"] = sign * (ratio - 1) * 10000
            row["cost_components_bps"] = components
            row["net_return_bps"] = row["gross_return_bps"] - math.fsum(components.values())
        rows.append(row)
    return rows


def chronological_label_targets(candidates, labels, timeline, symbols, *, minimum_train=30,
                                train_days=365, holding_bars=5, max_gross=.9):
    """Build weights at closed daily bars for next-open Broker execution.

    Supports a common pooled long-only constant mean, no tuning by symbol or
    regime. Assets share the same fixed gross budget. A known new candidate may
    extend its asset's holding clock; future realised labels never affect exits.
    """
    if minimum_train < 2 or train_days < 1 or holding_bars < 1 or not 0 < max_gross <= 1:
        raise ValueError("invalid label research protocol")
    index = pd.DatetimeIndex(pd.to_datetime(timeline, utc=True))
    if not index.is_monotonic_increasing or index.has_duplicates:
        raise ValueError("unique ordered daily timeline required")
    if len(index) > 1 and not ((index[1:] - index[:-1]) == pd.Timedelta(days=1)).all():
        raise ValueError("daily label target generation requires complete calendar grid")
    symbols = list(symbols)
    if not symbols or len(symbols) != len(set(symbols)):
        raise ValueError("unique assets required")
    outcomes, seen = [], set()
    for label in labels:
        if label["candidate_id"] in seen:
            raise ValueError("duplicate candidate label")
        seen.add(label["candidate_id"])
        if label.get("training_eligible") and label.get("direction") == "long":
            value = float(label["net_return_bps"])
            if not math.isfinite(value):
                raise ValueError("nonfinite mature label")
            outcomes.append((_time(label["available_at"]), value))
    outcomes.sort(key=lambda x: x[0])
    grouped, skipped = {}, 0
    for item in candidates:
        if item.get("direction") != "long" or item["symbol"] not in symbols:
            skipped += 1
            continue
        available = _time(item["context"]["available_at"])
        if available < _time(item["timestamp"]) + pd.Timedelta(days=1):
            raise ValueError("candidate before its signal closes")
        # Delayed signals wait for the next completed daily decision boundary.
        decision_bar = available.ceil("D") - pd.Timedelta(days=1)
        if decision_bar in index:
            grouped.setdefault(decision_bar, []).append((item, available))
    targets = pd.DataFrame(0., index=index, columns=symbols)
    expires = {s: -1 for s in symbols}
    predictions, attribution = [], []
    owners = {}
    for i, at in enumerate(index):
        for item, available in grouped.get(at, []):
            decision_time = at + pd.Timedelta(days=1)
            training = [(t, r) for t, r in outcomes
                        if decision_time - pd.Timedelta(days=train_days) <= t < decision_time]
            estimate = float(np.mean([r for _, r in training])) if len(training) >= minimum_train else None
            allow = estimate is not None and estimate > 0
            if allow:
                expires[item["symbol"]] = max(expires[item["symbol"]], i + holding_bars)
                owners[item["symbol"]] = item["candidate_id"]
            predictions.append({"candidate_id": item["candidate_id"], "symbol": item["symbol"],
                "decision_at": decision_time.isoformat(), "candidate_available_at": available.isoformat(),
                "training_count": len(training), "estimate_bps": estimate,
                "latest_training_available_at": training[-1][0].isoformat() if training else None,
                "decision": "allow" if allow else "veto" if estimate is not None else "abstain"})
        for symbol in symbols:
            if i < expires[symbol]:
                targets.loc[at, symbol] = max_gross / len(symbols)
                attribution.append({"timestamp": at.isoformat(), "symbol": symbol,
                    "candidate_id": owners[symbol]})
    return targets, {"predictions": predictions, "skipped_non_long_or_unknown_asset": skipped,
        "minimum_train": minimum_train, "train_days": train_days,
        "holding_bars": holding_bars, "max_gross": max_gross,
        "target_attribution": attribution,
        "attribution_rule": "new opening orders belong to the latest allowed candidate in stable input order; prior lots retain original owners through FIFO closes",
        "model": "pooled_long_constant_mean_EV", "formal_meta_layer": False,
        "cost_scope": "shared fixed label costs; account replay independently applies full Broker costs"}
