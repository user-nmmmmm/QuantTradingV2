from typing import Dict, Any, Optional
import pandas as pd
import numpy as np
from core.state import MarketState
from core.portfolio import Portfolio
from core.factors import VolumeFactors
from strategies.base import Strategy

"""
趋势突破策略（Trend Breakout）模块

该策略实现 Donchian Channel 风格的突破系统：
- 入场：收盘价突破过去 N 根 bar 的最高价（前高）
- 出场：收盘价跌破过去 M 根 bar 的最低价（退出通道）

与路由/风控的集成：
- allowed_states 允许 TREND_UP 与 VOLATILE（波动扩张时更容易出现突破）
- 入场信号提供 stop_loss（使用 Donchian Exit Level），供 RiskManager 做风险定仓
- 内置“健康度监控”（P5）：连续亏损与滚动收益为负时熄火（停止出信号）

成交量确认（OBV）：
- 突破入场额外要求 OBV 在 entry_window 窗口内同向累积（多头 OBV 上升 / 空头 OBV 下降），
  过滤缩量假突破（价格突破但背后无资金推动）
"""


class _PersistentHealthMixin:
    @staticmethod
    def _new_health_stats() -> Dict[str, Any]:
        return {
            "scope": "cross_symbol_aggregate",
            "total_trades": 0,
            "consecutive_losses": 0,
            "rolling_pnl": [],
            "is_alive": True,
            "death_reason": None,
        }

    def _initialize_health_state(self) -> None:
        self._health_state_store = None
        self.health_stats = self._new_health_stats()

    def bind_state_store(self, state_store) -> None:
        self._health_state_store = state_store
        loaded = state_store.get(f"strategy_health:{self.name}")
        if isinstance(loaded, dict):
            restored = self._new_health_stats()
            restored.update(loaded)
            restored["rolling_pnl"] = list(restored.get("rolling_pnl") or [])
            self.health_stats = restored

    def _persist_health(self) -> None:
        if self._health_state_store is not None:
            self._health_state_store.set(
                f"strategy_health:{self.name}", self.health_stats
            )

    def reset_runtime_state(self) -> None:
        super().reset_runtime_state()
        self.health_stats = self._new_health_stats()

    def on_trade_closed(
        self, symbol: str, realized_pnl: float, trade: Dict[str, Any],
        bar_index: int,
    ) -> None:
        del symbol, trade, bar_index
        self.health_stats["total_trades"] += 1
        self.health_stats["rolling_pnl"].append(float(realized_pnl))
        if realized_pnl < 0:
            self.health_stats["consecutive_losses"] += 1
        else:
            self.health_stats["consecutive_losses"] = 0
        self._persist_health()


class TrendBreakoutStrategy(_PersistentHealthMixin, Strategy):
    """
    P3: Production Implementation of Trend Breakout Alpha.

    P0 Regime Definition:
    - This alpha earns in VOLATILE expansion (ADX > 25) and strong TREND_UP regimes.

    P5 Failure Criteria (Alpha Death):
    - Rolling Sharpe (20 trades) < 0.0
    - Consecutive Losses > 5
    - Max Drawdown > 15% (Strategy Level)

    Logic:
    - Enter Long if Close > Max(High, 20)
    - Exit Long if Close < Min(Low, 10)
    - Allowed Regimes: TREND_UP, VOLATILE
    """

    def __init__(
        self, entry_window: int = 20, exit_window: int = 10, *, use_obv: bool = True,
    ):
        """
        参数：
        - entry_window：突破窗口（过去 N 根的最高价）
        - exit_window：退出窗口（过去 M 根的最低价）
        """
        # We allow VOLATILE because breakout often happens during volatility expansion
        super().__init__("TrendBreakout", {MarketState.TREND_UP, MarketState.VOLATILE})
        self.entry_window = entry_window
        self.exit_window = exit_window
        self.use_obv = use_obv

        self.col_high_max = f"HIGH_MAX_{self.entry_window}"
        self.col_low_min = f"LOW_MIN_{self.exit_window}"

        # P5: Health Monitoring
        self._initialize_health_state()

    def check_health(self) -> bool:
        """
        P5：策略健康度闸门（Alpha Death）。

        返回：
        - True：策略仍可交易
        - False：策略被禁用（is_alive=False），后续不再产生信号

        当前实现的判定：
        - 连续亏损 > 5：直接熄火
        - 最近 20 笔收益均值 < 0：认为 alpha 失效，熄火
        """
        if not self.health_stats["is_alive"]:
            return False

        # 1. Consecutive Losses
        if self.health_stats["consecutive_losses"] > 5:
            self.health_stats["is_alive"] = False
            self.health_stats["death_reason"] = "Consecutive Losses > 5"
            self._persist_health()
            return False

        # 2. Rolling Sharpe (Simplified check on last 20 PnL entries)
        pnl_history = self.health_stats["rolling_pnl"]
        if len(pnl_history) >= 20:
            recent_pnl = pnl_history[-20:]
            if np.mean(recent_pnl) < 0:  # Simple mean return check first
                # Calculate Sharpe if needed, but mean < 0 is enough to warn
                self.health_stats["is_alive"] = False
                self.health_stats["death_reason"] = (
                    "Rolling Mean Return < 0 (20 trades)"
                )
                self._persist_health()
                return False

        return True

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
        - 先通过 check_health()；若 alpha 熄火则不交易
        - close > HIGH_MAX_entry_window 视为有效突破，返回 buy 信号
        - OBV 需在 entry_window 窗口内净上升，确认突破有成交量支撑
        - 初始止损使用 Donchian Exit Level（LOW_MIN_exit_window）
        """
        # P5: Gate on health check before generating any signal
        if not self.check_health():
            return None

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

            # Risk Management Integration (P3 Requirement)
            # We provide a stop_loss for the RiskManager to size the position.
            # For breakout, maybe Low of breakout candle or recent low?
            # Let's use the Exit Level (Donchian Low) as the initial stop.
            stop_loss = df[self.col_low_min].iat[i]
            if pd.isna(stop_loss) or stop_loss >= close:
                # Fallback if stop is invalid (e.g. too close): use ATR-based or %?
                # Let's assume RiskManager handles sizing if we provide 'stop_loss'.
                # But if Donchian Low is higher than Close (impossible if breakout), check logic.
                stop_loss = close * 0.95  # Fallback 5%

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


class TrendBreakdownStrategy(_PersistentHealthMixin, Strategy):
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

    def check_health(self) -> bool:
        if not self.health_stats["is_alive"]:
            return False

        if self.health_stats["consecutive_losses"] > 5:
            self.health_stats["is_alive"] = False
            self.health_stats["death_reason"] = "Consecutive Losses > 5"
            self._persist_health()
            return False

        pnl_history = self.health_stats["rolling_pnl"]
        if len(pnl_history) >= 20 and np.mean(pnl_history[-20:]) < 0:
            self.health_stats["is_alive"] = False
            self.health_stats["death_reason"] = "Rolling Mean Return < 0 (20 trades)"
            self._persist_health()
            return False

        return True

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

    def should_enter(
        self,
        symbol: str,
        i: int,
        df: pd.DataFrame,
        state: MarketState,
        portfolio: Portfolio,
    ) -> Optional[Dict[str, Any]]:
        if not self.check_health():
            return None

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

            stop_loss = df[self.col_high_max].iat[i]
            if pd.isna(stop_loss) or stop_loss <= close:
                stop_loss = close * 1.05

            return {
                "action": "short",
                "stop_loss": stop_loss,
                "order_type": "market",
                "price": close,
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
