from abc import ABC, abstractmethod
from typing import Set, Dict, Any, Optional
import pandas as pd
import numpy as np
from core.state import MarketState
from core.portfolio import Portfolio
from core.execution_port import ExecutionPort
from core.risk import RiskManager

"""
Strategy（策略基类）模块

本模块定义策略插件的统一接口与“标准执行流程”（on_bar），用于在回测与实盘引擎中复用同一套调用方式。

核心接口：
- should_enter：在第 i 根 bar 收盘时判断是否产生入场信号
- should_exit：在第 i 根 bar 收盘时判断是否产生出场信号
- on_bar：通用编排（先出场、再入场），并与 Broker/RiskManager/Portfolio 协作完成下单与风控

重要约定（时间与执行）：
- 策略在 bar i 依据 df.iloc[:i]（含 i）产生信号，并通过 broker.submit_order(...) 提交订单
- Broker 在下一根 bar（i+1）开盘/触发条件下撮合成交（回测模型：Next-Bar Execution）
- 为避免“刚成交就立即又在同一根 bar 触发止损/出场”的抖动，on_bar 对新开仓做了 1 bar 的 exit 冷却

上下文（context）：
- 每个策略按 symbol 维护一个 context（字典），用于保存止损价、入场价、追踪止损等跨 bar 状态
"""


class Strategy(ABC):
    def __init__(self, name: str, allowed_states: Set[MarketState]):
        """
        参数：
        - name：策略名（用于报告归因、交易日志 strategy_id）
        - allowed_states：允许生效的市场状态集合（由 Router 决定是否调用策略）
        """
        self.name = name
        self.allowed_states = allowed_states

        # State tracking for the strategy per symbol
        # symbol -> { 'entry_price': float, 'stop_loss': float, 'trailing_stop': float }
        self.context: Dict[str, Dict[str, Any]] = {}

    def get_context(self, symbol: str) -> Dict[str, Any]:
        """
        获取某标的的策略上下文（不存在则自动创建）。

        context 典型字段：
        - entry_price：入场参考价（通常为 signal bar 的 close）
        - stop_loss：初始止损价
        - trailing_stop：追踪止损价
        - entry_bar：入场信号所在的 bar index（用于 exit 冷却）
        """
        if symbol not in self.context:
            self.context[symbol] = {}
        return self.context[symbol]

    @abstractmethod
    def should_enter(
        self,
        symbol: str,
        i: int,
        df: pd.DataFrame,
        state: MarketState,
        portfolio: Portfolio,
    ) -> Optional[Dict[str, Any]]:
        """
        判断是否入场（在 bar i 收盘时刻做决策）。

        返回：
        - None：不入场
        - dict：入场信号，至少包含：
          - action：'buy'（做多）或 'short'（做空）
        可选字段：
        - stop_loss：止损价（用于 RiskManager 按风险定仓）
        - order_type：'market'/'limit'/'stop'（默认 market）
        - price：限价/止损单价格（默认 close）
        """
        pass

    @abstractmethod
    def should_exit(
        self,
        symbol: str,
        i: int,
        df: pd.DataFrame,
        state: MarketState,
        portfolio: Portfolio,
    ) -> Optional[Dict[str, Any]]:
        """
        判断是否出场（在 bar i 收盘时刻做决策）。

        返回：
        - None：不出场
        - dict：出场信号，至少包含：
          - action：'sell'（平多）或 'cover'（平空）
        可选字段：
        - reason：出场原因（用于报告与调试，如 'stop'/'takeprofit'/'State changed'）
        - order_type/price：与入场类似
        """
        pass

    def on_bar(
        self,
        symbol: str,
        i: int,
        df: pd.DataFrame,
        state: MarketState,
        portfolio: Portfolio,
        broker: ExecutionPort,
        risk_manager: RiskManager,
        current_prices: Optional[Dict[str, float]] = None,
    ):
        """
        标准执行流程（每根 bar 调用一次）。

        执行顺序：
        1) 若当前持仓非 0：优先检查 should_exit 并提交平仓订单
        2) 若当前无持仓：在 allowed_states 内检查 should_enter，并通过 RiskManager 计算仓位后提交开仓订单

        关键细节：
        - exit 冷却：为避免“下根 bar 开盘成交后，同一根 bar 又触发 exit”的不合理抖动，
          新入场的仓位会跳过 1 次 exit 检查（just_entered）
        - 定仓逻辑：
          - 若 entry_signal 提供 stop_loss：按 risk_per_trade 做风险定仓
          - 否则：退化为按固定资金占比定仓（默认 10%）
        """
        current_pos = portfolio.get_position(symbol)
        qty = current_pos["qty"]

        # 1. Check Exit if we have a position
        # Skip exit check on the bar immediately after entry to avoid same-bar entry-exit churn:
        # Entry order is submitted at bar N, fills at bar N+1 open, then on_bar runs at bar N+1.
        # We must not check exit at bar N+1 for a freshly opened position.
        ctx_pre = self.get_context(symbol)
        just_entered = i <= ctx_pre.get("entry_bar", -2) + 1

        if qty != 0 and not just_entered:
            exit_signal = self.should_exit(symbol, i, df, state, portfolio)
            if exit_signal:
                action = exit_signal["action"]  # 'sell' or 'cover'
                reason = exit_signal.get("reason", "signal")

                # Execute Exit
                # Calculate qty to close (all)
                close_qty = abs(qty)
                current_price = df["close"].iat[i]

                # Extract optional order parameters
                order_type = exit_signal.get("order_type", "market")
                order_price = exit_signal.get("price", current_price)

                # If action matches position direction (sell for long, cover for short)
                if (qty > 0 and action == "sell") or (qty < 0 and action == "cover"):
                    timestamp = df.index[i]
                    submission = broker.submit_order(
                        symbol,
                        action,
                        close_qty,
                        price=order_price,
                        order_type=order_type,
                        timestamp=timestamp,
                        strategy_id=self.name,
                        exit_reason=reason,
                    )

                    if submission.accepted:
                        self.context[symbol] = {}

        # 2. Check Entry if we don't have a position (or if strategy allows pyramiding, but let's assume 1 pos for now)
        if qty == 0:
            if state in self.allowed_states:
                entry_signal = self.should_enter(symbol, i, df, state, portfolio)
                if entry_signal:
                    action = entry_signal["action"]  # 'buy' or 'short'
                    stop_loss = entry_signal.get("stop_loss", 0.0)
                    current_price = df["close"].iat[i]

                    # Extract optional order parameters
                    order_type = entry_signal.get("order_type", "market")
                    order_price = entry_signal.get("price", current_price)

                    # Calculate Position Size
                    # Use current_prices if available, else fallback to just this symbol
                    price_map = (
                        current_prices if current_prices else {symbol: current_price}
                    )
                    equity = portfolio.get_equity(price_map)

                    if stop_loss > 0:
                        size = risk_manager.calculate_position_size(
                            equity, current_price, stop_loss
                        )
                    else:
                        # Fallback: Use Fixed Percentage (e.g. 10% of Equity)
                        size = risk_manager.calculate_position_size_fixed_pct(
                            equity, current_price, pct=0.10
                        )

                    if size > 0:
                        current_volume = float(df["volume"].iat[i]) if "volume" in df.columns else 0.0
                        pending_provider = getattr(broker, "pending_open_notional", None)
                        pending_open_notional = (
                            pending_provider(price_map)
                            if callable(pending_provider) else {}
                        )
                        if not isinstance(pending_open_notional, dict):
                            pending_open_notional = {}
                        # Pre-trade Risk Check
                        if risk_manager.check_entry_risk(
                            portfolio,
                            symbol,
                            size,
                            current_price,
                            current_volume=current_volume,
                            current_prices=price_map,
                            pending_open_notional=pending_open_notional,
                        ):
                            submission = broker.submit_order(
                                symbol,
                                action,
                                size,
                                price=order_price,
                                order_type=order_type,
                                timestamp=df.index[i],
                                strategy_id=self.name,
                                # stop_loss is not passed to submit_order currently in Broker signature?
                                # Let's check Broker signature. It accepts strategy_id, exit_reason.
                                # It does NOT accept stop_loss.
                                # But we can store it in context.
                                exit_reason="signal",
                            )
                            if not submission.accepted:
                                return submission

                            # Initialize Context only after explicit acceptance
                            self.context[symbol] = {
                                "stop_loss": stop_loss,
                                "entry_price": current_price,  # Approx
                                "trailing_stop": -np.inf
                                if action == "buy"
                                else np.inf,  # Init trail
                                "entry_bar": i,  # Track bar to prevent same-bar exit
                            }
