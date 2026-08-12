from typing import Dict, Any, Optional
import pandas as pd
import numpy as np
from core.state import MarketState
from core.portfolio import Portfolio
from core.indicators import Indicators
from strategies.base import Strategy

"""
趋势跟踪策略（Trend Following）模块

包含两套对称策略：
- TrendUpStrategy：仅在 TREND_UP 状态下做多
- TrendDownStrategy：仅在 TREND_DOWN 状态下做空

共同特点：
- 通过 SMA 判断趋势方向与“回踩/反抽”位置
- 通过 ATR 计算止损与追踪止损（适配加密市场的波动特性）
- 采用基类 Strategy 的 on_bar 编排：下一根 bar 执行成交（由 Broker 完成）
"""


class TrendUpStrategy(Strategy):
    def __init__(
        self,
        sma_period: int = 30,
        sma_fast: int = 10,
        atr_period: int = 14,
        atr_multiplier: float = 2.5,  # Issue4 fix: 2.0 → 2.5, wider stop for crypto volatility
    ):
        """
        多头趋势策略（回踩买入 + ATR 止损/追踪）。

        参数：
        - sma_period：趋势均线周期（默认 30）
        - sma_fast：快均线周期（默认 10），用于多头结构确认（快 > 慢）
        - atr_period：ATR 周期（默认 14）
        - atr_multiplier：ATR 止损倍数（默认 2.5）
        """
        super().__init__("TrendUp", {MarketState.TREND_UP})
        self.sma_period = sma_period
        self.sma_fast = sma_fast
        self.atr_period = atr_period
        self.atr_multiplier = atr_multiplier

        # Column names
        self.col_sma = f"SMA_{self.sma_period}"
        self.col_sma_fast = f"SMA_{self.sma_fast}"
        self.col_atr = f"ATR_{self.atr_period}"

    def _ensure_indicators(self, df: pd.DataFrame):
        """
        确保本策略所需指标列存在（按需计算，避免重复计算开销）。
        """
        if self.col_sma not in df.columns:
            df[self.col_sma] = Indicators.SMA(df["close"], self.sma_period)
        if self.col_sma_fast not in df.columns:
            df[self.col_sma_fast] = Indicators.SMA(df["close"], self.sma_fast)
        if self.col_atr not in df.columns:
            df[self.col_atr] = Indicators.ATR(df, self.atr_period)

    def should_enter(
        self,
        symbol: str,
        i: int,
        df: pd.DataFrame,
        state: MarketState,
        portfolio: Portfolio,
    ) -> Optional[Dict[str, Any]]:
        """
        入场逻辑（在 bar i 收盘产生信号，i+1 执行）：
        1) 回踩慢均线附近（close <= SMA * 1.02）
        2) 慢均线斜率为正（趋势向上）
        3) 快均线在慢均线上方（结构确认）
        4) 当前收盘价较上一根收盘回升（“反弹确认”，避免追在下跌末端）
        """
        self._ensure_indicators(df)

        if i < 1:
            return None

        if pd.isna(df[self.col_sma].iat[i]) or pd.isna(df[self.col_atr].iat[i]):
            return None

        close = df["close"].iat[i]
        close_prev = df["close"].iat[i - 1]
        sma = df[self.col_sma].iat[i]
        sma_prev = df[self.col_sma].iat[i - 1]

        # Conditions
        # 1. Close pull back to SMA (Issue3 fix: ≤2% above SMA, was ≤0.5% — wider zone)
        cond_pullback = close <= sma * 1.02

        # 2. SMA slope > 0
        slope = sma - sma_prev
        cond_slope = slope > 0

        # 3. SMA_Fast > SMA (Optional)
        sma_fast_val = df[self.col_sma_fast].iat[i]
        cond_alignment = sma_fast_val > sma

        # 4. Issue3 fix: Bounce confirmation — current close is already recovering,
        #    not still falling into the SMA. Prevents entering at trend ends.
        cond_bounce = close > close_prev

        if cond_pullback and cond_slope and cond_alignment and cond_bounce:
            atr = df[self.col_atr].iat[i]
            stop_loss = close - self.atr_multiplier * atr

            return {
                "action": "buy",
                "stop_loss": stop_loss,
                "order_type": "limit",
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
        """
        出场逻辑（在 bar i 收盘产生信号，i+1 执行）：
        1) 价格跌破 SMA - 0.5*ATR：趋势走坏/噪声过滤后的破位
        2) 市场状态切换：路由层会做强制互斥，这里也做一次防守性检查
        3) 止损/追踪止损触发：用 bar 的 LOW 判断是否盘中触发（比 close 更贴近真实触发）

        追踪止损更新：
        - trail = close - atr_multiplier * ATR，仅向上抬升，不下调
        """
        self._ensure_indicators(df)
        ctx = self.get_context(symbol)

        close = df["close"].iat[i]
        sma = df[self.col_sma].iat[i]
        atr = df[self.col_atr].iat[i]

        # 1. close < SMA − 0.5×ATR  (Issue4 fix: ATR buffer prevents getting swept by noise,
        #    was plain close < SMA which fired too easily in high-volatility crypto)
        if close < sma - 0.5 * atr:
            return {"action": "sell", "reason": f"Close below SMA{self.sma_period}-ATR"}

        # 2. state != TREND_UP
        if state not in self.allowed_states:
            return {"action": "sell", "reason": "State changed"}

        # 3. Stop/Trail triggered — use bar LOW (not close) to detect intrabar stop breach
        stop_loss = ctx.get("stop_loss", -np.inf)
        trailing_stop = ctx.get("trailing_stop", -np.inf)
        effective_stop = max(stop_loss, trailing_stop)

        bar_low = df["low"].iat[i]
        if bar_low < effective_stop:
            return {"action": "sell", "reason": "Stop/Trail hit"}

        # Update Trailing Stop
        new_trail_candidate = close - self.atr_multiplier * atr
        if new_trail_candidate > trailing_stop:
            ctx["trailing_stop"] = new_trail_candidate

        return None


class TrendDownStrategy(Strategy):
    def __init__(self, sma_period: int = 30, atr_period: int = 14, atr_multiplier: float = 2.5):  # Issue4 fix: 2.0 → 2.5
        """
        空头趋势策略（反抽卖出 + ATR 止损/追踪）。

        参数：
        - sma_period：趋势均线周期（默认 30）
        - atr_period：ATR 周期（默认 14）
        - atr_multiplier：ATR 止损倍数（默认 2.5）
        """
        super().__init__("TrendDown", {MarketState.TREND_DOWN})
        self.sma_period = sma_period
        self.atr_period = atr_period
        self.atr_multiplier = atr_multiplier
        
        self.col_sma = f"SMA_{self.sma_period}"
        self.col_atr = f"ATR_{self.atr_period}"

    def _ensure_indicators(self, df: pd.DataFrame):
        """
        确保本策略所需指标列存在（按需计算）。
        """
        if self.col_sma not in df.columns:
            df[self.col_sma] = Indicators.SMA(df["close"], self.sma_period)
        if self.col_atr not in df.columns:
            df[self.col_atr] = Indicators.ATR(df, self.atr_period)

    def should_enter(
        self,
        symbol: str,
        i: int,
        df: pd.DataFrame,
        state: MarketState,
        portfolio: Portfolio,
    ) -> Optional[Dict[str, Any]]:
        """
        入场逻辑（做空）：
        1) 价格反抽到 SMA 附近（0.99*SMA <= close <= SMA）
        2) SMA 斜率为负（趋势向下）
        """
        self._ensure_indicators(df)

        if i < 1:
            return None

        if pd.isna(df[self.col_sma].iat[i]) or pd.isna(df[self.col_atr].iat[i]):
            return None

        close = df["close"].iat[i]
        close_prev = df["close"].iat[i - 1]
        sma = df[self.col_sma].iat[i]
        sma_prev = df[self.col_sma].iat[i - 1]

        # Conditions
        # 1. Close has rallied to the SMA area (±3% band).
        #    Widened from the original 1% band (0.99-1.00) which almost never triggered
        #    in daily crypto given typical 2-5% intraday moves.
        cond_rally = (close >= sma * 0.97) and (close <= sma * 1.03)

        # 2. SMA slope < 0 (downtrend structure)
        slope = sma - sma_prev
        cond_slope = slope < 0

        # 3. Price is rolling over (bar is red) — confirms rejection at SMA resistance
        #    Avoids entering short while momentum is still carrying price up through SMA.
        cond_rollover = close <= close_prev

        if cond_rally and cond_slope and cond_rollover:
            atr = df[self.col_atr].iat[i]
            stop_loss = close + self.atr_multiplier * atr

            return {
                "action": "short",
                "stop_loss": stop_loss,
                "order_type": "limit",
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
        """
        出场逻辑（平空）：
        1) close > SMA * 1.005：短线反弹超过阈值
        2) 市场状态切换
        3) 止损/追踪止损触发：用 bar HIGH 判断盘中触发

        追踪止损更新：
        - trail = close + atr_multiplier * ATR，仅向下移动（更贴近价格），不反向放宽
        """
        self._ensure_indicators(df)
        ctx = self.get_context(symbol)

        close = df["close"].iat[i]
        sma = df[self.col_sma].iat[i]
        atr = df[self.col_atr].iat[i]

        # 1. Close breaks above SMA + 0.5×ATR — price has bounced enough to invalidate the short.
        #    The original 0.5% fixed threshold (sma * 1.005) is far too tight for daily crypto
        #    where ATR is typically 3-6% of price, causing nearly instant premature covers.
        if close > sma + 0.5 * atr:
            return {"action": "cover", "reason": f"Close above SMA{self.sma_period}"}

        # 2. state != TREND_DOWN
        if state not in self.allowed_states:
            return {"action": "cover", "reason": "State changed"}

        # 3. Stop/Trail triggered — use bar HIGH (not close) to detect intrabar stop breach
        stop_loss = ctx.get("stop_loss", np.inf)
        trailing_stop = ctx.get("trailing_stop", np.inf)
        effective_stop = min(stop_loss, trailing_stop)

        bar_high = df["high"].iat[i]
        if bar_high > effective_stop:
            return {"action": "cover", "reason": "Stop/Trail hit"}

        # Update Trailing Stop for Short
        new_trail_candidate = close + self.atr_multiplier * atr

        if new_trail_candidate < trailing_stop:
            ctx["trailing_stop"] = new_trail_candidate

        return None
