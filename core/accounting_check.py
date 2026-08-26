"""
会计恒等式核对器（T-1.8）

验证：任意时刻 equity == initial_capital + 累计已实现PnL + 未实现PnL

由于成本（手续费/滑点）已经在平仓时净入 CloseEvent.realized_pnl（T-1.3/T-1.6/T-1.7
的口径统一之后），恒等式中不再单独出现"成本"项——成本正是权益变化与"毛盈亏"之间
差异的来源，而不是恒等式的第三项。

用法：BacktestEngine 在每根 bar 结束后调用一次 AccountingReconciler.check_bar(...)，
run() 结束时调用 result() 生成 Gate G2 需要的核对报告。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

from core.lots import Lot
from core.portfolio import Portfolio

REL_TOLERANCE = 1e-6
ABS_TOLERANCE = 1e-6


def _within_tolerance(actual: float, expected: float) -> bool:
    diff = abs(actual - expected)
    scale = max(abs(expected), 1.0)
    return diff <= max(ABS_TOLERANCE, REL_TOLERANCE * scale)


def unrealized_pnl_for_lots(lots: Iterable[Lot], mark_price: float) -> float:
    """未实现PnL：按当前标记价格估值所有未平仓批次，并扣除已经从现金中
    实际支付、但尚未计入已实现PnL的开仓侧成本（entry_cost_total）——否则
    与 equity（已扣过该笔现金）之间会产生固定偏差（T-1.8）。
    """
    total = 0.0
    for lot in lots:
        if lot.side == "long":
            total += (mark_price - lot.entry_price) * lot.qty_open
        else:
            total += (lot.entry_price - mark_price) * lot.qty_open
        total -= lot.entry_cost_per_unit * lot.qty_open
    return total


@dataclass
class AccountingDiscrepancy:
    index: int
    timestamp: Any
    equity: float
    expected_equity: float
    difference: float


@dataclass
class AccountingCheckResult:
    ok: bool
    checks_performed: int = 0
    max_abs_difference: float = 0.0
    discrepancies: List[AccountingDiscrepancy] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "checks_performed": self.checks_performed,
            "max_abs_difference": self.max_abs_difference,
            "discrepancy_count": len(self.discrepancies),
            "discrepancies": [
                {
                    "index": d.index,
                    "timestamp": str(d.timestamp),
                    "equity": d.equity,
                    "expected_equity": d.expected_equity,
                    "difference": d.difference,
                }
                for d in self.discrepancies[:20]
            ],
        }


class AccountingReconciler:
    """逐 bar 累计已实现PnL，并核对 equity 恒等式（per-bar + 期末累计）。"""

    def __init__(self, initial_capital: float):
        self.initial_capital = initial_capital
        self._realized_pnl_total = 0.0
        self._close_event_cursor = 0
        self._checks = 0
        self._max_abs_difference = 0.0
        self._discrepancies: List[AccountingDiscrepancy] = []

    def _consume_new_close_events(self, close_events: List[Any]) -> None:
        total = len(close_events)
        if self._close_event_cursor > total:
            self._close_event_cursor = 0
        for event in close_events[self._close_event_cursor:]:
            self._realized_pnl_total += event.realized_pnl
        self._close_event_cursor = total

    def check_bar(
        self,
        index: int,
        timestamp: Any,
        equity: float,
        portfolio: Portfolio,
        current_prices: Dict[str, float],
        close_events: List[Any],
    ) -> None:
        self._consume_new_close_events(close_events)
        unrealized = 0.0
        for symbol, lot_book in portfolio.lot_books.items():
            price = current_prices.get(symbol)
            if price is None:
                # No mark available this bar; fall back to each lot's own
                # entry price (zero unrealized contribution), consistent
                # with Portfolio.get_equity's own fallback behavior.
                continue
            unrealized += unrealized_pnl_for_lots(lot_book.open_lots, price)

        expected_equity = self.initial_capital + self._realized_pnl_total + unrealized
        self._checks += 1
        diff = equity - expected_equity
        self._max_abs_difference = max(self._max_abs_difference, abs(diff))
        if not _within_tolerance(equity, expected_equity):
            self._discrepancies.append(
                AccountingDiscrepancy(index, timestamp, equity, expected_equity, diff)
            )

    def result(self) -> AccountingCheckResult:
        return AccountingCheckResult(
            ok=len(self._discrepancies) == 0,
            checks_performed=self._checks,
            max_abs_difference=self._max_abs_difference,
            discrepancies=list(self._discrepancies),
        )
