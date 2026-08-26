from typing import Any, Dict, List, Optional

from core.lots import Lot, LotBook, LotClose, LotIdAllocator

"""
Portfolio（组合/账户）模块

本模块负责维护回测/实盘通用的账户状态，包括：
- 现金余额（cash）
- 持仓字典（positions）：symbol -> {qty, avg_price}
- 持仓批次账本（lot_books）：symbol -> LotBook，按 FIFO 追踪每一次开仓批次
  （position_id/lot_id），支持加仓、减仓、部分成交与方向反转的批次级核算（T-1.1/T-1.2）。

设计要点：
- 使用“带符号数量”统一表示多空：qty > 0 表示多头，qty < 0 表示空头。
- 现金流采用通用会计约定：买入（qty_delta > 0）会消耗现金；卖出/做空（qty_delta < 0）会回收/增加现金。
- avg_price 仅用于记录当前持仓的成本基准；平仓/减仓产生的盈亏通过 cash 的变化“隐式体现”。
- lot_books 与 positions 由同一个 update_position 调用同步维护，两者不会出现不一致。
"""


class Portfolio:
    def __init__(self, initial_capital: float = 10000.0):
        """
        初始化账户。

        参数：
        - initial_capital：初始资金（默认 10,000），同时初始化 cash。
        """
        self.initial_capital = initial_capital
        self.cash = initial_capital
        # positions: symbol -> {'qty': float, 'avg_price': float}
        self.positions: Dict[str, Dict[str, float]] = {}
        # lot_books: symbol -> LotBook (FIFO batch ledger, see core/lots.py)
        self.lot_books: Dict[str, LotBook] = {}
        # Shared across every LotBook this Portfolio creates, so re-running the
        # same backtest from a fresh Portfolio yields identical lot/position
        # ids in the same order (run-to-run determinism).
        self._lot_id_allocator = LotIdAllocator()

    def get_lot_book(self, symbol: str) -> LotBook:
        if symbol not in self.lot_books:
            self.lot_books[symbol] = LotBook(symbol, allocator=self._lot_id_allocator)
        return self.lot_books[symbol]

    def open_lots(self, symbol: str) -> List[Lot]:
        return self.get_lot_book(symbol).open_lots

    def update_lot_extremes(self, symbol: str, high: float, low: float) -> None:
        """按当前 bar 高低点刷新该 symbol 所有未平仓批次的 MAE/MFE（T-1.10）。"""
        if symbol in self.lot_books:
            self.lot_books[symbol].update_extremes(high, low)

    def get_position(self, symbol: str) -> Dict[str, float]:
        """
        获取某标的的当前持仓。

        返回结构：
        - qty：带符号数量（>0 多头，<0 空头）
        - avg_price：当前持仓成本基准（加仓时按加权平均更新）
        """
        return self.positions.get(symbol, {"qty": 0.0, "avg_price": 0.0})

    def update_position(
        self,
        symbol: str,
        qty_delta: float,
        price: float,
        fee: float = 0.0,
        *,
        strategy_id: str = "",
        order_id: Optional[str] = None,
        stop_price: Optional[float] = None,
        time: Any = None,
    ) -> List[LotClose]:
        """
        更新某标的持仓（一次成交/一次撮合的结果）。

        约定：
        - qty_delta：本次成交导致的数量变化（买入/回补为正；卖出/开空为负）
        - price：本次成交价格（已包含滑点）
        - fee：本次成交手续费（已折算为现金扣减）
        - strategy_id/order_id/stop_price/time：可选，透传给批次账本（lot_books），
          用于批次归属、同笔订单分批成交合并与初始风险(initial_risk)计算（T-1.1/T-1.2/T-1.9）。

        现金与持仓更新规则：
        1) 先扣手续费：cash -= fee
        2) 再计入现金流：cash -= qty_delta * price
           - 买入：qty_delta > 0 -> cash 减少
           - 卖出：qty_delta < 0 -> cash 增加
        3) 若本次属于“开仓/加仓”（绝对持仓变大），则更新 avg_price 为加权平均；
           若属于“减仓/平仓”，avg_price 不变（盈亏通过现金流隐式反映）。

        返回：
        - 本次 fill 在批次账本中核销掉的 LotClose 列表（纯开仓/加仓时为空列表），
          供 Broker 据此构造统一的 CloseEvent（T-1.3）。
        """
        self.cash -= fee

        lot_closes = self.get_lot_book(symbol).apply_fill(
            qty_delta,
            price,
            time=time,
            strategy_id=strategy_id,
            order_id=order_id,
            stop_price=stop_price,
        )

        current_pos = self.get_position(symbol)
        old_qty = current_pos["qty"]
        new_qty = old_qty + qty_delta

        # 现金流：买入消耗现金，卖出/开空回笼现金
        cost = qty_delta * price
        self.cash -= cost
        # A fill that crosses through zero closes the old lot and opens the
        # residual in the opposite direction at the current fill price.
        # Keeping the old average here corrupts short/long reversal cost basis.
        if old_qty != 0 and new_qty != 0 and ((old_qty > 0) != (new_qty > 0)):
            self.positions[symbol] = {
                "qty": new_qty, "avg_price": price
            }
            return lot_closes

        # 判断是否为“开仓/加仓”（绝对持仓增加）：
        # - 从 0 变为非 0：开仓
        # - 多头加仓：old_qty > 0 且 new_qty > old_qty
        # - 空头加仓：old_qty < 0 且 new_qty < old_qty（更负）
        is_opening = False
        if old_qty == 0 and new_qty != 0:
            is_opening = True
        elif old_qty > 0 and new_qty > old_qty:  # Increasing Long
            is_opening = True
        elif old_qty < 0 and new_qty < old_qty:  # Increasing Short
            is_opening = True

        if is_opening:
            # 加权平均成本（用绝对数量计算，避免多空符号干扰）
            total_value = (abs(old_qty) * current_pos["avg_price"]) + (
                abs(qty_delta) * price
            )
            new_avg_price = total_value / abs(new_qty)
            self.positions[symbol] = {"qty": new_qty, "avg_price": new_avg_price}
        else:
            # 减仓/平仓：avg_price 不变；本次成交产生的盈亏已体现在 cash 的变化中
            if new_qty == 0:
                if symbol in self.positions:
                    del self.positions[symbol]
            else:
                self.positions[symbol]["qty"] = new_qty
                # avg_price remains same

        return lot_closes

    def get_equity(self, current_prices: Dict[str, float]) -> float:
        """
        计算当前权益（Equity）= 现金 + 所有持仓按市价估值。

        current_prices：
        - symbol -> 当前价格
        - 若缺失某 symbol 的当前价，则回退使用 avg_price（用于容错，但会低估/高估真实权益）。
        """
        equity = self.cash
        for symbol, pos in self.positions.items():
            qty = pos["qty"]
            price = current_prices.get(
                symbol, pos["avg_price"]
            )  # Fallback to avg_price if no current price
            equity += qty * price
        return equity

    def get_total_value(self, current_prices: Dict[str, float]) -> float:
        """get_equity 的别名，语义上等同于账户总权益。"""
        return self.get_equity(current_prices)

    def get_total_exposure(self, current_prices: Dict[str, float]) -> float:
        """
        计算总敞口（Gross Exposure）：所有持仓按绝对数量估值之和。

        常用于杠杆与风险上限检查：
        exposure = Σ |qty| * price
        """
        exposure = 0.0
        for symbol, pos in self.positions.items():
            qty = abs(pos["qty"])
            price = current_prices.get(symbol, pos["avg_price"])
            exposure += qty * price
        return exposure
