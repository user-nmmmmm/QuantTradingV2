"""Broker（撮合/执行）模块

本模块用于回测环境下的"虚拟交易所"模拟，核心目标是：
- 将策略产生的订单（Order）在后续 bar 上按规则撮合成交
- 模拟现实交易成本：滑点（slippage）、手续费（maker/taker）、冲击成本（impact cost，可选）
- 将成交结果写入 Portfolio（现金与持仓）并记录 trade log

执行模型（回测假设）：
- 策略在 bar i 收盘基于历史数据产生信号，提交订单时记录 signal_time（order.timestamp）
- 引擎在 bar i+1 使用该 bar 的 OHLC 数据处理订单：
  - Market：在开盘价成交（再叠加滑点）
  - Limit：若触及限价则成交；开盘价可成交则按开盘价（taker），否则按限价（maker）
  - Stop：触发后视为市价单（taker），按触发价与开盘价之间的更不利价格成交

Split by change reason (A4) — see docs/architecture_review.md:
- core/broker/types.py        — Order/OrderType/TimeInForce data types
- core/broker/matching.py     — order submission, per-bar matching, book bookkeeping
- core/broker/fill_service.py — cost accounting, position update, event publishing
- core/broker/financing.py    — perpetual funding / margin-short borrow accrual
- core/broker/liquidation.py  — forced position reduction

``Broker`` holds the four collaborators below (``_matching``, ``_fill_service``, ``_financing``,
``_liquidation``) and forwards their methods with the same signatures. The collaborators reach the shared
state through their ``host`` reference, so behavior is identical to the former mixins (B4, ADR 0018).
``Order``, ``OrderType``,
``TimeInForce``, and ``BacktestOrderStatus`` are re-exported unchanged since
they're part of this module's public API.
"""
from typing import Dict, Any, List, Optional

import pandas as pd

from core.broker.types import BacktestOrderStatus, Order, OrderType, TimeInForce
from core.broker.matching import Matching
from core.broker.fill_service import FillService
from core.broker.financing import Financing
from core.broker.liquidation import Liquidation
from core.events import TradingEventPipeline
from core.foundation.logger import get_logger
from core.trading.lots import CloseEvent
from core.trading.portfolio import Portfolio
from core.risk.reservation import RiskReservationProjection
from core.foundation.domain import OrderIntent
from datetime import datetime
from typing import Callable
from typing import Iterable
from functools import cached_property
from core.risk.delegation import Delegated

logger = get_logger(__name__)

__all__ = [
    "TimeInForce", "OrderType", "BacktestOrderStatus", "Order", "Broker",
]


class Broker:
    # The order books live on Matching and the risk-action targets on Liquidation (B6, ADR 0028); these names forward.
    pending_orders = Delegated[List[Order]]("_matching")
    active_orders = Delegated[List[Order]]("_matching")  # Orders that persist across bars (Limit/Stop)
    risk_action_targets = Delegated[Dict[str, Dict[str, Dict[str, Any]]]]("_liquidation")

    @cached_property
    def _matching(self) -> Matching:
        return Matching(self)

    @cached_property
    def _fill_service(self) -> FillService:
        return FillService(self)

    @cached_property
    def _financing(self) -> Financing:
        return Financing(self)

    @cached_property
    def _liquidation(self) -> Liquidation:
        return Liquidation(self)

    def __init__(
        self,
        portfolio: Portfolio,
        commission_rate: float = 0.001,
        commission_rate_maker: float = 0.0005,
        slippage: float = 0.0,
        random_slip: bool = False,
        use_impact_cost: bool = False,
        max_participation_rate: float = 1.0,
        spread_bps: float = 0.0,
        volatility_slippage_factor: float = 0.0,
        impact_coefficient: float = 0.10,
        impact_exponent: float = 1.5,
        funding_interval_hours: float = 8.0,
        funding_rate_required: bool = True,
        default_borrow_rate_annual: float = 0.0,
        borrow_availability_required: bool = False,
        default_borrow_limit_qty: float = float("inf"),
        liquidation_penalty_bps: float = 0.0,
        opening_order_ttl_bars: int = 0,
        event_pipeline: Optional[TradingEventPipeline] = None,
        exchange_id: str = "backtest",
        account_id: str = "backtest",
        timeframe: str = "unknown",
    ):
        self.portfolio = portfolio
        self.event_pipeline = event_pipeline or TradingEventPipeline()
        self.reservation_projection = RiskReservationProjection(self.event_pipeline)
        self.exchange_id = exchange_id
        self.account_id = account_id
        self.timeframe = timeframe
        self._intent_sequence = 0
        self.commission_rate = commission_rate  # Taker
        self.commission_rate_maker = commission_rate_maker  # Maker
        self.slippage = slippage
        self.random_slip = random_slip
        self.use_impact_cost = use_impact_cost
        if not 0 < max_participation_rate <= 1:
            raise ValueError("max_participation_rate must be in (0, 1]")
        self.max_participation_rate = max_participation_rate
        if spread_bps < 0 or volatility_slippage_factor < 0:
            raise ValueError("spread and volatility slippage inputs cannot be negative")
        if impact_coefficient < 0 or impact_exponent <= 1:
            raise ValueError("impact_coefficient must be non-negative and exponent > 1")
        if funding_interval_hours <= 0:
            raise ValueError("funding_interval_hours must be positive")
        if default_borrow_rate_annual < 0 or default_borrow_limit_qty < 0:
            raise ValueError("borrow defaults cannot be negative")
        self.spread_bps = float(spread_bps)
        self.volatility_slippage_factor = float(volatility_slippage_factor)
        self.impact_coefficient = float(impact_coefficient)
        self.impact_exponent = float(impact_exponent)
        self.funding_interval_hours = float(funding_interval_hours)
        self.funding_rate_required = bool(funding_rate_required)
        self.default_borrow_rate_annual = float(default_borrow_rate_annual)
        self.borrow_availability_required = bool(borrow_availability_required)
        self.default_borrow_limit_qty = float(default_borrow_limit_qty)
        self.liquidation_penalty_bps = float(liquidation_penalty_bps)
        if opening_order_ttl_bars < 0:
            raise ValueError("opening_order_ttl_bars cannot be negative")
        # B-01: bars an unfilled opening order may rest before it expires and
        # releases its risk reservation. 0 disables the rule, which is the
        # default so constructing a Broker directly keeps the old behaviour;
        # BacktestEngine passes the configured value.
        self.opening_order_ttl_bars = int(opening_order_ttl_bars)
        self.last_prices: Dict[str, float] = {}
        # Participation cap state: the bar each symbol's budget was sized on
        # and how much of it is left. Tracked on the broker rather than inside
        # process_orders because that runs several times per bar (see
        # Matching._bar_volume_budget).
        self._volume_budget: Dict[str, float] = {}
        self._volume_budget_bar: Dict[str, Any] = {}
        self._last_funding_bucket: Dict[str, int] = {}
        self._last_borrow_time: Dict[str, pd.Timestamp] = {}
        self.execution_audit: List[Dict[str, Any]] = []
        self.trades: List[Dict[str, Any]] = []  # List to store executed trades
        # Unified close-event contract (T-1.3): every exit path (self-exit,
        # hard stop, Router state-switch, circuit breaker, EndOfBacktest)
        # closes lots through _execute_trade -> Portfolio.update_position,
        # so this is the single place CloseEvents are constructed.
        self.close_events: List[CloseEvent] = []
        self._close_event_sequence = 0
        # Terminal closing orders must remain queryable after leaving the book.
        self.orders_by_id: Dict[str, Order] = {}
        # Retain cumulative opening fills and their immutable approvals even
        # after an order leaves the matching queues.
        self.opening_orders: Dict[str, Order] = {}

    QUOTE_BORROW_SYMBOL = Financing.QUOTE_BORROW_SYMBOL

    def submit_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: Optional[float] = None,
        order_type: str = 'market',
        timestamp: Any = None,
        slippage: float = 0.0,
        strategy_id: str = 'Manual',
        exit_reason: str = 'signal',
        time_in_force: str = 'GTC',
        expire_time: Any = None,
        sequence: Optional[int] = None,
        _intent: Optional[OrderIntent] = None,
        stop_loss: float = 0.0,
        zero_cost: bool = False,
        risk_action_id: Optional[str] = None,
        approved_risk_amount: Optional[float] = None,
        signal_id: Optional[str] = None,
        causation_id: Optional[str] = None,
        match_not_before: Any = None,
        reference_price: Optional[float] = None,
    ) -> Order:
        return self._matching.submit_order(
            symbol,
            side,
            qty,
            price,
            order_type,
            timestamp,
            slippage,
            strategy_id,
            exit_reason,
            time_in_force,
            expire_time,
            sequence,
            _intent,
            stop_loss,
            zero_cost,
            risk_action_id,
            approved_risk_amount,
            signal_id,
            causation_id,
            match_not_before,
            reference_price,
        )

    def submit_intent(self, intent: OrderIntent) -> Order:
        return self._matching.submit_intent(intent)

    @staticmethod
    def _event_time(value: Any) -> datetime:
        return Matching._event_time(value)

    @classmethod
    def _iso_time(cls, value: Any) -> str:
        return cls._event_time(value).isoformat().replace("+00:00", "Z")

    def _publish_order_event(self, order: Order, occurred_at: Any) -> None:
        return self._matching._publish_order_event(order, occurred_at)

    def _set_status(self, order: Order, status: Any, occurred_at: Any = None) -> None:
        return self._matching._set_status(order, status, occurred_at)

    def process_orders(
        self,
        current_bar: Dict[str, pd.Series],
        *,
        order_filter: Optional[Callable[[Order], bool]] = None,
    ) -> List[Dict]:
        return self._matching.process_orders(current_bar, order_filter=order_filter)

    def _expire_aged_opening_order(self, order: Order, current_time: Any) -> bool:
        return self._matching._expire_aged_opening_order(order, current_time)

    def _bar_volume_budget(self, current_bar: Dict[str, pd.Series]) -> Dict[str, float]:
        return self._matching._bar_volume_budget(current_bar)

    def _audit_order(self, order: Order, timestamp: Any, outcome: str, reason: str, **details: Any) -> None:
        return self._matching._audit_order(order, timestamp, outcome, reason, **details)

    def has_active_open_order(self, symbol: str) -> bool:
        return self._matching.has_active_open_order(symbol)

    def pending_open_notional(self, current_prices: Optional[Dict[str, float]] = None) -> Dict[str, float]:
        return self._matching.pending_open_notional(current_prices)

    def cancel_opening_orders(self, symbols: Optional[Iterable[str]] = None, *, timestamp: Any = None) -> int:
        return self._matching.cancel_opening_orders(symbols, timestamp=timestamp)

    def cancel_protective_stops(self, symbols: Optional[Iterable[str]] = None) -> int:
        return self._matching.cancel_protective_stops(symbols)

    def cancel_symbol_orders(self, symbol: str) -> int:
        return self._matching.cancel_symbol_orders(symbol)

    def _execute_trade(
        self,
        order: Order,
        price: float,
        timestamp: Any,
        fill_qty: float,
        volume: float = 0,
        is_maker: bool = False,
        bar_context: Optional[pd.Series] = None,
    ) -> Optional[Dict]:
        return self._fill_service._execute_trade(
            order,
            price,
            timestamp,
            fill_qty,
            volume,
            is_maker,
            bar_context,
        )

    def accrue_carry(self, current_bar: Dict[str, pd.Series]) -> List[Dict[str, Any]]:
        return self._financing.accrue_carry(current_bar)

    def _accrue_short_borrow(self, symbol, position, bar, timestamp):
        return self._financing._accrue_short_borrow(symbol, position, bar, timestamp)

    def _accrue_quote_borrow(self, current_bar: Dict[str, pd.Series]):
        return self._financing._accrue_quote_borrow(current_bar)

    def force_liquidate(
        self,
        current_bar: Dict[str, pd.Series],
        *,
        timestamp: Any,
        reason: str = 'MarginLiquidation',
        remaining_fraction: float = 0.0,
        risk_action_id: Optional[str] = None,
        scope_symbols: Optional[set[str]] = None,
    ) -> List[Dict]:
        return self._liquidation.force_liquidate(
            current_bar,
            timestamp=timestamp,
            reason=reason,
            remaining_fraction=remaining_fraction,
            risk_action_id=risk_action_id,
            scope_symbols=scope_symbols,
        )
