# HISTORICAL ONLY: this script may differ from backtest.engine.BacktestEngine.
# Do not use it for trading or investment decisions without migration and baseline tests.

import pandas as pd
import numpy as np
from core.portfolio import Portfolio
from core.broker import Broker
from core.risk import RiskManager
from core.state import MarketState
from strategies.trend_following import TrendUpStrategy, TrendDownStrategy

def run_test():
    print("=== Testing TrendUpStrategy ===")
    test_trend_up()
    print("\n=== Testing TrendDownStrategy ===")
    test_trend_down()

def test_trend_up():
    print("Initializing components...")
    portfolio = Portfolio(initial_capital=10000.0)
    broker = Broker(portfolio)
    risk_manager = RiskManager(risk_per_trade=0.01) # 1% risk
    strategy = TrendUpStrategy()
    
    # 1. Generate Data
    print("Generating data...")
    dates = pd.date_range(start='2023-01-01', periods=100, freq='D')
    df = pd.DataFrame(index=dates)
    
    # Create close prices
    prices = [100.0] * 30
    for i in range(20): # 30-49
        prices.append(100.0 + (i+1) * 1.0) # 101...120
    
    prices.append(108.0) # 50
    prices.append(107.5) # 51
    prices.append(108.0) # 52
    
    for i in range(10): # 53-62
        prices.append(108.0 + (i+1) * 2.0) # 110...128
        
    prices.append(100.0) # Crash
    
    while len(prices) < 100:
        prices.append(100.0)
        
    df['close'] = pd.Series(prices, index=dates[:len(prices)])
    df['high'] = df['close'] + 2.0
    df['low'] = df['close'] - 2.0
    df['open'] = df['close']
    df['volume'] = 1000
    df.columns.name = 'BTC/USDT'
    symbol = 'BTC/USDT'
    
    state = MarketState.BULL_TREND
    
    for i in range(len(df)):
        strategy.on_bar(symbol, i, df, state, portfolio, broker, risk_manager)
        pos = portfolio.get_position(symbol)
        if pos['qty'] != 0:
            print(f"Bar {i}: Pos {pos['qty']:.4f} @ {pos['avg_price']:.2f}, Price {df['close'].iloc[i]:.2f}, Equity {portfolio.get_equity({symbol: df['close'].iloc[i]}):.2f}")
        
    print("Final Equity:", portfolio.get_equity({symbol: df['close'].iloc[-1]}))

def test_trend_down():
    print("Initializing components...")
    portfolio = Portfolio(initial_capital=10000.0)
    broker = Broker(portfolio)
    risk_manager = RiskManager(risk_per_trade=0.01)
    strategy = TrendDownStrategy()
    
    # Generate Downtrend Data
    dates = pd.date_range(start='2023-01-01', periods=100, freq='D')
    df = pd.DataFrame(index=dates)
    
    # 0-29: 200
    # 30-50: Downtrend 200 -> 180
    # 51-53: Rally to SMA30 (approx 193)
    # 54-62: Drop to 160
    # 63: Spike to 200
    
    prices = [200.0] * 30
    for i in range(20): 
        prices.append(200.0 - (i+1) * 1.0) # 199...180
        
    # SMA30 at 50 is approx 193
    prices.append(192.0) # 50
    prices.append(192.5) # 51 (Trigger Short)
    prices.append(192.0) # 52
    
    for i in range(10):
        prices.append(192.0 - (i+1) * 3.0) # 189...162
        
    prices.append(200.0) # Spike
    
    while len(prices) < 100:
        prices.append(200.0)
        
    df['close'] = pd.Series(prices, index=dates[:len(prices)])
    df['high'] = df['close'] + 2.0
    df['low'] = df['close'] - 2.0
    df['open'] = df['close']
    df['volume'] = 1000
    df.columns.name = 'BTC/USDT'
    symbol = 'BTC/USDT'
    
    state = MarketState.BEAR_TREND
    
    for i in range(len(df)):
        strategy.on_bar(symbol, i, df, state, portfolio, broker, risk_manager)
        pos = portfolio.get_position(symbol)
        if pos['qty'] != 0:
            print(f"Bar {i}: Pos {pos['qty']:.4f} @ {pos['avg_price']:.2f}, Price {df['close'].iloc[i]:.2f}, Equity {portfolio.get_equity({symbol: df['close'].iloc[i]}):.2f}")
            
    print("Final Equity:", portfolio.get_equity({symbol: df['close'].iloc[-1]}))

if __name__ == "__main__":
    run_test()
