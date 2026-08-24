from typing import Dict, Any, Optional
import pandas as pd
import numpy as np
from core.state import MarketState
from core.portfolio import Portfolio
from core.indicators import Indicators
from core.factors import MomentumFactors
from strategies.base import Strategy

"""
震荡均值回归策略（Range Mean Reversion）模块

策略目标：
- 在 SIDEWAYS（震荡）市场状态下做均值回归交易
- 以布林带上下轨作为极端价格区域，回归到中轨止盈
- 以 ATR/价格比例过滤高波动场景，避免在趋势/剧烈波动中“抄底摸顶”

风控与状态：
- 入场时给出 stop_loss（±1*ATR）供 RiskManager 做风险定仓
- 额外维护 trade_state：连续亏损计数与冷却期（连亏达到阈值后暂停一段 bars）

说明：
- 本策略覆盖 on_bar，用于在平仓后估算一次交易盈亏，从而更新 trade_state
"""

class RangeStrategy(Strategy):
    def __init__(self, atr_threshold_pct: float = 0.03, rsi_oversold: float = 30.0, rsi_overbought: float = 70.0):
        super().__init__("RangeMeanReversion", {MarketState.SIDEWAYS})
        """
        参数：
        - atr_threshold_pct：ATR/Price 上限，超过则认为波动过大不交易（默认 3%）
        - rsi_oversold/rsi_overbought：RSI 确认阈值，触布林下轨做多需 RSI < rsi_oversold，
          触上轨做空需 RSI > rsi_overbought，减少布林带单指标的假信号
        """
        self.atr_threshold_pct = atr_threshold_pct
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        
        # Extended Context: Track consecutive losses and cooldown
        # symbol -> { 'consecutive_losses': int, 'cooldown_until': int (index) }
        # symbol -> { 'consecutive_losses': int, 'cooldown_until': int (index) }
        self.trade_state: Dict[str, Dict[str, Any]] = {}

    def get_trade_state(self, symbol: str) -> Dict[str, Any]:
        """
        获取某标的的交易状态（连续亏损与冷却计数）。

        字段：
        - consecutive_losses：连续亏损次数
        - cooldown_until：冷却截止的 bar index（i <= cooldown_until 时跳过信号）
        """
        if symbol not in self.trade_state:
            self.trade_state[symbol] = {'consecutive_losses': 0, 'cooldown_until': -1}
        return self.trade_state[symbol]

    def _ensure_indicators(self, df: pd.DataFrame):
        """
        确保本策略所需指标列存在：
        - BB_UPPER/BB_MIDDLE/BB_LOWER：布林带（20, 2.0）
        - ATR_14：ATR（14）
        """
        if 'BB_UPPER' not in df.columns:
            df['BB_UPPER'], df['BB_MIDDLE'], df['BB_LOWER'] = Indicators.BBANDS(df['close'], 20, 2.0)
        if 'ATR_14' not in df.columns:
            df['ATR_14'] = Indicators.ATR(df, 14)
        if 'RSI_14' not in df.columns:
            df['RSI_14'] = MomentumFactors.RSI(df['close'], 14)

    def should_enter(self, symbol: str, i: int, df: pd.DataFrame, state: MarketState, portfolio: Portfolio) -> Optional[Dict[str, Any]]:
        """
        入场逻辑（bar i 收盘产生信号，i+1 执行）：
        1) 冷却期内不交易
        2) 过滤：ATR/Price 过大不交易
        3) Low 触碰下轨 -> 做多；High 触碰上轨 -> 做空
        4) 止损：±1*ATR（由 RiskManager 用于风险定仓）
        """
        self._ensure_indicators(df)
        ts = self.get_trade_state(symbol)
        
        # Check Cooldown
        if i <= ts['cooldown_until']:
            return None
            
        if i < 1: return None
        if pd.isna(df['BB_UPPER'].iat[i]) or pd.isna(df['ATR_14'].iat[i]): return None
        
        close = df['close'].iat[i]
        bb_upper = df['BB_UPPER'].iat[i]
        bb_lower = df['BB_LOWER'].iat[i]
        atr = df['ATR_14'].iat[i]
        
        # Filter: ATR/Price too high
        if (atr / close) > self.atr_threshold_pct:
            return None
            
        # Entry Logic
        # Touch Lower Band -> Long
        # We check if low <= lower band? Or close <= lower band?
        # "触碰下轨" usually means Low <= Lower.
        # But for signal stability, maybe close <= lower or close crossed lower?
        # Let's use Low <= Lower for "Touch".
        # But wait, if we use Low, we might have touched it intra-bar.
        # If we are making decision at Close of bar i for NEXT bar execution or THIS bar execution?
        # Assuming we run at Close of bar i.
        # If Low[i] <= Lower[i], we signal Buy.
        
        low = df['low'].iat[i]
        high = df['high'].iat[i]
        rsi = df['RSI_14'].iat[i]
        if pd.isna(rsi):
            return None

        entry_signal = None

        # RSI Confirmation: require oversold/overbought alongside the band touch
        # to filter false signals from the Bollinger Band alone.
        if low <= bb_lower and rsi < self.rsi_oversold:
            entry_signal = {'action': 'buy', 'stop_loss': close - 1 * atr}
        elif high >= bb_upper and rsi > self.rsi_overbought:
            entry_signal = {'action': 'short', 'stop_loss': close + 1 * atr}

        return entry_signal

    def should_exit(self, symbol: str, i: int, df: pd.DataFrame, state: MarketState, portfolio: Portfolio) -> Optional[Dict[str, Any]]:
        """
        出场逻辑（bar i 收盘产生信号，i+1 执行）：
        1) 回归到中轨止盈：多头 close >= 中轨；空头 close <= 中轨
        2) 止损触发：多头 bar_low < stop_loss；空头 bar_high > stop_loss
        """
        self._ensure_indicators(df)
        ctx = self.get_context(symbol)
        
        close = df['close'].iat[i]
        bb_mid = df['BB_MIDDLE'].iat[i]
        
        # Exit Conditions
        # 1. Return to Mid Band
        # If Long: Close >= Mid
        # If Short: Close <= Mid
        
        pos = portfolio.get_position(symbol)
        qty = pos['qty']
        
        reason = None
        if qty > 0 and close >= bb_mid:
            reason = 'Target hit (Mid Band)'
        elif qty < 0 and close <= bb_mid:
            reason = 'Target hit (Mid Band)'
            
        # 2. Stop Loss — use bar LOW/HIGH to detect intrabar breaches
        stop_loss = ctx.get('stop_loss')
        if stop_loss is not None:
            bar_low = df['low'].iat[i]
            bar_high = df['high'].iat[i]
            if qty > 0 and bar_low < stop_loss:
                reason = 'Stop Loss'
            elif qty < 0 and bar_high > stop_loss:
                reason = 'Stop Loss'
                
        if reason:
            action = 'sell' if qty > 0 else 'cover'
            return {'action': action, 'reason': reason}
            
        return None

    def on_trade_closed(
        self,
        symbol: str,
        realized_pnl: float,
        trade: Dict[str, Any],
        bar_index: int,
    ) -> None:
        del trade
        state = self.get_trade_state(symbol)
        if realized_pnl < 0:
            state["consecutive_losses"] += 1
            if state["consecutive_losses"] >= 3:
                state["cooldown_until"] = bar_index + 24
                state["consecutive_losses"] = 0
        else:
            state["consecutive_losses"] = 0
