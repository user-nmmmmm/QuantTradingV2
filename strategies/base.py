from abc import ABC, abstractmethod
from dataclasses import replace
from typing import Set, Dict, Any, Optional
import pandas as pd
import numpy as np
from core.state import MarketState
from core.portfolio import Portfolio
from core.execution_port import ExecutionPort
from core.events import Signal
from core.risk import RiskManager
from core.phase4 import EntryCandidate

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
        # How many round trips this strategy actually observed closing. Compared
        # against the reconstructed closed-trade count by
        # core.diagnostics.calculate_lifecycle_coverage: a shortfall means the
        # safeguards that update from on_trade_closed (health/alpha-death gates,
        # consecutive-loss cooldowns) are running blind.
        self.observed_close_events = 0
        # CloseEvent ids already delivered to on_trade_closed (T-1.5): guards
        # against double-counting if the same event is ever observed twice.
        self._consumed_close_event_ids: Set[str] = set()

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

    def reset_runtime_state(self) -> None:
        self.context = {}
        self.observed_close_events = 0
        self._consumed_close_event_ids = set()

    def bind_state_store(self, _state_store) -> None:
        """Optional live-state binding for strategies with durable health state."""

    def hard_stop_exit(
        self, symbol: str, i: int, df: pd.DataFrame, portfolio: Portfolio,
    ) -> Optional[Dict[str, Any]]:
        """Apply one shared intrabar hard-stop rule before strategy-specific exits."""
        qty = float(portfolio.get_position(symbol).get("qty", 0.0))
        stop = self.get_context(symbol).get("stop_loss")
        if not qty or stop in (None, 0):
            return None
        stop = float(stop)
        low = float(df["low"].iat[i]) if "low" in df else float(df["close"].iat[i])
        high = float(df["high"].iat[i]) if "high" in df else float(df["close"].iat[i])
        if qty > 0 and low <= stop:
            return {"action": "sell", "reason": "hard_stop", "order_type": "market"}
        if qty < 0 and high >= stop:
            return {"action": "cover", "reason": "hard_stop", "order_type": "market"}
        return None

    def on_trade_closed(
        self, symbol: str, realized_pnl: float, trade: Dict[str, Any],
        bar_index: int,
    ) -> None:
        """Hook invoked only after authoritative closing fills make a position flat."""

    def health_risk_multiplier(self) -> float:
        """Risk scaling demanded by the strategy's health lifecycle (SR1-3).

        1.0 for a strategy without a health machine; ``PROBATION`` returns a
        fraction so a recovering strategy re-enters with reduced size, and the
        same number reaches sizing, the reservation and the report.
        """
        return 1.0

    def health_snapshot(self) -> Dict[str, Any]:
        """Report-facing lifecycle state; empty when health is not modelled."""
        return {}

    def _consume_execution_trades(
        self, symbol: str, bar_index: int,
        portfolio: Portfolio, broker: ExecutionPort,
    ) -> None:
        """Deliver every CloseEvent this strategy opened, exactly once (T-1.3/T-1.4/T-1.5).

        Close events are authoritative: they are built once, at fill time, by
        Broker._execute_trade from the lot ledger (core.lots), regardless of
        which of the exit paths (self-exit, hard stop, Router state-switch,
        circuit breaker, EndOfBacktest) triggered the closing fill. Filtering
        by ``opening_strategy_id`` (captured on the lot when it was opened,
        not on whichever order/strategy closed it) is what makes external
        closes (Router/CircuitBreaker/EndOfBacktest) still get attributed to
        the strategy that actually opened the position. The per-event id
        dedupe guards idempotency if this is ever called more than once for
        the same event.
        """
        close_events = getattr(broker, "close_events", None) or []
        for event in close_events:
            if event.opening_strategy_id != self.name:
                continue
            if event.close_event_id in self._consumed_close_event_ids:
                continue
            self._consumed_close_event_ids.add(event.close_event_id)
            self.observed_close_events += 1
            trade = {
                "symbol": event.symbol,
                "close_event_id": event.close_event_id,
                "lot_id": event.lot_id,
                "position_id": event.position_id,
                "qty": event.qty,
                "fill_price": event.exit_price,
                "theoretical_price": event.theoretical_exit_price,
                "exit_reason": event.exit_reason,
                "strategy_id": event.opening_strategy_id,
                # SR1-2 cohort key inputs: when the exit happened, how much
                # risk it retired, and which portfolio risk action forced it.
                "timestamp": event.timestamp,
                "initial_risk": event.initial_risk,
                "risk_action_id": event.risk_action_id,
            }
            self.on_trade_closed(event.symbol, event.realized_pnl, trade, bar_index)
            if event.is_position_fully_closed:
                self.context[event.symbol] = {}

        ctx = self.get_context(symbol)
        if ctx.get("entry_pending") and not portfolio.get_position(symbol).get("qty", 0.0):
            has_active = getattr(broker, "has_active_open_order", None)
            if callable(has_active) and has_active(symbol) is False:
                self.context[symbol] = {}
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
        self._consume_execution_trades(symbol, i, portfolio, broker)
        current_pos = portfolio.get_position(symbol)
        qty = current_pos["qty"]

        # 1. Check Exit if we have a position
        # Skip exit check on the bar immediately after entry to avoid same-bar entry-exit churn:
        # Entry order is submitted at bar N, fills at bar N+1 open, then on_bar runs at bar N+1.
        # We must not check exit at bar N+1 for a freshly opened position.
        ctx_pre = self.get_context(symbol)
        just_entered = i <= ctx_pre.get("entry_bar", -2) + 1

        if qty != 0 and not ctx_pre.get("exit_pending"):
            exit_signal = self.hard_stop_exit(symbol, i, df, portfolio)
            if exit_signal is None and not just_entered:
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
                    self._publish_signal(
                        broker,
                        symbol=symbol,
                        timestamp=timestamp,
                        action=action,
                        signal_kind="exit",
                        price=order_price,
                        reason=reason,
                    )
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
                        self.context[symbol]["exit_pending"] = True

        # 2. Check Entry if we don't have a position (or if strategy allows pyramiding, but let's assume 1 pos for now)
        if qty == 0 and not self.get_context(symbol).get("entry_pending"):
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
                    # SR1-3: PROBATION re-enters at reduced size. Applied here,
                    # before the caps and the entry gate, so the clamp, the
                    # reservation and the order all see the same number.
                    size *= self.health_risk_multiplier()

                    if size > 0:
                        # Liquidity is enforced by the execution venue against the
                        # actual fill bar. The signal bar is only an estimate and
                        # must not drive a second, inconsistent volume limit.
                        current_volume = 0.0
                        pending_provider = getattr(broker, "pending_open_notional", None)
                        pending_open_notional = (
                            pending_provider(price_map)
                            if callable(pending_provider) else {}
                        )
                        if not isinstance(pending_open_notional, dict):
                            pending_open_notional = {}
                        # Clamp to the risk caps rather than dropping the trade.
                        # Risk-based sizing makes notional inversely proportional
                        # to stop distance, so a tight stop can exceed the
                        # concentration cap and would otherwise be rejected
                        # outright — silencing the lowest-risk signals.
                        clamp = getattr(risk_manager, "clamp_entry_qty", None)
                        if callable(clamp):
                            size = clamp(
                                portfolio,
                                symbol,
                                size,
                                current_price,
                                current_volume=current_volume,
                                current_prices=price_map,
                                pending_open_notional=pending_open_notional,
                                action=action,
                            )
                    if size > 0:
                        # Pre-trade Risk Check (final gate; clamp already applied)
                        if risk_manager.check_entry_risk(
                            portfolio,
                            symbol,
                            size,
                            current_price,
                            current_volume=current_volume,
                            current_prices=price_map,
                            pending_open_notional=pending_open_notional,
                            action=action,
                        ):
                            self._publish_signal(
                                broker,
                                symbol=symbol,
                                timestamp=df.index[i],
                                action=action,
                                signal_kind="entry",
                                price=order_price,
                                reason="signal",
                            )
                            submission = broker.submit_order(
                                symbol,
                                action,
                                size,
                                price=order_price,
                                order_type=order_type,
                                timestamp=df.index[i],
                                strategy_id=self.name,
                                exit_reason="signal",
                                # Persisted onto the opened lot as initial_risk
                                # = |entry - stop| * qty (T-1.9).
                                stop_loss=stop_loss,
                            )
                            if not submission.accepted:
                                return submission

                            # Initialize Context only after explicit acceptance
                            self.context[symbol] = {
                                "entry_pending": True,
                                "stop_loss": stop_loss,
                                "entry_price": current_price,  # Approx
                                "trailing_stop": -np.inf
                                if action == "buy"
                                else np.inf,  # Init trail
                                "entry_bar": i,  # Track bar to prevent same-bar exit
                            }
    def process_exit_only(
        self, symbol: str, i: int, df: pd.DataFrame, state: MarketState,
        portfolio: Portfolio, broker: ExecutionPort,
    ) -> Optional[Any]:
        """Evaluate an existing position without considering a new entry."""

        self._consume_execution_trades(symbol, i, portfolio, broker)
        qty = portfolio.get_position(symbol)["qty"]
        ctx = self.get_context(symbol)
        just_entered = i <= ctx.get("entry_bar", -2) + 1
        if qty == 0 or ctx.get("exit_pending"):
            return None
        signal = self.hard_stop_exit(symbol, i, df, portfolio)
        if signal is None and not just_entered:
            signal = self.should_exit(symbol, i, df, state, portfolio)
        if not signal:
            return None
        action = signal["action"]
        if not ((qty > 0 and action == "sell") or (qty < 0 and action == "cover")):
            return None
        price = float(signal.get("price", df["close"].iat[i]))
        reason = str(signal.get("reason", "signal"))
        self._publish_signal(
            broker, symbol=symbol, timestamp=df.index[i], action=action,
            signal_kind="exit", price=price, reason=reason,
        )
        result = broker.submit_order(
            symbol, action, abs(qty), price=price,
            order_type=signal.get("order_type", "market"), timestamp=df.index[i],
            strategy_id=self.name, exit_reason=reason,
        )
        if result.accepted:
            ctx["exit_pending"] = True
        return result

    def build_entry_candidate(
        self, symbol: str, i: int, df: pd.DataFrame, state: MarketState,
        portfolio: Portfolio,
    ) -> Optional[EntryCandidate]:
        """Create an entry proposal without spending portfolio risk budget."""

        if portfolio.get_position(symbol)["qty"] != 0:
            return None
        if self.get_context(symbol).get("entry_pending") or state not in self.allowed_states:
            return None
        signal = self.should_enter(symbol, i, df, state, portfolio)
        if not signal:
            return None
        score = float(signal.get("score", signal.get("priority", 0.0)))
        return EntryCandidate(symbol, self, i, df, state, dict(signal), score)

    def submit_entry_candidate(
        self, candidate: EntryCandidate, *, portfolio: Portfolio,
        broker: ExecutionPort, risk_manager: RiskManager,
        current_prices: Dict[str, float],
        risk_governor: Optional[Any] = None,
    ) -> Optional[Any]:
        """Size and submit a proposal after portfolio-level ranking.

        ``risk_governor`` (SR3-2) meters the *correlated* risk budget: the
        planned initial risk of this entry is checked against what one session
        and one correlation cluster may put at stake, and the size is scaled
        down (or the entry dropped) before any reservation is made.
        """

        symbol, i, df = candidate.symbol, candidate.bar_index, candidate.frame
        signal = candidate.signal
        action = str(signal["action"])
        current_price = float(df["close"].iat[i])
        order_price = float(signal.get("price", current_price))
        stop_loss = float(signal.get("stop_loss", 0.0) or 0.0)
        equity = portfolio.get_equity(current_prices)
        if stop_loss > 0:
            size = risk_manager.calculate_position_size(equity, current_price, stop_loss)
        else:
            size = risk_manager.calculate_position_size_fixed_pct(equity, current_price, pct=0.10)
        size *= self.health_risk_multiplier()  # SR1-3 probation scaling
        pending_provider = getattr(broker, "pending_open_notional", None)
        pending = pending_provider(current_prices) if callable(pending_provider) else {}
        clamp = getattr(risk_manager, "clamp_entry_qty", None)
        if callable(clamp):
            size = clamp(
                portfolio, symbol, size, current_price, current_volume=0.0,
                current_prices=current_prices, pending_open_notional=pending,
                action=action,
            )
        budget_decision = None
        if risk_governor is not None and size > 0 and stop_loss > 0:
            planned_risk = size * abs(current_price - stop_loss)
            budget_decision = risk_governor.evaluate(
                symbol=symbol, planned_risk=planned_risk,
                equity=equity, portfolio=portfolio,
            )
            size *= budget_decision.scale
        if size <= 0 or not risk_manager.check_entry_risk(
            portfolio, symbol, size, current_price, current_volume=0.0,
            current_prices=current_prices, pending_open_notional=pending,
            action=action,
        ):
            if risk_governor is not None and budget_decision is not None:
                risk_governor.commit(
                    replace(budget_decision, allowed=False, allowed_risk=0.0),
                    symbol=symbol,
                )
            return None
        self._publish_signal(
            broker, symbol=symbol, timestamp=df.index[i], action=action,
            signal_kind="entry", price=order_price, reason="portfolio_allocation",
        )
        result = broker.submit_order(
            symbol, action, size, price=order_price,
            order_type=signal.get("order_type", "market"), timestamp=df.index[i],
            strategy_id=self.name, exit_reason="signal", stop_loss=stop_loss,
        )
        if result.accepted:
            self.context[symbol] = {
                "entry_pending": True, "stop_loss": stop_loss,
                "entry_price": current_price,
                "trailing_stop": -np.inf if action == "buy" else np.inf,
                "entry_bar": i,
            }
        if risk_governor is not None and budget_decision is not None:
            committed = (
                budget_decision if result.accepted
                else replace(budget_decision, allowed=False, allowed_risk=0.0)
            )
            risk_governor.commit(committed, symbol=symbol)
            try:
                result.risk_budget = committed.to_dict()
            except AttributeError:  # frozen/foreign result objects
                pass
        return result

    def _publish_signal(
        self,
        broker: ExecutionPort,
        *,
        symbol: str,
        timestamp: Any,
        action: str,
        signal_kind: str,
        price: float,
        reason: str,
    ) -> None:
        """Publish the decision fact before the resulting order (T-2.9)."""

        pipeline = getattr(broker, "event_pipeline", None)
        if pipeline is None:
            return
        point = pd.Timestamp(timestamp)
        if point.tzinfo is None:
            point = point.tz_localize("UTC")
        else:
            point = point.tz_convert("UTC")
        pipeline.publish(
            Signal(
                strategy_id=self.name,
                symbol=symbol,
                action=action,
                signal_kind=signal_kind,
                reference_price=float(price),
                reason=reason,
                bar_time=point.to_pydatetime(),
            ),
            occurred_at=point.to_pydatetime(),
            idempotency_key=(
                f"{self.name}:{symbol}:{point.isoformat()}:{signal_kind}:{action}"
            ),
            symbol=symbol,
            source="backtest_strategy",
        )
