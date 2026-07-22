from typing import Dict, Any, Optional, List
import pandas as pd
import numpy as np
from core.state import MarketState
from core.portfolio import Portfolio
from core.indicators import Indicators
from core.broker import Broker
from core.risk import RiskManager
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
    def __init__(self, atr_threshold_pct: float = 0.03):
        super().__init__("RangeMeanReversion", {MarketState.SIDEWAYS})
        """
        参数：
        - atr_threshold_pct：ATR/Price 上限，超过则认为波动过大不交易（默认 3%）
        """
        self.atr_threshold_pct = atr_threshold_pct
        
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
        if pd.isna(df['BB_UPPER'].iloc[i]) or pd.isna(df['ATR_14'].iloc[i]): return None
        
        close = df['close'].iloc[i]
        bb_upper = df['BB_UPPER'].iloc[i]
        bb_lower = df['BB_LOWER'].iloc[i]
        atr = df['ATR_14'].iloc[i]
        
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
        
        low = df['low'].iloc[i]
        high = df['high'].iloc[i]
        
        entry_signal = None
        
        if low <= bb_lower:
            entry_signal = {'action': 'buy', 'stop_loss': close - 1 * atr}
        elif high >= bb_upper:
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
        
        close = df['close'].iloc[i]
        bb_mid = df['BB_MIDDLE'].iloc[i]
        
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
            bar_low = df['low'].iloc[i]
            bar_high = df['high'].iloc[i]
            if qty > 0 and bar_low < stop_loss:
                reason = 'Stop Loss'
            elif qty < 0 and bar_high > stop_loss:
                reason = 'Stop Loss'
                
        if reason:
            action = 'sell' if qty > 0 else 'cover'
            return {'action': action, 'reason': reason}
            
        return None
        # Or I can return None and let Base implementation check Stop Loss?
        # My Base implementation:
        # 1. Check Exit (should_exit)
        # 2. (Not implemented in Base yet: Auto Stop Loss Check inside on_bar?)
        # Let's check Base implementation again.
        
        # Base implementation in previous turn:
        # It calls `should_exit`. If returns signal, execute.
        # IT DOES NOT have built-in stop loss check outside of `should_exit`?
        # Wait, let me check `strategies/base.py`.
        
        # Checking strategies/base.py...
        # It calls `should_exit`.
        # Inside `TrendStrategies`, I implemented "Stop/Trail triggered" check INSIDE `should_exit`.
        # So I must do the same here.
        
        stop_loss = ctx.get('stop_loss', np.inf if qty < 0 else -np.inf)
        
        if qty > 0 and close < stop_loss:
            reason = 'Stop Loss hit'
        elif qty < 0 and close > stop_loss:
            reason = 'Stop Loss hit'
            
        if reason:
            action = 'sell' if qty > 0 else 'cover'
            return {'action': action, 'reason': reason}
            
        return None

    def on_bar(self, symbol: str, i: int, df: pd.DataFrame, state: MarketState, portfolio: Portfolio, broker: Broker, risk_manager: RiskManager, current_prices: Optional[Dict[str, float]] = None):
        """
        扩展标准 on_bar：
        - 先执行基类 on_bar（负责出入场与下单）
        - 若检测到本 bar 结束后仓位从非 0 变为 0，则估算该次交易盈亏并更新 trade_state

        注意：
        - 这里的盈亏是基于 Portfolio.avg_price 与 df['close'] 的近似估算，用于“连亏冷却”逻辑，
          并不等同于 Broker 的精确成交价记录（trades.csv 中的 fill_price/commission 更准确）。
        """
        # Override on_bar to handle PnL tracking for Circuit Breaker
        # We need to intercept the Exit Execution to calculate PnL.
        
        current_pos = portfolio.get_position(symbol)
        qty_before = current_pos['qty']
        
        # Call Base on_bar logic
        # But Base on_bar executes the order. We need to know if it did.
        # And Base on_bar doesn't return anything.
        
        # Alternative: We can check portfolio before and after.
        # If qty changed from !=0 to 0, we closed a position.
        
        super().on_bar(symbol, i, df, state, portfolio, broker, risk_manager, current_prices)
        
        qty_after = portfolio.get_position(symbol)['qty']
        
        if qty_before != 0 and qty_after == 0:
            # Position Closed. Calculate PnL.
            # We need Entry Price.
            # Base Strategy updates context. But clears it on Exit.
            # Wait, Base Strategy:
            # if result['status'] == 'filled': self.context[symbol] = {}
            # So we lost the entry price?
            # Yes, Base Strategy clears context.
            
            # Solution: Store entry price in a local var before calling super, or rely on Portfolio avg_price.
            # Portfolio avg_price is reliable for PnL calculation of the closed position.
            
            entry_price = current_pos['avg_price']
            exit_price = df['close'].iloc[i] # Approximation. Real exec price is in Broker.
            # But Broker executed at current_price (close).
            
            pnl = 0
            if qty_before > 0: # Long
                pnl = (exit_price - entry_price) * abs(qty_before)
            else: # Short
                pnl = (entry_price - exit_price) * abs(qty_before)
                
            ts = self.get_trade_state(symbol)
            if pnl < 0:
                ts['consecutive_losses'] += 1
                if ts['consecutive_losses'] >= 3:
                    ts['cooldown_until'] = i + 24
                    ts['consecutive_losses'] = 0 # Reset or keep? "连亏 3 次 → 冷却". After cooldown, reset? Usually yes.
            else:
                ts['consecutive_losses'] = 0
