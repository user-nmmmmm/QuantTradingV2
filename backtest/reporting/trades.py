"""FIFO trade reconstruction, round-trip folding, and trade-metric aggregation.

Two granularities live here and must not be confused:

**Leg** - one FIFO match between an opening fill and a closing fill, produced
by :meth:`_reconstruct_closed_trades`. A position opened over five
participation-limited fills and closed over three produces up to seven legs.

**Round trip** - one position from flat to flat, produced by
:meth:`_aggregate_round_trips` by folding the legs that share a
``position_id``. This is the unit a trader means by "a trade", and the unit
every headline count (``TotalTrades``, ``WinRate``, ``ProfitFactor``,
``Expectancy``) is computed on.

Counting legs as trades inflated the trade count and skewed win rate and
profit factor exactly in proportion to how often the participation cap split
an order - i.e. worst for the large-capital runs that ``backtest/capacity.py``
exists to evaluate. The one metric that stays leg-level is
``lifecycle_coverage``, whose counterpart (``Strategy.observed_close_events``)
counts lot closes.
"""

from collections import deque
from typing import Any, Deque, Dict, List, Optional, Tuple

import pandas as pd

from core.metrics import calculate_profit_factor


def _is_missing(value: Any) -> bool:
    """True for None/NaN, which pandas hands back for absent numeric fields."""
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


class TradeReconstructionMixin:
    def _extended_trade_analytics(
        self, closed_trades: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        raise NotImplementedError

    def _analyze_trades(self, trades_df: pd.DataFrame) -> Dict[str, Any]:
        """基于 trades.csv 重建往返交易并统计交易级指标（薄封装，见下三个方法）。"""
        return self._trade_metrics_from_closed(
            self._aggregate_round_trips(self._reconstruct_closed_trades(trades_df))
        )

    def _reconstruct_closed_trades(self, trades_df: pd.DataFrame) -> List[Dict[str, Any]]:
        """
        基于成交明细重建已平仓交易（FIFO 配对开平仓）。

        方法：
        - 按 symbol 分组，使用 FIFO 栈（long_stack/short_stack）配对开平仓
        - 每条闭合记录附带 symbol、entry_time、exit_time（若成交明细无 fill_time
          列则为 None），供 core.metrics 的归因/交易质量函数使用
        """
        if trades_df.empty:
            return []

        closed_trades: List[Dict[str, Any]] = []

        # Group by symbol
        for symbol, group in trades_df.groupby("symbol"):
            long_stack: Deque[
                Tuple[float, float, str, float, float, Any, float]
            ] = deque()  # (qty, price, strategy_id, unit_comm, unit_slip, entry_time, theoretical_price)
            short_stack: Deque[
                Tuple[float, float, str, float, float, Any, float]
            ] = deque()

            columns = list(group.columns)
            column_index = {name: index for index, name in enumerate(columns)}
            slip_index = column_index.get("slip")
            strategy_index = column_index.get("strategy_id")
            fill_time_index = column_index.get("fill_time")
            exit_reason_index = column_index.get("exit_reason")
            theoretical_index = column_index.get("theoretical_price")
            lot_closes_index = column_index.get("lot_closes")
            for row in group.itertuples(index=False, name=None):
                side = row[column_index["side"]]
                qty = row[column_index["qty"]]
                price = row[column_index["fill_price"]]
                comm = row[column_index["commission"]]
                # Broker stores 'slip' as unit price difference (absolute)
                unit_slip = row[slip_index] if slip_index is not None else 0.0
                # T-1.6 cost-field contract: theoretical_price is the zero-cost
                # reference price (falls back to fill_price for trade records
                # recorded before this field existed).
                theoretical_price = (
                    row[theoretical_index] if theoretical_index is not None else price
                )

                unit_comm = comm / qty if qty > 0 else 0.0

                strategy_id = (
                    row[strategy_index] if strategy_index is not None else "Unknown"
                )
                fill_time = row[fill_time_index] if fill_time_index is not None else None
                # The closing fill carries who ended the trade and why. Keeping
                # only the entry strategy hides the case where a position is
                # force-closed by the router rather than by the strategy's own
                # exit rule — which is invisible in strategy/symbol attribution.
                exit_reason = (
                    row[exit_reason_index] if exit_reason_index is not None else None
                )
                # T-1.9/T-1.10: per-lot initial_risk/MAE/MFE for this fill's
                # closes, consumed positionally in the same FIFO order they
                # were produced in at fill time (core.broker._execute_trade).
                lot_closes_list = (
                    row[lot_closes_index] if lot_closes_index is not None else None
                )
                if not isinstance(lot_closes_list, list):
                    lot_closes_list = []
                lot_close_ptr = 0

                def _next_lot_detail():
                    nonlocal lot_close_ptr
                    if lot_close_ptr < len(lot_closes_list):
                        detail = lot_closes_list[lot_close_ptr]
                        lot_close_ptr += 1
                        return detail
                    lot_close_ptr += 1
                    return None

                if side == "buy":
                    # Check if covering short
                    remaining = qty
                    while remaining > 0 and short_stack:
                        (
                            s_qty, s_price, s_strat, s_unit_comm, s_unit_slip, s_time,
                            s_theoretical,
                        ) = short_stack.popleft()
                        matched = min(remaining, s_qty)
                        lot_detail = _next_lot_detail()

                        # Short PnL: (Entry - Exit) * qty
                        gross_pnl = (s_price - price) * matched
                        # T-1.6/T-1.7: zero-cost reference PnL for cost-sensitivity.
                        gross_pnl_theoretical = (s_theoretical - theoretical_price) * matched

                        # Commission: Entry + Exit
                        trade_comm = (s_unit_comm + unit_comm) * matched

                        # Slippage: Entry + Exit
                        # Note: Slippage is always a cost (positive value in record)
                        trade_slip = (s_unit_slip + unit_slip) * matched

                        net_pnl = gross_pnl - trade_comm

                        closed_trades.append(
                            {
                                "gross_pnl": gross_pnl,
                                "gross_pnl_theoretical": gross_pnl_theoretical,
                                "net_pnl": net_pnl,
                                "commission": trade_comm,
                                "slippage": trade_slip,
                                "strategy": s_strat,
                                "symbol": symbol,
                                "entry_time": s_time,
                                "exit_time": fill_time,
                                "exit_reason": exit_reason,
                                "exit_strategy": strategy_id,
                                "lot_id": lot_detail.get("lot_id") if lot_detail else None,
                                "position_id": lot_detail.get("position_id") if lot_detail else None,
                                "initial_risk": lot_detail.get("initial_risk") if lot_detail else None,
                                "mae": lot_detail.get("mae") if lot_detail else None,
                                "mfe": lot_detail.get("mfe") if lot_detail else None,
                            }
                        )

                        remaining -= matched
                        if s_qty > matched:
                            short_stack.appendleft(
                                (
                                    s_qty - matched,
                                    s_price,
                                    s_strat,
                                    s_unit_comm,
                                    s_unit_slip,
                                    s_time,
                                    s_theoretical,
                                ),
                            )

                    if remaining > 0:
                        long_stack.append(
                            (
                                remaining, price, strategy_id, unit_comm, unit_slip,
                                fill_time, theoretical_price,
                            )
                        )

                elif side == "sell":
                    # Close Long
                    remaining = qty
                    while remaining > 0 and long_stack:
                        (
                            l_qty, l_price, l_strat, l_unit_comm, l_unit_slip, l_time,
                            l_theoretical,
                        ) = long_stack.popleft()
                        matched = min(remaining, l_qty)
                        lot_detail = _next_lot_detail()

                        # Long PnL: (Exit - Entry) * qty
                        gross_pnl = (price - l_price) * matched
                        gross_pnl_theoretical = (theoretical_price - l_theoretical) * matched
                        trade_comm = (l_unit_comm + unit_comm) * matched
                        trade_slip = (l_unit_slip + unit_slip) * matched
                        net_pnl = gross_pnl - trade_comm

                        closed_trades.append(
                            {
                                "gross_pnl": gross_pnl,
                                "gross_pnl_theoretical": gross_pnl_theoretical,
                                "net_pnl": net_pnl,
                                "commission": trade_comm,
                                "slippage": trade_slip,
                                "strategy": l_strat,
                                "symbol": symbol,
                                "entry_time": l_time,
                                "exit_time": fill_time,
                                "exit_reason": exit_reason,
                                "exit_strategy": strategy_id,
                                "lot_id": lot_detail.get("lot_id") if lot_detail else None,
                                "position_id": lot_detail.get("position_id") if lot_detail else None,
                                "initial_risk": lot_detail.get("initial_risk") if lot_detail else None,
                                "mae": lot_detail.get("mae") if lot_detail else None,
                                "mfe": lot_detail.get("mfe") if lot_detail else None,
                            }
                        )

                        remaining -= matched
                        if l_qty > matched:
                            long_stack.appendleft(
                                (
                                    l_qty - matched,
                                    l_price,
                                    l_strat,
                                    l_unit_comm,
                                    l_unit_slip,
                                    l_time,
                                    l_theoretical,
                                ),
                            )

                    if remaining > 0:
                        short_stack.append(
                            (
                                remaining, price, strategy_id, unit_comm, unit_slip,
                                fill_time, theoretical_price,
                            )
                        )

                elif side == "short":
                    # Open Short
                    short_stack.append(
                        (
                            qty, price, strategy_id, unit_comm, unit_slip, fill_time,
                            theoretical_price,
                        )
                    )

                elif side == "cover":
                    # Close Short (Buy to Cover)
                    remaining = qty
                    while remaining > 0 and short_stack:
                        (
                            s_qty, s_price, s_strat, s_unit_comm, s_unit_slip, s_time,
                            s_theoretical,
                        ) = short_stack.popleft()
                        matched = min(remaining, s_qty)
                        lot_detail = _next_lot_detail()

                        gross_pnl = (s_price - price) * matched
                        gross_pnl_theoretical = (s_theoretical - theoretical_price) * matched
                        trade_comm = (s_unit_comm + unit_comm) * matched
                        trade_slip = (s_unit_slip + unit_slip) * matched
                        net_pnl = gross_pnl - trade_comm

                        closed_trades.append(
                            {
                                "gross_pnl": gross_pnl,
                                "gross_pnl_theoretical": gross_pnl_theoretical,
                                "net_pnl": net_pnl,
                                "commission": trade_comm,
                                "slippage": trade_slip,
                                "strategy": s_strat,
                                "symbol": symbol,
                                "entry_time": s_time,
                                "exit_time": fill_time,
                                "exit_reason": exit_reason,
                                "exit_strategy": strategy_id,
                                "lot_id": lot_detail.get("lot_id") if lot_detail else None,
                                "position_id": lot_detail.get("position_id") if lot_detail else None,
                                "initial_risk": lot_detail.get("initial_risk") if lot_detail else None,
                                "mae": lot_detail.get("mae") if lot_detail else None,
                                "mfe": lot_detail.get("mfe") if lot_detail else None,
                            }
                        )

                        remaining -= matched
                        if s_qty > matched:
                            short_stack.appendleft(
                                (
                                    s_qty - matched,
                                    s_price,
                                    s_strat,
                                    s_unit_comm,
                                    s_unit_slip,
                                    s_time,
                                    s_theoretical,
                                ),
                            )

                    if remaining > 0:
                        long_stack.append(
                            (
                                remaining, price, strategy_id, unit_comm, unit_slip,
                                fill_time, theoretical_price,
                            )
                        )

        return closed_trades

    def _aggregate_round_trips(
        self, closed_legs: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """Fold FIFO legs into one record per position (flat -> flat).

        Legs are grouped by ``(symbol, position_id)`` - ``core.lots`` mints a
        ``position_id`` when a symbol goes from flat to held and retires it
        when the symbol is flat again, so that grouping *is* the round trip.
        Legs without one (fills recorded before ``lot_closes`` existed) stay
        one-leg round trips, which preserves the previous behaviour for old
        trade files rather than silently merging unrelated positions.
        """
        groups: Dict[Any, List[Dict[str, Any]]] = {}
        ordered_keys: List[Any] = []
        for index, leg in enumerate(closed_legs):
            position_id = leg.get("position_id")
            key = (
                (leg.get("symbol"), position_id)
                if not _is_missing(position_id) else ("", index)
            )
            if key not in groups:
                groups[key] = []
                ordered_keys.append(key)
            groups[key].append(leg)
        return [self._fold_round_trip(groups[key]) for key in ordered_keys]

    @staticmethod
    def _fold_round_trip(legs: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Collapse one position's legs into a single closed-trade record.

        ``_reconstruct_closed_trades`` appends legs in closing-fill order and,
        within one closing fill, in FIFO order of the lots it retires. So for
        one position ``legs[0]`` holds the earliest entry and ``legs[-1]`` the
        fill that finally flattened it - which is why the opening side is read
        off the first leg and the closing side off the last.
        """
        first, last = legs[0], legs[-1]
        # Each partial close of one lot repeats that lot's *whole* initial
        # risk (core.lots keeps ``initial_risk`` a whole-lot value and tracks
        # the retired portion separately), so summing legs would double count.
        # Summing one value per distinct lot gives the risk the position
        # actually put at stake, which is what an R-multiple divides by.
        risk_by_lot: Dict[Any, float] = {}
        for index, leg in enumerate(legs):
            risk = leg.get("initial_risk")
            if _is_missing(risk):
                continue
            lot_id = leg.get("lot_id")
            risk_by_lot.setdefault(
                lot_id if not _is_missing(lot_id) else ("leg", index), float(risk)
            )
        entry_strategies = {leg.get("strategy") for leg in legs}

        def _extreme(field: str) -> Optional[float]:
            values = [
                float(leg[field]) for leg in legs
                if not _is_missing(leg.get(field))
            ]
            return max(values) if values else None

        def _total(field: str) -> float:
            return sum(
                float(leg.get(field) or 0.0) for leg in legs
                if not _is_missing(leg.get(field))
            )

        return {
            "gross_pnl": _total("gross_pnl"),
            "gross_pnl_theoretical": _total("gross_pnl_theoretical"),
            "net_pnl": _total("net_pnl"),
            "commission": _total("commission"),
            "slippage": _total("slippage"),
            "strategy": first.get("strategy"),
            "symbol": first.get("symbol"),
            "entry_time": first.get("entry_time"),
            "exit_time": last.get("exit_time"),
            "exit_reason": last.get("exit_reason"),
            "exit_strategy": last.get("exit_strategy"),
            "position_id": first.get("position_id"),
            # A position can be pyramided into by more than one strategy, in
            # which case attributing the whole round trip to the first entry
            # is a choice, not a fact - say so instead of hiding it.
            "mixed_entry_strategies": len(entry_strategies) > 1,
            "legs": len(legs),
            "initial_risk": sum(risk_by_lot.values()) if risk_by_lot else None,
            # MAE/MFE are per-unit price excursions, so the position's worst
            # excursion is the worst any of its lots saw, not their sum.
            "mae": _extreme("mae"),
            "mfe": _extreme("mfe"),
        }

    def _trade_metrics_from_closed(
        self, closed_trades: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """把往返交易（`_aggregate_round_trips` 的输出）聚合为扁平指标字典。"""
        if not closed_trades:
            return {
                "TotalTrades": 0,
                "WinRate": 0.0,
                "ProfitFactor": None,
                "ProfitFactorStatus": "insufficient",
                "ProfitFactorSamples": 0,
                "ProfitFactorLosses": 0,
                "GrossPnL": 0.0,
                "TotalCommission": 0.0,
                "TotalSlippage": 0.0,
                "NetPnL": 0.0,
                "ExtendedAnalytics": self._extended_trade_analytics([]),
            }

        # 1. Global Metrics
        all_net_pnls = [t["net_pnl"] for t in closed_trades]
        all_gross_pnls = [t["gross_pnl"] for t in closed_trades]
        all_comms = [t["commission"] for t in closed_trades]
        all_slips = [t["slippage"] for t in closed_trades]

        if not all_net_pnls:
            return {
                "TotalTrades": 0,
                "WinRate": 0.0,
                "ProfitFactor": None,
                "ProfitFactorStatus": "insufficient",
                "ProfitFactorSamples": 0,
                "ProfitFactorLosses": 0,
                "Expectancy": 0.0,
                "AvgWin": 0.0,
                "AvgLoss": 0.0,
                "GrossPnL": 0.0,
                "TotalCommission": 0.0,
                "TotalSlippage": 0.0,
                "NetPnL": 0.0,
                "ExtendedAnalytics": self._extended_trade_analytics([]),
            }

        wins = [p for p in all_net_pnls if p > 0]
        losses = [p for p in all_net_pnls if p < 0]

        win_rate = len(wins) / len(all_net_pnls)
        pf = calculate_profit_factor(all_net_pnls)

        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = sum(losses) / len(losses) if losses else 0

        loss_rate = 1 - win_rate
        expectancy = (win_rate * avg_win) + (loss_rate * avg_loss)

        metrics = {
            "TotalTrades": len(all_net_pnls),
            "WinRate": win_rate,
            "ProfitFactor": pf["value"],
            "ProfitFactorStatus": pf["status"],
            "ProfitFactorSamples": pf["sample_size"],
            "ProfitFactorLosses": pf["loss_count"],
            "ProfitFactorLower95": pf["lower"],
            "ProfitFactorUpper95": pf["upper"],
            "Expectancy": expectancy,
            "AvgWin": avg_win,
            "AvgLoss": avg_loss,
            "GrossPnL": sum(all_gross_pnls),
            "TotalCommission": sum(all_comms),
            "TotalSlippage": sum(all_slips),
            "NetPnL": sum(all_net_pnls),
            "ExtendedAnalytics": self._extended_trade_analytics(closed_trades),
        }

        # 2. Per Strategy Metrics
        strat_map: Dict[str, List[Dict[str, Any]]] = {}
        for t in closed_trades:
            s = t["strategy"]
            if s not in strat_map:
                strat_map[s] = []
            strat_map[s].append(t)

        for s, trades in strat_map.items():
            pnls = [t["net_pnl"] for t in trades]
            s_wins = [p for p in pnls if p > 0]
            s_losses = [p for p in pnls if p < 0]
            s_wr = len(s_wins) / len(pnls)
            s_pf = calculate_profit_factor(pnls)
            s_total = sum(pnls)
            s_comm = sum(t["commission"] for t in trades)
            s_slip = sum(t["slippage"] for t in trades)

            metrics[f"Strat_{s}_Trades"] = len(pnls)
            metrics[f"Strat_{s}_WinRate"] = s_wr
            metrics[f"Strat_{s}_ProfitFactor"] = s_pf["value"]
            metrics[f"Strat_{s}_ProfitFactorStatus"] = s_pf["status"]
            metrics[f"Strat_{s}_ProfitFactorSamples"] = s_pf["sample_size"]
            metrics[f"Strat_{s}_ProfitFactorLosses"] = s_pf["loss_count"]
            metrics[f"Strat_{s}_NetPnL"] = s_total
            metrics[f"Strat_{s}_Comm"] = s_comm
            metrics[f"Strat_{s}_Slip"] = s_slip

        return metrics

