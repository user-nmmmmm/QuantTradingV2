from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field
from enum import Enum
import random

import pandas as pd

from core.logger import get_logger
from core.portfolio import Portfolio

logger = get_logger(__name__)

"""
Broker（撮合/执行）模块

本模块用于回测环境下的“虚拟交易所”模拟，核心目标是：
- 将策略产生的订单（Order）在后续 bar 上按规则撮合成交
- 模拟现实交易成本：滑点（slippage）、手续费（maker/taker）、冲击成本（impact cost，可选）
- 将成交结果写入 Portfolio（现金与持仓）并记录 trade log

执行模型（回测假设）：
- 策略在 bar i 收盘基于历史数据产生信号，提交订单时记录 signal_time（order.timestamp）
- 引擎在 bar i+1 使用该 bar 的 OHLC 数据处理订单：
  - Market：在开盘价成交（再叠加滑点）
  - Limit：若触及限价则成交；开盘价可成交则按开盘价（taker），否则按限价（maker）
  - Stop：触发后视为市价单（taker），按触发价与开盘价之间的更不利价格成交
"""


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


class BacktestOrderStatus(Enum):
    """订单状态机（简化版），用于回测记录与调试。"""
    CREATED = "created"
    SUBMITTED = "submitted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"


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

    # State tracking
    status: BacktestOrderStatus = BacktestOrderStatus.CREATED
    filled_qty: float = 0.0
    remaining_qty: float = 0.0
    avg_fill_price: float = 0.0
    time_in_force: TimeInForce = TimeInForce.GTC
    expire_time: Any = None
    id: str = field(
        default_factory=lambda: str(random.randint(100000, 999999))
    )  # Simple ID

    @property
    def accepted(self) -> bool:
        """Whether the backtest venue accepted this order into its queue.

        Acceptance does not mean the order filled; live acceptance similarly means
        the exchange accepted the request, while fills remain a later state.
        """
        return self.status not in {
            BacktestOrderStatus.REJECTED,
            BacktestOrderStatus.EXPIRED,
            BacktestOrderStatus.CANCELED,
        }

class Broker:
    def __init__(
        self,
        portfolio: Portfolio,
        commission_rate: float = 0.001,
        commission_rate_maker: float = 0.0005,
        slippage: float = 0.0,
        random_slip: bool = False,
        use_impact_cost: bool = False,
        max_participation_rate: float = 1.0,
    ):
        self.portfolio = portfolio
        self.commission_rate = commission_rate  # Taker
        self.commission_rate_maker = commission_rate_maker  # Maker
        self.slippage = slippage
        self.random_slip = random_slip
        self.use_impact_cost = use_impact_cost
        if not 0 < max_participation_rate <= 1:
            raise ValueError("max_participation_rate must be in (0, 1]")
        self.max_participation_rate = max_participation_rate
        self.trades = []  # List to store executed trades
        self.pending_orders: List[Order] = []
        self.active_orders: List[
            Order
        ] = []  # Orders that persist across bars (Limit/Stop)

    def submit_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        price: float = None,
        order_type: str = "market",  # keeping string for compatibility, convert to Enum
        timestamp: Any = None,
        slippage: float = 0.0,
        strategy_id: str = "Manual",
        exit_reason: str = "signal",
        time_in_force: str = "GTC",
        expire_time: Any = None,
    ) -> Order:
        """
        提交订单（进入撮合队列）。

        参数：
        - order_type：'market' / 'limit' / 'stop'（为兼容旧接口，这里仍接受字符串并映射到 Enum）
        - price：限价/止损单必填；市价单可为 None
        - timestamp：信号产生时间（通常为 bar i 的收盘时间），用于日志与反向检查

        行为：
        - 仅做基本参数校验与结构化封装，加入 pending_orders
        - 实际撮合发生在 process_orders（由引擎在每根 bar 调用）
        """
        # Map string to Enum
        otype_map = {
            "market": OrderType.MARKET,
            "limit": OrderType.LIMIT,
            "stop": OrderType.STOP,
        }
        otype = otype_map.get(order_type.lower(), OrderType.MARKET)

        order = Order(
            symbol=symbol,
            side=side,
            qty=qty,
            order_type=otype,
            price=price,
            timestamp=timestamp,
            slippage=slippage,
            strategy_id=strategy_id,
            exit_reason=exit_reason,
            status=BacktestOrderStatus.CREATED,
            remaining_qty=max(qty, 0.0),
            time_in_force=TimeInForce(time_in_force.upper()),
            expire_time=expire_time,
        )
        if qty <= 0:
            logger.warning(
                "Order rejected: quantity must be positive for %s %s %.8f",
                symbol,
                side,
                qty,
            )
            order.status = BacktestOrderStatus.REJECTED
            return order

        if otype in [OrderType.LIMIT, OrderType.STOP] and price is None:
            logger.warning(
                "Order rejected: price is required for %s order on %s",
                order_type,
                symbol,
            )
            order.status = BacktestOrderStatus.REJECTED
            return order

        self.pending_orders.append(order)
        return order

    def process_orders(self, current_bar: Dict[str, pd.Series]) -> List[Dict]:
        """Match eligible orders against real bars with a shared volume budget."""
        executed_trades: List[Dict] = []
        next_active_orders: List[Order] = []
        for order in self.pending_orders:
            order.status = BacktestOrderStatus.SUBMITTED
            self.active_orders.append(order)
        self.pending_orders = []

        volume_budget = {
            symbol: max(float(bar.get("volume", 0.0)), 0.0) * self.max_participation_rate
            for symbol, bar in current_bar.items()
        }

        for order in self.active_orders:
            bar_data = current_bar.get(order.symbol)
            if bar_data is None:
                next_active_orders.append(order)
                continue
            current_time = bar_data.name
            if order.timestamp is not None and current_time <= order.timestamp:
                next_active_orders.append(order)
                continue
            if order.expire_time is not None and current_time > order.expire_time:
                order.status = BacktestOrderStatus.EXPIRED
                continue
            if (
                order.time_in_force == TimeInForce.DAY
                and order.timestamp is not None
                and pd.Timestamp(current_time).date() > pd.Timestamp(order.timestamp).date()
            ):
                order.status = BacktestOrderStatus.EXPIRED
                continue

            open_price = float(bar_data["open"])
            high_price = float(bar_data["high"])
            low_price = float(bar_data["low"])
            exec_price = None
            is_maker = False
            if order.order_type == OrderType.MARKET:
                exec_price = open_price
            elif order.order_type == OrderType.LIMIT:
                if order.side in {"buy", "cover"} and low_price <= order.price:
                    exec_price = open_price if open_price <= order.price else order.price
                    is_maker = open_price > order.price
                elif order.side in {"sell", "short"} and high_price >= order.price:
                    exec_price = open_price if open_price >= order.price else order.price
                    is_maker = open_price < order.price
            elif order.order_type == OrderType.STOP:
                if order.side in {"buy", "cover"} and high_price >= order.price:
                    exec_price = max(open_price, order.price)
                elif order.side in {"sell", "short"} and low_price <= order.price:
                    exec_price = min(open_price, order.price)

            if exec_price is None:
                if order.time_in_force in {TimeInForce.IOC, TimeInForce.FOK}:
                    order.status = BacktestOrderStatus.CANCELED
                else:
                    next_active_orders.append(order)
                continue

            available = volume_budget.get(order.symbol, 0.0)
            requested = order.remaining_qty
            if order.time_in_force == TimeInForce.FOK and available < requested:
                order.status = BacktestOrderStatus.CANCELED
                continue
            fill_qty = min(requested, available)
            if fill_qty <= 0:
                if order.time_in_force in {TimeInForce.IOC, TimeInForce.FOK}:
                    order.status = BacktestOrderStatus.CANCELED
                else:
                    next_active_orders.append(order)
                continue

            trade = self._execute_trade(
                order, exec_price, current_time, fill_qty,
                float(bar_data.get("volume", 0.0)), is_maker=is_maker,
            )
            if trade is None:
                order.status = BacktestOrderStatus.REJECTED
                continue
            executed_trades.append(trade)
            volume_budget[order.symbol] = max(available - trade["qty"], 0.0)
            previous_filled = order.filled_qty
            order.filled_qty += trade["qty"]
            order.remaining_qty = max(order.qty - order.filled_qty, 0.0)
            order.avg_fill_price = (
                (order.avg_fill_price * previous_filled + trade["fill_price"] * trade["qty"])
                / order.filled_qty
            )
            if order.remaining_qty <= 1e-12:
                order.status = BacktestOrderStatus.FILLED
            elif order.time_in_force == TimeInForce.IOC:
                order.status = BacktestOrderStatus.CANCELED
            else:
                order.status = BacktestOrderStatus.PARTIALLY_FILLED
                next_active_orders.append(order)

        self.active_orders = next_active_orders
        return executed_trades
    def _execute_trade(
        self,
        order: Order,
        price: float,
        timestamp: Any,
        fill_qty: float,
        volume: float = 0,
        is_maker: bool = False,
    ) -> Optional[Dict]:
        base_slip = order.slippage if order.slippage > 0 else self.slippage
        slip_rate = random.uniform(0, base_slip) if self.random_slip and base_slip > 0 else base_slip
        impact_slip = 0.0
        if self.use_impact_cost and volume > 0:
            participation = fill_qty / volume
            if participation > 0.01:
                impact_slip = participation * 0.1
        total_slip_rate = slip_rate + impact_slip
        if order.side in {"buy", "cover"}:
            fill_price = price * (1 + total_slip_rate)
            qty_delta = fill_qty
            slip_dir = "positive"
        elif order.side in {"sell", "short"}:
            fill_price = price * (1 - total_slip_rate)
            qty_delta = -fill_qty
            slip_dir = "negative"
        else:
            return None

        current_pos = self.portfolio.get_position(order.symbol)
        if order.side == "sell":
            fill_qty = min(fill_qty, max(current_pos["qty"], 0.0))
            qty_delta = -fill_qty
        elif order.side == "cover":
            fill_qty = min(fill_qty, max(-current_pos["qty"], 0.0))
            qty_delta = fill_qty
        if fill_qty <= 0:
            return None

        value = fill_qty * fill_price
        fee_rate = self.commission_rate_maker if is_maker else self.commission_rate
        commission = value * fee_rate
        self.portfolio.update_position(order.symbol, qty_delta, fill_price, commission)
        trade_record = {
            "order_id": order.id,
            "signal_time": order.timestamp,
            "fill_time": timestamp,
            "symbol": order.symbol,
            "side": order.side,
            "qty": fill_qty,
            "fill_price": fill_price,
            "commission": commission,
            "slip": price * total_slip_rate,
            "slip_dir": slip_dir,
            "strategy_id": order.strategy_id,
            "exit_reason": order.exit_reason,
            "is_maker": is_maker,
        }
        self.trades.append(trade_record)
        logger.info(
            "Trade filled symbol=%s side=%s qty=%.8f fill_price=%.8f signal_time=%s fill_time=%s",
            order.symbol, order.side, fill_qty, fill_price, order.timestamp, timestamp,
        )
        return trade_record
    def cancel_symbol_orders(self, symbol: str) -> int:
        """
        Cancel all pending and active orders for a given symbol.
        Returns the number of orders cancelled.
        Called on state switch to prevent stale limit orders from filling
        in the wrong market regime.
        """
        before = len(self.pending_orders) + len(self.active_orders)

        self.pending_orders = [o for o in self.pending_orders if o.symbol != symbol]
        cancelled = [o for o in self.active_orders if o.symbol == symbol]
        self.active_orders = [o for o in self.active_orders if o.symbol != symbol]

        for o in cancelled:
            o.status = BacktestOrderStatus.CANCELED

        n = before - len(self.pending_orders) - len(self.active_orders)
        if n > 0:
            logger.info(
                "Cancelled %s stale order(s) for %s during state switch",
                n,
                symbol,
            )
        return n

    # Keep compatibility if needed, or remove.
    # Since we are refactoring, I will remove execute_order to force updates.
