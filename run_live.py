import argparse
import os
import sys

# Add project root
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from core.portfolio import Portfolio
from core.logger import configure_logging, get_logger
from core.live_broker import LiveBroker
from core.system_factory import build_risk_manager, build_strategy_registry
from live_trading.engine import LiveTradingEngine

configure_logging()
logger = get_logger(__name__)

"""
实盘入口脚本（run_live.py）

用途：
- 初始化 Portfolio/RiskManager/LiveBroker/LiveTradingEngine
- 启动轮询式实盘交易循环（LiveTradingEngine.run）

注意：
- api_key/secret 可通过命令行传入，也可通过环境变量由 LiveBroker/ccxt 读取（视实现）
- sandbox 仅在交易所支持测试网时生效
"""

def main():
    """
    解析参数并启动实盘引擎。

    默认策略集：
    - TrendUp / TrendDown：趋势策略（参数可在此处调整）
    - RangeMeanReversion：震荡均值回归策略
    """
    parser = argparse.ArgumentParser(description="QuantTrading Live Engine")
    parser.add_argument("--symbols", nargs="+", default=["BTC/USDT", "ETH/USDT"], help="Symbols to trade")
    parser.add_argument("--interval", type=int, default=60, help="Loop interval in seconds")
    parser.add_argument("--sandbox", action="store_true", help="Use Exchange Sandbox/Testnet")
    parser.add_argument("--exchange", type=str, default="binance", help="Exchange ID (ccxt)")
    parser.add_argument("--market-type", type=str, default="spot", choices=["spot", "future", "futures", "swap", "margin"], help="Exchange market type to sync and trade against")
    parser.add_argument("--api_key", type=str, help="API Key (optional, can use env vars)")
    parser.add_argument("--secret", type=str, help="API Secret (optional, can use env vars)")
    
    args = parser.parse_args()
    
    # 1. Setup Core Components
    portfolio = Portfolio() # Initial capital will be synced from exchange
    
    risk_manager = build_risk_manager()
    
    # 2. Setup Broker
    broker = LiveBroker(
        portfolio=portfolio,
        exchange_id=args.exchange,
        api_key=args.api_key,
        secret=args.secret,
        sandbox=args.sandbox,
        market_type=args.market_type,
    )
    
    # 3. Setup Strategies
    strategies = build_strategy_registry()
    
    # 4. Initialize Engine
    engine = LiveTradingEngine(
        symbols=args.symbols,
        strategies=strategies,
        broker=broker,
        risk_manager=risk_manager,
        interval_seconds=args.interval
    )
    
    # 5. Run
    engine.initialize()
    engine.run()

if __name__ == "__main__":
    main()
