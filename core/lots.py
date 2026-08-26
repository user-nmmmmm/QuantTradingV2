"""
Lot（持仓批次）账本模块

为每一次开仓建立唯一的 lot_id / position_id，并以 FIFO 顺序追踪加仓、减仓、
部分成交与方向反转，使得任何一次平仓都能精确定位其对应的开仓批次
（Phase 1 任务 T-1.1 / T-1.2）。

约定：
- position_id：从"某标的从空仓变为有仓位"开始，到"再次归零"结束的一段持仓生命周期。
  该周期内可能包含多笔加仓（多个 lot）。
- lot_id：单次开仓（或对同一笔挂单的后续部分成交）产生的批次。
- 同一订单（order_id 相同）在多根 bar 上分批成交时，视为同一批次的持续建仓，
  按加权平均价合并进同一个 Lot；不同订单的加仓（金字塔加仓）产生新的 Lot，
  但共享同一个 position_id。
- 平仓按 FIFO（先进先出）顺序核销最早的 Lot；若平仓数量超过当前反向持仓，
  剩余部分在同一次 fill 中开出新方向的 Lot（方向反转）。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from itertools import count
from typing import Any, Deque, List, Optional

_QTY_EPS = 1e-12


class LotIdAllocator:
    """Generates lot_id/position_id strings.

    Scoped per Portfolio (not a module-level global) so that two separate
    backtest runs starting from a fresh Portfolio produce identical ids in
    the same order - required for the engine's run-to-run determinism
    (tests/test_backtest_regression.py::TestBacktestEngineEquivalenceBaseline).
    """

    def __init__(self) -> None:
        self._lot_seq = count(1)
        self._position_seq = count(1)

    def next_lot_id(self) -> str:
        return f"LOT-{next(self._lot_seq):09d}"

    def next_position_id(self) -> str:
        return f"POS-{next(self._position_seq):09d}"


@dataclass
class Lot:
    """一个开仓批次（尚未完全平仓或已部分/全部平仓）。"""

    lot_id: str
    position_id: str
    symbol: str
    side: str  # "long" | "short"
    qty_open: float
    qty_original: float
    entry_price: float
    entry_time: Any
    strategy_id: str
    order_id: Optional[str] = None
    stop_price: Optional[float] = None
    initial_risk: Optional[float] = None
    mae: float = 0.0
    mfe: float = 0.0
    # Total $ cost (commission/slippage/impact) already paid to open this lot
    # (accumulates across merged partial-fill entries). Needed so a still-open
    # lot's unrealized PnL - and a later close's realized_pnl - correctly
    # nets out the entry-side cost, not just the exit-side cost (T-1.8).
    entry_cost_total: float = 0.0

    @property
    def is_closed(self) -> bool:
        return self.qty_open <= _QTY_EPS

    @property
    def entry_cost_per_unit(self) -> float:
        return self.entry_cost_total / self.qty_original if self.qty_original else 0.0

    def _recompute_initial_risk(self) -> None:
        if self.stop_price is not None:
            self.initial_risk = abs(self.entry_price - self.stop_price) * self.qty_open


@dataclass
class LotClose:
    """一次平仓 fill 对某个 Lot 造成的核销结果。"""

    lot_id: str
    position_id: str
    symbol: str
    side: str
    qty_closed: float
    entry_price: float
    strategy_id: str
    order_id: Optional[str]
    initial_risk: Optional[float]
    mae: float
    mfe: float
    fully_closed: bool
    entry_cost_share: float


@dataclass(frozen=True)
class CloseEvent:
    """统一的平仓事件契约（T-1.3）。

    无论触发方是策略自身出场、Router 状态切换、硬止损、熔断还是
    EndOfBacktest 收尾平仓，都会且只会通过 Broker 的同一个成交入口
    （``core.broker.Broker._execute_trade``）构造出结构完全一致的
    CloseEvent，供开仓策略消费（T-1.4）。``close_event_id`` 全局唯一，
    用于保证重复投递时的幂等消费（T-1.5）。
    """

    close_event_id: str
    position_id: str
    lot_id: str
    symbol: str
    opening_strategy_id: str
    exit_reason: str
    qty: float
    exit_price: float
    theoretical_exit_price: float
    realized_pnl: float
    timestamp: Any
    is_position_fully_closed: bool


class LotBook:
    """单个 symbol 的 FIFO 批次账本。"""

    def __init__(self, symbol: str, allocator: Optional[LotIdAllocator] = None):
        self.symbol = symbol
        self._lots: Deque[Lot] = deque()
        self._current_position_id: Optional[str] = None
        self._allocator = allocator or LotIdAllocator()

    @property
    def open_lots(self) -> List[Lot]:
        return list(self._lots)

    @property
    def net_qty(self) -> float:
        total = 0.0
        for lot in self._lots:
            total += lot.qty_open if lot.side == "long" else -lot.qty_open
        return total

    def apply_fill(
        self,
        qty_delta: float,
        price: float,
        *,
        time: Any = None,
        strategy_id: str = "",
        order_id: Optional[str] = None,
        stop_price: Optional[float] = None,
        fee: float = 0.0,
    ) -> List[LotClose]:
        """应用一次带符号数量变化的 fill，返回本次核销掉的 LotClose 列表（纯开仓/加仓返回空列表）。

        ``fee`` 是这次 fill 的总成本（手续费+滑点+冲击），按开仓/平仓两部分的
        数量占比拆分：平仓部分体现为 LotClose.entry_cost_share 之外的、由调用方
        （Broker）自行叠加的平仓侧成本；开仓部分累加进新/被合并 Lot 的
        entry_cost_total，供后续平仓与未平仓估值正确扣除开仓侧成本（T-1.8）。
        """
        if qty_delta == 0:
            return []

        fill_side = "long" if qty_delta > 0 else "short"
        opposite_side = "short" if fill_side == "long" else "long"
        total_fill_qty = abs(qty_delta)
        remaining = total_fill_qty
        closes: List[LotClose] = []

        while remaining > _QTY_EPS and self._lots and self._lots[0].side == opposite_side:
            lot = self._lots[0]
            close_qty = min(remaining, lot.qty_open)
            entry_cost_share = lot.entry_cost_per_unit * close_qty
            lot.qty_open -= close_qty
            remaining -= close_qty
            fully_closed = lot.qty_open <= _QTY_EPS
            closes.append(
                LotClose(
                    lot_id=lot.lot_id,
                    position_id=lot.position_id,
                    symbol=self.symbol,
                    side=lot.side,
                    qty_closed=close_qty,
                    entry_price=lot.entry_price,
                    strategy_id=lot.strategy_id,
                    order_id=lot.order_id,
                    initial_risk=lot.initial_risk,
                    mae=lot.mae,
                    mfe=lot.mfe,
                    fully_closed=fully_closed,
                    entry_cost_share=entry_cost_share,
                )
            )
            if fully_closed:
                self._lots.popleft()

        if not self._lots:
            self._current_position_id = None

        if remaining > _QTY_EPS:
            add_qty = remaining
            fee_for_open = fee * (add_qty / total_fill_qty) if total_fill_qty else 0.0
            last_lot = self._lots[-1] if self._lots else None
            if (
                last_lot is not None
                and last_lot.side == fill_side
                and order_id is not None
                and last_lot.order_id == order_id
            ):
                total_qty = last_lot.qty_open + add_qty
                last_lot.entry_price = (
                    last_lot.entry_price * last_lot.qty_open + price * add_qty
                ) / total_qty
                last_lot.qty_open = total_qty
                last_lot.qty_original += add_qty
                last_lot.entry_cost_total += fee_for_open
                if stop_price is not None:
                    last_lot.stop_price = stop_price
                last_lot._recompute_initial_risk()
            else:
                if not self._lots:
                    self._current_position_id = self._allocator.next_position_id()
                position_id = self._current_position_id or self._allocator.next_position_id()
                self._current_position_id = position_id
                new_lot = Lot(
                    lot_id=self._allocator.next_lot_id(),
                    position_id=position_id,
                    symbol=self.symbol,
                    side=fill_side,
                    qty_open=add_qty,
                    qty_original=add_qty,
                    entry_price=price,
                    entry_time=time,
                    strategy_id=strategy_id,
                    order_id=order_id,
                    stop_price=stop_price,
                    entry_cost_total=fee_for_open,
                )
                new_lot._recompute_initial_risk()
                self._lots.append(new_lot)

        return closes

    def update_extremes(self, high: float, low: float) -> None:
        """按当前 bar 的高低点刷新所有未平仓批次的 MAE/MFE（不利/有利变动的最大值）。"""
        for lot in self._lots:
            if lot.side == "long":
                favorable = high - lot.entry_price
                adverse = lot.entry_price - low
            else:
                favorable = lot.entry_price - low
                adverse = high - lot.entry_price
            if favorable > lot.mfe:
                lot.mfe = favorable
            if adverse > lot.mae:
                lot.mae = adverse
