"""Cross-symbol rolling spread model for correlation/statistical-arbitrage research."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class PairSignal:
    action: str
    z_score: float
    hedge_ratio: float


class PairsTradingModel:
    def __init__(self, window: int = 60, entry_z: float = 2.0, exit_z: float = 0.5, min_correlation: float = 0.6):
        self.window = window
        self.entry_z = entry_z
        self.exit_z = exit_z
        self.min_correlation = min_correlation

    def signal(self, left: pd.Series, right: pd.Series) -> PairSignal:
        aligned = pd.concat([left, right], axis=1).dropna().iloc[-self.window:]
        if len(aligned) < self.window:
            return PairSignal("hold", 0.0, 0.0)
        x, y = aligned.iloc[:, 0], aligned.iloc[:, 1]
        correlation = float(x.pct_change().corr(y.pct_change()))
        if not np.isfinite(correlation) or abs(correlation) < self.min_correlation:
            return PairSignal("hold", 0.0, 0.0)
        variance = float(y.var())
        hedge = 0.0 if variance <= 0 else float(x.cov(y) / variance)
        spread = x - hedge * y
        sigma = float(spread.std())
        z_score = 0.0 if sigma <= 0 else float((spread.iloc[-1] - spread.mean()) / sigma)
        if z_score >= self.entry_z:
            action = "short_left_long_right"
        elif z_score <= -self.entry_z:
            action = "long_left_short_right"
        elif abs(z_score) <= self.exit_z:
            action = "exit"
        else:
            action = "hold"
        return PairSignal(action, z_score, hedge)
