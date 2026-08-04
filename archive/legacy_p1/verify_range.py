# HISTORICAL ONLY: this script may differ from backtest.engine.BacktestEngine.
# Do not use it for trading or investment decisions without migration and baseline tests.


import pandas as pd
import numpy as np
from core.state import MarketState
from core.portfolio import Portfolio
from core.broker import Broker
from core.risk import RiskManager
from strategies.mean_reversion import RangeStrategy
from core.indicators import Indicators

def verify_range_strategy():
    print("Verifying RangeStrategy Logic...")
    
    # Setup
    portfolio = Portfolio(10000)
    broker = Broker(portfolio)
    risk_manager = RiskManager()
    strategy = RangeStrategy()
    
    # Create Mock Data
    # We need enough bars for BB (20) and ATR (14)
    dates = pd.date_range(start='2024-01-01', periods=100, freq='D')
    
    # Generate oscillating price (Sine wave)
    x = np.linspace(0, 8*np.pi, 100)
    base_price = 100
    prices = base_price + 5 * np.sin(x)
    
    df = pd.DataFrame({
        'close': prices,
        'high': prices + 1,
        'low': prices - 1,
        'open': prices
    }, index=dates)
    
    # Calculate Indicators manually to check logic match
    df['BB_UPPER'], df['BB_MIDDLE'], df['BB_LOWER'] = Indicators.BBANDS(df['close'], 20, 2.0)
    df['ATR_14'] = Indicators.ATR(df, 14)
    
    symbol = "TEST-USD"
    state = MarketState.SIDEWAYS
    
    # Test 1: Entry Long
    # Find a point where Low <= Lower Band
    # We generated Sine wave. 
    # Let's force a dip at index 30
    df.iloc[30, df.columns.get_loc('low')] = df['BB_LOWER'].iloc[30] - 1.0
    df.iloc[30, df.columns.get_loc('close')] = df['BB_LOWER'].iloc[30] + 0.5 # Close inside
    
    print(f"\n[Test 1] Bar 30: Force Low <= BB_LOWER")
    strategy.on_bar(symbol, 30, df, state, portfolio, broker, risk_manager)
    
    pos = portfolio.get_position(symbol)
    if pos['qty'] > 0:
        print(f"✅ Entered Long: Qty={pos['qty']:.4f}, Price={pos['avg_price']:.4f}")
    else:
        print("❌ Failed to Enter Long")
        
    # Test 2: Exit at Mid Band
    # Find next point where Close >= Mid Band
    # Index 35?
    mid_35 = df['BB_MIDDLE'].iloc[35]
    df.iloc[35, df.columns.get_loc('close')] = mid_35 + 1.0
    
    print(f"\n[Test 2] Bar 35: Force Close >= BB_MIDDLE")
    strategy.on_bar(symbol, 35, df, state, portfolio, broker, risk_manager)
    
    pos = portfolio.get_position(symbol)
    if pos['qty'] == 0:
        print("✅ Exited Long at Mid Band")
    else:
        print(f"❌ Failed to Exit. Qty={pos['qty']}")
        
    # Test 3: Circuit Breaker (3 Consecutive Losses)
    print(f"\n[Test 3] Trigger 3 Consecutive Losses")
    
    # Reset State for clean test
    strategy.trade_state[symbol] = {'consecutive_losses': 0, 'cooldown_until': -1}
    
    for k in range(3):
        idx = 40 + k*2
        # Force Entry
        df.iloc[idx, df.columns.get_loc('low')] = df['BB_LOWER'].iloc[idx] - 1.0
        df.iloc[idx, df.columns.get_loc('close')] = df['BB_LOWER'].iloc[idx] + 0.5
        
        strategy.on_bar(symbol, idx, df, state, portfolio, broker, risk_manager)
        if portfolio.get_position(symbol)['qty'] == 0:
            print(f"❌ Iter {k}: Failed to enter")
            continue
            
        # Force Stop Loss (Loss)
        # Stop is close - 1*ATR.
        # We need next bar to be BELOW stop loss.
        next_idx = idx + 1
        stop_price = strategy.context[symbol]['stop_loss']
        df.iloc[next_idx, df.columns.get_loc('close')] = stop_price - 1.0
        
        strategy.on_bar(symbol, next_idx, df, state, portfolio, broker, risk_manager)
        
        ts = strategy.get_trade_state(symbol)
        print(f"Iter {k}: Losses={ts['consecutive_losses']}, Cooldown={ts['cooldown_until']}")
        
    ts = strategy.get_trade_state(symbol)
    if ts['consecutive_losses'] == 0 and ts['cooldown_until'] > 0:
        print("✅ Circuit Breaker Activated (Losses reset, Cooldown set)")
    else:
        print(f"❌ Circuit Breaker Failed. State: {ts}")
        
    # Test 4: Cooldown Logic
    # Try to enter immediately after
    next_idx = 50
    df.iloc[next_idx, df.columns.get_loc('low')] = df['BB_LOWER'].iloc[next_idx] - 1.0
    
    print(f"\n[Test 4] Attempt Entry during Cooldown (Index {next_idx} <= {ts['cooldown_until']})")
    strategy.on_bar(symbol, next_idx, df, state, portfolio, broker, risk_manager)
    
    if portfolio.get_position(symbol)['qty'] == 0:
        print("✅ Entry Blocked by Cooldown")
    else:
        print("❌ Entry Allowed during Cooldown")

if __name__ == "__main__":
    verify_range_strategy()
