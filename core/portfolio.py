from typing import Dict, Optional

"""
Portfolio（组合/账户）模块

本模块负责维护回测/实盘通用的账户状态，包括：
- 现金余额（cash）
- 持仓字典（positions）：symbol -> {qty, avg_price}

设计要点：
- 使用“带符号数量”统一表示多空：qty > 0 表示多头，qty < 0 表示空头。
- 现金流采用通用会计约定：买入（qty_delta > 0）会消耗现金；卖出/做空（qty_delta < 0）会回收/增加现金。
- avg_price 仅用于记录当前持仓的成本基准；平仓/减仓产生的盈亏通过 cash 的变化“隐式体现”。
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

    def get_position(self, symbol: str) -> Dict[str, float]:
        """
        获取某标的的当前持仓。

        返回结构：
        - qty：带符号数量（>0 多头，<0 空头）
        - avg_price：当前持仓成本基准（加仓时按加权平均更新）
        """
        return self.positions.get(symbol, {"qty": 0.0, "avg_price": 0.0})

    def update_position(
        self, symbol: str, qty_delta: float, price: float, fee: float = 0.0
    ):
        """
        更新某标的持仓（一次成交/一次撮合的结果）。

        约定：
        - qty_delta：本次成交导致的数量变化（买入/回补为正；卖出/开空为负）
        - price：本次成交价格（已包含滑点）
        - fee：本次成交手续费（已折算为现金扣减）

        现金与持仓更新规则：
        1) 先扣手续费：cash -= fee
        2) 再计入现金流：cash -= qty_delta * price
           - 买入：qty_delta > 0 -> cash 减少
           - 卖出：qty_delta < 0 -> cash 增加
        3) 若本次属于“开仓/加仓”（绝对持仓变大），则更新 avg_price 为加权平均；
           若属于“减仓/平仓”，avg_price 不变（盈亏通过现金流隐式反映）。
        """
        self.cash -= fee

        current_pos = self.get_position(symbol)
        old_qty = current_pos["qty"]
        new_qty = old_qty + qty_delta

        # 现金流：买入消耗现金，卖出/开空回笼现金
        cost = qty_delta * price
        self.cash -= cost

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
