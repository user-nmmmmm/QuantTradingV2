"""High-volatility contraction/reversion strategy for the VOLATILE regime."""

from __future__ import annotations

from typing import Any, Dict, Optional

import pandas as pd

from core.factors import MomentumFactors
from core.portfolio import Portfolio
from core.state import MarketState
from strategies.base import Strategy


class VolatilityReversionStrategy(Strategy):
    """Trade failed volatility extensions with ATR-defined hard risk.

    Entries additionally require a Stochastic Oscillator (%K) confirmation
    at the extension extreme (oversold for longs, overbought for shorts),
    to avoid acting on a lone z-score extreme with no momentum exhaustion.
    """

    def __init__(
        self,
        window: int = 20,
        entry_z: float = 2.0,
        stop_atr: float = 1.5,
        stoch_oversold: float = 20.0,
        stoch_overbought: float = 80.0,
        *,
        use_stochastic: bool = True,
    ):
        super().__init__("VolatilityReversion", {MarketState.VOLATILE})
        self.window = window
        self.entry_z = entry_z
        self.stop_atr = stop_atr
        self.stoch_oversold = stoch_oversold
        self.stoch_overbought = stoch_overbought
        self.use_stochastic = use_stochastic

    def _indicators(self, df: pd.DataFrame) -> tuple[pd.Series, pd.Series, pd.Series]:
        mean = df["close"].rolling(self.window).mean()
        std = df["close"].rolling(self.window).std()
        atr = (df["high"] - df["low"]).rolling(self.window).mean()
        return mean, std, atr

    def _ensure_stoch(self, df: pd.DataFrame) -> None:
        if "STOCH_K" not in df.columns:
            df["STOCH_K"], df["STOCH_D"] = MomentumFactors.STOCH(df)

    def should_enter(self, symbol: str, i: int, df: pd.DataFrame, state: MarketState, portfolio: Portfolio) -> Optional[Dict[str, Any]]:
        if state not in self.allowed_states or i < self.window:
            return None
        mean, std, atr = self._indicators(df)
        close, center, sigma, risk = map(float, (df["close"].iat[i], mean.iat[i], std.iat[i], atr.iat[i]))
        if not all(pd.notna(value) and value > 0 for value in (close, center, sigma, risk)):
            return None
        stoch_k = None
        if self.use_stochastic:
            self._ensure_stoch(df)
            stoch_k = df["STOCH_K"].iat[i]
            if pd.isna(stoch_k):
                return None
        z_score = (close - center) / sigma
        if z_score <= -self.entry_z and (
            not self.use_stochastic or stoch_k < self.stoch_oversold
        ):
            return {"action": "buy", "price": close, "stop_loss": close - self.stop_atr * risk}
        if z_score >= self.entry_z and (
            not self.use_stochastic or stoch_k > self.stoch_overbought
        ):
            return {"action": "short", "price": close, "stop_loss": close + self.stop_atr * risk}
        return None

    def should_exit(self, symbol: str, i: int, df: pd.DataFrame, state: MarketState, portfolio: Portfolio) -> Optional[Dict[str, Any]]:
        qty = portfolio.get_position(symbol)["qty"]
        if not qty:
            return None
        center = self._indicators(df)[0].iat[i]
        close = float(df["close"].iat[i])
        if state not in self.allowed_states or (qty > 0 and close >= center):
            return {"action": "sell", "reason": "Volatility mean reversion complete"}
        if qty < 0 and close <= center:
            return {"action": "cover", "reason": "Volatility mean reversion complete"}
        return None
