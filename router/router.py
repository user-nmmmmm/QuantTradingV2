from typing import Any, Dict, Optional

import pandas as pd

from core.execution_port import ExecutionPort
from core.portfolio import Portfolio
from core.risk import RiskManager
from core.state import MarketState
from strategies.base import Strategy
from core.allocation import EntryCandidate, PortfolioSignalAllocator
from core.entry_audit import note
from core.timeframes import observed_bars_after, as_utc_timestamp


class Router:
    def __init__(
        self,
        strategies: Dict[str, Strategy],
        regime_map: Optional[Dict[str, str]] = None,
        cooldown_bars: int = 3,
        log_path: Optional[str] = None,
        log_flush_every: int = 256,
        max_holding_days: Optional[float] = None,
        risk_governor: Optional[Any] = None,
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
        self._cooldown_started_at: Dict[str, Any] = {}
        self._cooldown_progress: Dict[str, tuple[Any, int]] = {}
        self.log_buffer = []
        self._log_header_written = False
        self.max_holding_days = (
            float(max_holding_days) if max_holding_days is not None else None
        )
        # SR3-2: one governor for the whole run so a session's entries
        # share a correlated-risk budget instead of each claiming it.
        self.allocator = PortfolioSignalAllocator(risk_governor)

    def collect_candidate(
        self,
        symbol: str,
        i: int,
        df: pd.DataFrame,
        state: MarketState,
        portfolio: Portfolio,
        broker: ExecutionPort,
        risk_manager: RiskManager,
        current_prices: Optional[Dict[str, float]] = None,
    ) -> Optional[EntryCandidate]:
        """Production Phase-4 route path: exits first, then return an entry proposal.

        This method never performs an implicit StateSwitch liquidation.  An
        existing lot is evaluated by its opening strategy (or by the explicit
        maximum-holding-period controller); only flat symbols can become entry
        candidates for the portfolio allocator.
        """

        managed = self.process_position_management(
            symbol, i, df, state, portfolio, broker
        )
        if managed:
            return None
        return self.collect_entry_candidate(
            symbol, i, df, state, portfolio, broker, risk_manager, current_prices
        )

    def process_position_management(
        self,
        symbol: str,
        i: int,
        df: pd.DataFrame,
        state: MarketState,
        portfolio: Portfolio,
        broker: ExecutionPort,
    ) -> bool:
        """Consume fills and manage an existing position; return whether held."""
        current_time = df.index[i]
        for strategy in self.strategies.values():
            strategy._consume_execution_trades(symbol, i, portfolio, broker)

        qty = float(portfolio.get_position(symbol)["qty"])
        if qty != 0:
            if self._max_holding_expired(symbol, current_time, portfolio):
                self._submit_time_exit(symbol, i, df, qty, broker)
                self._log_routing(current_time, symbol, state.name,
                                  "MaxHoldingPeriod", qty,
                                  route_event="time_exit", strategy_changed=False)
                return True
            opening_name = self._opening_strategy_name(symbol, portfolio)
            strategy = self.strategies.get(opening_name or "")
            if strategy is not None:
                strategy.process_exit_only(symbol, i, df, state, portfolio, broker)
            self.symbol_states[symbol] = state
            self._log_routing(current_time, symbol, state.name,
                              opening_name or "UNOWNED_POSITION", qty,
                              route_event="position_exit_control", strategy_changed=False)
            return True
        return False

    def collect_entry_candidate(
        self,
        symbol: str,
        i: int,
        df: pd.DataFrame,
        state: MarketState,
        portfolio: Portfolio,
        broker: ExecutionPort,
        risk_manager: RiskManager,
        current_prices: Optional[Dict[str, float]] = None,
    ) -> Optional[EntryCandidate]:
        """Return an entry proposal for an already confirmed flat symbol."""
        if "scheduled_exit" in df and bool(df["scheduled_exit"].iat[i]):
            return None
        del risk_manager, current_prices
        current_time = df.index[i]
        if float(portfolio.get_position(symbol)["qty"]) != 0:
            note("position_held")
            return None

        if symbol in self.cooldowns:
            started = self._cooldown_started_at.get(symbol)
            if started is not None:
                last, elapsed = self._cooldown_progress.get(symbol, (started, 0))
                if as_utc_timestamp(current_time) > as_utc_timestamp(last):
                    elapsed += observed_bars_after(df, i, last)
                    self._cooldown_progress[symbol] = (current_time, elapsed)
                cooling = elapsed <= self.cooldown_bars
            else:
                cooling = i <= self.cooldowns[symbol]
            if cooling:
                note("router_cooldown")
                self._log_routing(current_time, symbol, state.name, "COOLDOWN", 0.0,
                                  route_event="cooldown", strategy_changed=False)
                return None
            del self.cooldowns[symbol]
            self._cooldown_started_at.pop(symbol, None)
            self._cooldown_progress.pop(symbol, None)

        last_state = self.symbol_states.get(symbol)
        previous_name = self._map_state_to_strategy(last_state)
        strategy_name = self._map_state_to_strategy(state)
        changed = last_state is not None and previous_name != strategy_name
        if changed:
            note("router_switch_cooldown")
            # State transition means cancel stale *entry* intent and temporarily
            # stop new risk.  It does not grant Router authority to close lots.
            broker.cancel_symbol_orders(symbol)
            self.cooldowns[symbol] = i + self.cooldown_bars
            self._cooldown_started_at[symbol] = current_time
            self._cooldown_progress[symbol] = (current_time, 0)
            self.symbol_states[symbol] = state
            self._log_routing(current_time, symbol, state.name, "SWITCH_COOLDOWN", 0.0,
                              route_event="stop_new_entries", strategy_changed=True)
            return None
        self.symbol_states[symbol] = state
        if not strategy_name or strategy_name == "Cash":
            note("market_state_cash")
            self._log_routing(current_time, symbol, state.name, "CASH", 0.0,
                              route_event="cash", strategy_changed=False)
            return None
        strategy = self.strategies.get(strategy_name)
        if strategy is None or state not in strategy.allowed_states:
            note("strategy_unavailable")
            self._log_routing(current_time, symbol, state.name, "MISSING_STRATEGY", 0.0,
                              route_event="missing_strategy", strategy_changed=False)
            return None
        self._log_routing(current_time, symbol, state.name, strategy_name, 0.0,
                          route_event="candidate", strategy_changed=False)
        note(strategy=strategy_name)
        return strategy.build_entry_candidate(symbol, i, df, state, portfolio)

    def checkpoint(self) -> dict:
        return {"schema": "router-runtime/v1",
                "states": {symbol: state.name for symbol, state in self.symbol_states.items()
                           if isinstance(state, MarketState)},
                "cooldowns": dict(self.cooldowns),
                "started_at": {symbol: pd.Timestamp(value).isoformat()
                               for symbol, value in self._cooldown_started_at.items()},
                "progress": {symbol: [pd.Timestamp(value[0]).isoformat(), value[1]]
                             for symbol, value in self._cooldown_progress.items()}}

    def restore_checkpoint(self, row: dict) -> None:
        if row.get("schema") != "router-runtime/v1":
            raise ValueError("unsupported router runtime checkpoint")
        states = {symbol: MarketState[name] for symbol, name in row["states"].items()}
        cooldowns = {symbol: int(value) for symbol, value in row["cooldowns"].items()}
        started = {symbol: pd.Timestamp(value) for symbol, value in row["started_at"].items()}
        progress = {symbol: (pd.Timestamp(value[0]), int(value[1]))
                    for symbol, value in row["progress"].items()}
        self.symbol_states, self.cooldowns = states, cooldowns
        self._cooldown_started_at, self._cooldown_progress = started, progress

    @staticmethod
    def _opening_strategy_name(symbol: str, portfolio: Portfolio) -> Optional[str]:
        book = portfolio.lot_books.get(symbol)
        lots = book.open_lots if book is not None else []
        return str(lots[0].strategy_id) if lots else None

    def _max_holding_expired(self, symbol: str, current_time, portfolio: Portfolio) -> bool:
        if self.max_holding_days is None or self.max_holding_days <= 0:
            return False
        book = portfolio.lot_books.get(symbol)
        lots = book.open_lots if book is not None else []
        entries = [pd.Timestamp(lot.entry_time) for lot in lots if lot.entry_time is not None]
        if not entries:
            return False
        return pd.Timestamp(current_time) - min(entries) >= pd.Timedelta(days=self.max_holding_days)

    @staticmethod
    def _submit_time_exit(symbol: str, i: int, df: pd.DataFrame, qty: float,
                          broker: ExecutionPort):
        return broker.submit_order(
            symbol, "sell" if qty > 0 else "cover", abs(qty),
            float(df["close"].iat[i]), timestamp=df.index[i],
            strategy_id="MaxHoldingPeriod", exit_reason="MaxHoldingPeriod",
        )

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
