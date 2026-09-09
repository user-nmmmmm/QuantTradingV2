"""Safety-enforcing, persistent broker used by the live entry point."""

from __future__ import annotations

from typing import Any, Dict, Optional

from core.domain import OrderErrorCode, OrderIntent, OrderSubmissionResult
from core.live_broker import LiveBroker
from core.logger import get_logger
from core.order_store import OrderStore
from core.portfolio import Portfolio


logger = get_logger(__name__)


class SafeLiveBroker(LiveBroker):
    """LiveBroker whose public constructor cannot accept plaintext credentials."""

    def __init__(
        self,
        portfolio: Portfolio,
        safety_guard: Any,
        exchange_id: str = "binance",
        sandbox: bool = True,
        market_type: str = "spot",
        base_currency: str = "USDT",
        exchange_options: Optional[Dict[str, Any]] = None,
        order_store: Optional[OrderStore] = None,
        order_store_path: str = "reports/live_orders.db",
        account_id: Optional[str] = None,
        position_mode: str = "one_way",
        require_market_metadata: bool = False,
        require_resident_protection: bool = False,
        alert_sink=None,
    ) -> None:
        self.safety_guard = safety_guard
        super().__init__(
            portfolio=portfolio,
            exchange_id=exchange_id,
            sandbox=sandbox,
            market_type=market_type,
            base_currency=base_currency,
            exchange_options=exchange_options,
            order_store=order_store or OrderStore(order_store_path),
            account_id=account_id,
            position_mode=position_mode,
            require_market_metadata=require_market_metadata,
            require_resident_protection=require_resident_protection,
            alert_sink=alert_sink,
        )

    def submit_intent(self, intent: OrderIntent) -> OrderSubmissionResult:
        """Apply the same safety gate to canonical and legacy submissions."""
        if not isinstance(intent, OrderIntent):
            raise TypeError("intent must be OrderIntent")
        # Idempotent replay must not reserve the daily risk limit a second time.
        existing = self.order_store.get(intent.client_order_id)
        if existing is None:
            if intent.action in {"buy", "short"} and self._has_other_active_open_order(
                intent.symbol, intent.client_order_id
            ):
                return self.record_local_rejection(
                    intent,
                    "active opening order blocks additional risk for this symbol",
                    OrderErrorCode.SAFETY_POLICY,
                )
            try:
                self.safety_guard.assert_order_allowed(
                    intent.symbol,
                    intent.action,
                    intent.requested_qty,
                    intent.reference_price or intent.price,
                )
            except ValueError as exc:
                logger.critical(
                    "Order blocked by safety policy client_order_id=%s reason=%s",
                    intent.client_order_id, exc,
                )
                return self.record_local_rejection(
                    intent, str(exc), OrderErrorCode.SAFETY_POLICY
                )
        return super().submit_intent(intent)
