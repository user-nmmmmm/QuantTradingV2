"""Boolean entry gate and the risk-decision/reservation/intent publishing flow.

Split out of core/risk.py (A4) — see docs/architecture_review.md. See
core/risk_circuit_breaker.py's module docstring for why this is a mixin
rather than a standalone collaborator object.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Dict, Optional, Tuple

from core.domain import OrderIntent, RiskDecision, RiskReservation
from core.events import TradingEventPipeline, stable_uuid5
from core.logger import get_logger
from core.portfolio import Portfolio
from core.risk.reservation import RiskReservationProjection

logger = get_logger(__name__)


class EntryPolicyMixin:
    """The final admission gate (``check_entry_risk``) and the atomic
    decision/reservation/intent publishing flow (``approve_and_create_intent``).

    Expects ``self`` to carry ``liquidity_limit_pct``, ``_entry_notional_caps``
    (from ``PositionSizingMixin``), and the ``_blocks_new_risk``/
    ``_health_allows_new_risk`` gates from ``CircuitBreakerMixin``.
    """

    def check_entry_risk(
        self,
        portfolio: Portfolio,
        symbol: str,
        qty: float,
        price: float,
        current_volume: float = 0,
        current_prices: Optional[Dict[str, float]] = None,
        pending_open_notional: Optional[Dict[str, float]] = None,
        reservation_projection: Optional[RiskReservationProjection] = None,
        action: str = "buy",
    ) -> bool:
        """
        校验一笔“拟进入的交易”是否违反风控规则。

        输入：
        - portfolio：当前账户状态（现金、持仓）
        - symbol/qty/price：拟下单信息（qty 为绝对数量，side 在上层决定）
        - current_volume：当前 bar 的成交量（用于流动性上限）
        - current_prices：symbol -> 当前价格（用于敞口/权益估算）
        - action：'buy' 或 'short'。做多需要用现金全额支付（现货无杠杆融资建模），
          做空暂不做现金占用校验（保证金/借券成本尚未建模，见 backtest_assumptions.md）。

        返回：
        - True：允许交易
        - False：拒绝交易

        注意：这是**闸门**，不做削减。上层应先用 `clamp_entry_qty` 把仓位削到
        上限内，本方法作为最后一道防线（含实盘路径）。
        """
        if self._blocks_new_risk():
            logger.warning("Trade Rejected: Circuit Breaker Active")
            return False
        if not self._health_allows_new_risk():
            return False

        if qty <= 0 or price <= 0:
            return False

        # 1. Liquidity Check
        if current_volume > 0:
            max_qty = current_volume * self.liquidity_limit_pct
            if qty > max_qty:
                logger.warning(f"Trade Rejected: Liquidity Limit. Qty {qty:.4f} > Max {max_qty:.4f} (1% of {current_volume})")
                return False

        trade_value = qty * price

        reserved_by_symbol = (
            reservation_projection.pending_notional(current_prices)
            if reservation_projection is not None else pending_open_notional or {}
        )
        caps = self._entry_notional_caps(
            portfolio, symbol, price, current_prices, reserved_by_symbol, action
        )
        if caps is None:
            return False

        context = self._last_entry_context
        equity = context["equity"]

        # 2. Cash Sufficiency Check
        if "cash" in caps and trade_value > caps["cash"]:
            logger.warning(
                f"Trade Rejected: Insufficient Cash. Need {trade_value:.2f}, "
                f"free cash {caps['cash']:.2f} (cash={portfolio.cash:.2f}, "
                f"reserved={context['reserved']:.2f})"
            )
            return False

        # 3. Leverage Check
        if trade_value > caps["leverage"]:
            projected_leverage = (
                context["exposure"] + context["reserved"] + trade_value
            ) / equity
            logger.warning(f"Trade Rejected: Leverage Limit. Projected {projected_leverage:.2f} > Max {self.max_leverage}")
            return False

        # 4. Concentration Check (Max Position Size)
        if trade_value > caps["concentration"]:
            new_pos_value = context["position_value"] + trade_value
            logger.warning(f"Trade Rejected: Concentration Limit. Symbol {symbol} would be {new_pos_value/equity:.1%} > Max {self.max_pos_size_pct:.1%}")
            return False

        # 5. Correlation cluster / crypto beta budgets (SR3-2). Fifteen
        # correlated majors are one position with fifteen tickers.
        for cap_name in ("cluster_exposure", "crypto_beta_exposure"):
            if cap_name in caps and trade_value > caps[cap_name]:
                logger.warning(
                    "Trade Rejected: %s budget. Need %.2f, headroom %.2f (%s)",
                    cap_name, trade_value, caps[cap_name], symbol,
                )
                return False

        return True


    def approve_and_create_intent(
        self,
        portfolio: Portfolio,
        intent: OrderIntent,
        *,
        reference_price: float,
        event_pipeline: TradingEventPipeline,
        reservation_projection: RiskReservationProjection,
        occurred_at,
        source: str = "risk",
        current_volume: float = 0,
        current_prices: Optional[Dict[str, float]] = None,
    ) -> Tuple[RiskDecision, Optional[OrderIntent]]:
        """Evaluate and atomically create an approved intent and reservation."""
        if not isinstance(intent, OrderIntent):
            raise TypeError("intent must be OrderIntent")
        if intent.action not in {"buy", "short"} or intent.reduce_only:
            raise ValueError("risk reservations are only created for opening intents")
        decision_id = intent.risk_decision_id or str(
            stable_uuid5("risk-decision", intent.account, intent.intent_id)
        )
        reservation_id = intent.reservation_id or str(
            stable_uuid5("risk-reservation", intent.account, intent.intent_id)
        )
        with reservation_projection.transaction():
            reserved = reservation_projection.pending_notional(current_prices)
            approved = self.check_entry_risk(
                portfolio,
                intent.symbol,
                intent.requested_qty,
                reference_price,
                current_volume=current_volume,
                current_prices=current_prices,
                pending_open_notional=reserved,
                action=intent.action,
            )
            decision = RiskDecision(
                decision_id=decision_id,
                account=intent.account,
                symbol=intent.symbol,
                action=intent.action,
                requested_qty=intent.requested_qty,
                approved_qty=intent.requested_qty if approved else 0,
                reference_price=reference_price,
                approved=approved,
                reason="approved" if approved else "risk_limit",
                intent_id=intent.intent_id,
            )
            if not approved:
                event_pipeline.publish(
                    decision,
                    occurred_at=occurred_at,
                    correlation_id=intent.correlation_id,
                    idempotency_key=decision_id,
                    account_id=intent.account,
                    symbol=intent.symbol,
                    timeframe=intent.timeframe,
                    source=source,
                )
                return decision, None
            enriched = replace(
                intent,
                risk_decision_id=decision_id,
                reservation_id=reservation_id,
            )
            reservation = RiskReservation(
                reservation_id=reservation_id,
                risk_decision_id=decision_id,
                intent_id=enriched.intent_id,
                account=enriched.account,
                symbol=enriched.symbol,
                action=enriched.action,
                reserved_qty=enriched.requested_qty,
                reference_price=reference_price,
            )
            event_pipeline.publish_approved_intent(
                decision,
                reservation,
                enriched,
                occurred_at=occurred_at,
                source=source,
            )
            return decision, enriched
