"""Trade settlement: cost accounting, position update, close/fill event publishing.

Split out of core/broker.py (A4) — see docs/architecture_review.md. See
core/broker_matching.py's module docstring for why this is a mixin rather
than a standalone collaborator object.
"""
from __future__ import annotations

import random
from dataclasses import asdict
from typing import Any, Dict, List, Optional

import pandas as pd

from core.accounts import AccountMode
from core.broker_types import BacktestOrderStatus, Order, OrderType
from core.cost_model import CostBreakdown
from core.events import FillEvent
from core.logger import get_logger
from core.lots import CloseEvent

logger = get_logger(__name__)


class FillServiceMixin:
    """Turn a matched (price, qty) pair into a settled trade.

    Expects ``self`` to carry ``portfolio``, ``last_prices``,
    ``commission_rate``/``commission_rate_maker``, ``random_slip``,
    ``spread_bps``, ``volatility_slippage_factor``, ``use_impact_cost``,
    ``impact_coefficient``/``impact_exponent``, ``liquidation_penalty_bps``,
    ``borrow_availability_required``, ``default_borrow_limit_qty``,
    ``close_events``, ``_close_event_sequence``, ``trades``, ``event_pipeline``,
    plus ``_set_status``/``_audit_order``/``_event_time`` (from
    ``MatchingMixin``).
    """

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
        base_slip = order.slippage if order.slippage > 0 else self.slippage
        slip_rate = random.uniform(0, base_slip) if self.random_slip and base_slip > 0 else base_slip
        quoted_spread_bps = (
            float(bar_context.get("spread_bps"))
            if bar_context is not None and pd.notna(bar_context.get("spread_bps"))
            else self.spread_bps
        )
        spread_slip = max(quoted_spread_bps, 0.0) / 20000.0
        volatility_slip = 0.0
        if bar_context is not None and price > 0:
            if pd.notna(bar_context.get("volatility")):
                bar_volatility = max(float(bar_context.get("volatility")), 0.0)
            else:
                bar_volatility = max(
                    float(bar_context.get("high", price))
                    - float(bar_context.get("low", price)),
                    0.0,
                ) / price
            volatility_slip = self.volatility_slippage_factor * bar_volatility
        impact_slip = 0.0
        participation = 0.0
        if self.use_impact_cost and volume > 0:
            participation = fill_qty / volume
            impact_slip = self.impact_coefficient * (
                max(participation, 0.0) ** self.impact_exponent
            )
        liquidation_penalty = (
            self.liquidation_penalty_bps / 10000.0
            if order.exit_reason in {"MarginLiquidation", "AccountLiquidation"}
            else 0.0
        )
        total_slip_rate = 0.0 if order.zero_cost else (
            slip_rate + spread_slip + volatility_slip
            + impact_slip + liquidation_penalty
        )
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
        if order.order_type is OrderType.LIMIT and order.price is not None:
            if order.side in {"buy", "cover"}:
                fill_price = min(fill_price, order.price)
            else:
                fill_price = max(fill_price, order.price)


        current_pos = self.portfolio.get_position(order.symbol)
        if order.side == "sell":
            fill_qty = min(fill_qty, max(current_pos["qty"], 0.0))
            qty_delta = -fill_qty
        elif order.side == "cover":
            fill_qty = min(fill_qty, max(-current_pos["qty"], 0.0))
            qty_delta = fill_qty
        if fill_qty <= 0:
            self._set_status(order, BacktestOrderStatus.NO_POSITION, timestamp)
            return None

        value = fill_qty * fill_price
        fee_rate = 0.0 if order.zero_cost else (
            self.commission_rate_maker if is_maker else self.commission_rate
        )
        commission = value * fee_rate
        is_opening = order.side in {"buy", "short"}
        if order.side == "short" and not self.portfolio.account_mode.allows_short:
            self._audit_order(order, timestamp, "rejected", "spot_short_forbidden")
            self._set_status(order, BacktestOrderStatus.REJECTED, timestamp)
            return None
        if (
            order.side == "short"
            and self.portfolio.account_mode is AccountMode.SPOT_MARGIN
        ):
            available_raw = (
                bar_context.get("borrow_available_qty")
                if bar_context is not None else None
            )
            if available_raw is None or pd.isna(available_raw):
                if self.borrow_availability_required:
                    self._audit_order(order, timestamp, "rejected", "missing_borrow_availability")
                    self._set_status(order, BacktestOrderStatus.REJECTED, timestamp)
                    return None
                available_borrow = self.default_borrow_limit_qty
                borrow_source = "configured_default"
            else:
                available_borrow = max(float(available_raw), 0.0)
                borrow_source = "historical_bar"
            projected_short = max(
                -self.portfolio.get_position(order.symbol)["qty"], 0.0
            ) + fill_qty
            if projected_short > available_borrow + 1e-12:
                self._audit_order(
                    order,
                    timestamp,
                    "rejected",
                    "borrow_limit",
                    requested_short_qty=projected_short,
                    borrow_available_qty=available_borrow,
                    source=borrow_source,
                )
                self._set_status(order, BacktestOrderStatus.REJECTED, timestamp)
                return None
        if is_opening and self.portfolio.account_mode.uses_margin:
            margin_available = self.portfolio.projected_margin_available(
                self.last_prices, value
            )
            if margin_available < commission - 1e-12:
                self._audit_order(
                    order,
                    timestamp,
                    "rejected",
                    "initial_margin",
                    projected_available_margin=margin_available,
                )
                self._set_status(order, BacktestOrderStatus.REJECTED, timestamp)
                return None
        if (
            is_opening
            and self.portfolio.account_mode is AccountMode.SPOT
            and value + commission > self.portfolio.cash + 1e-12
        ):
            logger.warning(
                "Order rejected: insufficient cash for %s %s; required=%.8f available=%.8f",
                order.side, order.symbol, value + commission, self.portfolio.cash,
            )
            self._audit_order(order, timestamp, "rejected", "insufficient_cash")
            self._set_status(order, BacktestOrderStatus.REJECTED, timestamp)
            return None
        lot_closes = self.portfolio.update_position(
            order.symbol,
            qty_delta,
            fill_price,
            commission,
            strategy_id=order.strategy_id,
            order_id=order.id,
            stop_price=(order.stop_loss or None),
            time=timestamp,
        )
        costs = CostBreakdown(
            commission=commission,
            slippage=abs(price * (slip_rate + spread_slip + volatility_slip)) * fill_qty,
            impact=abs(price * impact_slip) * fill_qty,
            funding=None,
            borrow=None,
            funding_status=(
                "accrued_separately"
                if self.portfolio.account_mode is AccountMode.PERPETUAL
                else "not_applicable"
            ),
            borrow_status=(
                "accrued_separately"
                if self.portfolio.account_mode is AccountMode.SPOT_MARGIN
                else "not_applicable"
            ),
        )
        close_event_ids = []
        new_close_events: List[CloseEvent] = []
        if lot_closes:
            # T-1.6/T-1.7: fill_price already embeds slippage AND impact (both
            # feed total_slip_rate above), so gross_pnl computed from fill
            # prices already reflects them. Netting only commission here (not
            # costs.slippage/impact again) avoids the same double-count this
            # phase fixed in core.metrics.calculate_cost_sensitivity (I-25).
            position_fully_closed = (
                self.portfolio.get_position(order.symbol).get("qty", 0.0) == 0.0
            )
            position_fully_closed = bool(position_fully_closed)
            for lot_close in lot_closes:
                self._close_event_sequence += 1
                close_event_id = f"{lot_close.lot_id}:{self._close_event_sequence}"
                exit_cost_share = (
                    commission * (lot_close.qty_closed / fill_qty) if fill_qty else 0.0
                )
                # T-1.8: net out BOTH sides' cost so realized_pnl - and the
                # accounting identity built from it - isn't short the entry
                # commission/slippage that was already paid when this lot
                # opened (it was deducted from cash then, not just now).
                cost_share = exit_cost_share + lot_close.entry_cost_share
                if lot_close.side == "long":
                    gross_pnl = (fill_price - lot_close.entry_price) * lot_close.qty_closed
                else:
                    gross_pnl = (lot_close.entry_price - fill_price) * lot_close.qty_closed
                realized_pnl = gross_pnl - cost_share
                event = CloseEvent(
                    close_event_id=close_event_id,
                    position_id=lot_close.position_id,
                    lot_id=lot_close.lot_id,
                    symbol=order.symbol,
                    opening_strategy_id=lot_close.strategy_id,
                    exit_reason=order.exit_reason,
                    qty=lot_close.qty_closed,
                    exit_price=fill_price,
                    theoretical_exit_price=price,
                    realized_pnl=realized_pnl,
                    timestamp=timestamp,
                    is_position_fully_closed=position_fully_closed,
                )
                self.close_events.append(event)
                new_close_events.append(event)
                close_event_ids.append(close_event_id)
        # T-1.9/T-1.10: per-lot risk/excursion detail for this fill's closes,
        # in the same FIFO order _reconstruct_closed_trades matches its own
        # long/short stacks against this same trades_df row - so it can pick
        # these up positionally instead of losing them to a scalar field.
        lot_close_details = [
            {
                "lot_id": lot_close.lot_id,
                "position_id": lot_close.position_id,
                "qty_closed": lot_close.qty_closed,
                "initial_risk": lot_close.initial_risk,
                "mae": lot_close.mae,
                "mfe": lot_close.mfe,
            }
            for lot_close in lot_closes
        ]
        trade_record = {
            "order_id": order.id,
            "signal_time": order.timestamp,
            "fill_time": timestamp,
            "symbol": order.symbol,
            "side": order.side,
            "qty": fill_qty,
            "fill_price": fill_price,
            "theoretical_price": price,
            "commission": commission,
            "slip": abs(fill_price - price),
            "slip_dir": slip_dir,
            "spread_bps": quoted_spread_bps,
            "spread_slippage_rate": spread_slip,
            "volatility_slippage_rate": volatility_slip,
            "impact_slippage_rate": impact_slip,
            "participation_rate": participation,
            "account_mode": self.portfolio.account_mode.value,
            "costs": costs.to_dict(),
            "strategy_id": order.strategy_id,
            "exit_reason": order.exit_reason,
            "is_maker": is_maker,
            "close_event_ids": close_event_ids,
            "lot_closes": lot_close_details,
            "data_quality_context": {
                str(key): bool(value)
                for key, value in (bar_context.items() if bar_context is not None else [])
                if str(key).startswith("anomaly_")
            },
        }
        self.trades.append(trade_record)
        self._audit_order(
            order,
            timestamp,
            "filled",
            "matched",
            fill_qty=fill_qty,
            participation_rate=participation,
            total_slippage_rate=total_slip_rate,
        )
        if order.intent is not None:
            fill_id = f"{order.id}:{order.filled_qty + fill_qty:.12f}"
            fill = self.event_pipeline.publish(
                FillEvent(
                    fill_id=fill_id,
                    client_order_id=order.id,
                    symbol=order.symbol,
                    side=order.side,
                    qty=fill_qty,
                    price=fill_price,
                    fee=commission,
                    liquidity="maker" if is_maker else "taker",
                ),
                occurred_at=self._event_time(timestamp),
                correlation_id=order.intent.correlation_id,
                causation_id=order.last_event_id,
                idempotency_key=fill_id,
                account_id=order.intent.account,
                symbol=order.symbol,
                timeframe=order.intent.timeframe,
                source="backtest",
            )
            order.last_event_id = str(fill.event_id)
            for close_event in new_close_events:
                close_payload = asdict(close_event)
                close_payload["timestamp"] = self._event_time(close_event.timestamp)
                close_envelope = self.event_pipeline.publish(
                    close_payload,
                    event_type="close",
                    occurred_at=self._event_time(timestamp),
                    correlation_id=order.intent.correlation_id,
                    causation_id=order.last_event_id,
                    idempotency_key=close_event.close_event_id,
                    account_id=order.intent.account,
                    symbol=order.symbol,
                    timeframe=order.intent.timeframe,
                    source="backtest",
                )
                order.last_event_id = str(close_envelope.event_id)
        logger.info(
            "Trade filled symbol=%s side=%s qty=%.8f fill_price=%.8f signal_time=%s fill_time=%s",
            order.symbol, order.side, fill_qty, fill_price, order.timestamp, timestamp,
        )
        return trade_record
