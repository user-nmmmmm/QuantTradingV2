"""Opt-in long-only daily portfolio alpha, sharing the existing health/stops."""

from __future__ import annotations

from dataclasses import asdict, replace
import json
import math
from typing import Any, Mapping, Sequence

import pandas as pd

from core.candidate_scoring import ScoreBreakdown
from core.entry_audit import note
from core.protective_stops import ProtectiveStopPolicy
from core.selection_v2 import (SelectionPolicyV2, _signal_facts_prepared, completed_history,
                               signal_facts, utc_time)
from core.state import MarketState
from strategies.trend_breakout import TrendBreakoutStrategy
from strategies.trend_portfolio_v2 import TrendPortfolioV2Strategy


class TrendPortfolioV3Strategy(TrendPortfolioV2Strategy):
    """Two fixed entry variants with continuous momentum and portfolio targets.

    New risk must receive a weight from the weekly portfolio coordinator. The
    strategy never independently converts momentum strength into an exposure.
    Metadata membership belongs to the all-qualified selector; price signals
    remain available independently to the existing passive health observers.
    """

    MARKET_RISK_MULTIPLIERS = {
        MarketState.TREND_UP: 1.0, MarketState.TREND_DOWN: 1.0,
        MarketState.SIDEWAYS: 1.0, MarketState.VOLATILE: 1.0,
    }

    def __init__(self, *, variant: str = "momentum", horizons: Sequence[int] = (60, 120),
                 min_listing_days: int = 180, min_quote_volume: float = 5_000_000.0,
                 use_obv: bool = True, entry_window: int = 20, medium_exit_window: int = 60,
                 volatility_window: int = 60, target_annual_volatility: float = 0.10,
                 max_asset_weight: float = 0.30, max_initial_risk: float = 0.01,
                 initial_atr_multiple: float = 2.0, trailing_atr_multiple: float = 2.5):
        policy = SelectionPolicyV2(
            variant=variant, horizons=tuple(horizons), min_listing_days=min_listing_days,
            min_quote_volume=min_quote_volume, use_obv=use_obv, entry_window=entry_window,
            exit_window=medium_exit_window, volatility_window=volatility_window,
            initial_atr_multiple=initial_atr_multiple,
            target_annual_volatility=target_annual_volatility, max_symbol_weight=max_asset_weight,
            max_symbol_initial_risk=max_initial_risk)
        super().__init__(
            entry_window=entry_window, exit_window=medium_exit_window, use_obv=use_obv,
            horizons=policy.horizons, weights=(0.5, 0.5), long_score_threshold=0.0,
            medium_exit_window=medium_exit_window, exit_mode="atr",
            initial_atr_multiple=initial_atr_multiple, trailing_atr_multiple=trailing_atr_multiple,
            volatility_window=volatility_window, target_annual_volatility=target_annual_volatility,
            max_asset_weight=max_asset_weight, max_initial_risk=max_initial_risk)
        self.name = "TrendPortfolioV3"
        self.variant = variant
        self.selection_policy = policy
        self.trend_parameters_identity = json.dumps(asdict(policy), sort_keys=True, separators=(",", ":"))
        self._initialize_health_state()
        self._portfolio_target_weights: dict[str, float] = {}

    def configure_stop_policy(self, policy: ProtectiveStopPolicy) -> None:
        super().configure_stop_policy(policy)
        if hasattr(self, "selection_policy"):
            self.selection_policy = replace(self.selection_policy, atr_period=policy.atr_period,
                                            initial_atr_multiple=self.initial_atr_multiple,
                                            min_stop_distance_pct=policy.min_stop_distance_pct,
                                            max_stop_distance_pct=policy.max_stop_distance_pct)
            self.trend_parameters_identity = json.dumps(asdict(self.selection_policy), sort_keys=True, separators=(",", ":"))

    @property
    def _required_history(self) -> int:
        return self.selection_policy.required_closes - 1

    def reset_runtime_state(self) -> None:
        super().reset_runtime_state()
        self._portfolio_target_weights = {}

    def set_portfolio_targets(self, weights: Mapping[str, float]) -> None:
        """Install coordinator-approved, pre-health desired exposure weights."""
        parsed = {symbol: float(weight) for symbol, weight in weights.items()}
        if any(not math.isfinite(weight) or weight < 0 for weight in parsed.values()):
            raise ValueError("portfolio target weights must be finite and nonnegative")
        self._portfolio_target_weights = parsed

    def signal_facts(self, history: pd.DataFrame, as_of: Any) -> dict[str, Any]:
        return signal_facts(history, as_of=as_of, policy=self.selection_policy)

    def _facts_at(self, df: pd.DataFrame, i: int) -> dict[str, Any] | None:
        if i < self._required_history or i >= len(df):
            return None
        return self.signal_facts(df.iloc[:i + 1], utc_time(df.index[i]) + pd.Timedelta(days=1))

    def _score_signal(self, df: pd.DataFrame, i: int, *, channel_level: Any, side: str):
        facts = self._facts_at(df, i)
        if facts is None or facts.get("score") is None:
            return None
        close = float(df["close"].iat[i])
        return ScoreBreakdown(float(facts["score"]), {
            f"return_{horizon}": close / float(df["close"].iat[i - horizon]) - 1.0
            for horizon in self.horizons})

    def _raw_entry_signal(self, symbol: str, i: int, df: pd.DataFrame, portfolio):
        facts = self._facts_at(df, i)
        if facts is None:
            note("signal_warmup")
            return None
        if not facts["add_allowed"]:
            note("v3_signal_gate", v3_reasons=facts["reasons"], breakout=facts["breakout"])
            return None
        self._ensure_indicators(df)
        plan = self._plan_stop(side="buy", reference_price=facts["close"],
                               structural_stop=df[self.col_low_min].iat[i], df=df, i=i)
        if not plan.accepted:
            note("invalid_stop", stop_rejection=plan.reject_reason)
            return None
        components = {f"return_{horizon}": facts["close"] / float(df["close"].iat[i - horizon]) - 1.0
                      for horizon in self.horizons}
        return {
            "action": "buy", "price": facts["close"], "stop_loss": plan.stop_price,
            "stop_plan": plan.to_dict(), "order_type": "market", "score": facts["score"],
            "trend_score": facts["score"], "score_components": components,
            "target_weight": self._portfolio_target_weights.get(symbol, 0.0),
            "market_risk_multiplier": 1.0, "portfolio_variant": self.variant,
            "signal_observed_at": facts["observed_at"], "signal_available_at": facts["available_at"],
        }

    def should_enter(self, symbol, i, df, state, portfolio):
        if self.entry_risk_multiplier(state) == 0:
            note("market_state_zero_risk")
            return None
        # Directly use the shared health lifecycle, avoiding V2's SIDEWAYS
        # strength gate and its normal-regime sizing multipliers.
        return TrendBreakoutStrategy.should_enter(self, symbol, i, df, state, portfolio)

    def should_exit(self, symbol, i, df, state, portfolio):
        if state not in self.allowed_states:
            return {"action": "sell", "reason": f"Regime {state.name} Not Allowed"}
        if i < 0 or i >= len(df):
            return None
        as_of = utc_time(df.index[i]) + pd.Timedelta(days=1)
        completed = completed_history(df.iloc[:i + 1], as_of)
        # Position management never gets to use a delayed publication merely
        # because the historical cache already contains its eventual value.
        if completed.empty or completed.index[-1] != utc_time(df.index[i]):
            return None
        if len(completed) == i + 1:
            self._ensure_atr(df)
            self._update_protective_stop(symbol, i, df, side="long")
        else:
            # Cached ATR was calculated over the raw history; when an older
            # publication is still hidden, recompute only on visible facts.
            stop_history = completed[["high", "low", "close"]].copy()
            self._ensure_atr(stop_history)
            self._update_protective_stop(symbol, len(stop_history) - 1, stop_history, side="long")
        facts = _signal_facts_prepared(completed, as_of=as_of, policy=self.selection_policy, price_only=True)
        if facts.get("score") is not None and facts["score"] < 0:
            return {"action": "sell", "reason": "MomentumReversal", "trend_score": facts["score"]}
        if len(completed) > self.medium_exit_window:
            low = pd.to_numeric(completed["low"].iloc[-self.medium_exit_window - 1:-1], errors="coerce")
            close = float(completed["close"].iloc[-1])
            if low.notna().all() and math.isfinite(close) and close < float(low.min()):
                return {"action": "sell", "reason": f"MediumChannelExit{self.medium_exit_window}"}
        return None
