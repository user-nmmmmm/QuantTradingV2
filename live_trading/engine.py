import time
import os
import json
from datetime import datetime
from typing import Callable, Dict, List, Optional

import pandas as pd

from core.portfolio import Portfolio
from core.risk import RiskManager
from core.data_fetcher import DataFetcher
from core.logger import get_logger
from core.live_broker import LiveBroker
from core.system_factory import (
    build_router,
    build_state_machine,
    market_type_supports_shorts,
)

logger = get_logger(__name__)

"""
实盘轮询引擎（LiveTradingEngine）模块

设计目标：
- 以固定间隔轮询获取最新 K 线（DataFetcher.fetch_ccxt）
- 使用 MarketStateMachine 判定 regime，并通过 Router 调用策略
- 通过 LiveBroker 与交易所同步账户/仓位，并提交订单

执行模型：
- 每次 tick 使用“最后一根已完成的 bar”（i = len(df)-1）做决策
- 订单提交后由 LiveBroker 执行（具体成交时间/价格由交易所决定）

监控输出：
- 每次 tick 结束后导出 reports/live_status.json，便于外部面板/脚本读取
"""


class LiveTradingEngine:
    def __init__(
        self,
        symbols: List[str],
        strategies: Dict,
        broker: LiveBroker,
        risk_manager: RiskManager,
        interval_seconds: int = 60,  # Check every minute
        lookback_days: int = 30,  # Data buffer
        timeframe: str = "1d",
        data_fetcher: Optional[DataFetcher] = None,
        clock: Optional[Callable[[], datetime]] = None,
        state_file: str = "reports/live_status.json",
    ):
        """
        参数：
        - symbols：交易标的列表（ccxt 格式，如 "BTC/USDT"）
        - strategies：策略实例字典（供 Router 使用）
        - broker：LiveBroker（封装 ccxt 下单与账户同步）
        - risk_manager：RiskManager（定仓与风控）
        - interval_seconds：轮询间隔（秒）
        - lookback_days：数据缓冲期（当前实现主要通过 limit 控制）
        - timeframe：K 线周期（当前 DataFetcher.fetch_ccxt 内部决定，参数保留作扩展）
        """
        self.symbols = symbols
        self.strategies = strategies
        self.broker = broker
        self.risk_manager = risk_manager
        self.interval = interval_seconds
        self.lookback_days = lookback_days
        self.timeframe = timeframe

        self.fetcher = data_fetcher or DataFetcher()
        self._clock = clock or datetime.now
        self.state_machine = build_state_machine()
        self._current_trading_day = None

        allow_short = market_type_supports_shorts(getattr(self.broker, "market_type", "spot"))
        self.router = build_router(strategies, allow_short=allow_short)
        if not allow_short:
            logger.warning(
                "Live broker market_type=%s does not support short routing; TREND_DOWN is mapped to Cash",
                getattr(self.broker, "market_type", "spot"),
            )

        # Data Buffer: symbol -> DataFrame
        self.data_map: Dict[str, pd.DataFrame] = {}

        # Ensure reports directory exists for state export
        self.state_file = state_file
        state_dir = os.path.dirname(self.state_file)
        if state_dir:
            os.makedirs(state_dir, exist_ok=True)

    def _now(self) -> datetime:
        return self._clock()

    def _reset_daily_risk_if_needed(self, current_time: datetime) -> None:
        trading_day = current_time.date()
        if trading_day != self._current_trading_day:
            self.risk_manager.reset_daily_breaker()
            self._current_trading_day = trading_day

    def initialize(self):
        """
        初始化与预热：
        - broker.sync() 同步账户/仓位
        - 为每个 symbol 拉取一段历史数据，填充 data_map，供指标与状态机使用
        """
        logger.info("Initializing Live Trading Engine...")
        self.broker.sync()
        self._reset_daily_risk_if_needed(self._now())

        # Fetch initial data
        for symbol in self.symbols:
            logger.info(f"Warming up data for {symbol}...")
            df = self.fetcher.fetch_ccxt(
                symbol,
                timeframe=self.timeframe,
                limit=max(self.lookback_days, 100),
            )
            if not df.empty:
                self.data_map[symbol] = df
                logger.info(f"Loaded {len(df)} bars for {symbol}")
            else:
                logger.warning(f"Failed to load data for {symbol}")

    def run(self):
        """
        主循环：
        - 按 interval_seconds 周期调用 _tick()
        - 捕获 KeyboardInterrupt 以优雅退出
        """
        logger.info("Starting Main Loop...")
        try:
            while True:
                self._tick()
                logger.info(f"Sleeping for {self.interval} seconds...")
                time.sleep(self.interval)
        except KeyboardInterrupt:
            logger.info("Live Trading Stopped by User")

    def _tick(self):
        """
        单次轮询迭代：
        1) 更新行情数据缓冲
        2) 同步账户/仓位
        3) 对每个 symbol：计算状态 -> 路由策略 -> 提交订单
        4) 导出当前状态 JSON
        """
        now = self._now()
        logger.info("Tick: %s", now.isoformat())
        self._reset_daily_risk_if_needed(now)

        # 1. Update Data
        self._update_data()

        # 2. Sync Portfolio
        self.broker.sync()

        # 3. Process each symbol
        for symbol in self.symbols:
            if symbol not in self.data_map:
                continue

            df = self.data_map[symbol]
            if df.empty:
                continue

            # Current Index (Last completed bar)
            # In live trading, 'i' is the last index
            i = len(df) - 1

            # Get Market State
            state = self.state_machine.get_state(df, i)
            logger.info(f"{symbol} State: {state.name}")

            # Get Current Prices for Valuation
            current_price = df["close"].iloc[-1]
            current_prices = {symbol: current_price}  # Simplification

            # Route & Execute
            # Note: Router.route expects 'i' to be the index to act on.
            # It calls strategy.on_bar(..., i, ...)
            self.router.route(
                symbol,
                i,
                df,
                state,
                self.broker.portfolio,
                self.broker,
                self.risk_manager,
                current_prices,
            )

        # 4. Export State
        self._export_state()

    def _update_data(self):
        """
        更新行情缓冲（symbol -> DataFrame）。

        当前策略：
        - 每次拉取最近 100 根（覆盖最新已完成 bar 与潜在修正）
        - concat 后按时间去重（保留最后一条），再排序
        """
        for symbol in self.symbols:
            # In a real efficient engine, we fetch only new candles.
            # Here, we re-fetch the last 100 bars to ensure we have the latest closed bar
            # and potential updates.
            new_df = self.fetcher.fetch_ccxt(
                symbol,
                timeframe=self.timeframe,
                limit=max(self.lookback_days, 100),
            )

            if new_df.empty:
                continue

            # Merge logic
            # Simplest: Just replace the tail of existing buffer or append new ones
            # We rely on timestamp index

            current_df = self.data_map.get(symbol, pd.DataFrame())

            if current_df.empty:
                self.data_map[symbol] = new_df
            else:
                # Combine and drop duplicates
                # Use combine_first or concat + drop_duplicates
                updated = pd.concat([current_df, new_df])
                updated = updated[~updated.index.duplicated(keep="last")]
                updated = updated.sort_index()
                self.data_map[symbol] = updated

    def _export_state(self):
        """
        导出引擎状态到 reports/live_status.json，便于外部监控：
        - timestamp、cash、equity、positions、symbols
        """
        try:
            # Calculate Equity
            # We need current prices for all symbols in portfolio
            # We can use the latest close from self.data_map
            current_prices = {}
            for s, df in self.data_map.items():
                if not df.empty:
                    current_prices[s] = df["close"].iloc[-1]

            equity = self.broker.portfolio.get_equity(current_prices)

            state_data = {
                "timestamp": self._now().strftime("%Y-%m-%d %H:%M:%S"),
                "cash": self.broker.portfolio.cash,
                "equity": equity,
                "positions": self.broker.portfolio.positions,
                "symbols": self.symbols,
                "last_update": self._now().isoformat(),
            }

            with open(self.state_file, "w") as f:
                json.dump(state_data, f, indent=2)

        except Exception as e:
            logger.error(f"Failed to export state: {e}")
