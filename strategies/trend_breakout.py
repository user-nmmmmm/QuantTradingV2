"""
趋势突破策略（Trend Breakout）模块

该策略实现 Donchian Channel 风格的突破系统：
- 入场：收盘价突破过去 N 根 bar 的最高价（前高）
- 出场：收盘价跌破过去 M 根 bar 的最低价（退出通道）

与路由/风控的集成：
- allowed_states 只包含 TREND_UP：与生产 routing 一致（STR-P1-07）；
  VOLATILE 需要 SR5 样本外证据和对应 routing 条目才能重新加入
- 入场信号提供 stop_loss（使用 Donchian Exit Level），供 RiskManager 做风险定仓
- 内置“健康度生命周期”（SR1）：按退出 cohort 统计，进入 COOLDOWN/PROBATION/MANUAL_LOCK

成交量确认（OBV）：
- 突破入场额外要求 OBV 在 entry_window 窗口内同向累积（多头 OBV 上升 / 空头 OBV 下降），
  过滤缩量假突破（价格突破但背后无资金推动）
"""

from typing import Dict, Any, Optional
import pandas as pd
from core.state import MarketState
from core.portfolio import Portfolio
from core.factors import VolumeFactors
from core.candidate_scoring import (
    CandidateScorePolicy,
    score_breakout_candidate,
)
from core.indicators import Indicators
from core.protective_stops import (
    ProtectiveStopPolicy,
    plan_initial_stop,
    update_trailing_stop,
)
from core.strategy_health import (
    HealthStatus,
    StrategyHealthMachine,
    StrategyHealthPolicy,
)
from strategies.base import Strategy


def _bar_time(df: pd.DataFrame, i: int) -> Any:
    """Bar timestamp used as "now" for time-based health transitions."""
    try:
        return df.index[i]
    except (IndexError, TypeError):
        return None




class _PersistentHealthMixin:
    """Durable alpha-health lifecycle shared by the Donchian strategies.

    SR1-1: the old ``is_alive`` boolean was a permanent kill switch - once a
    run of cross-symbol losses flipped it, no later profit ever turned it back
    on, and the strategy silently stopped trading for years while the report
    still said ``completed``. It is replaced by
    :class:`core.strategy_health.StrategyHealthMachine`, whose only terminal
    state is an explicit, audited ``MANUAL_LOCK``.

    SR1-2: observations are exit **cohorts**, not per-symbol closes, so one
    DailyLossLimit action closing fifteen correlated coins is one observation.

    ``health_stats`` is kept as a read-only compatibility view for existing
    callers/reports; the machine is the authority.
    """

    def _initialize_health_state(
        self, policy: Optional[StrategyHealthPolicy] = None,
    ) -> None:
        self._health_state_store = None
        self.health = StrategyHealthMachine(self.name, policy or StrategyHealthPolicy())
        self.health_stats = self._new_health_stats()
        self._reset_setup_counters()

    def _reset_setup_counters(self) -> None:
        # SR1-4: how many setups the alpha actually produced, and how many of
        # them the health gate suppressed. Without this a gated strategy is
        # indistinguishable from a market that offered nothing.
        self.raw_setup_count = 0
        self.suppressed_setup_count = 0
        self.last_raw_setup_at: Optional[Any] = None
        self.last_suppressed_setup_at: Optional[Any] = None

    def record_raw_setup(self, timestamp: Any, *, suppressed: bool) -> None:
        self.raw_setup_count += 1
        self.last_raw_setup_at = timestamp
        if suppressed:
            self.suppressed_setup_count += 1
            self.last_suppressed_setup_at = timestamp

    def configure_health_policy(self, policy: StrategyHealthPolicy) -> None:
        """Install the configured lifecycle policy at the composition boundary.

        Strategies must not read config themselves (import-direction rule in
        tests/test_architecture_boundaries.py), so the policy is injected by
        ``composition.factory.build_strategy_registry``.
        """
        self.health.policy = policy

    def _new_health_stats(self) -> Dict[str, Any]:
        return {
            "scope": "exit_cohort_aggregate",
            "total_trades": 0,
            "consecutive_losses": 0,
            "rolling_pnl": [],
            "is_alive": True,
            "death_reason": None,
        }

    def _refresh_health_stats(self) -> None:
        """Project the machine onto the legacy ``health_stats`` shape."""
        self.health_stats["consecutive_losses"] = (
            self.health.consecutive_negative_cohorts
        )
        self.health_stats["is_alive"] = self.health.status in (
            HealthStatus.ACTIVE, HealthStatus.PROBATION,
        )
        self.health_stats["death_reason"] = (
            None if self.health_stats["is_alive"]
            else self.health.trigger_reason or self.health.status.value
        )
        self.health_stats["status"] = self.health.status.value
        self.health_stats["risk_multiplier"] = self.health.risk_multiplier

    @property
    def health_state_key(self) -> str:
        return f"strategy_health:{self.name}"

    def bind_state_store(self, state_store) -> None:
        self._health_state_store = state_store
        loaded = state_store.get(self.health_state_key)
        if isinstance(loaded, dict):
            self.health.load(loaded)
            self.health_stats["total_trades"] = sum(
                cohort.trade_count for cohort in self.health.cohorts
            )
            self.health_stats["rolling_pnl"] = [
                cohort.net_pnl for cohort in self.health.cohorts
            ]
            self._refresh_health_stats()

    def _persist_health(self) -> None:
        if self._health_state_store is not None:
            self._health_state_store.set(self.health_state_key, self.health.to_dict())

    def reset_runtime_state(self) -> None:
        super().reset_runtime_state()
        self.health.reset()
        self.health_stats = self._new_health_stats()
        self._reset_setup_counters()

    def health_risk_multiplier(self) -> float:
        return self.health.risk_multiplier

    def health_snapshot(self) -> Dict[str, Any]:
        snapshot = self.health.snapshot()
        snapshot.update({
            "raw_setup_count": self.raw_setup_count,
            "suppressed_raw_setups": self.suppressed_setup_count,
            "last_raw_setup_at": (
                str(self.last_raw_setup_at) if self.last_raw_setup_at is not None
                else None
            ),
            "last_suppressed_setup_at": (
                str(self.last_suppressed_setup_at)
                if self.last_suppressed_setup_at is not None else None
            ),
        })
        return snapshot

    def on_trade_closed(
        self, symbol: str, realized_pnl: float, trade: Dict[str, Any],
        bar_index: int,
    ) -> None:
        """Fold one authoritative close into its exit cohort (SR1-2).

        ``trade`` carries the close-event identity, exit reason, timestamp,
        lot initial risk and the breaker action id when the exit was forced by
        portfolio risk - everything the cohort key needs.
        """
        trade = trade or {}
        close_event_id = str(
            trade.get("close_event_id")
            or f"{self.name}:{symbol}:{bar_index}:{len(self.health.cohorts)}"
        )
        cohort = self.health.ingest_close(
            close_event_id=close_event_id,
            symbol=symbol,
            realized_pnl=float(realized_pnl),
            exit_reason=trade.get("exit_reason"),
            initial_risk=trade.get("initial_risk"),
            timestamp=trade.get("timestamp"),
            risk_action_id=trade.get("risk_action_id"),
            bar_index=bar_index,
        )
        if cohort is None:  # duplicate delivery - already counted
            return
        self.health_stats["total_trades"] += 1
        self.health_stats["rolling_pnl"].append(float(realized_pnl))
        self._refresh_health_stats()
        self._persist_health()

    def check_health(self, now: Any = None) -> bool:
        """Whether the strategy may open new risk right now.

        Exits are never gated by health (REG-01): callers only consult this
        before ``should_enter``.
        """
        allowed = self.health.allows_new_entries(now)
        self._refresh_health_stats()
        self._persist_health()
        return allowed


class _ProtectiveStopMixin:
    """Hybrid initial stop + Chandelier trailing stop (SR2-1/2/3).

    Only facts from the completed signal bar are used: the ATR and Donchian
    level at bar ``i``. The stop that is *in force* during bar ``i`` is always
    the one computed at bar ``i-1`` - ``Strategy.on_bar`` checks
    ``hard_stop_exit`` before ``should_exit``, so a trail update can never
    reach back and change the level that was already tested this bar.
    """

    stop_policy: ProtectiveStopPolicy = ProtectiveStopPolicy()

    def configure_stop_policy(self, policy: ProtectiveStopPolicy) -> None:
        self.stop_policy = policy

    @property
    def _atr_column(self) -> str:
        return f"ATR_{self.stop_policy.atr_period}"

    def _uses_atr(self) -> bool:
        # The candidate score measures breakout extent in ATR units, so ATR is
        # computed whenever scoring is on, not only for the ATR stop legs.
        return (
            self.stop_policy.use_atr_initial_stop
            or self.stop_policy.use_trailing_stop
            or self.stop_policy.breakeven_after_r is not None
            or getattr(self, "score_policy", None) is not None
            and self.score_policy.enabled
        )

    def _ensure_atr(self, df: pd.DataFrame) -> None:
        if not self._uses_atr():
            return
        column = self._atr_column
        if column not in df.columns:
            df[column] = Indicators.ATR(df, self.stop_policy.atr_period)

    def _atr_at(self, df: pd.DataFrame, i: int) -> Optional[float]:
        column = self._atr_column
        if column not in df.columns:
            return None
        value = df[column].iat[i]
        return None if pd.isna(value) else float(value)

    def _plan_stop(
        self, *, side: str, reference_price: float,
        structural_stop: Any, df: pd.DataFrame, i: int,
    ):
        return plan_initial_stop(
            side=side,
            reference_price=float(reference_price),
            structural_stop=(
                None if structural_stop is None or pd.isna(structural_stop)
                else float(structural_stop)
            ),
            atr=self._atr_at(df, i),
            policy=self.stop_policy,
        )

    def _update_protective_stop(
        self, symbol: str, i: int, df: pd.DataFrame, *, side: str,
    ) -> Optional[float]:
        """Advance the trail for an open position; never loosen it."""
        ctx = self.get_context(symbol)
        if not ctx or ctx.get("stop_loss") in (None, 0):
            return None
        long_side = side == "long"
        extreme_key = "highest_high_since_fill" if long_side else "lowest_low_since_fill"
        if long_side:
            bar_extreme = float(
                df["high"].iat[i] if "high" in df else df["close"].iat[i]
            )
        else:
            bar_extreme = float(
                df["low"].iat[i] if "low" in df else df["close"].iat[i]
            )
        previous = ctx.get(extreme_key)
        if previous is None:
            ctx[extreme_key] = bar_extreme
        elif long_side:
            ctx[extreme_key] = max(float(previous), bar_extreme)
        else:
            ctx[extreme_key] = min(float(previous), bar_extreme)
        ctx.setdefault("initial_stop", ctx.get("stop_loss"))
        new_stop = update_trailing_stop(
            side=side,
            current_stop=ctx.get("stop_loss"),
            initial_stop=ctx.get("initial_stop"),
            extreme_since_fill=ctx[extreme_key],
            atr=self._atr_at(df, i),
            policy=self.stop_policy,
            entry_price=ctx.get("entry_price"),
        )
        if new_stop is not None:
            ctx["trailing_stop"] = new_stop
            ctx["effective_stop"] = new_stop
            ctx["stop_loss"] = new_stop
        return new_stop


class _CandidateScoringMixin:
    """Attach an economically meaningful score to every signal (SR3-1).

    Without this the allocator ranks a batch of equal zeros and the surviving
    order is alphabetical (STR-P1-03). All inputs come from the signal bar.
    """

    score_policy: CandidateScorePolicy = CandidateScorePolicy()

    def configure_score_policy(self, policy: CandidateScorePolicy) -> None:
        self.score_policy = policy

    @property
    def _adx_column(self) -> str:
        return "ADX_14"

    def _ensure_score_inputs(self, df: pd.DataFrame) -> None:
        if not self.score_policy.enabled:
            return
        if self._adx_column not in df.columns:
            df[self._adx_column] = Indicators.ADX(df, 14)

    def _score_signal(
        self, df: pd.DataFrame, i: int, *, channel_level: Any, side: str,
    ) -> Any:
        if not self.score_policy.enabled:
            return None
        close = df["close"].iat[i]
        obv_change = obv_scale = None
        if "OBV" in df.columns and i >= self.entry_window:
            obv_now = df["OBV"].iat[i]
            obv_prior = df["OBV"].iat[i - self.entry_window]
            if pd.notna(obv_now) and pd.notna(obv_prior):
                obv_change = float(obv_now) - float(obv_prior)
                window = df["OBV"].iloc[max(0, i - self.entry_window): i + 1]
                obv_scale = float(window.diff().abs().mean() * self.entry_window)
        traded_notional = None
        if "volume" in df.columns:
            volume = df["volume"].iat[i]
            if pd.notna(volume) and pd.notna(close):
                traded_notional = float(volume) * float(close)
        adx = df[self._adx_column].iat[i] if self._adx_column in df.columns else None
        return score_breakout_candidate(
            reference_price=close,
            channel_level=channel_level,
            atr=self._atr_at(df, i),
            adx=adx,
            obv_change=obv_change,
            obv_scale=obv_scale,
            traded_notional=traded_notional,
            policy=self.score_policy,
            side=side,
        )


class TrendBreakoutStrategy(
    _PersistentHealthMixin, _ProtectiveStopMixin, _CandidateScoringMixin, Strategy,
):
    """
    P3: Production Implementation of Trend Breakout Alpha.

    Regime scope:
    - Routed and permitted in TREND_UP only. The VOLATILE claim was removed
      under STR-P1-07: production routes VOLATILE to Cash and no OOS evidence
      supports the alpha there.

    SR1 Health Lifecycle (replaces the old permanent "Alpha Death" switch):
    - Observations are exit cohorts, not per-symbol closes
    - A losing streak of cohorts pauses the alpha into a bounded COOLDOWN,
      which expires into a reduced-size PROBATION - see
      docs/strategy_health_contract.md

    Logic:
    - Enter Long if Close > Max(High, 20)
    - Exit Long if Close < Min(Low, 10)
    - Allowed Regimes: TREND_UP
    """

    def __init__(
        self, entry_window: int = 20, exit_window: int = 10, *, use_obv: bool = True,
    ):
        """
        参数：
        - entry_window：突破窗口（过去 N 根的最高价）
        - exit_window：退出窗口（过去 M 根的最低价）
        """
        # STR-P1-07: allowed_states must describe what this strategy is
        # actually permitted to trade, not what it might plausibly trade.
        # Production routes VOLATILE to Cash and there is no out-of-sample
        # evidence for the alpha in that regime, so claiming VOLATILE here
        # made the run's real scope unreadable. VOLATILE may be added back
        # only with SR5 evidence and a matching routing entry.
        super().__init__("TrendBreakout", {MarketState.TREND_UP})
        self.entry_window = entry_window
        self.exit_window = exit_window
        self.use_obv = use_obv

        self.col_high_max = f"HIGH_MAX_{self.entry_window}"
        self.col_low_min = f"LOW_MIN_{self.exit_window}"

        # P5: Health Monitoring
        self._initialize_health_state()

    def _ensure_indicators(self, df: pd.DataFrame):
        """
        计算 Donchian 通道所需滚动极值列，并 shift(1) 避免未来函数。
        - HIGH_MAX_N：过去 N 根的最高价（不含当前 bar）
        - LOW_MIN_M：过去 M 根的最低价（不含当前 bar）
        """
        if self.col_high_max not in df.columns:
            # Shift 1 to avoid lookahead bias (Standard Donchian uses previous N days)
            df[self.col_high_max] = (
                df["high"].rolling(window=self.entry_window).max().shift(1)
            )

        if self.col_low_min not in df.columns:
            df[self.col_low_min] = (
                df["low"].rolling(window=self.exit_window).min().shift(1)
            )

        if "OBV" not in df.columns and "volume" in df.columns:
            df["OBV"] = VolumeFactors.OBV(df)

        self._ensure_atr(df)
        self._ensure_score_inputs(df)

    def should_enter(
        self,
        symbol: str,
        i: int,
        df: pd.DataFrame,
        state: MarketState,
        portfolio: Portfolio,
    ) -> Optional[Dict[str, Any]]:
        """
        入场逻辑（做多）：
        - 先通过 check_health(now)；COOLDOWN/MANUAL_LOCK 期间不开新仓（已有仓位照常管理）
        - close > HIGH_MAX_entry_window 视为有效突破，返回 buy 信号
        - OBV 需在 entry_window 窗口内净上升，确认突破有成交量支撑
        - 初始止损使用 Donchian Exit Level（LOW_MIN_exit_window）
        """
        # SR1: the raw setup is evaluated first so a suppressed setup can be
        # counted (shadow_setup_count / suppressed_raw_setups). The health
        # lifecycle then gates NEW risk only; exits are never gated.
        now = _bar_time(df, i)
        signal = self._raw_entry_signal(symbol, i, df, portfolio)
        allowed = self.check_health(now)
        if signal is not None:
            self.record_raw_setup(now, suppressed=not allowed)
        return signal if allowed else None

    def _raw_entry_signal(
        self, symbol: str, i: int, df: pd.DataFrame, portfolio: Portfolio,
    ) -> Optional[Dict[str, Any]]:
        """The breakout setup itself, with no health gate applied."""
        self._ensure_indicators(df)

        if i < self.entry_window:
            return None

        close = df["close"].iat[i]
        high_max = df[self.col_high_max].iat[i]

        # Check Entry Signal
        if pd.notna(high_max) and close > high_max:
            # Volume Confirmation: OBV must have net-accumulated over the
            # entry window, otherwise the breakout lacks volume support.
            # Skipped (not required) when the data source has no volume.
            if self.use_obv and "OBV" in df.columns:
                obv_now = df["OBV"].iat[i]
                obv_prior = df["OBV"].iat[i - self.entry_window]
                if pd.isna(obv_now) or pd.isna(obv_prior) or obv_now <= obv_prior:
                    return None

            # Breakout!

            # SR2-2: hybrid initial stop = max(Donchian exit level,
            # close - k*ATR), clamped to the pre-registered distance band.
            # A signal whose stop cannot be measured is rejected - the old
            # implicit ``close * 0.95`` fallback hid exactly the anomalies this
            # roadmap needs to see.
            plan = self._plan_stop(
                side="buy", reference_price=close,
                structural_stop=df[self.col_low_min].iat[i], df=df, i=i,
            )
            if not plan.accepted:
                return None
            stop_loss = plan.stop_price
            breakdown = self._score_signal(
                df, i, channel_level=high_max, side="buy",
            )

            return {
                "action": "buy",
                "stop_loss": stop_loss,
                "order_type": "market",  # Breakouts usually need market entry to ensure fill
                # Or Limit at Close? Docs say "Limit Orders (passive entry at Close)".
                # But breakout needs to trigger. Let's use Market for Breakout to guarantee entry.
                # Actually, the system executes at Next Open.
                # If we signal "buy" now (at Close), execution is Open(i+1).
                # We can set limit=Close(i) to try to get a good price, but might miss.
                # Let's stick to system default (which is Market if no price specified? Or Limit?)
                # Looking at Broker: submit_order takes price.
                "price": close,  # Use Close as reference price
                "stop_plan": plan.to_dict(),
                # SR3-1: a real ranking key, not the implicit alphabetical one.
                "score": breakdown.total if breakdown is not None else 0.0,
                "score_components": (
                    breakdown.components if breakdown is not None else {}
                ),
            }

        return None

    def should_exit(
        self,
        symbol: str,
        i: int,
        df: pd.DataFrame,
        state: MarketState,
        portfolio: Portfolio,
    ) -> Optional[Dict[str, Any]]:
        """
        出场逻辑：
        1) close < LOW_MIN_exit_window：突破失败/回撤，平多
        2) 状态不再属于 allowed_states：由策略层做一次防守性平仓
        """
        self._ensure_indicators(df)
        # SR2-3: ratchet the protective stop of the position that is still
        # open. Applied after this bar's stop test, so it only governs the
        # next bar (no lookahead, monotone by construction).
        self._update_protective_stop(symbol, i, df, side="long")

        close = df["close"].iat[i]
        low_min = df[self.col_low_min].iat[i]

        # 1. Exit Signal
        if pd.notna(low_min) and close < low_min:
            return {
                "action": "sell",
                "reason": f"Breakout Exit (Below Low{self.exit_window})",
            }

        # 2. Regime Check (System Rule)
        if state not in self.allowed_states:
            return {"action": "sell", "reason": f"Regime {state.name} Not Allowed"}

        return None


class TrendBreakdownStrategy(
    _PersistentHealthMixin, _ProtectiveStopMixin, _CandidateScoringMixin, Strategy,
):
    """
    Mirrored Donchian breakdown strategy for short-side trend participation.
    """

    def __init__(
        self, entry_window: int = 20, exit_window: int = 10, *, use_obv: bool = True,
    ):
        super().__init__("TrendBreakdown", {MarketState.TREND_DOWN})
        self.entry_window = entry_window
        self.exit_window = exit_window
        self.use_obv = use_obv

        self.col_high_max = f"HIGH_MAX_{self.exit_window}"
        self.col_low_min = f"LOW_MIN_{self.entry_window}"
        self._initialize_health_state()

    def _ensure_indicators(self, df: pd.DataFrame):
        if self.col_high_max not in df.columns:
            df[self.col_high_max] = (
                df["high"].rolling(window=self.exit_window).max().shift(1)
            )
        if self.col_low_min not in df.columns:
            df[self.col_low_min] = (
                df["low"].rolling(window=self.entry_window).min().shift(1)
            )
        if "OBV" not in df.columns and "volume" in df.columns:
            df["OBV"] = VolumeFactors.OBV(df)

        self._ensure_atr(df)
        self._ensure_score_inputs(df)

    def should_enter(
        self,
        symbol: str,
        i: int,
        df: pd.DataFrame,
        state: MarketState,
        portfolio: Portfolio,
    ) -> Optional[Dict[str, Any]]:
        now = _bar_time(df, i)
        signal = self._raw_entry_signal(symbol, i, df, portfolio)
        allowed = self.check_health(now)
        if signal is not None:
            self.record_raw_setup(now, suppressed=not allowed)
        return signal if allowed else None

    def _raw_entry_signal(
        self, symbol: str, i: int, df: pd.DataFrame, portfolio: Portfolio,
    ) -> Optional[Dict[str, Any]]:
        """The breakdown setup itself, with no health gate applied."""
        self._ensure_indicators(df)
        if i < self.entry_window:
            return None

        close = df["close"].iat[i]
        low_min = df[self.col_low_min].iat[i]
        if pd.notna(low_min) and close < low_min:
            # Volume Confirmation: OBV must have net-declined over the entry
            # window, otherwise the breakdown lacks volume support.
            # Skipped (not required) when the data source has no volume.
            if self.use_obv and "OBV" in df.columns:
                obv_now = df["OBV"].iat[i]
                obv_prior = df["OBV"].iat[i - self.entry_window]
                if pd.isna(obv_now) or pd.isna(obv_prior) or obv_now >= obv_prior:
                    return None

            plan = self._plan_stop(
                side="short", reference_price=close,
                structural_stop=df[self.col_high_max].iat[i], df=df, i=i,
            )
            if not plan.accepted:
                return None
            breakdown = self._score_signal(
                df, i, channel_level=low_min, side="short",
            )

            return {
                "action": "short",
                "stop_loss": plan.stop_price,
                "order_type": "market",
                "price": close,
                "stop_plan": plan.to_dict(),
                "score": breakdown.total if breakdown is not None else 0.0,
                "score_components": (
                    breakdown.components if breakdown is not None else {}
                ),
            }

        return None

    def should_exit(
        self,
        symbol: str,
        i: int,
        df: pd.DataFrame,
        state: MarketState,
        portfolio: Portfolio,
    ) -> Optional[Dict[str, Any]]:
        self._ensure_indicators(df)
        self._update_protective_stop(symbol, i, df, side="short")

        close = df["close"].iat[i]
        high_max = df[self.col_high_max].iat[i]

        if pd.notna(high_max) and close > high_max:
            return {
                "action": "cover",
                "reason": f"Breakdown Exit (Above High{self.exit_window})",
            }

        if state not in self.allowed_states:
            return {"action": "cover", "reason": f"Regime {state.name} Not Allowed"}

        return None
