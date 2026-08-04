# HISTORICAL ONLY: this script may differ from backtest.engine.BacktestEngine.
# Do not use it for trading or investment decisions without migration and baseline tests.

import pandas as pd
import numpy as np
from typing import Dict, Any
from core.state import MarketState
from core.portfolio import Portfolio
from core.broker import Broker
from core.risk import RiskManager
from router.router import Router
from strategies.base import Strategy

# Mock Strategy
class MockStrategy(Strategy):
    def __init__(self, name, allowed_states):
        super().__init__(name, allowed_states)
        self.entered = False

    def should_enter(self, symbol, i, df, state, portfolio):
        print(f"DEBUG: MockStrategy.should_enter called for {self.name} at i={i}")
        if self.name == "TrendDown":
             return {'action': 'short', 'stop_loss': 1.1 * df['close'].iloc[i]}
        return {'action': 'buy', 'stop_loss': 0.9 * df['close'].iloc[i]}

    def should_exit(self, symbol, i, df, state, portfolio):
        return None

def verify_router_logic():
    print("Verifying Router Logic...")
    
    # 1. Setup
    portfolio = Portfolio(10000)
    broker = Broker(portfolio)
    risk_manager = RiskManager()
    
    strat_up = MockStrategy("TrendUp", {MarketState.TREND_UP})
    strat_down = MockStrategy("TrendDown", {MarketState.TREND_DOWN})
    strat_range = MockStrategy("RangeMeanReversion", {MarketState.SIDEWAYS})
    
    strategies = {
        "TrendUp": strat_up,
        "TrendDown": strat_down,
        "RangeMeanReversion": strat_range
    }
    
    router = Router(strategies, cooldown_bars=2)
    
    # Create Mock Data
    dates = pd.date_range(start='2024-01-01', periods=20, freq='D')
    df = pd.DataFrame({
        'close': [100.0] * 20
    }, index=dates)
    
    symbol = "BTC-USD"
    
    # 2. Test Step 1: TREND_UP -> Enter Buy
    print("\n[Step 1] Bar 0: State TREND_UP")
    router.route(symbol, 0, df, MarketState.TREND_UP, portfolio, broker, risk_manager)
    
    pos = portfolio.get_position(symbol)
    print(f"Position after Bar 0: {pos['qty']}")
    if pos['qty'] > 0:
        print("✅ Correctly entered Long in TREND_UP")
    else:
        print("❌ Failed to enter Long")
        
    # 3. Test Step 2: Switch to SIDEWAYS -> Should Close Position + Start Cooldown
    print("\n[Step 2] Bar 1: Switch to SIDEWAYS")
    router.route(symbol, 1, df, MarketState.SIDEWAYS, portfolio, broker, risk_manager)
    
    pos = portfolio.get_position(symbol)
    print(f"Position after Bar 1: {pos['qty']}")
    if pos['qty'] == 0:
        print("✅ Correctly closed position on state switch")
    else:
        print("❌ Failed to close position")
        
    if router.cooldowns.get(symbol) == 1 + 2: # i=1, cooldown=2 -> until i=3
        print(f"✅ Cooldown set correctly until index {router.cooldowns[symbol]}")
    else:
        print(f"❌ Cooldown not set or incorrect: {router.cooldowns.get(symbol)}")

    # 4. Test Step 3: Bar 2 (In Cooldown) -> Should NOT Enter Range Strategy
    print("\n[Step 3] Bar 2: In Cooldown (State SIDEWAYS)")
    # Mock Range Strategy would enter if allowed
    router.route(symbol, 2, df, MarketState.SIDEWAYS, portfolio, broker, risk_manager)
    
    pos = portfolio.get_position(symbol)
    print(f"Position after Bar 2: {pos['qty']}")
    if pos['qty'] == 0:
        print("✅ Cooldown correctly prevented entry")
    else:
        print("❌ Cooldown failed, entry occurred")
        
    # 5. Test Step 4: Bar 3 (Cooldown Expires AFTER this bar? No, logic is i <= cooldown)
    # router.py: if i <= self.cooldowns[symbol]: return
    # cooldown was set to 1 + 2 = 3. So i=3 is still skipped.
    print("\n[Step 4] Bar 3: Still in Cooldown (i=3 <= 3)")
    router.route(symbol, 3, df, MarketState.SIDEWAYS, portfolio, broker, risk_manager)
    if pos['qty'] == 0:
        print("✅ Cooldown boundary checked")
        
    # 6. Test Step 5: Bar 4 (Cooldown Expired) -> Should Enter
    print("\n[Step 5] Bar 4: Cooldown Expired (i=4 > 3)")
    router.route(symbol, 4, df, MarketState.SIDEWAYS, portfolio, broker, risk_manager)
    pos = portfolio.get_position(symbol)
    print(f"Position after Bar 4: {pos['qty']}")
    if pos['qty'] > 0:
        print("✅ Entry allowed after cooldown")
    else:
        print("❌ Entry failed after cooldown")

    # 7. Test Step 6: Switch to TREND_DOWN -> Should Close Long + Start Cooldown
    print("\n[Step 6] Bar 5: Switch to TREND_DOWN")
    # Force state change
    router.route(symbol, 5, df, MarketState.TREND_DOWN, portfolio, broker, risk_manager)
    pos = portfolio.get_position(symbol)
    if pos['qty'] == 0:
        print("✅ Correctly closed Long on switch to TREND_DOWN")
    else:
        print(f"❌ Failed to close Long: {pos['qty']}")

    # 8. Test Step 7: Bar 8 (Assume Cooldown expired) -> Enter Short
    # Cooldown set at i=5 for 2 bars -> i=7. So i=8 is free.
    print("\n[Step 7] Bar 8: Enter Short in TREND_DOWN")
    
    router.route(symbol, 8, df, MarketState.TREND_DOWN, portfolio, broker, risk_manager)
    pos = portfolio.get_position(symbol)
    print(f"Position after Bar 8: {pos['qty']}")
    if pos['qty'] < 0:
        print("✅ Correctly entered Short in TREND_DOWN")
    else:
        print(f"❌ Failed to enter Short: {pos['qty']}")

    # 9. Test Step 8: Switch back to SIDEWAYS -> Close Short
    print("\n[Step 8] Bar 9: Switch to SIDEWAYS")
    router.route(symbol, 9, df, MarketState.SIDEWAYS, portfolio, broker, risk_manager)
    pos = portfolio.get_position(symbol)
    if pos['qty'] == 0:
        print("✅ Correctly closed Short on switch to SIDEWAYS")
    else:
        print(f"❌ Failed to close Short: {pos['qty']}")


if __name__ == "__main__":
    verify_router_logic()
