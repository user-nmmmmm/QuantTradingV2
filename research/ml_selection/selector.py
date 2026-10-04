"""Causal candidate gating injected explicitly into offline backtests only."""
from __future__ import annotations

from dataclasses import replace
import math
from collections.abc import Mapping

import numpy as np
import pandas as pd

from research.ml_selection.dataset import FEATURE_COLUMNS


ACCOUNT_FEATURES = ("cash_fraction", "gross_exposure_fraction", "portfolio_drawdown",
                    "held_count_fraction", "pending_count_fraction")


def utc(value):
    stamp = pd.Timestamp(value)
    if pd.isna(stamp):
        raise ValueError("a finite timestamp is required")
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


def _pending_order_count(broker):
    """Count outstanding matching-book orders, including partial fills, once."""
    venue = getattr(broker, "broker", broker)
    seen = set()
    terminal = {"filled", "canceled", "cancelled", "rejected", "no_position",
                "expired", "expired_unsubmitted"}
    for name in ("pending_orders", "active_orders"):
        book = getattr(venue, name, ()) or ()
        records = book.values() if isinstance(book, Mapping) else book
        for order in records:
            value = order.get if isinstance(order, Mapping) else lambda key, default=None: getattr(order, key, default)
            status = value("status", "")
            if str(getattr(status, "value", status)).lower() in terminal:
                continue
            remaining = float(value("remaining_qty", 0.0))
            if not math.isfinite(remaining) or remaining < 0:
                raise ValueError("outstanding order quantity must be finite and nonnegative")
            if remaining > 0:
                identifier = value("id", None)
                seen.add(("order", str(identifier)) if identifier else ("object", id(order)))
    return len(seen)


class ResearchSelector:
    """Transform complete original-strategy batches, without sizing or ordering.

    Dataset labels are deliberately removed from the serving table. Actual
    fills and risk-approved sizes remain the original engine's responsibility.
    """

    def __init__(self, dataset, *, mode="model", model=None, policy=None,
                 deterministic=True, seed=42, min_expected_return=0.0,
                 score_scale=100.0, score_cap=5.0, initial_capital=None):
        if mode not in {"model", "policy", "momentum", "random", "qualified_native"}:
            raise ValueError("unsupported selection mode")
        if mode in {"model", "policy"} and model is None:
            raise ValueError("model required")
        if mode == "policy" and policy is None:
            raise ValueError("policy required")
        if any(isinstance(value, bool) or not math.isfinite(float(value))
               for value in (min_expected_return, score_scale, score_cap)) or score_scale <= 0 or score_cap <= 0:
            raise ValueError("invalid selection thresholds")
        if initial_capital is not None and (isinstance(initial_capital, bool)
                or not math.isfinite(float(initial_capital)) or initial_capital <= 0):
            raise ValueError("initial_capital must be finite and positive")
        self.mode, self.model, self.policy = mode, model, policy
        self.deterministic = deterministic
        self.threshold, self.score_scale, self.score_cap = min_expected_return, score_scale, score_cap
        self.rng = np.random.default_rng(seed)
        rows = dataset.copy()
        rows["as_of"] = pd.to_datetime(rows.as_of, utc=True)
        keep = ["symbol", "as_of", "eligible", "exclusion_reason", *FEATURE_COLUMNS]
        self.table = rows[keep].set_index(["symbol", "as_of"])
        if self.table.index.has_duplicates:
            raise ValueError("duplicate candidate snapshots")
        self.audit = []
        self.trajectory = []
        self.initial_capital = float(initial_capital) if initial_capital is not None else None
        self.high_water = self.initial_capital or 0.0

    def select(self, candidates, *, event, portfolio, broker, risk_manager, current_prices):
        candidates = list(candidates)
        if event.timeframe != "1d":
            raise ValueError("ML selection requires daily closed bars")
        bar_time, as_of = utc(event.timestamp), utc(event.timestamp) + pd.Timedelta(days=1)
        equity = float(portfolio.get_total_value(dict(current_prices)))
        if not math.isfinite(equity):
            raise ValueError("portfolio equity must be finite")
        capital = self.initial_capital or getattr(portfolio, "initial_capital", None)
        if capital is not None and (not math.isfinite(float(capital)) or float(capital) <= 0):
            raise ValueError("portfolio initial capital must be finite and positive")
        peak = getattr(risk_manager, "high_water_equity", None)
        if peak is not None:
            peak = float(peak)
            if not math.isfinite(peak) or peak <= 0:
                raise ValueError("risk high-water equity must be finite and positive")
            # Risk is updated on every engine bar, including blocked/no-entry
            # bars, and can explicitly rebase its peak on an approved recovery.
            self.high_water = max(peak, equity)
            drawdown = getattr(risk_manager, "last_drawdown", None)
        else:
            self.high_water = max(self.high_water, float(capital or 0.0), equity)
            drawdown = None
        if drawdown is None:
            drawdown = max(0.0, 1 - equity / self.high_water) if self.high_water > 0 else 0.0
        drawdown = float(drawdown)
        if not math.isfinite(drawdown) or drawdown < 0:
            raise ValueError("portfolio drawdown must be finite and nonnegative")
        # Updating the accounting state precedes the empty candidate return.
        if not candidates:
            return []
        held = [(s, p) for s, p in portfolio.positions.items() if p.get("qty", 0)]
        gross = sum(abs(float(p.get("qty", 0)) * float(current_prices.get(s, p.get("avg_price", 0))))
                    for s, p in held)
        pending_count = _pending_order_count(broker)
        account = {"cash_fraction": float(portfolio.cash) / equity if equity > 0 else 0.0,
                   "gross_exposure_fraction": gross / equity if equity > 0 else 0.0,
                   "portfolio_drawdown": drawdown,
                   "held_count_fraction": len(held) / 8.0,
                   "pending_count_fraction": pending_count / 8.0}
        valid, features = [], []
        for candidate in candidates:
            key = (candidate.symbol, as_of)
            if key not in self.table.index:
                self.audit.append({"bar_time": bar_time.isoformat(), "as_of": as_of.isoformat(),
                                   "symbol": candidate.symbol, "selected": False,
                                   "reason": "missing_causal_snapshot"})
                continue
            row = self.table.loc[key]
            if not bool(row.eligible):
                self.audit.append({"bar_time": bar_time.isoformat(), "as_of": as_of.isoformat(),
                                   "symbol": candidate.symbol, "selected": False,
                                   "reason": str(row.exclusion_reason)})
                continue
            valid.append(candidate)
            features.append({**{name: row[name] for name in FEATURE_COLUMNS}, **account})
        if not valid:
            return []
        frame = pd.DataFrame(features)
        if self.mode in {"model", "policy"}:
            predictions = np.asarray(self.model.predict(frame), dtype=float)
            if predictions.shape != (len(valid),) or not np.isfinite(predictions).all():
                raise ValueError("nonfinite or malformed model predictions")
            scaled = predictions * self.score_scale
            scores = np.clip(self.score_cap * .5 * (1 + scaled / (1 + np.abs(scaled))), .01, self.score_cap)
            gates = predictions > self.threshold
        elif self.mode == "momentum":
            predictions = frame["return_20d"].to_numpy(dtype=float)
            scaled = predictions * self.score_scale
            scores = np.clip(self.score_cap * .5 * (1 + scaled / (1 + np.abs(scaled))), .01, self.score_cap)
            gates = np.ones(len(valid), dtype=bool)
        elif self.mode == "random":
            predictions = self.rng.random(len(valid))
            scores, gates = predictions * self.score_cap, np.ones(len(valid), dtype=bool)
        else:
            predictions = np.array([c.score for c in valid], dtype=float)
            scores, gates = predictions, np.ones(len(valid), dtype=bool)
        probabilities = np.ones(len(valid))
        if self.mode == "policy":
            gates, probabilities = self.policy.act(frame, deterministic=self.deterministic)
            if not self.deterministic:
                for i in range(len(valid)):
                    self.trajectory.append({"bar_time": bar_time, "as_of": as_of,
                                            "features": {name: float(frame.iloc[i][name])
                                                         for name in self.policy.features},
                                            "action": bool(gates[i]),
                                            "probability": float(probabilities[i])})
        selected = []
        for i, candidate in enumerate(valid):
            self.audit.append({"bar_time": bar_time.isoformat(), "as_of": as_of.isoformat(),
                               "symbol": candidate.symbol, "strategy": candidate.strategy_name,
                               "native_score": float(candidate.score),
                               "predicted_value": float(predictions[i]),
                               "allocation_score": float(scores[i]),
                               "selection_probability": float(probabilities[i]),
                               "selected": bool(gates[i]), "mode": self.mode,
                               "reason": "selected" if gates[i] else "model_gate",
                               **account})
            if gates[i]:
                selected.append(replace(candidate, score=float(scores[i])))
        return selected
