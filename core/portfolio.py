from typing import Any, Dict, List, Optional

from core.accounts import AccountMode, FinancingEntry, MarginSnapshot
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
    def __init__(
        self,
        initial_capital: float = 10000.0,
        *,
        account_mode: AccountMode | str = AccountMode.SPOT,
        initial_margin_rate: float = 1.0,
        maintenance_margin_rate: float = 0.05,
    ):
        """
        初始化账户。

        参数：
        - initial_capital：初始资金（默认 10,000），同时初始化 cash。
        """
        self.account_mode = AccountMode(account_mode)
        if not 0 < initial_margin_rate <= 1:
            raise ValueError("initial_margin_rate must be in (0, 1]")
        if not 0 <= maintenance_margin_rate <= initial_margin_rate:
            raise ValueError(
                "maintenance_margin_rate must be between 0 and initial_margin_rate"
            )
        self.initial_margin_rate = float(initial_margin_rate)
        self.maintenance_margin_rate = float(maintenance_margin_rate)
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
        self.margin_ledger: List[MarginSnapshot] = []
        self.financing_ledger: List[FinancingEntry] = []
        # Positive means a cost paid by the account, negative means a credit.
        self.cumulative_financing_cost = 0.0

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
        lot_closes = self.get_lot_book(symbol).apply_fill(
            qty_delta,
            price,
            time=time,
            strategy_id=strategy_id,
            order_id=order_id,
            stop_price=stop_price,
            fee=fee,
        )

        current_pos = self.get_position(symbol)
        old_qty = current_pos["qty"]
        new_qty = old_qty + qty_delta

        self.cash -= fee
        if self.account_mode is AccountMode.SPOT:
            # Spot owns the asset: buys exchange quote cash for inventory and
            # sells exchange inventory back into quote cash.
            self.cash -= qty_delta * price
        else:
            # Leveraged accounts keep collateral separate from position
            # notional.  Only realised PnL and explicit costs move collateral.
            for lot_close in lot_closes:
                if lot_close.side == "long":
                    self.cash += (
                        price - lot_close.entry_price
                    ) * lot_close.qty_closed
                else:
                    self.cash += (
                        lot_close.entry_price - price
                    ) * lot_close.qty_closed
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
            price = current_prices.get(symbol, pos["avg_price"])
            if self.account_mode is AccountMode.SPOT:
                equity += qty * price
            elif qty > 0:
                equity += (price - pos["avg_price"]) * qty
            else:
                equity += (pos["avg_price"] - price) * abs(qty)
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

    def margin_snapshot(
        self,
        current_prices: Dict[str, float],
        *,
        timestamp: Any = None,
        record: bool = False,
    ) -> MarginSnapshot:
        """Return an auditable initial/maintenance/free-margin reconciliation."""
        equity = self.get_equity(current_prices)
        gross = self.get_total_exposure(current_prices)
        if self.account_mode.uses_margin:
            initial = gross * self.initial_margin_rate
            maintenance = gross * self.maintenance_margin_rate
            available = equity - initial
            ratio = float("inf") if maintenance <= 0 else equity / maintenance
            liquidation = gross > 0 and equity <= maintenance
        else:
            initial = gross
            maintenance = 0.0
            available = self.cash
            ratio = float("inf")
            liquidation = False
        snapshot = MarginSnapshot(
            timestamp=timestamp,
            account_mode=self.account_mode.value,
            equity=equity,
            gross_notional=gross,
            initial_margin=initial,
            maintenance_margin=maintenance,
            available_margin=available,
            margin_ratio=ratio,
            liquidation_required=liquidation,
        )
        if record:
            self.margin_ledger.append(snapshot)
        return snapshot

    def projected_margin_available(
        self,
        current_prices: Dict[str, float],
        additional_notional: float,
    ) -> float:
        snapshot = self.margin_snapshot(current_prices)
        if not self.account_mode.uses_margin:
            return snapshot.available_margin - additional_notional
        return snapshot.equity - (
            snapshot.gross_notional + max(additional_notional, 0.0)
        ) * self.initial_margin_rate

    def apply_financing(
        self,
        *,
        timestamp: Any,
        symbol: str,
        kind: str,
        rate: float,
        notional: float,
        amount: float,
        source: str,
    ) -> FinancingEntry:
        """Post a signed financing cost to collateral and the audit ledger."""
        entry = FinancingEntry(
            timestamp=timestamp,
            symbol=symbol,
            kind=kind,
            rate=float(rate),
            notional=float(notional),
            amount=float(amount),
            source=source,
        )
        self.cash -= entry.amount
        self.cumulative_financing_cost += entry.amount
        self.financing_ledger.append(entry)
        return entry
