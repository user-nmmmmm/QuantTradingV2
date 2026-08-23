from typing import Dict, Optional

import pandas as pd

from core.execution_port import ExecutionPort
from core.portfolio import Portfolio
from core.risk import RiskManager
from core.state import MarketState
from strategies.base import Strategy


class Router:
    def __init__(
        self,
        strategies: Dict[str, Strategy],
        regime_map: Optional[Dict[str, str]] = None,
        cooldown_bars: int = 3,
        log_path: str = None,
        log_flush_every: int = 256,
    ):
        self.strategies = strategies
        self.cooldown_bars = cooldown_bars
        if not regime_map:
            raise ValueError("regime_map is required and cannot be empty")
        self.regime_map = dict(regime_map)
        self.log_path = log_path
        self.log_flush_every = log_flush_every
        self.symbol_states: Dict[str, MarketState] = {}
        self.cooldowns: Dict[str, int] = {}
        self.log_buffer = []
        self._log_header_written = False

    def route(
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
        current_time = df.index[i]
        route_event = "active"
        strategy_changed = False

        # Deliver fills before cooldown/Cash/missing-route early returns. The
        # opening strategy must observe Router/CircuitBreaker closing fills.
        for strategy in self.strategies.values():
            strategy._consume_execution_trades(symbol, i, portfolio, broker)

        if symbol in self.cooldowns:
            if i <= self.cooldowns[symbol]:
                self._log_routing(
                    current_time,
                    symbol,
                    state.name,
                    "COOLDOWN",
                    0.0,
                    route_event="cooldown",
                    strategy_changed=False,
                )
                return
            del self.cooldowns[symbol]

        last_state = self.symbol_states.get(symbol)
        previous_strategy_name = self._map_state_to_strategy(last_state)
        strategy_name = self._map_state_to_strategy(state)

        if last_state is not None and state != last_state:
            if previous_strategy_name != strategy_name:
                self._handle_switch(symbol, i, df, previous_strategy_name, portfolio, broker)
                self.cooldowns[symbol] = i + self.cooldown_bars
                self.symbol_states[symbol] = state
                self._log_routing(
                    current_time,
                    symbol,
                    state.name,
                    "SWITCH_COOLDOWN",
                    0.0,
                    route_event="strategy_switch",
                    strategy_changed=True,
                )
                return

            route_event = "regime_change"

        self.symbol_states[symbol] = state

        if not strategy_name or strategy_name == "Cash":
            self._log_routing(
                current_time,
                symbol,
                state.name,
                "CASH",
                0.0,
                route_event="cash",
                strategy_changed=False,
            )
            return

        strategy = self.strategies.get(strategy_name)
        if not strategy:
            self._log_routing(
                current_time,
                symbol,
                state.name,
                "MISSING_STRATEGY",
                0.0,
                route_event="missing_strategy",
                strategy_changed=False,
            )
            return

        if state in strategy.allowed_states:
            current_qty = portfolio.get_position(symbol)["qty"]
            self._log_routing(
                current_time,
                symbol,
                state.name,
                strategy_name,
                current_qty,
                route_event=route_event,
                strategy_changed=strategy_changed,
            )
            strategy.on_bar(symbol, i, df, state, portfolio, broker, risk_manager, current_prices)

    def _map_state_to_strategy(self, state: Optional[MarketState]) -> Optional[str]:
        if state is None:
            return None
        return self.regime_map.get(state.name)

    def _log_routing(
        self,
        timestamp,
        symbol,
        regime,
        strategy,
        qty,
        route_event: str,
        strategy_changed: bool,
    ):
        if self.log_path:
            self.log_buffer.append(
                {
                    "timestamp": timestamp,
                    "symbol": symbol,
                    "regime": regime,
                    "strategy": strategy,
                    "current_qty": qty,
                    "route_event": route_event,
                    "strategy_changed": strategy_changed,
                }
            )
            if len(self.log_buffer) >= self.log_flush_every:
                self._flush_log_buffer()

    def _flush_log_buffer(self):
        if not (self.log_path and self.log_buffer):
            return
        pd.DataFrame(self.log_buffer).to_csv(
            self.log_path,
            mode="a" if self._log_header_written else "w",
            header=not self._log_header_written,
            index=False,
        )
        self._log_header_written = True
        self.log_buffer = []

    def save_log(self):
        self._flush_log_buffer()

    def _handle_switch(
        self,
        symbol: str,
        i: int,
        df: pd.DataFrame,
        old_strategy_name: Optional[str],
        portfolio: Portfolio,
        broker: ExecutionPort,
    ):
        broker.cancel_symbol_orders(symbol)

        qty = portfolio.get_position(symbol)["qty"]
        if qty == 0:
            return

        current_price = df["close"].iat[i]
        timestamp = df.index[i]
        if qty > 0:
            broker.submit_order(
                symbol,
                "sell",
                abs(qty),
                current_price,
                timestamp=timestamp,
                strategy_id="Router",
                exit_reason="StateSwitch",
            )
        else:
            broker.submit_order(
                symbol,
                "cover",
                abs(qty),
                current_price,
                timestamp=timestamp,
                strategy_id="Router",
                exit_reason="StateSwitch",
            )
