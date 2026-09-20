"""Causal long-only trend ensemble for explicitly opted-in research runs."""

from __future__ import annotations

from dataclasses import replace
import json
import math
from numbers import Integral
from typing import Any, Dict, Optional, Sequence

import numpy as np
import pandas as pd

from core.candidate_scoring import ScoreBreakdown
from core.entry_audit import note
from core.portfolio import Portfolio
from core.protective_stops import ProtectiveStopPolicy
from core.risk import RiskManager
from core.state import MarketState
from strategies.trend_breakout import TrendBreakoutStrategy


class TrendPortfolioV2Strategy(TrendBreakoutStrategy):
    """Return signs select direction; Donchian selects the entry time.

    Default score: .50*sign(r20)+.30*sign(r60)+.20*sign(r120), strictly >.30
    for longs. SIDEWAYS also requires score>=.60 and a close at least .50 ATR
    above the previous entry channel. TREND_DOWN permits position management
    but never new longs. Health and portfolio controls remain authoritative.

    Volatility targets size new entries; they do not promise achieved portfolio
    volatility or rebalance held positions. Horizons are bars, and annualization
    must be explicitly changed from daily 365 for another timeframe (4h:2190).
    """

    MARKET_RISK_MULTIPLIERS = {
        MarketState.TREND_UP: 1.0, MarketState.SIDEWAYS: 0.25,
        MarketState.VOLATILE: 0.5, MarketState.TREND_DOWN: 0.0,
    }

    def __init__(
        self, entry_window: int = 20, exit_window: int = 10, *,
        use_obv: bool = True, horizons: Sequence[int] = (20, 60, 120),
        weights: Optional[Sequence[float]] = None,
        long_score_threshold: float = 0.3,
        market_state_mode: str = "risk_multiplier", sideways_breakout_atr: float = 0.5,
        exit_mode: str = "atr", medium_exit_window: int = 60,
        initial_atr_multiple: float = 2.0, trailing_atr_multiple: float = 2.5,
        volatility_sizing: bool = True, volatility_window: int = 20,
        periods_per_year: float = 365.0, target_annual_volatility: float = 0.10,
        asset_base_weight: float = 0.5, max_asset_weight: float = 0.25,
        max_initial_risk: float = 0.01,
    ):
        horizons = tuple(horizons)
        if not horizons or any(isinstance(h, bool) or not isinstance(h, Integral) or h <= 0
                               for h in horizons):
            raise ValueError("horizons must contain positive integer bar counts")
        if len(set(horizons)) != len(horizons):
            raise ValueError("horizons must be unique")
        supplied_weights = tuple(weights) if weights is not None else (
            (0.5, 0.3, 0.2) if len(horizons) == 3 else (1.0,) * len(horizons)
        )
        if len(supplied_weights) != len(horizons):
            raise ValueError("weights must match the number of horizons")
        try:
            numeric_weights = tuple(float(weight) for weight in supplied_weights)
            total_weight = math.fsum(numeric_weights)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("weights must be finite positive numbers") from exc
        if (any(isinstance(weight, bool) for weight in supplied_weights)
                or any(not math.isfinite(weight) or weight <= 0 for weight in numeric_weights)
                or not math.isfinite(total_weight)):
            raise ValueError("weights must be finite positive numbers")
        for name, value in (("entry_window", entry_window), ("exit_window", exit_window),
                            ("medium_exit_window", medium_exit_window),
                            ("volatility_window", volatility_window)):
            if isinstance(value, bool) or not isinstance(value, Integral) or value <= 0:
                raise ValueError(f"{name} must be a positive integer bar count")
        if volatility_window < 2:
            raise ValueError("volatility_window must include at least two returns")
        if market_state_mode not in {"hard_gate", "risk_multiplier"}:
            raise ValueError("market_state_mode must be hard_gate or risk_multiplier")
        if exit_mode not in {"baseline", "atr", "hybrid"}:
            raise ValueError("exit_mode must be baseline, atr or hybrid")
        if not isinstance(volatility_sizing, bool):
            raise ValueError("volatility_sizing must be boolean")
        positive = {
            "initial_atr_multiple": initial_atr_multiple,
            "trailing_atr_multiple": trailing_atr_multiple,
            "periods_per_year": periods_per_year,
            "target_annual_volatility": target_annual_volatility,
            "asset_base_weight": asset_base_weight,
            "max_asset_weight": max_asset_weight, "max_initial_risk": max_initial_risk,
        }
        for name, value in positive.items():
            if isinstance(value, bool) or not math.isfinite(float(value)) or float(value) <= 0:
                raise ValueError(f"{name} must be finite and positive")
        for name, value in (("asset_base_weight", asset_base_weight),
                            ("max_asset_weight", max_asset_weight),
                            ("max_initial_risk", max_initial_risk)):
            if float(value) > 1:
                raise ValueError(f"{name} must not exceed one")
        if not math.isfinite(float(long_score_threshold)) or not 0 <= long_score_threshold < 1:
            raise ValueError("long_score_threshold must be in [0, 1)")
        if not math.isfinite(float(sideways_breakout_atr)) or sideways_breakout_atr < 0:
            raise ValueError("sideways_breakout_atr must be finite and nonnegative")

        super().__init__(entry_window, exit_window, use_obv=use_obv)
        self.name = "TrendPortfolioV2"
        self.allowed_states = set(self.MARKET_RISK_MULTIPLIERS)
        self.horizons = tuple(int(h) for h in horizons)
        self.weights = tuple(weight / total_weight for weight in numeric_weights)
        self.long_score_threshold = float(long_score_threshold)
        self.market_state_mode = market_state_mode
        self.sideways_breakout_atr = float(sideways_breakout_atr)
        self.exit_mode = exit_mode
        self.medium_exit_window = int(medium_exit_window)
        self.initial_atr_multiple = float(initial_atr_multiple)
        self.trailing_atr_multiple = float(trailing_atr_multiple)
        self.volatility_sizing = volatility_sizing
        self.volatility_window = int(volatility_window)
        self.periods_per_year = float(periods_per_year)
        self.target_annual_volatility = float(target_annual_volatility)
        self.asset_base_weight = float(asset_base_weight)
        self.max_asset_weight = float(max_asset_weight)
        self.max_initial_risk = float(max_initial_risk)
        # The observer fingerprints public scalar parameters; include tuples.
        self.trend_parameters_identity = json.dumps({
            "horizons": self.horizons, "weights": self.weights,
            "component": "sign_of_trailing_return", "sideways_min_score": 0.6,
            "market_risk_multipliers": {
                state.name: multiplier for state, multiplier in self.MARKET_RISK_MULTIPLIERS.items()
            },
        }, sort_keys=True, separators=(",", ":"))
        self._initialize_health_state()
        self.configure_stop_policy(ProtectiveStopPolicy())

    def configure_stop_policy(self, policy: ProtectiveStopPolicy) -> None:
        """Choose the exit arm while retaining injected shared safety bounds."""
        self.stop_policy = replace(
            policy,
            initial_stop_mode="structural_donchian" if self.exit_mode == "baseline" else self.exit_mode,
            use_atr_initial_stop=self.exit_mode != "baseline",
            use_trailing_stop=self.exit_mode != "baseline",
            initial_atr_multiple=self.initial_atr_multiple,
            trailing_atr_multiple=self.trailing_atr_multiple,
        )

    def entry_risk_multiplier(self, state: MarketState) -> float:
        if self.market_state_mode == "hard_gate":
            return 1.0 if state == MarketState.TREND_UP else 0.0
        return self.MARKET_RISK_MULTIPLIERS.get(state, 0.0)

    @property
    def _required_history(self) -> int:
        return max(*self.horizons, self.entry_window, self.exit_window,
                   self.stop_policy.atr_period - 1,
                   self.volatility_window if self.volatility_sizing else 0)

    def _uses_atr(self) -> bool:
        return True  # SIDEWAYS breakout strength needs ATR in every exit arm.

    def _ensure_score_inputs(self, df: pd.DataFrame) -> None:
        pass

    def _score_signal(
        self, df: pd.DataFrame, i: int, *, channel_level: Any, side: str,
    ) -> Optional[ScoreBreakdown]:
        if i < self._required_history or i >= len(df):
            return None
        try:
            prices = df.iloc[i - self._required_history:i + 1].loc[
                :, ["high", "low", "close"]
            ].to_numpy(dtype=float)
            if not np.isfinite(prices).all() or not (prices > 0).all():
                return None
            close = float(df["close"].iat[i])
            components = {
                f"trend_{horizon}": float(np.sign(close - float(df["close"].iat[i - horizon])))
                for horizon in self.horizons
            }
        except (KeyError, TypeError, ValueError):
            return None
        # A strict decimal gate must not be crossed by floating-point dust.
        total = round(math.fsum(weight * components[f"trend_{horizon}"]
                               for horizon, weight in zip(self.horizons, self.weights)), 12)
        return ScoreBreakdown(total, components)

    def _realized_volatility(self, df: pd.DataFrame, i: int) -> Optional[float]:
        """Sample standard deviation of completed trailing log returns."""
        if i < self.volatility_window or i >= len(df):
            return None
        try:
            closes = df["close"].iloc[i - self.volatility_window:i + 1].to_numpy(dtype=float)
            if not np.isfinite(closes).all() or not (closes > 0).all():
                return None
            realized = float(np.std(np.diff(np.log(closes)), ddof=1) * math.sqrt(self.periods_per_year))
        except (KeyError, TypeError, ValueError):
            return None
        return realized if math.isfinite(realized) and realized > 1e-12 else None

    def _raw_entry_signal(
        self, symbol: str, i: int, df: pd.DataFrame, portfolio: Portfolio,
    ) -> Optional[Dict[str, Any]]:
        if i < self._required_history or i >= len(df):
            note("signal_warmup")
            return None
        breakdown = self._score_signal(df, i, channel_level=None, side="buy")
        if breakdown is None:
            note("invalid_trend_score")
            return None
        if breakdown.total <= self.long_score_threshold:
            note("trend_score_gate", trend_score=breakdown.total)
            return None
        try:
            self._ensure_atr(df)
            atr = self._atr_at(df, i)
        except (KeyError, TypeError, ValueError):
            return None
        if atr is None or not math.isfinite(atr) or atr <= 0:
            note("invalid_atr")
            return None
        realized = self._realized_volatility(df, i)
        if self.volatility_sizing and realized is None:
            note("invalid_realized_volatility")
            return None
        signal = super()._raw_entry_signal(symbol, i, df, portfolio)
        if signal is None:
            return None
        signal["trend_score"] = breakdown.total
        signal["realized_volatility"] = realized
        signal["volatility_periods_per_year"] = self.periods_per_year
        signal["breakout_atr"] = (float(df["close"].iat[i]) - float(df[self.col_high_max].iat[i])) / atr
        if self.volatility_sizing:
            signal["target_weight"] = min(
                self.max_asset_weight,
                self.asset_base_weight * breakdown.total * self.target_annual_volatility / realized,
            )
        return signal

    def should_enter(
        self, symbol: str, i: int, df: pd.DataFrame,
        state: MarketState, portfolio: Portfolio,
    ) -> Optional[Dict[str, Any]]:
        signal = super().should_enter(symbol, i, df, state, portfolio)
        if signal is None:
            return None
        multiplier = self.entry_risk_multiplier(state)
        if multiplier <= 0:
            note("market_state_zero_risk")
            return None
        if state == MarketState.SIDEWAYS and (
            signal["score"] < 0.6 or signal.get("breakout_atr", -math.inf) < self.sideways_breakout_atr
        ):
            note("sideways_strong_breakout_required")
            return None
        signal["market_risk_multiplier"] = multiplier
        return signal

    def initial_entry_quantity(
        self, *, signal: Dict[str, Any], equity: float, current_price: float,
        stop_loss: float, risk_manager: RiskManager,
    ) -> float:
        if not self.volatility_sizing:
            return super().initial_entry_quantity(
                signal=signal, equity=equity, current_price=current_price,
                stop_loss=stop_loss, risk_manager=risk_manager,
            )
        target_weight = float(signal.get("target_weight", 0.0) or 0.0)
        if (not all(math.isfinite(value) for value in (equity, current_price, stop_loss, target_weight))
                or equity <= 0 or current_price <= 0 or not 0 < stop_loss < current_price
                or target_weight <= 0):
            return 0.0
        # Both APIs preserve account health and breaker checks and apply the
        # account multiplier exactly once. The later shared path applies state
        # and strategy-health multipliers, reservation and correlation caps.
        risk_weight = self.max_initial_risk * current_price / (current_price - stop_loss)
        weight_qty = risk_manager.calculate_position_size_fixed_pct(
            equity, current_price, pct=min(target_weight, self.max_asset_weight, risk_weight),
        )
        risk_qty = risk_manager.calculate_position_size(equity, current_price, stop_loss)
        return max(0.0, min(weight_qty, risk_qty))

    def should_exit(
        self, symbol: str, i: int, df: pd.DataFrame,
        state: MarketState, portfolio: Portfolio,
    ) -> Optional[Dict[str, Any]]:
        if self.exit_mode == "baseline":
            return super().should_exit(symbol, i, df, state, portfolio)
        if state not in self.allowed_states:
            return {"action": "sell", "reason": f"Regime {state.name} Not Allowed"}
        self._ensure_indicators(df)
        # The base flow checks the old stop first. This completed-bar update
        # is only effective next bar; the shared trailing stop never loosens.
        self._update_protective_stop(symbol, i, df, side="long")
        score = self._score_signal(df, i, channel_level=None, side="buy")
        if score is not None and score.total < 0:
            return {"action": "sell", "reason": "TrendScoreReversal", "trend_score": score.total}
        if i >= self.medium_exit_window:
            channel = df["low"].iloc[i - self.medium_exit_window:i]
            if channel.notna().all() and float(df["close"].iat[i]) < float(channel.min()):
                return {"action": "sell", "reason": f"MediumChannelExit{self.medium_exit_window}"}
        return None
