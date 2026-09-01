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

``Broker`` composes the four mixins below via inheritance rather than
holding separate collaborator objects, so every method still reads/writes
the exact same ``self`` attributes as before the split — behavior-identical,
mechanical. A true composition redesign is a bigger change on this
money-path-adjacent code and is deliberately left for a dedicated pass, not
bundled into this file-size cleanup. ``Order``, ``OrderType``,
``TimeInForce``, and ``BacktestOrderStatus`` are re-exported unchanged since
they're part of this module's public API.
"""
from typing import Dict, Any, List, Optional

import pandas as pd

from core.broker.types import BacktestOrderStatus, Order, OrderType, TimeInForce
from core.broker.matching import MatchingMixin
from core.broker.fill_service import FillServiceMixin
from core.broker.financing import FinancingMixin
from core.broker.liquidation import LiquidationMixin
from core.events import TradingEventPipeline
from core.logger import get_logger
from core.lots import CloseEvent
from core.portfolio import Portfolio
from core.risk.reservation import RiskReservationProjection

logger = get_logger(__name__)

__all__ = [
    "TimeInForce", "OrderType", "BacktestOrderStatus", "Order", "Broker",
]


class Broker(MatchingMixin, FillServiceMixin, FinancingMixin, LiquidationMixin):
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
        self.last_prices: Dict[str, float] = {}
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
        self.pending_orders: List[Order] = []
        self.active_orders: List[
            Order
        ] = []  # Orders that persist across bars (Limit/Stop)
