import os
from typing import Dict, List, Any, Optional

import numpy as np
import pandas as pd

from core.indicators import Indicators
from core.portfolio import Portfolio
from core.broker import Broker
from core.logger import get_logger
from core.system_factory import (
    build_risk_manager,
    build_router,
    build_state_machine,
    build_strategy_registry,
)
from config.config import config

logger = get_logger(__name__)

"""
回测引擎（BacktestEngine）模块

功能：
- 对多标的 OHLCV 数据进行时间对齐与指标预计算
- 运行“Next-Bar Execution”回测循环：
  - 在 bar i：策略基于 i 的数据产生信号并提交订单
  - 在 bar i+1：Broker.process_orders(...) 按下一根 bar 的价格撮合成交（近似）
- 集成风控与状态机：
  - MarketStateMachine：根据行情判定 regime（TREND_UP/TREND_DOWN/SIDEWAYS/VOLATILE）
  - Router：按 regime 选择策略，并处理状态切换互斥/冷却
  - RiskManager：定仓、杠杆约束、回撤/日内熔断

输出：
- trades：来自 Broker 的成交记录（用于报告）
- equity_curve：权益曲线（cash + 持仓市值）
- benchmark：等权买入并持有基准（可选）
"""


class BacktestEngine:
    def __init__(
        self,
        initial_capital: float = 10000.0,
        slippage: Optional[float] = None,
        random_slip: bool = False,
        warmup_period: int = 30,
    ):
        """
        参数：
        - initial_capital：初始资金（默认 10000）
        - slippage：滑点比例或最大滑点（取决于 random_slip）
        - random_slip：是否启用随机滑点（均匀分布 [0, slippage]）
        - warmup_period：指标预热 bars（前 N 根 bar 只记录权益不交易）
        """
        self.initial_capital = initial_capital
        # If config is present, use it to override or supplement
        # But command line args usually take precedence if passed explicitly?
        # Here we trust the caller passed the right overrides.

        # Load params from config
        self.config_execution = config.get("execution") or {}
        self.config_risk = config.get("risk") or {}

        if slippage is None:
            self.slippage = self.config_execution.get("slippage_bps", 0.0) / 10000.0
        else:
            self.slippage = slippage
        self.random_slip = random_slip
        self.warmup_period = warmup_period

    @staticmethod
    def _prepare_dataframe(df: pd.DataFrame) -> pd.DataFrame:
        """
        规范化 DataFrame 的时间索引并去重：
        - index 强制转换为 DatetimeIndex
        - 若带时区，统一转为 UTC 后去 tz（回测内部使用 tz-naive）
        - 删除重复时间戳（保留最后一条），并按时间排序
        """
        if df is None or df.empty:
            return pd.DataFrame()

        normalized = df.copy()
        normalized.index = pd.to_datetime(normalized.index, errors="coerce")
        valid_mask = ~pd.isna(normalized.index)
        normalized = normalized.loc[valid_mask].copy()

        if normalized.empty:
            return normalized

        idx = normalized.index
        if idx.tz is not None:
            idx = idx.tz_convert("UTC").tz_localize(None)
        normalized.index = idx

        normalized = normalized[~normalized.index.duplicated(keep="last")]
        normalized = normalized.sort_index()
        return normalized

    @staticmethod
    def _looks_daily_or_slower(indices: List[pd.DatetimeIndex]) -> bool:
        """
        启发式判断数据是否为日线/更慢周期：
        - 若中位数 bar 间隔小于 23 小时，则认为是日内数据（不做按日期对齐）
        - 否则允许在“无法精确时间戳交集”时，退化为按日历日期对齐
        """
        for idx in indices:
            if len(idx) < 2:
                continue

            diffs = idx.to_series().diff().dropna()
            if diffs.empty:
                continue

            median_gap_seconds = diffs.dt.total_seconds().median()
            if pd.notna(median_gap_seconds) and median_gap_seconds < 23 * 3600:
                return False

        return True

    def run(
        self,
        data_map: Dict[str, pd.DataFrame],
        strategies: Optional[Dict[str, Any]] = None,
        routing_log_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        运行多标的回测。

        参数：
        - data_map：symbol -> OHLCV DataFrame（索引为时间戳）
        - strategies：策略实例字典；None 则使用默认策略集
        - routing_log_path：路由日志输出路径；None 则写入 ./reports/routing_log.csv

        关键步骤：
        1) 初始化 Portfolio/Broker/RiskManager/MarketStateMachine/Router
        2) 规范化数据、对齐时间轴、预计算指标（Indicators.calculate_all）
        3) 主循环：
           - 撮合：Broker.process_orders（执行上一根 bar 提交的订单）
           - 估值：portfolio.get_total_value(current_prices)
           - 风控：日内熔断触发则提交平仓订单并停止开仓
           - 路由：state_machine.get_state -> router.route -> strategy.on_bar
        4) 保存路由日志并计算基准曲线
        """
        # 1. Setup Core Components
        portfolio = Portfolio(self.initial_capital)

        # Setup Broker with Config
        broker = Broker(
            portfolio,
            slippage=self.slippage,
            random_slip=self.random_slip,
            commission_rate=self.config_execution.get("commission_rate_taker", 0.001),
            commission_rate_maker=self.config_execution.get(
                "commission_rate_maker", 0.0005
            ),
            use_impact_cost=self.config_execution.get("use_impact_cost", False),
            max_participation_rate=self.config_execution.get(
                "max_participation_rate", self.config_risk.get("liquidity_limit_pct", 0.01)
            ),
        )

        # Setup Risk Manager with Config
        risk_manager = build_risk_manager()

        # Issue2 fix: stability_period=2 → 1 bar faster state-change detection (was 3)
        state_machine = build_state_machine()

        # 2. Setup Strategies & Router
        if strategies is None:
            strategies = build_strategy_registry()

        # Setup Router logging path
        if routing_log_path is None:
            log_dir = os.path.join(os.getcwd(), "reports")
            if not os.path.exists(log_dir):
                os.makedirs(log_dir)
            routing_log_path = os.path.join(log_dir, "routing_log.csv")
        else:
            # Ensure directory exists
            log_dir = os.path.dirname(routing_log_path)
            if log_dir and not os.path.exists(log_dir):
                os.makedirs(log_dir)

        router = build_router(strategies, log_path=routing_log_path)

        # 3. Prepare Data
        # Get intersection of indices to sync time axis
        if not data_map:
            return {}

        normalized_data_map: Dict[str, pd.DataFrame] = {}
        for symbol, df in data_map.items():
            normalized_df = self._prepare_dataframe(df)
            if normalized_df.empty:
                logger.warning(
                    "Skipping %s: empty or invalid dataframe after normalization",
                    symbol,
                )
                continue
            normalized_data_map[symbol] = normalized_df

        if not normalized_data_map:
            logger.warning("No valid symbol data available after normalization")
            return {"trades": [], "equity_curve": pd.DataFrame()}

        # Use the union of real bar events. Missing bars are never synthesized.
        event_index = pd.DatetimeIndex([])
        processed_data: Dict[str, pd.DataFrame] = {}
        for symbol, df in normalized_data_map.items():
            enriched = df.copy()
            Indicators.calculate_all(enriched)
            processed_data[symbol] = enriched
            event_index = event_index.union(enriched.index)
        common_index = event_index.sort_values()
        if len(common_index) == 0:
            return {"trades": [], "equity_curve": pd.DataFrame()}
        # 4. Main Loop
        equity_curve = []
        timestamps = common_index

        # Track Daily Start Equity for Circuit Breaker
        daily_start_equity = self.initial_capital
        current_day = None

        logger.info("Starting backtest on %s bars", len(timestamps))

        # Skip first N bars to allow indicators to warm up (SMA30, ATR14, etc.)
        start_idx = self.warmup_period

        last_prices: Dict[str, float] = {}
        for i, current_time in enumerate(timestamps):
            this_day = current_time.date()
            if current_day != this_day:
                if equity_curve:
                    daily_start_equity = equity_curve[-1]["equity"]
                current_day = this_day
                risk_manager.reset_daily_breaker()

            current_bars_data = {
                symbol: df.loc[current_time]
                for symbol, df in processed_data.items()
                if current_time in df.index
            }
            broker.process_orders(current_bars_data)

            for symbol, bar in current_bars_data.items():
                close = float(bar["close"])
                if pd.notna(close):
                    last_prices[symbol] = close

            total_value = portfolio.get_total_value(last_prices)
            if risk_manager.check_circuit_breaker(total_value, daily_start_equity):
                for symbol, pos in list(portfolio.positions.items()):
                    qty = pos["qty"]
                    if qty == 0 or symbol not in current_bars_data:
                        continue
                    side = "sell" if qty > 0 else "cover"
                    broker.submit_order(
                        symbol, side, abs(qty), last_prices[symbol],
                        timestamp=current_time, strategy_id="CircuitBreaker",
                        exit_reason="MaxLoss",
                    )
            else:
                for symbol, bar in current_bars_data.items():
                    df = processed_data[symbol]
                    local_i = df.index.get_loc(current_time)
                    if not isinstance(local_i, int) or local_i < self.warmup_period:
                        continue
                    state = state_machine.get_state(df, local_i)
                    router.route(
                        symbol, local_i, df, state, portfolio, broker,
                        risk_manager, dict(last_prices),
                    )

            total_value = portfolio.get_total_value(last_prices)
            equity_curve.append({
                "timestamp": current_time,
                "equity": total_value,
                "cash": portfolio.cash,
            })
        logger.info("Backtest completed")

        # Save Routing Log
        router.save_log()

        # 5. Calculate Benchmark (Equal Weight Buy & Hold)
        benchmark_series = None
        if processed_data and len(timestamps) > 0:
            try:
                # Extract close prices
                closes = pd.DataFrame(
                    {sym: df["close"] for sym, df in processed_data.items()}
                )
                # Calculate returns
                returns = closes.pct_change(fill_method=None).fillna(0)
                # Equal weight returns
                portfolio_returns = returns.mean(axis=1)
                # Cumulative return index (start at 1.0)
                benchmark_idx = (1 + portfolio_returns).cumprod()
                # Normalize to initial capital
                # We want it to start matching the equity curve at start_idx (or 0)
                # But equity curve stays flat until start_idx.
                # Let's normalize so that at start_idx, Benchmark = Initial Capital

                if start_idx < len(benchmark_idx):
                    base_val = benchmark_idx.iloc[start_idx]
                    if base_val != 0:
                        benchmark_series = (
                            benchmark_idx / base_val
                        ) * self.initial_capital
                        # Set values before start_idx to initial_capital (cash)
                        benchmark_series.iloc[:start_idx] = self.initial_capital
                else:
                    benchmark_series = benchmark_idx * self.initial_capital  # Fallback

            except Exception as e:
                logger.warning("Failed to calculate benchmark: %s", e)

        return {
            "trades": broker.trades,
            "equity_curve": pd.DataFrame(equity_curve).set_index("timestamp"),
            "benchmark": benchmark_series,
        }
