"""Order/enum types shared by the backtest broker's split modules.

Split out of core/broker.py (A4) — see docs/architecture_review.md. Kept
separate from broker.py itself so the matching/fill-service/financing/
liquidation mixins can import these types without a circular import back
into broker.py (which composes those mixins).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from core.domain import OrderIntent, OrderStatus


class TimeInForce(Enum):
    GTC = "GTC"
    DAY = "DAY"
    IOC = "IOC"
    FOK = "FOK"


class OrderType(Enum):
    """订单类型：市价/限价/止损（简化版）。"""
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


# Compatibility name retained for callers; there is one canonical status enum.
BacktestOrderStatus = OrderStatus


@dataclass
class Order:
    """
    订单数据结构（回测用）。

    字段说明（常用）：
    - symbol：交易标的（与数据源保持一致，例如 BTC-USDT 或 BTC/USDT）
    - side：buy/sell/short/cover（由策略决定；Broker 内转换为 qty_delta 更新持仓）
    - qty：下单数量（正数，方向由 side 决定）
    - order_type/price：限价/止损单需要 price
    - timestamp：信号产生时间（signal_time），用于对齐与反向检查（防 lookahead）
    - strategy_id：用于归因与报告拆分
    - exit_reason：signal/stop/takeprofit/reverse 等，用于报告分析
    """
    symbol: str
    side: str
    qty: float
    order_type: OrderType = OrderType.MARKET
    price: Optional[float] = None  # For limit/stop, or expected price
    timestamp: Any = None
    strategy_id: str = "Manual"
    slippage: float = 0.0  # Expected slippage rate
    stop_loss: float = 0.0
    take_profit: float = 0.0
    exit_reason: str = "signal"  # signal, stop, takeprofit, reverse
    # SR1-2: set on synthetic orders emitted by a portfolio-level risk action
    # (breaker transition id / daily-loss action id) so every CloseEvent this
    # order produces lands in exactly one health cohort.
    risk_action_id: Optional[str] = None
    # T-1.11: EndOfBacktest "mark_to_market" mode closes tail positions at the
    # last price with zero extra commission/slippage - the same choke point
    # used for a real close, just with costs suppressed for this one order.
    zero_cost: bool = False

    # State tracking
    status: OrderStatus = BacktestOrderStatus.CREATED
    submitted_date: Any = None
    filled_qty: float = 0.0
    remaining_qty: float = 0.0
    avg_fill_price: float = 0.0
    time_in_force: TimeInForce = TimeInForce.GTC
    expire_time: Any = None
    id: str = ""
    intent: Optional[OrderIntent] = None
    last_event_id: Optional[str] = None
    # B-01: how many distinct bars this order has been matchable against
    # without filling anything, and the last bar counted. Tracked per bar
    # rather than per matching pass because the engine matches each bar more
    # than once (general book, then resident stops).
    idle_bars: int = 0
    last_counted_bar: Any = None

    @property
    def accepted(self) -> bool:
        """Whether the backtest venue accepted this order into its queue.

        Acceptance does not mean the order filled; live acceptance similarly means
        the exchange accepted the request, while fills remain a later state.
        """
        return self.status not in {
            BacktestOrderStatus.REJECTED,
            BacktestOrderStatus.NO_POSITION,
            BacktestOrderStatus.EXPIRED,
            BacktestOrderStatus.CANCELED,
        }
